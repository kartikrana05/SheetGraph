"""
Saved endpoints — turn a natural-language question into a reusable REST API.

The user describes what they want and which filters it should accept. The model
writes read-only Cypher with named parameters; we validate it, store it under a
slug, and serve it at GET /api/data/{slug}?filter=value.

The saved artefact is a *parameterised* query, not a frozen result — which is
what makes it worth calling more than once.
"""

from __future__ import annotations

import json
import re
import time

import graphdb
from llm import complete_json
from query import build_schema_prompt, validate_cypher

# `ds` is bound internally to scope every query to its dataset; a user-declared
# parameter of that name would silently break the isolation.
RESERVED_PARAMS = {"ds"}
PARAM_TYPES = {"string", "number", "boolean"}
MAX_PARAMS = 8
MAX_ROWS = 1000
DEFAULT_ROWS = 100

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")
PARAM_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,30}$")
CYPHER_PARAM_RE = re.compile(r"\$([a-zA-Z][a-zA-Z0-9_]*)")


DRAFT_PROMPT = """\
You design read-only REST endpoints backed by Cypher over a Neo4j graph.

The user will describe what the endpoint should return and which filters it should
accept. Produce one parameterised Cypher query plus the parameter declarations.

Return ONLY a JSON object:
{
  "name": "Top distributors by revenue",
  "description": "One line on what a caller gets back",
  "cypher": "MATCH (d:Distributor) WHERE d._ds = $ds AND ($city IS NULL OR d.city = $city) RETURN ... LIMIT toInteger($limit)",
  "params": [
    {"name": "city", "type": "string", "required": false, "default": null,
     "description": "Filter to one city", "example": "Mumbai"},
    {"name": "limit", "type": "number", "required": false, "default": 25,
     "description": "Maximum rows", "example": 10}
  ]
}

RULES:
1. The query MUST be read-only. Never CREATE, MERGE, SET, DELETE, REMOVE, DROP or LOAD.
2. EVERY node pattern must be scoped with `$ds`, e.g. MATCH (d:Distributor) WHERE d._ds = $ds
3. OPTIONAL filters must use the null-guard idiom so that omitting them widens the
   query rather than returning nothing:
       AND ($city IS NULL OR d.city = $city)
   For case-insensitive text matching prefer:
       AND ($name IS NULL OR toLower(d.name) CONTAINS toLower($name))
4. REQUIRED filters are compared directly: AND d.tier = $tier
5. Always accept a `limit` parameter, default 25, and end with LIMIT toInteger($limit).
6. Parameter names are lowerCamelCase, letters and digits only. Never name one "ds".
7. Every parameter you declare MUST appear in the Cypher, and every $parameter in the
   Cypher except $ds MUST be declared. Mismatches are rejected.
8. Types are exactly "string", "number" or "boolean".
9. Return readable aliased scalars, not whole nodes:
       RETURN d.name AS distributor, sum(toFloat(i.netValue)) AS revenue
10. Use toFloat() before numeric aggregation, since spreadsheet values may be text.
11. At most 8 parameters.
12. Never invent labels or properties — use only the schema given.
"""


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:48].strip("-")
    if len(slug) < 3:
        slug = f"endpoint-{slug}" if slug else "endpoint"
    return slug


def _coerce(value, declared_type: str):
    """Turn a query-string value into the declared type, or raise."""
    if value is None:
        return None

    if declared_type == "number":
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"expected a number, got {value!r}")
        return int(number) if number.is_integer() else number

    if declared_type == "boolean":
        lowered = str(value).strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
        raise ValueError(f"expected true or false, got {value!r}")

    return str(value)


def validate_params(raw_params: list) -> list[dict]:
    cleaned: list[dict] = []
    seen: set[str] = set()

    for param in raw_params or []:
        name = str(param.get("name", "")).strip()
        if not PARAM_NAME_RE.match(name):
            raise ValueError(f"Invalid parameter name {name!r}")
        if name in RESERVED_PARAMS:
            raise ValueError(f"{name!r} is reserved for internal use")
        if name in seen:
            raise ValueError(f"Duplicate parameter {name!r}")
        seen.add(name)

        declared_type = str(param.get("type", "string")).lower()
        if declared_type not in PARAM_TYPES:
            declared_type = "string"

        default = param.get("default")
        if default is not None:
            try:
                default = _coerce(default, declared_type)
            except ValueError as exc:
                raise ValueError(f"Default for {name!r}: {exc}")

        cleaned.append({
            "name": name,
            "type": declared_type,
            "required": bool(param.get("required", False)),
            "default": default,
            "description": str(param.get("description", ""))[:200],
            "example": param.get("example"),
        })

    if len(cleaned) > MAX_PARAMS:
        raise ValueError(f"At most {MAX_PARAMS} parameters are allowed")

    return cleaned


def check_cypher(cypher: str, params: list[dict]) -> None:
    """
    Reject anything unsafe or internally inconsistent.

    A mismatch between declared parameters and the ones the query actually uses
    is the difference between a filter that works and one that silently does
    nothing, so it is treated as an error rather than tidied up.
    """
    ok, reason = validate_cypher(cypher)
    if not ok:
        raise ValueError(reason)

    used = set(CYPHER_PARAM_RE.findall(cypher)) - RESERVED_PARAMS
    declared = {p["name"] for p in params}

    undeclared = used - declared
    if undeclared:
        raise ValueError(
            f"The query uses {', '.join(sorted(undeclared))} but never declares "
            f"{'them' if len(undeclared) > 1 else 'it'} as a parameter"
        )

    unused = declared - used
    if unused:
        raise ValueError(
            f"{', '.join(sorted(unused))} {'are' if len(unused) > 1 else 'is'} declared "
            f"but never used in the query, so filtering by "
            f"{'them' if len(unused) > 1 else 'it'} would do nothing"
        )

    if "_ds" not in cypher and "$ds" not in cypher:
        raise ValueError("The query must be scoped to the dataset with $ds")


def draft(dataset_id: str, prompt: str) -> dict:
    """Ask the model for a parameterised query, then validate it hard."""
    dataset = graphdb.get_dataset(dataset_id)
    if not dataset:
        raise ValueError("That dataset no longer exists")

    system = (
        f"{DRAFT_PROMPT}\n\nThe graph you are querying:\n\n"
        f"{build_schema_prompt(dataset['schema'])}"
    )

    result = complete_json(system, prompt, temperature=0.1, max_tokens=1500)

    cypher = (result.get("cypher") or "").strip()
    if not cypher:
        raise ValueError("The model did not produce a query for that description")

    params = validate_params(result.get("params"))
    check_cypher(cypher, params)

    return {
        "name": result.get("name") or "Untitled endpoint",
        "description": result.get("description", ""),
        "cypher": cypher,
        "params": params,
    }


# ─────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────


def _unique_slug(base: str) -> str:
    slug, suffix = base, 2
    while True:
        with graphdb.open_session() as session:
            exists = session.run(
                "MATCH (e:_Endpoint {slug: $slug}) RETURN count(e) AS n", slug=slug
            ).single()["n"]
        if not exists:
            return slug
        slug = f"{base}-{suffix}"
        suffix += 1


def save(dataset_id: str, name: str, description: str, cypher: str, params: list) -> dict:
    dataset = graphdb.get_dataset(dataset_id)
    if not dataset:
        raise ValueError("That dataset no longer exists")

    cleaned_params = validate_params(params)
    check_cypher(cypher, cleaned_params)

    slug = _unique_slug(_slugify(name))
    if not SLUG_RE.match(slug):
        raise ValueError(f"Could not derive a usable name from {name!r}")

    with graphdb.open_session() as session:
        session.run(
            """
            MATCH (d:_Dataset {id: $datasetId})
            MERGE (e:_Endpoint {slug: $slug})
            SET e.name = $name,
                e.description = $description,
                e.cypher = $cypher,
                e.params = $params,
                e.datasetId = $datasetId,
                e.calls = coalesce(e.calls, 0),
                e.createdAt = coalesce(e.createdAt, datetime())
            MERGE (e)-[:QUERIES]->(d)
            """,
            slug=slug, name=name, description=description, cypher=cypher,
            params=json.dumps(cleaned_params), datasetId=dataset_id,
        ).consume()

    return {
        "slug": slug, "name": name, "description": description,
        "cypher": cypher, "params": cleaned_params, "datasetId": dataset_id,
    }


def get(slug: str) -> dict | None:
    with graphdb.open_session() as session:
        record = session.run(
            "MATCH (e:_Endpoint {slug: $slug}) RETURN e", slug=slug
        ).single()

    if not record:
        return None

    data = dict(record["e"])
    data["params"] = json.loads(data.get("params") or "[]")
    if "createdAt" in data:
        data["createdAt"] = str(data["createdAt"])
    return data


def list_for_dataset(dataset_id: str) -> list[dict]:
    with graphdb.open_session() as session:
        rows = session.run(
            """
            MATCH (e:_Endpoint {datasetId: $id})
            RETURN e.slug AS slug, e.name AS name, e.description AS description,
                   e.cypher AS cypher, e.params AS params, e.calls AS calls,
                   toString(e.createdAt) AS createdAt
            ORDER BY e.createdAt DESC
            """,
            id=dataset_id,
        ).data()

    for row in rows:
        row["params"] = json.loads(row.get("params") or "[]")
    return rows


def delete(slug: str) -> bool:
    with graphdb.open_session() as session:
        result = session.run(
            "MATCH (e:_Endpoint {slug: $slug}) DETACH DELETE e RETURN count(e) AS n",
            slug=slug,
        ).single()
    return bool(result and result["n"])


# ─────────────────────────────────────────────
# Execution
# ─────────────────────────────────────────────


def resolve_args(endpoint: dict, supplied: dict) -> dict:
    """
    Build the Cypher parameter map from the query string.

    Absent optional parameters resolve to None rather than being omitted, which
    is what makes the `$x IS NULL OR ...` idiom in the query widen instead of
    failing on a missing parameter.
    """
    args: dict = {"ds": endpoint["datasetId"]}
    unknown = set(supplied) - {p["name"] for p in endpoint["params"]}
    if unknown:
        raise ValueError(f"Unknown parameter(s): {', '.join(sorted(unknown))}")

    for param in endpoint["params"]:
        name = param["name"]
        raw = supplied.get(name)

        if raw is None or raw == "":
            if param["required"]:
                raise ValueError(f"Missing required parameter '{name}'")
            args[name] = param["default"]
            continue

        try:
            args[name] = _coerce(raw, param["type"])
        except ValueError as exc:
            raise ValueError(f"Parameter '{name}': {exc}")

    # Bound the row count no matter what the caller asks for.
    if "limit" in args:
        value = args["limit"]
        if not isinstance(value, (int, float)) or value <= 0:
            value = DEFAULT_ROWS
        args["limit"] = int(min(value, MAX_ROWS))

    return args


def run(slug: str, supplied: dict) -> dict:
    endpoint = get(slug)
    if not endpoint:
        raise LookupError(f"No endpoint named '{slug}'")

    args = resolve_args(endpoint, supplied)

    # Re-validate on every call: the stored Cypher is data, and data that
    # reaches a database deserves the same scrutiny the second time.
    ok, reason = validate_cypher(endpoint["cypher"])
    if not ok:
        raise ValueError(f"Stored query is no longer valid: {reason}")

    started = time.time()
    with graphdb.open_session() as session:
        rows = session.run(endpoint["cypher"], **args).data()

    with graphdb.open_session() as session:
        session.run(
            "MATCH (e:_Endpoint {slug: $slug}) SET e.calls = coalesce(e.calls, 0) + 1",
            slug=slug,
        ).consume()

    return {
        "endpoint": slug,
        "params": {k: v for k, v in args.items() if k != "ds"},
        "count": len(rows),
        "tookMs": round((time.time() - started) * 1000),
        "data": rows[:MAX_ROWS],
    }


def curl_for(endpoint: dict, base_url: str) -> str:
    """A copy-pasteable example using each parameter's example or default."""
    query = []
    for param in endpoint["params"]:
        value = param.get("example")
        if value is None:
            value = param.get("default")
        if value is None:
            value = {"string": "value", "number": 10, "boolean": "true"}[param["type"]]
        query.append(f"{param['name']}={value}")

    url = f"{base_url.rstrip('/')}/api/data/{endpoint['slug']}"
    if query:
        url += "?" + "&".join(str(q) for q in query)
    return f"curl '{url}'"
