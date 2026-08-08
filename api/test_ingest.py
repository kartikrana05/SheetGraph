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

import sys
import traceback
from unittest import mock

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

    def session(self):
        return RecordingSession(self.log)


SCHEMA = {
    "datasetName": "PM Tracker",
    "summary": "",
    "nodes": [
        {
            "label": "Project",
            "key": "projectId",
            "keyColumn": "Project ID",
            "properties": [
                {"name": "projectName", "column": "Project Name"},
                {"name": "budget", "column": "Budget"},
            ],
        },
        {"label": "Person", "key": "name", "keyColumn": "Owner", "properties": []},
    ],
    "relationships": [{"type": "OWNED_BY", "from": "Project", "to": "Person"}],
}

RECORDS = [
    {"Project ID": "P1", "Project Name": "Atlas", "Budget": 100.0, "Owner": "Priya"},
    {"Project ID": "P2", "Project Name": "Nova", "Budget": 200.0, "Owner": "Priya"},
    # Same project appearing twice — must dedupe to one node, and the second
    # row must enrich rather than blank out the first.
    {"Project ID": "P1", "Project Name": None, "Budget": 150.0, "Owner": "Arun"},
    # Missing key — must be skipped entirely.
    {"Project ID": None, "Project Name": "Ghost", "Budget": 5.0, "Owner": "Arun"},
]


def run_ingest():
    driver = RecordingDriver()
    with mock.patch.object(graphdb, "driver", return_value=driver):
        counts = graphdb.ingest(SCHEMA, RECORDS, "ds1")
    return driver.log, counts


@check("nodes deduplicate on key")
def _():
    log, counts = run_ingest()
    assert counts["byLabel"]["Project"] == 2, counts       # P1, P2 — not 3
    assert counts["byLabel"]["Person"] == 2, counts        # Priya, Arun
    assert counts["nodes"] == 4, counts


@check("rows with a null key are skipped")
def _():
    _, counts = run_ingest()
    assert counts["byLabel"]["Project"] == 2, "the null-key 'Ghost' row leaked in"


@check("later rows enrich rather than overwrite")
def _():
    log, _ = run_ingest()
    batch = next(p["batch"] for q, p in log if "MERGE (n:`Project`" in q)
    p1 = next(r for r in batch if r["key"] == "P1")
    # Row 3 had a null Project Name; it must not erase Atlas from row 1.
    assert p1["props"]["projectName"] == "Atlas", p1
    assert p1["props"]["budget"] == 150.0, p1


@check("node MERGE is scoped by dataset")
def _():
    log, _ = run_ingest()
    merges = [q for q, _ in log if q.startswith("UNWIND $batch AS row MERGE")]
    assert merges, "no node merge issued"
    for q in merges:
        assert "_ds: row._ds" in q, q


@check("constraints are created per label")
def _():
    log, _ = run_ingest()
    constraints = [q for q, _ in log if "CREATE CONSTRAINT" in q]
    assert len(constraints) == 2, constraints
    assert any("`Project`" in q and "`projectId`" in q for q in constraints)


@check("relationships match both endpoints within the dataset")
def _():
    log, counts = run_ingest()
    rel = next(q for q, _ in log if "MERGE (a)-[r:`OWNED_BY`]->(b)" in q)
    assert "MATCH (a:`Project` {`projectId`: row.from, _ds: row._ds})" in rel, rel
    assert "MATCH (b:`Person` {`name`: row.to, _ds: row._ds})" in rel, rel
    # P1→Priya, P2→Priya, P1→Arun. The null-key row is excluded.
    assert counts["relationships"] == 3, counts


@check("values travel as parameters, never inlined")
def _():
    log, _ = run_ingest()
    for cypher, params in log:
        if "UNWIND" in cypher:
            assert "batch" in params, cypher
            assert "Priya" not in cypher, "a data value was concatenated into Cypher"


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
    profile = {
        "filename": "t.csv",
        "rowCount": 2,
        "columnCount": 2,
        "columns": [{"name": "A"}, {"name": "B"}],
        "preview": [],
    }
    raw = {
        "nodes": [
            {"label": "thing one", "key": "a", "keyColumn": "A", "properties": [
                {"name": "b", "column": "B"},
                {"name": "nope", "column": "MISSING"},
            ]},
            {"label": "Other", "key": "b", "keyColumn": "B", "properties": []},
            {"label": "Other", "key": "b", "keyColumn": "B", "properties": []},
            {"label": "Bad", "key": "x", "keyColumn": "NOT_A_COLUMN", "properties": []},
        ],
        "relationships": [
            {"type": "points to", "from": "thing one", "to": "Other"},
            {"type": "DANGLING", "from": "thing one", "to": "Nowhere"},
            {"type": "SELF", "from": "Other", "to": "Other"},
        ],
    }
    schema, warnings = schema_infer.validate_schema(raw, profile)
    labels = [n["label"] for n in schema["nodes"]]
    assert labels == ["ThingOne", "Other"], labels
    assert schema["relationships"] == [
        {"type": "POINTS_TO", "from": "ThingOne", "to": "Other", "reason": ""}
    ], schema["relationships"]
    # Five distinct faults injected above: unknown property column, duplicate
    # label, unknown key column, dangling relationship, self-referencing
    # relationship. Each must produce exactly one warning.
    assert len(warnings) == 5, warnings


@check("empty schema raises rather than silently ingesting nothing")
def _():
    profile = {"filename": "t.csv", "rowCount": 1, "columnCount": 1,
               "columns": [{"name": "A"}], "preview": []}
    try:
        schema_infer.validate_schema({"nodes": [], "relationships": []}, profile)
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


if __name__ == "__main__":
    for name in PASSED:
        print(f"  PASS  {name}")
    for name, exc, tb in FAILED:
        print(f"  FAIL  {name}: {exc}")
        print("        " + tb.replace("\n", "\n        ")[:900])

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)
