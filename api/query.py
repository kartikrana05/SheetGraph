"""
Natural language → Cypher → results → natural language.

The schema is injected at request time from whatever the user uploaded, which
is what lets this work on an arbitrary spreadsheet instead of one hardcoded
domain.
"""

from __future__ import annotations

import json
import re

import graphdb
from llm import complete, complete_json

# Word-boundary matched so a property called "createdAt" or "assetId" does not
# trip the "create"/"set" filters.
BLOCKED_KEYWORDS = [
    "create", "delete", "set", "merge", "drop", "remove", "detach",
    "load csv", "foreach", "call dbms", "call db.create", "apoc",
]

ALLOWED_STARTS = ("match", "optional match", "with", "unwind", "return")

MAX_ROWS_TO_MODEL = 12
MAX_ROWS_TO_CLIENT = 100


def node_properties(node: dict) -> list[str]:
    """Every property name on a node, across all the sheets that feed it."""
    names: list[str] = [node["key"]]
    for source in node.get("sources", []):
        for prop in source.get("properties", []):
            if prop["name"] not in names:
                names.append(prop["name"])
    return names


def build_schema_prompt(schema: dict) -> str:
    sheets = schema.get("sheets") or []
    lines = []

    if len(sheets) > 1:
        lines.append(
            f"This dataset was built from {len(sheets)} tables: "
            + ", ".join(f'"{s}"' for s in sheets)
        )
        lines.append("")

    lines.append("NODE LABELS:")
    for node in schema["nodes"]:
        props = ", ".join(node_properties(node))
        origins = [s["sheet"] for s in node.get("sources", [])]
        line = f"- (:{node['label']}) key={node['key']} | properties: {props}"
        # Telling the model which entities span several tables is what makes it
        # write cross-sheet queries instead of treating each table separately.
        if len(origins) > 1:
            line += f"  <- joined across {len(origins)} tables: {', '.join(origins)}"
        lines.append(line)

    lines.append("")
    lines.append("RELATIONSHIPS:")
    if schema["relationships"]:
        for rel in schema["relationships"]:
            lines.append(f"- (:{rel['from']})-[:{rel['type']}]->(:{rel['to']})")
    else:
        lines.append("- (none - this dataset has isolated nodes only)")

    joined = [n["label"] for n in schema["nodes"] if len(n.get("sources", [])) > 1]
    if joined:
        lines.append("")
        lines.append(
            "NOTE: " + ", ".join(joined) + " exist in more than one source table, so a "
            "single query can traverse from data that started in one table to data that "
            "started in another. Prefer such queries when the question spans both."
        )

    return "\n".join(lines)


def system_prompt(schema: dict, dataset_name: str) -> str:
    return f"""\
You are a Cypher expert answering questions about a dataset called "{dataset_name}",
which was built from a spreadsheet the user uploaded.

{build_schema_prompt(schema)}

Every node also carries a `_ds` property identifying the dataset.

Return ONLY a JSON object, no markdown, no prose:
{{"cypher": "MATCH ...", "explanation": "one sentence on what this finds"}}

If the question is not answerable from this schema (general knowledge, coding help,
chit-chat, or data that simply is not in these columns), return:
{{"cypher": null, "explanation": "a helpful sentence explaining what this dataset can answer instead"}}

CYPHER RULES:
- EVERY node pattern you match must be constrained to this dataset. Use the $ds
  parameter, e.g. MATCH (p:Project) WHERE p._ds = $ds
- Read-only only. Never CREATE, MERGE, SET, DELETE, REMOVE, DROP or LOAD.
- Always end with LIMIT (default 25) unless the user asks for a single aggregate.
- Use toFloat() before numeric comparison or aggregation, since spreadsheet values
  may arrive as strings.
- Use toLower() + CONTAINS for fuzzy text matching.
- Never invent labels or properties. Use only what is listed above.
- Prefer returning readable scalar columns over whole nodes, so the answer is easy
  to summarise. Alias them clearly, e.g. RETURN p.name AS project, count(t) AS tasks
"""


def validate_cypher(cypher: str) -> tuple[bool, str]:
    """
    Defence in depth. The prompt asks for read-only Cypher; this enforces it.

    Note this is the second of three layers — the third is that the Neo4j user
    should be read-only at the database level in a real deployment.
    """
    if not cypher or not cypher.strip():
        return False, "Empty query"

    lowered = cypher.lower()

    for keyword in BLOCKED_KEYWORDS:
        pattern = rf"\b{re.escape(keyword)}\b"
        if re.search(pattern, lowered):
            return False, f"Write or unsafe operation '{keyword}' is not allowed"

    stripped = lowered.lstrip()
    if not stripped.startswith(ALLOWED_STARTS):
        return False, "Query must start with MATCH, OPTIONAL MATCH, WITH, UNWIND or RETURN"

    if ";" in cypher.rstrip().rstrip(";"):
        return False, "Multiple statements are not allowed"

    return True, "ok"


def run_cypher(cypher: str, dataset_id: str) -> list[dict]:
    with graphdb.open_session() as session:
        return session.run(cypher, ds=dataset_id).data()


def ask(dataset_id: str, message: str, history: list[dict] | None = None) -> dict:
    dataset = graphdb.get_dataset(dataset_id)
    if not dataset:
        return {
            "answer": "That dataset no longer exists. Upload a sheet to get started.",
            "cypher": None,
            "rows": [],
        }

    schema = dataset["schema"]
    system = system_prompt(schema, dataset.get("name") or "your dataset")

    context = ""
    for turn in (history or [])[-4:]:
        context += f"\nUser: {turn.get('user','')}\nAssistant: {turn.get('assistant','')}\n"

    user_prompt = f"{context}\nUser question: {message}" if context else message

    try:
        parsed = complete_json(system, user_prompt, temperature=0.1, max_tokens=1024)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return {
            "answer": f"I could not turn that into a query. {exc}",
            "cypher": None,
            "rows": [],
            "error": f"{type(exc).__name__}: {exc}",
        }

    cypher = parsed.get("cypher")
    explanation = parsed.get("explanation", "")

    if not cypher:
        return {"answer": explanation, "cypher": None, "rows": []}

    ok, reason = validate_cypher(cypher)
    if not ok:
        return {
            "answer": f"That query was blocked: {reason}. This tool is read-only.",
            "cypher": cypher,
            "rows": [],
            "blocked": True,
        }

    try:
        rows = run_cypher(cypher, dataset_id)
    except Exception as exc:
        # One repair attempt with the real database error fed back in.
        try:
            repaired = complete_json(
                system,
                f"This Cypher failed.\n\nQuery:\n{cypher}\n\nError:\n{exc}\n\n"
                f"Original question: {message}\n\nReturn corrected JSON.",
                temperature=0.1,
                max_tokens=1024,
            )
            cypher = repaired.get("cypher") or cypher
            ok, reason = validate_cypher(cypher)
            if not ok:
                raise ValueError(reason)
            rows = run_cypher(cypher, dataset_id)
        except Exception as exc2:
            return {
                "answer": "That query failed even after I tried to repair it. "
                          "Try asking in a simpler way.",
                "cypher": cypher,
                "rows": [],
                "error": str(exc2),
            }

    if not rows:
        return {
            "answer": "No rows matched that question. The data may not contain what you asked for.",
            "cypher": cypher,
            "rows": [],
            "totalRows": 0,
            "explanation": explanation,
        }

    summary = complete(
        "You are a concise data analyst. You summarise query results in plain business "
        "English. Never mention Cypher, graphs, databases or queries. Never use markdown.",
        f'The user asked: "{message}"\n\n'
        f"{len(rows)} rows came back. Here are the first {min(len(rows), MAX_ROWS_TO_MODEL)}:\n"
        f"{json.dumps(rows[:MAX_ROWS_TO_MODEL], indent=2, default=str)}\n\n"
        f"Write 2-4 sentences answering the question. Cite specific names and numbers.",
        temperature=0.3,
        max_tokens=300,
    )

    return {
        "answer": summary,
        "cypher": cypher,
        "explanation": explanation,
        "rows": rows[:MAX_ROWS_TO_CLIENT],
        "totalRows": len(rows),
    }


def suggest_questions(schema: dict, dataset_name: str) -> list[str]:
    """Generate starter questions so the UI is never a blank box."""
    try:
        result = complete_json(
            "You write example analytical questions. Return ONLY "
            '{"questions": ["...", "..."]} with exactly 6 questions.',
            f'A dataset called "{dataset_name}" has this graph schema:\n\n'
            f"{build_schema_prompt(schema)}\n\n"
            "Write 6 short, specific questions a manager would actually ask of this data. "
            "Vary them: at least one counting question, one ranking question, one that "
            "traverses a relationship, and one looking for gaps or anomalies. "
            "If any entity is joined across several tables, make at least two questions "
            "span those tables — that is the most interesting thing this graph can do. "
            "No question may exceed 12 words.",
            temperature=0.5,
            max_tokens=512,
        )
        questions = [q for q in result.get("questions", []) if isinstance(q, str)]
        if questions:
            return questions[:6]
    except Exception:
        pass

    # Deterministic fallback so the UI still works if the model is unavailable.
    fallback = []
    for node in schema["nodes"][:3]:
        fallback.append(f"How many {node['label']} records are there?")
    for rel in schema["relationships"][:3]:
        fallback.append(f"Which {rel['from']} has the most {rel['to']}?")
    return fallback[:6] or ["What is in this dataset?"]
