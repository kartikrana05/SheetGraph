"""
Schema inference — the core idea of this project.

Given a column profile of an arbitrary spreadsheet, propose a property-graph
schema: which columns become node labels, which become properties hanging off
those nodes, and which columns imply relationships between them.

The user can then refine the proposal in natural language ("split owner into
its own Person node", "drop the budget columns") and we re-derive.
"""

from __future__ import annotations

import json
import re

from llm import complete_json

RESERVED_LABELS = {"_Dataset", "_Column"}

SYSTEM_PROMPT = """\
You are a data modelling expert who converts flat spreadsheets into property-graph schemas for Neo4j.

You will be given a statistical profile of a spreadsheet: every column with its
semantic type, distinct-value count, fill rate, uniqueness ratio and sample values.

Your job is to propose a graph schema. Return ONLY a JSON object, no prose, no markdown.

OUTPUT FORMAT:
{
  "datasetName": "Short human name for this dataset",
  "summary": "2-3 sentences on what this sheet appears to track and how you modelled it",
  "nodes": [
    {
      "label": "Project",
      "key": "projectId",
      "keyColumn": "Project ID",
      "properties": [
        {"name": "projectName", "column": "Project Name"},
        {"name": "status", "column": "Status"}
      ],
      "reason": "Why this is its own node"
    }
  ],
  "relationships": [
    {
      "type": "OWNED_BY",
      "from": "Project",
      "to": "Person",
      "reason": "Each row links a project to its owner"
    }
  ]
}

MODELLING RULES:
1. Every row of the sheet describes one primary entity. That entity is always a node.
2. A column with LOW distinct count relative to row count (a category — status, region,
   owner, department, priority, sprint) is a strong candidate to become its OWN node,
   because that is what makes the graph interesting to traverse. Prefer promoting these.
3. A column with a HIGH uniqueness ratio on the primary entity (an ID, a title, a
   free-text description) stays a PROPERTY of the primary node, not its own node.
4. Numeric measures (budget, hours, count, amount, percentage) are ALWAYS properties,
   never nodes.
5. Dates are properties. Only promote a date to a node if the user explicitly asks.
6. Every node MUST have a "key" — the property that uniquely identifies it. For a
   promoted category node the key is usually the category value itself.
   "keyColumn" must be the EXACT column name from the profile.
7. Relationship types are UPPER_SNAKE_CASE verbs read from the primary entity outward
   (OWNED_BY, ASSIGNED_TO, BELONGS_TO, IN_STATUS, TARGETS).
8. Node labels are PascalCase and singular (Project, Person, Sprint, Region).
9. Property names are lowerCamelCase.
10. Aim for 3 to 7 node labels. Fewer than 3 makes a boring graph; more than 7 is noise.
11. Every "column" value you emit MUST be an exact column name from the profile.
    Never invent a column. Never reference a column twice as a property of the same node.
12. Every node label in "relationships" MUST exist in "nodes".
"""

REFINE_PROMPT = """\
You are refining an existing property-graph schema based on a user instruction.

You will be given the column profile of the source spreadsheet, the current schema,
and the user's instruction. Apply the instruction and return the COMPLETE updated
schema in exactly the same JSON format. Do not return a diff.

Preserve everything the user did not ask you to change. Obey all the original
modelling rules: exact column names only, every node needs a key and keyColumn,
labels PascalCase, relationship types UPPER_SNAKE_CASE, relationship endpoints must
exist in nodes.

If the instruction is impossible (references a column that does not exist, or asks
for something incoherent), return the schema unchanged and explain why in the
"summary" field, prefixed with "Could not apply: ".

Return ONLY the JSON object.
"""


def _profile_for_prompt(profile: dict) -> str:
    """Compact the profile so it fits the context window comfortably."""
    lines = [
        f"File: {profile['filename']}",
        f"Rows: {profile['rowCount']}, Columns: {profile['columnCount']}",
        "",
        "COLUMNS:",
    ]
    for col in profile["columns"]:
        samples = ", ".join(
            json.dumps(v, default=str) for v in col["sampleValues"][:5]
        )
        lines.append(
            f"- \"{col['name']}\" | type={col['semanticType']} "
            f"| distinct={col['distinctCount']} | uniqueRatio={col['uniqueRatio']} "
            f"| fillRate={col['fillRate']} | samples: {samples}"
        )
    return "\n".join(lines)


def _pascal(value: str) -> str:
    parts = re.split(r"[^0-9a-zA-Z]+", str(value))
    return "".join(p[:1].upper() + p[1:] for p in parts if p) or "Entity"


def _upper_snake(value: str) -> str:
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", str(value)).strip("_").upper()
    return slug or "RELATED_TO"


def _camel(value: str) -> str:
    parts = [p for p in re.split(r"[^0-9a-zA-Z]+", str(value)) if p]
    if not parts:
        return "field"
    head, *tail = parts
    name = head[:1].lower() + head[1:] + "".join(p[:1].upper() + p[1:] for p in tail)
    return f"f_{name}" if name[0].isdigit() else name


def validate_schema(schema: dict, profile: dict) -> tuple[dict, list[str]]:
    """
    Repair an LLM-proposed schema against the real column list.

    The model is good at the modelling judgement and bad at exact string
    fidelity, so we normalise casing, drop references to columns that do not
    exist, and guarantee every node has a usable key. Returns the cleaned
    schema plus a list of human-readable warnings to surface in the UI.
    """
    warnings: list[str] = []
    valid_columns = {c["name"] for c in profile["columns"]}

    cleaned_nodes = []
    seen_labels: set[str] = set()

    for node in schema.get("nodes") or []:
        label = _pascal(node.get("label", ""))
        if not label or label in RESERVED_LABELS:
            warnings.append(f"Dropped node with reserved or empty label: {node.get('label')!r}")
            continue
        if label in seen_labels:
            warnings.append(f"Dropped duplicate node label {label!r}")
            continue

        key_column = node.get("keyColumn")
        if key_column not in valid_columns:
            warnings.append(
                f"Node {label!r} referenced unknown key column {key_column!r} and was dropped"
            )
            continue

        properties = []
        seen_props = {_camel(node.get("key") or key_column)}
        for prop in node.get("properties") or []:
            column = prop.get("column")
            if column not in valid_columns:
                warnings.append(
                    f"Property {prop.get('name')!r} on {label!r} referenced unknown column "
                    f"{column!r} and was dropped"
                )
                continue
            name = _camel(prop.get("name") or column)
            if name in seen_props:
                continue
            seen_props.add(name)
            properties.append({"name": name, "column": column})

        seen_labels.add(label)
        cleaned_nodes.append(
            {
                "label": label,
                "key": _camel(node.get("key") or key_column),
                "keyColumn": key_column,
                "properties": properties,
                "reason": node.get("reason", ""),
            }
        )

    cleaned_rels = []
    seen_rels: set[tuple[str, str, str]] = set()

    for rel in schema.get("relationships") or []:
        source = _pascal(rel.get("from", ""))
        target = _pascal(rel.get("to", ""))
        rel_type = _upper_snake(rel.get("type", ""))

        if source not in seen_labels or target not in seen_labels:
            warnings.append(
                f"Dropped relationship {rel_type} because {source} or {target} is not a node"
            )
            continue
        if source == target:
            warnings.append(f"Dropped self-referencing relationship {rel_type} on {source}")
            continue

        signature = (source, rel_type, target)
        if signature in seen_rels:
            continue
        seen_rels.add(signature)

        cleaned_rels.append(
            {"type": rel_type, "from": source, "to": target, "reason": rel.get("reason", "")}
        )

    if not cleaned_nodes:
        raise ValueError(
            "Could not derive any valid nodes from this sheet. "
            "Check that it has headers in the first row."
        )

    return (
        {
            "datasetName": schema.get("datasetName") or profile["filename"],
            "summary": schema.get("summary", ""),
            "nodes": cleaned_nodes,
            "relationships": cleaned_rels,
        },
        warnings,
    )


def propose_schema(profile: dict, hint: str | None = None) -> tuple[dict, list[str]]:
    """Ask the model for an initial schema, then validate it against reality."""
    user = _profile_for_prompt(profile)
    if hint:
        user += (
            f"\n\nThe user has given this additional context about what they want "
            f"to analyse — weight it heavily:\n{hint}"
        )

    raw = complete_json(SYSTEM_PROMPT, user, temperature=0.2, max_tokens=2048)
    return validate_schema(raw, profile)


def refine_schema(profile: dict, schema: dict, instruction: str) -> tuple[dict, list[str]]:
    """Apply a natural-language edit to an existing schema."""
    user = (
        f"{_profile_for_prompt(profile)}\n\n"
        f"CURRENT SCHEMA:\n{json.dumps(schema, indent=2)}\n\n"
        f"USER INSTRUCTION:\n{instruction}"
    )
    raw = complete_json(REFINE_PROMPT, user, temperature=0.1, max_tokens=2048)
    return validate_schema(raw, profile)
