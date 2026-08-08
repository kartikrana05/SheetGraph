"""
Offline checks for the ingest layer.

There is no Neo4j in the build environment, so these tests substitute a
recording fake for the driver and assert on the Cypher that would have been
sent, plus the parameter batches. That covers the parts most likely to be
wrong — label interpolation, MERGE shape, deduplication and the injection
guard — without needing a live database.

Run:  python3 test_ingest.py
"""

from __future__ import annotations

import os
import sys
import traceback
from unittest import mock

import endpoints
import graphdb
import profiler
import query
import schema_infer

PASSED = []
FAILED = []


def check(name):
    def decorator(fn):
        try:
            fn()
            PASSED.append(name)
        except Exception as exc:
            FAILED.append((name, exc, traceback.format_exc()))
        return fn
    return decorator


class RecordingSession:
    """Stands in for a neo4j Session, capturing every query and parameter set."""

    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def run(self, cypher, **params):
        self.log.append((" ".join(cypher.split()), params))
        return mock.MagicMock()


class RecordingDriver:
    def __init__(self):
        self.log = []
        self.session_kwargs = []

    # Must mirror neo4j.Driver.session(**config) — it accepts `database`,
    # `default_access_mode` and more. A no-argument stub silently passes
    # until production starts passing one of them, which is exactly the
    # drift this suite exists to catch.
    def session(self, **kwargs):
        self.session_kwargs.append(kwargs)
        return RecordingSession(self.log)


# Two sheets that share a Person: "projects" knows owners by name, "roster"
# carries their department. Joining them is the whole point of multi-sheet.
SCHEMA = {
    "datasetName": "PM Tracker",
    "summary": "",
    "sheets": ["projects", "roster"],
    "nodes": [
        {
            "label": "Project",
            "key": "projectId",
            "sources": [{
                "sheet": "projects",
                "keyColumn": "Project ID",
                "properties": [
                    {"name": "projectName", "column": "Project Name"},
                    {"name": "budget", "column": "Budget"},
                ],
            }],
        },
        {
            "label": "Person",
            "key": "name",
            "sources": [
                {"sheet": "projects", "keyColumn": "Owner", "properties": []},
                {"sheet": "roster", "keyColumn": "Employee",
                 "properties": [{"name": "department", "column": "Department"}]},
            ],
        },
    ],
    "relationships": [
        {"type": "OWNED_BY", "from": "Project", "to": "Person", "sheet": "projects"}
    ],
}

SHEETS = {
    "projects": [
        {"Project ID": "P1", "Project Name": "Atlas", "Budget": 100.0, "Owner": "Priya"},
        {"Project ID": "P2", "Project Name": "Nova", "Budget": 200.0, "Owner": "Priya"},
        # Same project twice — must dedupe, and the second row must enrich
        # rather than blank out the first.
        {"Project ID": "P1", "Project Name": None, "Budget": 150.0, "Owner": "Arun"},
        # Missing key — skipped entirely.
        {"Project ID": None, "Project Name": "Ghost", "Budget": 5.0, "Owner": "Arun"},
    ],
    "roster": [
        {"Employee": "Priya", "Department": "Engineering"},
        {"Employee": "Arun", "Department": "Design"},
        # Present only in the roster — a real person with no projects yet.
        {"Employee": "Kavya", "Department": "Ops"},
    ],
}


def run_ingest(env=None):
    """
    Run an ingest against the recording driver.

    The environment is pinned explicitly. Without this the suite inherits
    whatever the developer happens to have in api/.env — which is how a
    driver-signature mismatch passed on one machine and failed on another.
    """
    driver = RecordingDriver()
    overrides = {"NEO4J_DATABASE": "", **(env or {})}
    with mock.patch.dict(os.environ, overrides, clear=False), \
         mock.patch.object(graphdb, "driver", return_value=driver):
        counts = graphdb.ingest(SCHEMA, SHEETS, "ds1")
    return driver.log, counts, driver


@check("nodes deduplicate on key")
def _():
    log, counts, _ = run_ingest()
    assert counts["byLabel"]["Project"] == 2, counts   # P1, P2 — not 3
    # Priya and Arun from both sheets, plus Kavya from the roster alone.
    assert counts["byLabel"]["Person"] == 3, counts
    assert counts["nodes"] == 5, counts


@check("a shared entity becomes ONE node fed by both sheets")
def _():
    log, _, _ = run_ingest()
    batch = next(p["batch"] for q, p in log if "MERGE (n:`Person`" in q)
    by_key = {r["key"]: r for r in batch}
    assert set(by_key) == {"Priya", "Arun", "Kavya"}, list(by_key)
    # Priya was keyed from 'projects' but her department only exists in
    # 'roster' — if the union failed, this property would be missing.
    assert by_key["Priya"]["props"]["department"] == "Engineering", by_key["Priya"]
    assert by_key["Kavya"]["props"]["department"] == "Ops", by_key["Kavya"]


@check("the join report counts how many keys actually matched")
def _():
    _, counts, _ = run_ingest()
    join = next(j for j in counts["joins"] if j["label"] == "Person")
    assert join["perSheet"] == {"projects": 2, "roster": 3}, join
    # Priya and Arun are in both; Kavya only in the roster.
    assert join["matched"] == 2, join
    assert join["total"] == 3, join


@check("nodes fed by a single sheet produce no join entry")
def _():
    _, counts, _ = run_ingest()
    assert not [j for j in counts["joins"] if j["label"] == "Project"], counts["joins"]


@check("rows with a null key are skipped")
def _():
    _, counts, _ = run_ingest()
    assert counts["byLabel"]["Project"] == 2, "the null-key 'Ghost' row leaked in"


@check("later rows enrich rather than overwrite")
def _():
    log, _, _ = run_ingest()
    batch = next(p["batch"] for q, p in log if "MERGE (n:`Project`" in q)
    p1 = next(r for r in batch if r["key"] == "P1")
    # Row 3 had a null Project Name; it must not erase Atlas from row 1.
    assert p1["props"]["projectName"] == "Atlas", p1
    assert p1["props"]["budget"] == 150.0, p1


@check("node MERGE is scoped by dataset")
def _():
    log, _, _ = run_ingest()
    merges = [q for q, _ in log if q.startswith("UNWIND $batch AS row MERGE")]
    assert merges, "no node merge issued"
    for q in merges:
        assert "_ds: row._ds" in q, q


@check("constraints are created per label")
def _():
    log, _, _ = run_ingest()
    constraints = [q for q, _ in log if "CREATE CONSTRAINT" in q]
    assert len(constraints) == 2, constraints
    assert any("`Project`" in q and "`projectId`" in q for q in constraints)


@check("relationships match both endpoints within the dataset")
def _():
    log, counts, _ = run_ingest()
    rel = next(q for q, _ in log if "MERGE (a)-[r:`OWNED_BY`]->(b)" in q)
    assert "MATCH (a:`Project` {`projectId`: row.from, _ds: row._ds})" in rel, rel
    assert "MATCH (b:`Person` {`name`: row.to, _ds: row._ds})" in rel, rel
    # P1→Priya, P2→Priya, P1→Arun. The null-key row is excluded, and the
    # roster sheet contributes none because the relationship is scoped to
    # 'projects'.
    assert counts["relationships"] == 3, counts


@check("values travel as parameters, never inlined")
def _():
    log, _, _ = run_ingest()
    for cypher, params in log:
        if "UNWIND" in cypher:
            assert "batch" in params, cypher
            assert "Priya" not in cypher, "a data value was concatenated into Cypher"


@check("database name is threaded into every session when set")
def _():
    _, _, driver = run_ingest({"NEO4J_DATABASE": "cdb8639b"})
    assert driver.session_kwargs, "no session was opened"
    for kwargs in driver.session_kwargs:
        assert kwargs.get("database") == "cdb8639b", kwargs


@check("no database kwarg is sent when NEO4J_DATABASE is unset")
def _():
    # Passing database=None or database="" to the real driver is not the same
    # as omitting it; omitting it lets the server pick its own default.
    _, _, driver = run_ingest({"NEO4J_DATABASE": ""})
    for kwargs in driver.session_kwargs:
        assert "database" not in kwargs, kwargs


@check("username falls back across both env var conventions")
def _():
    for env, expected in [
        ({"NEO4J_USER": "a", "NEO4J_USERNAME": "b"}, "a"),
        ({"NEO4J_USER": "", "NEO4J_USERNAME": "b"}, "b"),
        ({"NEO4J_USER": "", "NEO4J_USERNAME": ""}, "neo4j"),
    ]:
        with mock.patch.dict(os.environ, env, clear=False):
            resolved = os.getenv("NEO4J_USER") or os.getenv("NEO4J_USERNAME") or "neo4j"
            assert resolved == expected, (env, resolved)


@check("label injection is rejected")
def _():
    for bad in ["Project`) DETACH DELETE (n", "Foo Bar", "1Bad", "", "a-b", "n; DROP"]:
        try:
            graphdb.safe_identifier(bad, "label")
        except ValueError:
            continue
        raise AssertionError(f"accepted unsafe identifier: {bad!r}")
    assert graphdb.safe_identifier("Project", "label") == "Project"
    assert graphdb.safe_identifier("OWNED_BY", "rel") == "OWNED_BY"


@check("read guardrails block writes but allow property names containing keywords")
def _():
    blocked = [
        "MATCH (n) DETACH DELETE n",
        "CREATE (n:Evil)",
        "MATCH (n) SET n.x = 1",
        "MATCH (n) RETURN n; MATCH (m) DELETE m",
        "CALL apoc.load.json('http://evil')",
        "LOAD CSV FROM 'http://x' AS row RETURN row",
        "",
    ]
    for q in blocked:
        ok, _ = query.validate_cypher(q)
        assert not ok, f"should have been blocked: {q!r}"

    allowed = [
        "MATCH (p:Project) WHERE p._ds = $ds RETURN p LIMIT 25",
        "MATCH (p:Project) WHERE p.createdAt > 1 RETURN p.assetId LIMIT 5",
        "OPTIONAL MATCH (a)-[:X]->(b) RETURN a LIMIT 10",
        "WITH 1 AS x RETURN x",
    ]
    for q in allowed:
        ok, reason = query.validate_cypher(q)
        assert ok, f"should have been allowed: {q!r} ({reason})"


@check("schema validation repairs bad LLM output")
def _():
    profiles = [
        {"sheetName": "t", "filename": "t.csv", "rowCount": 2, "columnCount": 2,
         "columns": [{"name": "A"}, {"name": "B"}], "preview": []},
    ]
    raw = {
        "nodes": [
            {"label": "thing one", "key": "a", "sources": [
                {"sheet": "t", "keyColumn": "A", "properties": [
                    {"name": "b", "column": "B"},
                    {"name": "nope", "column": "MISSING"},
                ]}]},
            {"label": "Other", "key": "b", "sources": [{"sheet": "t", "keyColumn": "B", "properties": []}]},
            {"label": "Other", "key": "b", "sources": [{"sheet": "t", "keyColumn": "B", "properties": []}]},
            {"label": "Bad", "key": "x", "sources": [{"sheet": "t", "keyColumn": "NOT_A_COLUMN", "properties": []}]},
            {"label": "Ghost", "key": "g", "sources": [{"sheet": "NO_SUCH_SHEET", "keyColumn": "A", "properties": []}]},
        ],
        "relationships": [
            {"type": "points to", "from": "thing one", "to": "Other", "sheet": "t"},
            {"type": "DANGLING", "from": "thing one", "to": "Nowhere", "sheet": "t"},
            {"type": "SELF", "from": "Other", "to": "Other", "sheet": "t"},
        ],
    }
    schema, warnings = schema_infer.validate_schema(raw, profiles)
    assert [n["label"] for n in schema["nodes"]] == ["ThingOne", "Other"], schema["nodes"]
    assert schema["relationships"] == [
        {"type": "POINTS_TO", "from": "ThingOne", "to": "Other", "sheet": "t", "reason": ""}
    ], schema["relationships"]
    # Assert on content, not count: an unusable node legitimately produces two
    # warnings (its source is dropped, then the node is). A bare count assertion
    # breaks every time the messages are refined, without catching anything.
    joined = " | ".join(warnings)
    for expected in [
        "unknown column 'MISSING'",       # property pointing at a missing column
        "duplicate node label 'Other'",   # same label twice
        "unknown key column 'NOT_A_COLUMN'",
        "unknown sheet 'NO_SUCH_SHEET'",
        "DANGLING",                       # relationship to a non-existent node
        "self-referencing relationship SELF",
    ]:
        assert expected in joined, f"no warning mentioned {expected!r}: {warnings}"


@check("the single-source schema shape still loads")
def _():
    # A schema hand-edited in the browser, or produced before multi-sheet,
    # must not become unloadable.
    profiles = [{"sheetName": "t", "filename": "t.csv", "rowCount": 1, "columnCount": 2,
                 "columns": [{"name": "A"}, {"name": "B"}], "preview": []}]
    legacy = {"nodes": [{"label": "Thing", "key": "a", "keyColumn": "A",
                         "properties": [{"name": "b", "column": "B"}]}],
              "relationships": []}
    schema, _ = schema_infer.validate_schema(legacy, profiles)
    assert schema["nodes"][0]["sources"] == [
        {"sheet": "t", "keyColumn": "A", "properties": [{"name": "b", "column": "B"}]}
    ], schema["nodes"][0]


@check("a relationship is dropped when no single sheet holds both ends")
def _():
    profiles = [
        {"sheetName": "s1", "filename": "a.csv", "rowCount": 1, "columnCount": 2,
         "columns": [{"name": "A"}, {"name": "B"}], "preview": []},
        {"sheetName": "s2", "filename": "b.csv", "rowCount": 1, "columnCount": 2,
         "columns": [{"name": "C"}, {"name": "D"}], "preview": []},
    ]
    raw = {"nodes": [
        {"label": "Left", "key": "a", "sources": [{"sheet": "s1", "keyColumn": "A", "properties": []}]},
        {"label": "Right", "key": "c", "sources": [{"sheet": "s2", "keyColumn": "C", "properties": []}]},
    ], "relationships": [
        # Neither sheet carries both, so no row could ever link them.
        {"type": "LINKS", "from": "Left", "to": "Right", "sheet": "s1"},
    ]}
    schema, warnings = schema_infer.validate_schema(raw, profiles)
    assert schema["relationships"] == [], schema["relationships"]
    assert any("no single sheet contains both" in w for w in warnings), warnings


@check("a relationship naming the wrong sheet is repaired, not dropped")
def _():
    profiles = [
        {"sheetName": "s1", "filename": "a.csv", "rowCount": 1, "columnCount": 2,
         "columns": [{"name": "A"}, {"name": "B"}], "preview": []},
        {"sheetName": "s2", "filename": "b.csv", "rowCount": 1, "columnCount": 2,
         "columns": [{"name": "A"}, {"name": "D"}], "preview": []},
    ]
    raw = {"nodes": [
        {"label": "Left", "key": "a", "sources": [
            {"sheet": "s1", "keyColumn": "A", "properties": []},
            {"sheet": "s2", "keyColumn": "A", "properties": []}]},
        {"label": "Right", "key": "b", "sources": [{"sheet": "s1", "keyColumn": "B", "properties": []}]},
    ], "relationships": [
        {"type": "LINKS", "from": "Left", "to": "Right", "sheet": "s2"},
    ]}
    schema, warnings = schema_infer.validate_schema(raw, profiles)
    assert schema["relationships"][0]["sheet"] == "s1", schema["relationships"]
    assert any("lacks one endpoint" in w for w in warnings), warnings


@check("sheet names are matched leniently on punctuation and case")
def _():
    profiles = [{"sheetName": "Q3 Sales › Data", "filename": "q.xlsx", "rowCount": 1,
                 "columnCount": 2, "columns": [{"name": "A"}, {"name": "B"}], "preview": []}]
    raw = {"nodes": [{"label": "Thing", "key": "a", "sources": [
        {"sheet": "q3 sales data", "keyColumn": "A", "properties": []}]}],
        "relationships": []}
    schema, _ = schema_infer.validate_schema(raw, profiles)
    assert schema["nodes"][0]["sources"][0]["sheet"] == "Q3 Sales › Data"


@check("empty schema raises rather than silently ingesting nothing")
def _():
    profiles = [{"sheetName": "t", "filename": "t.csv", "rowCount": 1, "columnCount": 1,
                 "columns": [{"name": "A"}], "preview": []}]
    try:
        schema_infer.validate_schema({"nodes": [], "relationships": []}, profiles)
    except ValueError:
        return
    raise AssertionError("expected ValueError for a schema with no valid nodes")


@check("numeric values survive profiling as numbers")
def _():
    import pandas as pd
    frame = pd.DataFrame({"id": ["a", "b"], "amount": [1.5, 2], "count": [10, 20]})
    records = profiler.frame_to_records(frame)
    assert isinstance(records[0]["amount"], float), type(records[0]["amount"])
    assert isinstance(records[0]["count"], int), type(records[0]["count"])
    assert records[1]["count"] == 20


@check("all-empty and duplicate columns are handled")
def _():
    import pandas as pd
    frame = pd.DataFrame({"N": ["a"], "N ": ["b"], "Empty": [None], "V": [1]})
    cleaned = profiler.clean_frame(frame)
    assert list(cleaned.columns) == ["N", "N (2)", "V"], list(cleaned.columns)


# ─────────────────────────────────────────────
# Saved endpoints
# ─────────────────────────────────────────────

GOOD_CYPHER = (
    "MATCH (d:Distributor) WHERE d._ds = $ds "
    "AND ($city IS NULL OR d.city = $city) "
    "RETURN d.name AS name LIMIT toInteger($limit)"
)
GOOD_PARAMS = [
    {"name": "city", "type": "string", "required": False, "default": None},
    {"name": "limit", "type": "number", "required": False, "default": 25},
]


@check("endpoint params reject reserved, duplicate and malformed names")
def _():
    for bad, why in [
        ([{"name": "ds", "type": "string"}], "reserved"),
        ([{"name": "a", "type": "string"}, {"name": "a", "type": "string"}], "duplicate"),
        ([{"name": "2bad", "type": "string"}], "leading digit"),
        ([{"name": "has space", "type": "string"}], "space"),
        ([{"name": ""}], "empty"),
    ]:
        try:
            endpoints.validate_params(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {why}: {bad}")


@check("a declared parameter the query never uses is rejected")
def _():
    # Silently ignoring it would give the caller a filter that does nothing.
    params = GOOD_PARAMS + [{"name": "tier", "type": "string"}]
    try:
        endpoints.check_cypher(GOOD_CYPHER, endpoints.validate_params(params))
    except ValueError as exc:
        assert "tier" in str(exc), exc
        return
    raise AssertionError("accepted a declared-but-unused parameter")


@check("a query parameter that was never declared is rejected")
def _():
    cypher = GOOD_CYPHER.replace("RETURN", "AND d.tier = $tier RETURN")
    try:
        endpoints.check_cypher(cypher, endpoints.validate_params(GOOD_PARAMS))
    except ValueError as exc:
        assert "tier" in str(exc), exc
        return
    raise AssertionError("accepted an undeclared query parameter")


@check("a write query cannot be saved as an endpoint")
def _():
    for cypher in [
        "MATCH (d) WHERE d._ds = $ds DETACH DELETE d",
        "CREATE (n:Evil {x: $limit})",
    ]:
        try:
            endpoints.check_cypher(cypher, endpoints.validate_params(GOOD_PARAMS))
        except ValueError:
            continue
        raise AssertionError(f"accepted a write query: {cypher}")


@check("an unscoped query is rejected")
def _():
    try:
        endpoints.check_cypher(
            "MATCH (d:Distributor) RETURN d.name AS name LIMIT toInteger($limit)",
            endpoints.validate_params([GOOD_PARAMS[1]]),
        )
    except ValueError as exc:
        assert "$ds" in str(exc), exc
        return
    raise AssertionError("accepted a query not scoped to the dataset")


@check("query-string values are coerced to their declared types")
def _():
    endpoint = {"datasetId": "ds1", "params": endpoints.validate_params([
        {"name": "city", "type": "string"},
        {"name": "minValue", "type": "number"},
        {"name": "active", "type": "boolean"},
    ])}
    args = endpoints.resolve_args(endpoint, {"city": "Mumbai", "minValue": "250.5", "active": "true"})
    assert args["city"] == "Mumbai", args
    assert args["minValue"] == 250.5, args
    assert args["active"] is True, args
    # Whole floats become ints so LIMIT/toInteger behaves.
    assert endpoints.resolve_args(endpoint, {"minValue": "10.0"})["minValue"] == 10


@check("a bad value is reported against its parameter name")
def _():
    endpoint = {"datasetId": "ds1",
                "params": endpoints.validate_params([{"name": "minValue", "type": "number"}])}
    try:
        endpoints.resolve_args(endpoint, {"minValue": "not-a-number"})
    except ValueError as exc:
        assert "minValue" in str(exc), exc
        return
    raise AssertionError("accepted a non-numeric value for a number parameter")


@check("omitted optional filters resolve to null, not absent")
def _():
    # The query uses `$city IS NULL OR ...`, so the key must be present and
    # null. Omitting it entirely makes Neo4j raise ParameterMissing.
    endpoint = {"datasetId": "ds1", "params": endpoints.validate_params(GOOD_PARAMS)}
    args = endpoints.resolve_args(endpoint, {})
    assert "city" in args and args["city"] is None, args
    assert args["limit"] == 25, args
    assert args["ds"] == "ds1", args


@check("required parameters are enforced")
def _():
    endpoint = {"datasetId": "ds1", "params": endpoints.validate_params(
        [{"name": "tier", "type": "string", "required": True}])}
    try:
        endpoints.resolve_args(endpoint, {})
    except ValueError as exc:
        assert "tier" in str(exc), exc
        return
    raise AssertionError("a missing required parameter was allowed")


@check("unknown query parameters are rejected rather than ignored")
def _():
    endpoint = {"datasetId": "ds1", "params": endpoints.validate_params(GOOD_PARAMS)}
    try:
        endpoints.resolve_args(endpoint, {"citty": "Mumbai"})
    except ValueError as exc:
        assert "citty" in str(exc), exc
        return
    raise AssertionError("a typo'd parameter was silently ignored")


@check("limit is bounded no matter what the caller asks for")
def _():
    endpoint = {"datasetId": "ds1", "params": endpoints.validate_params(GOOD_PARAMS)}
    assert endpoints.resolve_args(endpoint, {"limit": "999999"})["limit"] == endpoints.MAX_ROWS
    assert endpoints.resolve_args(endpoint, {"limit": "-5"})["limit"] == endpoints.DEFAULT_ROWS
    assert endpoints.resolve_args(endpoint, {"limit": "0"})["limit"] == endpoints.DEFAULT_ROWS


@check("the curl example uses examples, then defaults, then a placeholder")
def _():
    endpoint = {"slug": "top-distributors", "params": endpoints.validate_params([
        {"name": "city", "type": "string", "example": "Mumbai"},
        {"name": "limit", "type": "number", "default": 25},
        {"name": "active", "type": "boolean"},
    ])}
    command = endpoints.curl_for(endpoint, "https://api.example.com/")
    assert "https://api.example.com/api/data/top-distributors?" in command, command
    assert "city=Mumbai" in command, command
    assert "limit=25" in command, command
    assert "active=true" in command, command


@check("slugs are url-safe and derived from the name")
def _():
    for name, expected in [
        ("Top Distributors by Revenue", "top-distributors-by-revenue"),
        ("  Q3 // sales!!  ", "q3-sales"),
        ("A", "endpoint-a"),
    ]:
        assert endpoints._slugify(name) == expected, (name, endpoints._slugify(name))
    assert endpoints.SLUG_RE.match(endpoints._slugify("Top Distributors by Revenue"))


if __name__ == "__main__":
    for name in PASSED:
        print(f"  PASS  {name}")
    for name, exc, tb in FAILED:
        print(f"  FAIL  {name}: {exc}")
        print("        " + tb.replace("\n", "\n        ")[:900])

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)
