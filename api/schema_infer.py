"""
Schema inference — the core idea of this project.

Given column profiles for one or more uploaded tables, propose a single
property-graph schema: which columns become node labels, which stay as
properties, how they relate, and — crucially — which entities appear in more
than one sheet and should therefore become ONE node fed by several sources.

That last part is what turns a pile of spreadsheets into a connected graph.
"""

from __future__ import annotations

import json
import re

from llm import complete_json

RESERVED_LABELS = {"_Dataset", "_Column"}
MAX_SHEETS = 8
# Prompt size grows with sheets x columns, and it is the multi-sheet uploads
# that push the request long enough to be dropped. A sheet with more columns
# than this contributes its first N; the profile is ordered as the sheet is,
# so the primary key and main attributes come first.
MAX_COLUMNS_PER_SHEET = 22

SYSTEM_PROMPT = """\
You are a data modelling expert who converts spreadsheets into a single property-graph
schema for Neo4j.

You will be given a statistical profile of one or more tables. Each table lists its
columns with semantic type, distinct-value count, fill rate, uniqueness ratio and
sample values.

Return ONLY a JSON object, no prose, no markdown.

OUTPUT FORMAT:
{
  "datasetName": "Short human name for this whole dataset",
  "summary": "2 sentences: what these tables track and which entities you joined across them",
  "nodes": [
    {
      "label": "Product",
      "key": "sku",
      "sources": [
        {
          "sheet": "sales",
          "keyColumn": "SKU",
          "properties": [{"name": "unitPrice", "column": "Unit Price"}]
        },
        {
          "sheet": "product_master",
          "keyColumn": "Product Code",
          "properties": [{"name": "brand", "column": "Brand"}]
        }
      ],
      "reason": "One short clause. Omit entirely if obvious."
    }
  ],
  "relationships": [
    {
      "type": "CONTAINS",
      "from": "Invoice",
      "to": "Product",
      "sheet": "sales",
      "reason": ""
    }
  ]
}

MODELLING RULES:
1. Every row of a table describes one primary entity. That entity is always a node.
2. A column with LOW distinct count relative to row count (a category — status, region,
   owner, department, priority, channel) should become its OWN node. That is what makes
   the graph worth traversing. Prefer promoting these.
3. A column with a HIGH uniqueness ratio on the primary entity (an ID, or a free-text
   field) stays a PROPERTY of the primary node, not its own node.
4. FREE TEXT IS NEVER A NODE, even when its distinct count is low. If the sample values
   are sentences, notes, comments, descriptions or subject lines, it is a property.
   Judge by whether the values read like prose, not by the count.
5. Numeric measures (amount, quantity, price, hours, score, percentage) are ALWAYS
   properties. So are dates.
6. Every node MUST have a "key" — the property uniquely identifying it — and every
   source MUST give the exact "keyColumn" from that sheet's profile.

CROSS-SHEET JOINING — the most important part:
7. If the SAME real-world entity appears in two or more tables, emit ONE node with
   several entries in "sources". Match on meaning and on overlapping sample values,
   not on identical column names: "SKU" in one sheet and "Product Code" in another are
   the same product if their values look alike.
8. Look hard for these joins. Two tables that share a customer, product, employee,
   region or order are the reason to model them together at all. A schema where no
   node has more than one source is usually a missed opportunity — say so in the
   summary if you genuinely could not find any.
9. Do NOT join two columns just because their names match. If the sample values are
   clearly different kinds of thing, keep them as separate nodes.

NAMING AND VALIDITY:
10. Node labels are PascalCase and singular. Property names are lowerCamelCase.
    Relationship types are UPPER_SNAKE_CASE verbs.
11. Every "sheet" value MUST be an exact sheet name from the profile. Every "column"
    MUST be an exact column name from THAT sheet. Never invent either.
12. A relationship's "sheet" must be a sheet where BOTH endpoint nodes have a source —
    that is what makes the two ends joinable row by row.
13. Aim for 4 to 9 node labels overall. Fewer is a boring graph; more is noise.
14. Keep prose to a minimum. "reason" is one short clause or an empty string, and
    "summary" is two sentences. The JSON must be COMPLETE — a response cut off
    mid-object is unusable, so favour brevity over explanation.
"""

REFINE_PROMPT = """\
You are refining an existing property-graph schema based on a user instruction.

You will be given the column profiles of the source tables, the current schema, and the
user's instruction. Apply it and return the COMPLETE updated schema in exactly the same
JSON format. Do not return a diff.

Preserve everything the user did not ask you to change. Obey all the original modelling
rules: exact sheet and column names only, every node needs a key and every source needs
a keyColumn, labels PascalCase, relationship types UPPER_SNAKE_CASE, and a
relationship's sheet must be one where both endpoints have a source.

If the instruction is impossible (references something that does not exist, or is
incoherent), return the schema unchanged and explain why in "summary", prefixed with
"Could not apply: ".

Return ONLY the JSON object.
"""


def _sheet_block(profile: dict) -> str:
    lines = [
        f'TABLE "{profile["sheetName"]}"  ({profile["rowCount"]} rows, '
        f'{profile["columnCount"]} columns)',
    ]
    columns = profile["columns"][:MAX_COLUMNS_PER_SHEET]
    dropped = len(profile["columns"]) - len(columns)

    for col in columns:
        # Three samples is enough to judge a column's meaning, and this block is
        # repeated for every column of every sheet — the single biggest lever on
        # how long inference takes.
        samples = ", ".join(json.dumps(v, default=str) for v in col["sampleValues"][:3])
        lines.append(
            f'  - "{col["name"]}" | type={col["semanticType"]} '
            f'| distinct={col["distinctCount"]} | uniqueRatio={col["uniqueRatio"]} '
            f'| fillRate={col["fillRate"]} | samples: {samples}'
        )

    if dropped:
        # Say so rather than silently modelling a subset of the sheet.
        lines.append(f'  ({dropped} further columns omitted to keep this prompt small)')

    return "\n".join(lines)


def profiles_for_prompt(profiles: list[dict]) -> str:
    return "\n\n".join(_sheet_block(p) for p in profiles[:MAX_SHEETS])


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


def _resolve_sheet(given: str, valid: dict[str, dict]) -> str | None:
    """
    Map a sheet name from the model onto a real one.

    The model reliably gets the modelling judgement right and unreliably
    reproduces long names verbatim, so an exact match is tried first and a
    normalised match second.
    """
    if given in valid:
        return given
    if not given:
        return None
    squashed = re.sub(r"[^a-z0-9]", "", str(given).lower())
    for name in valid:
        if re.sub(r"[^a-z0-9]", "", name.lower()) == squashed:
            return name
    return None


def validate_schema(schema: dict, profiles: list[dict]) -> tuple[dict, list[str]]:
    """
    Repair an LLM-proposed schema against the real sheets and columns.

    Returns the cleaned schema plus human-readable warnings for the UI. Anything
    referencing a sheet or column that does not exist is dropped rather than
    allowed to fail later at ingest time with a Cypher error.
    """
    warnings: list[str] = []
    by_sheet = {p["sheetName"]: {c["name"] for c in p["columns"]} for p in profiles}

    cleaned_nodes = []
    seen_labels: set[str] = set()

    for node in schema.get("nodes") or []:
        label = _pascal(node.get("label", ""))
        if not label or label in RESERVED_LABELS:
            warnings.append(f"Dropped node with reserved or empty label {node.get('label')!r}")
            continue
        if label in seen_labels:
            warnings.append(f"Dropped duplicate node label {label!r}")
            continue

        # Accept the single-source shape too, so a schema hand-edited in the
        # browser or produced by an older prompt still loads.
        raw_sources = node.get("sources")
        if not raw_sources and node.get("keyColumn"):
            raw_sources = [{
                "sheet": node.get("sheet") or (profiles[0]["sheetName"] if profiles else ""),
                "keyColumn": node["keyColumn"],
                "properties": node.get("properties") or [],
            }]

        key = _camel(node.get("key") or "id")
        sources = []

        for source in raw_sources or []:
            sheet = _resolve_sheet(source.get("sheet", ""), by_sheet)
            if sheet is None:
                warnings.append(
                    f"Node {label!r} referenced unknown sheet {source.get('sheet')!r}; that source was dropped"
                )
                continue

            columns = by_sheet[sheet]
            key_column = source.get("keyColumn")
            if key_column not in columns:
                warnings.append(
                    f"Node {label!r} referenced unknown key column {key_column!r} "
                    f"in sheet {sheet!r}; that source was dropped"
                )
                continue

            properties = []
            seen_props = {key}
            for prop in source.get("properties") or []:
                column = prop.get("column")
                if column not in columns:
                    warnings.append(
                        f"Property {prop.get('name')!r} on {label!r} referenced unknown "
                        f"column {column!r} in {sheet!r} and was dropped"
                    )
                    continue
                name = _camel(prop.get("name") or column)
                if name in seen_props:
                    continue
                seen_props.add(name)
                properties.append({"name": name, "column": column})

            sources.append({"sheet": sheet, "keyColumn": key_column, "properties": properties})

        if not sources:
            warnings.append(f"Dropped node {label!r} — it had no usable source")
            continue

        seen_labels.add(label)
        cleaned_nodes.append({
            "label": label,
            "key": key,
            "sources": sources,
            "reason": node.get("reason", ""),
        })

    node_sheets = {n["label"]: {s["sheet"] for s in n["sources"]} for n in cleaned_nodes}

    cleaned_rels = []
    seen_rels: set[tuple[str, str, str, str]] = set()

    for rel in schema.get("relationships") or []:
        source_label = _pascal(rel.get("from", ""))
        target_label = _pascal(rel.get("to", ""))
        rel_type = _upper_snake(rel.get("type", ""))

        if source_label not in seen_labels or target_label not in seen_labels:
            warnings.append(
                f"Dropped {rel_type} — {source_label} or {target_label} is not a node"
            )
            continue
        if source_label == target_label:
            warnings.append(f"Dropped self-referencing relationship {rel_type} on {source_label}")
            continue

        # Both endpoints must be present in the same sheet, otherwise there is
        # no row that carries both sides and the relationship can never be built.
        shared = node_sheets[source_label] & node_sheets[target_label]
        if not shared:
            warnings.append(
                f"Dropped {source_label}-[:{rel_type}]->{target_label} — no single sheet "
                f"contains both, so there is no row linking them"
            )
            continue

        sheet = _resolve_sheet(rel.get("sheet", ""), by_sheet)
        if sheet not in shared:
            chosen = sorted(shared)[0]
            if sheet is not None:
                warnings.append(
                    f"{rel_type} named sheet {sheet!r}, which lacks one endpoint; used {chosen!r} instead"
                )
            sheet = chosen

        signature = (source_label, rel_type, target_label, sheet)
        if signature in seen_rels:
            continue
        seen_rels.add(signature)

        cleaned_rels.append({
            "type": rel_type,
            "from": source_label,
            "to": target_label,
            "sheet": sheet,
            "reason": rel.get("reason", ""),
        })

    if not cleaned_nodes:
        raise ValueError(
            "Could not derive any valid nodes from these sheets. "
            "Check that each has headers in the first row."
        )

    default_name = profiles[0]["sheetName"] if profiles else "Dataset"
    return (
        {
            "datasetName": schema.get("datasetName") or default_name,
            "summary": schema.get("summary", ""),
            "sheets": [p["sheetName"] for p in profiles],
            "nodes": cleaned_nodes,
            "relationships": cleaned_rels,
        },
        warnings,
    )


def join_report(schema: dict) -> list[dict]:
    """Which nodes are fed by more than one sheet — the cross-sheet joins."""
    return [
        {"label": n["label"], "key": n["key"],
         "sheets": [s["sheet"] for s in n["sources"]],
         "keyColumns": [s["keyColumn"] for s in n["sources"]]}
        for n in schema["nodes"]
        if len(n["sources"]) > 1
    ]


def _norm(name: str) -> str:
    """Squash a column name for cross-sheet comparison."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def heuristic_schema(profiles: list[dict]) -> dict:
    """
    Derive a schema from the column statistics alone, with no model call.

    This is the fallback when inference fails — a rate limit, an outage, a
    truncated response. It applies the same rules the prompt describes, just
    mechanically: the most unique identifier is the primary key, low-cardinality
    categories become their own nodes, measures and dates stay as properties,
    and a column whose name matches another sheet's key is treated as a join.

    Cruder than the model — it cannot tell that "Sales Rep ID" and "Employee ID"
    are the same thing — but it always returns something usable.
    """
    nodes: dict[str, dict] = {}
    relationships: list[dict] = []

    # Pass one: each sheet's primary entity, keyed on its most unique column.
    primary_of: dict[str, str] = {}
    for profile in profiles:
        sheet = profile["sheetName"]
        columns = profile["columns"]
        if not columns:
            continue

        key_col = max(
            columns,
            key=lambda c: (c["semanticType"] == "identifier", c["uniqueRatio"]),
        )
        label = _pascal(re.sub(r"\s*(id|code|no|number)\s*$", "", key_col["name"], flags=re.I)) or "Record"
        while label in nodes and nodes[label]["sources"][0]["sheet"] != sheet:
            label = f"{label}Item"

        properties = [
            {"name": _camel(c["name"]), "column": c["name"]}
            for c in columns
            if c is not key_col
            and c["semanticType"] in {"measure", "date", "text", "boolean", "identifier"}
        ]

        nodes.setdefault(label, {
            "label": label,
            "key": _camel(key_col["name"]),
            "sources": [],
            "reason": f"Primary entity of {sheet}",
        })
        nodes[label]["sources"].append({
            "sheet": sheet, "keyColumn": key_col["name"], "properties": properties,
        })
        primary_of[sheet] = label

    # Foreign keys are joins, not categories. Without this, "Product Code" in
    # Orders becomes a separate ProductCode node alongside the Product node it
    # actually refers to.
    claimed_keys = {
        _norm(src["keyColumn"])
        for node in nodes.values()
        for src in node["sources"]
    }

    # Pass two: promote low-cardinality categories to their own nodes.
    for profile in profiles:
        sheet = profile["sheetName"]
        parent = primary_of.get(sheet)
        if not parent:
            continue
        rows = max(1, profile["rowCount"])

        for col in profile["columns"]:
            if col["semanticType"] != "category":
                continue
            if _norm(col["name"]) in claimed_keys:
                continue
            if col["distinctCount"] > 30 or col["distinctCount"] / rows > 0.5:
                continue
            # Sentences are not entities, however few distinct values there are.
            if any(len(str(v)) > 40 for v in col["sampleValues"][:3]):
                continue

            label = _pascal(col["name"])
            if label == parent or label in nodes:
                continue
            nodes[label] = {
                "label": label, "key": "name", "sources": [
                    {"sheet": sheet, "keyColumn": col["name"], "properties": []}
                ],
                "reason": f"{col['distinctCount']} distinct values across {rows} rows",
            }
            relationships.append({
                "type": _upper_snake(f"HAS_{col['name']}"),
                "from": parent, "to": label, "sheet": sheet, "reason": "",
            })

    # Pass three: a column matching another sheet's key column is a join.
    key_columns = {
        _norm(src["keyColumn"]): (label, src["keyColumn"])
        for label, node in nodes.items()
        for src in node["sources"]
    }
    for profile in profiles:
        sheet = profile["sheetName"]
        parent = primary_of.get(sheet)
        for col in profile["columns"]:
            match = key_columns.get(_norm(col["name"]))
            if not match:
                continue
            label, _ = match
            if label == parent:
                continue
            already = any(s["sheet"] == sheet for s in nodes[label]["sources"])
            if not already:
                nodes[label]["sources"].append(
                    {"sheet": sheet, "keyColumn": col["name"], "properties": []}
                )
            if parent and not any(
                r["from"] == parent and r["to"] == label for r in relationships
            ):
                relationships.append({
                    "type": _upper_snake(f"REFERS_TO_{label}"),
                    "from": parent, "to": label, "sheet": sheet, "reason": "",
                })

    return {
        "datasetName": profiles[0]["filename"].rsplit(".", 1)[0] if profiles else "Dataset",
        "summary": "Derived from column statistics without a language model, because "
                   "inference was unavailable. Review it before seeding — the joins in "
                   "particular are matched on column name only.",
        "nodes": list(nodes.values()),
        "relationships": relationships,
    }


def propose_schema(profiles: list[dict], hint: str | None = None) -> tuple[dict, list[str]]:
    """Ask the model for a schema across all sheets, then validate it."""
    user = profiles_for_prompt(profiles)
    if len(profiles) > 1:
        user += (
            f"\n\nThere are {len(profiles)} tables. Look carefully for entities that "
            f"appear in more than one of them and model each as a SINGLE node with "
            f"multiple sources."
        )
    if hint:
        user += f"\n\nThe user wants to analyse this — weight it heavily:\n{hint}"

    try:
        raw = complete_json(SYSTEM_PROMPT, user, temperature=0.2, max_tokens=4000)
        return validate_schema(raw, profiles)
    except Exception as exc:
        # Never leave the user stuck at step two. A mechanical schema they can
        # refine beats an error they cannot act on.
        schema, warnings = validate_schema(heuristic_schema(profiles), profiles)
        warnings.insert(0, f"AI schema inference failed ({type(exc).__name__}: {exc}). "
                           f"Fell back to a rule-based schema — review it, and use the "
                           f"box below to change anything.")
        return schema, warnings


def refine_schema(profiles: list[dict], schema: dict, instruction: str) -> tuple[dict, list[str]]:
    """Apply a natural-language edit to an existing schema."""
    user = (
        f"{profiles_for_prompt(profiles)}\n\n"
        f"CURRENT SCHEMA:\n{json.dumps(schema, indent=2)}\n\n"
        f"USER INSTRUCTION:\n{instruction}"
    )
    raw = complete_json(REFINE_PROMPT, user, temperature=0.1, max_tokens=4000)
    return validate_schema(raw, profiles)
