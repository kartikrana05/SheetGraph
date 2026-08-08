"""
Neo4j access layer: connection, schema-driven ingest, and read queries.

Every node this app creates carries a `_ds` property holding the dataset id,
so multiple uploaded sheets can coexist in one database without colliding and
can be dropped independently.
"""

from __future__ import annotations

import os
import re
import time

from neo4j import GraphDatabase, Driver

BATCH_SIZE = 500
IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

_driver: Driver | None = None


def driver() -> Driver:
    global _driver
    if _driver is None:
        uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        user = os.getenv("NEO4J_USER", "neo4j")
        password = os.getenv("NEO4J_PASSWORD", "password")
        _driver = GraphDatabase.driver(
            uri,
            auth=(user, password),
            max_connection_lifetime=300,
            connection_acquisition_timeout=30,
        )
    return _driver


def wait_for_neo4j(timeout: int = 120) -> bool:
    """
    Poll until Neo4j answers.

    Zerops Docker services run in full VMs and boot slowly, so the API can
    easily come up before the database does. Rather than crash-looping we
    wait, and report honestly if it never arrives.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with driver().session() as session:
                session.run("RETURN 1").consume()
            return True
        except Exception:
            time.sleep(3)
    return False


def safe_identifier(value: str, kind: str) -> str:
    """
    Guard label / relationship-type interpolation.

    Cypher cannot parameterise labels or relationship types, so these values
    are concatenated into the query string. They originate from LLM output,
    which means this check is the only thing standing between a creative
    model and a Cypher injection. Values are already normalised upstream;
    this is the belt to that suspenders.
    """
    if not IDENT_RE.match(value or ""):
        raise ValueError(f"Unsafe {kind}: {value!r}")
    return value


# ─────────────────────────────────────────────
# Ingest
# ─────────────────────────────────────────────


def ensure_constraints(schema: dict) -> None:
    """One uniqueness constraint per node label, scoped by dataset."""
    with driver().session() as session:
        for node in schema["nodes"]:
            label = safe_identifier(node["label"], "label")
            key = safe_identifier(node["key"], "property")
            session.run(
                f"CREATE CONSTRAINT IF NOT EXISTS "
                f"FOR (n:`{label}`) REQUIRE (n.`{key}`, n._ds) IS UNIQUE"
            ).consume()


def _node_rows(records: list[dict], node: dict, dataset_id: str) -> list[dict]:
    """Project raw sheet rows onto one node label, deduplicated by key."""
    key_column = node["keyColumn"]
    seen: dict = {}

    for row in records:
        key_value = row.get(key_column)
        if key_value is None or key_value == "":
            continue
        key_value = str(key_value)

        props = {}
        for prop in node["properties"]:
            value = row.get(prop["column"])
            if value is not None and value != "":
                props[prop["name"]] = value

        # Later rows enrich earlier ones rather than replacing them, so a
        # sparse row cannot blank out properties another row supplied.
        if key_value in seen:
            seen[key_value]["props"].update(props)
        else:
            seen[key_value] = {"key": key_value, "props": props, "_ds": dataset_id}

    return list(seen.values())


def _rel_rows(
    records: list[dict], rel: dict, nodes_by_label: dict, dataset_id: str
) -> list[dict]:
    """Every sheet row that has both endpoints becomes one relationship."""
    source = nodes_by_label[rel["from"]]
    target = nodes_by_label[rel["to"]]

    pairs = set()
    for row in records:
        from_value = row.get(source["keyColumn"])
        to_value = row.get(target["keyColumn"])
        if from_value in (None, "") or to_value in (None, ""):
            continue
        pairs.add((str(from_value), str(to_value)))

    return [{"from": f, "to": t, "_ds": dataset_id} for f, t in pairs]


def ingest(schema: dict, records: list[dict], dataset_id: str) -> dict:
    """
    Load the sheet into the graph according to the schema.

    Idempotent: MERGE on (key, dataset) means re-running is safe, which is
    the mitigation for the Zerops Docker volume not being contractually
    durable across deploys.
    """
    ensure_constraints(schema)
    nodes_by_label = {n["label"]: n for n in schema["nodes"]}

    counts = {"nodes": 0, "relationships": 0, "byLabel": {}, "byType": {}}

    with driver().session() as session:
        for node in schema["nodes"]:
            label = safe_identifier(node["label"], "label")
            key = safe_identifier(node["key"], "property")
            rows = _node_rows(records, node, dataset_id)

            for start in range(0, len(rows), BATCH_SIZE):
                batch = rows[start : start + BATCH_SIZE]
                session.run(
                    f"UNWIND $batch AS row "
                    f"MERGE (n:`{label}` {{`{key}`: row.key, _ds: row._ds}}) "
                    f"SET n += row.props",
                    batch=batch,
                ).consume()

            counts["byLabel"][label] = len(rows)
            counts["nodes"] += len(rows)

        for rel in schema["relationships"]:
            rel_type = safe_identifier(rel["type"], "relationship type")
            source = nodes_by_label[rel["from"]]
            target = nodes_by_label[rel["to"]]
            from_label = safe_identifier(source["label"], "label")
            to_label = safe_identifier(target["label"], "label")
            from_key = safe_identifier(source["key"], "property")
            to_key = safe_identifier(target["key"], "property")

            rows = _rel_rows(records, rel, nodes_by_label, dataset_id)

            for start in range(0, len(rows), BATCH_SIZE):
                batch = rows[start : start + BATCH_SIZE]
                session.run(
                    f"UNWIND $batch AS row "
                    f"MATCH (a:`{from_label}` {{`{from_key}`: row.from, _ds: row._ds}}) "
                    f"MATCH (b:`{to_label}` {{`{to_key}`: row.to, _ds: row._ds}}) "
                    f"MERGE (a)-[r:`{rel_type}`]->(b)",
                    batch=batch,
                ).consume()

            label = f"{rel['from']}-[:{rel_type}]->{rel['to']}"
            counts["byType"][label] = len(rows)
            counts["relationships"] += len(rows)

    return counts


# ─────────────────────────────────────────────
# Dataset registry
# ─────────────────────────────────────────────


def save_dataset(dataset_id: str, name: str, schema: dict, profile: dict, counts: dict) -> None:
    """
    Persist the schema alongside the data.

    This is what makes the chat work on arbitrary sheets: at query time we
    read the schema back out and inject it into the prompt, instead of
    hardcoding a domain like a single-purpose app would.
    """
    import json

    with driver().session() as session:
        session.run(
            """
            MERGE (d:_Dataset {id: $id})
            SET d.name = $name,
                d.schema = $schema,
                d.columns = $columns,
                d.rowCount = $rowCount,
                d.nodeCount = $nodeCount,
                d.relCount = $relCount,
                d.filename = $filename,
                d.createdAt = coalesce(d.createdAt, datetime())
            """,
            id=dataset_id,
            name=name,
            schema=json.dumps(schema),
            columns=json.dumps([c["name"] for c in profile["columns"]]),
            rowCount=profile["rowCount"],
            nodeCount=counts["nodes"],
            relCount=counts["relationships"],
            filename=profile["filename"],
        ).consume()


def get_dataset(dataset_id: str) -> dict | None:
    import json

    with driver().session() as session:
        record = session.run(
            "MATCH (d:_Dataset {id: $id}) RETURN d", id=dataset_id
        ).single()

    if not record:
        return None

    data = dict(record["d"])
    data["schema"] = json.loads(data["schema"])
    data["columns"] = json.loads(data.get("columns", "[]"))
    if "createdAt" in data:
        data["createdAt"] = str(data["createdAt"])
    return data


def list_datasets() -> list[dict]:
    with driver().session() as session:
        rows = session.run(
            """
            MATCH (d:_Dataset)
            RETURN d.id AS id, d.name AS name, d.filename AS filename,
                   d.rowCount AS rowCount, d.nodeCount AS nodeCount,
                   d.relCount AS relCount, toString(d.createdAt) AS createdAt
            ORDER BY d.createdAt DESC
            """
        ).data()
    return rows


def delete_dataset(dataset_id: str) -> int:
    with driver().session() as session:
        result = session.run(
            "MATCH (n) WHERE n._ds = $id DETACH DELETE n RETURN count(n) AS deleted",
            id=dataset_id,
        ).single()
        session.run("MATCH (d:_Dataset {id: $id}) DETACH DELETE d", id=dataset_id).consume()
    return result["deleted"] if result else 0


# ─────────────────────────────────────────────
# Read queries for the visualiser
# ─────────────────────────────────────────────


def overview(dataset_id: str, node_limit: int = 150, edge_limit: int = 300) -> dict:
    """
    A sampled subgraph for the initial render.

    Returning every node would melt the browser on a large sheet, so we take
    a bounded sample and let the user expand outward from there.
    """
    with driver().session() as session:
        # Sample evenly across relationship types rather than taking the first
        # N edges the planner happens to return. Without this, one high-volume
        # relationship crowds every other one out of the opening view.
        rel_types = session.run(
            """
            MATCH (a)-[r]->(b) WHERE a._ds = $id AND b._ds = $id
            RETURN type(r) AS t, count(r) AS c ORDER BY c DESC
            """,
            id=dataset_id,
        ).data()

        per_type = max(10, edge_limit // max(1, len(rel_types)))

        edges = session.run(
            """
            MATCH (a)-[r]->(b)
            WHERE a._ds = $id AND b._ds = $id
            WITH type(r) AS relType, a, b
            WITH relType, collect({a: a, b: b})[..$perType] AS sample
            UNWIND sample AS s
            RETURN elementId(s.a) AS sourceId, labels(s.a)[0] AS sourceLabel,
                   properties(s.a) AS sourceProps,
                   elementId(s.b) AS targetId, labels(s.b)[0] AS targetLabel,
                   properties(s.b) AS targetProps,
                   relType
            """,
            id=dataset_id,
            perType=per_type,
        ).data()

        # Nodes with no relationships at all would otherwise be invisible —
        # worth surfacing, because they usually mean a column the schema
        # failed to connect.
        isolated = session.run(
            """
            MATCH (n)
            WHERE n._ds = $id AND NOT (n)--()
            RETURN elementId(n) AS id, labels(n)[0] AS label, properties(n) AS props
            LIMIT $limit
            """,
            id=dataset_id,
            limit=min(node_limit, 30),
        ).data()

    nodes: dict = {}
    links = []

    for row in edges:
        nodes[row["sourceId"]] = {"id": row["sourceId"], "label": row["sourceLabel"], "props": row["sourceProps"]}
        nodes[row["targetId"]] = {"id": row["targetId"], "label": row["targetLabel"], "props": row["targetProps"]}
        links.append({"source": row["sourceId"], "target": row["targetId"], "type": row["relType"]})

    for row in isolated:
        nodes[row["id"]] = {"id": row["id"], "label": row["label"], "props": row["props"]}

    return {"nodes": list(nodes.values()), "edges": links}


def expand(element_id: str, limit: int = 40) -> dict:
    with driver().session() as session:
        rows = session.run(
            """
            MATCH (n)-[r]-(m)
            WHERE elementId(n) = $id
            RETURN elementId(m) AS id, labels(m)[0] AS label, properties(m) AS props,
                   type(r) AS relType, startNode(r) = n AS outgoing
            LIMIT $limit
            """,
            id=element_id,
            limit=limit,
        ).data()

    nodes = []
    edges = []
    for row in rows:
        nodes.append({"id": row["id"], "label": row["label"], "props": row["props"]})
        if row["outgoing"]:
            edges.append({"source": element_id, "target": row["id"], "type": row["relType"]})
        else:
            edges.append({"source": row["id"], "target": element_id, "type": row["relType"]})

    return {"nodes": nodes, "edges": edges}


def stats(dataset_id: str) -> dict:
    with driver().session() as session:
        labels = session.run(
            """
            MATCH (n) WHERE n._ds = $id
            RETURN labels(n)[0] AS label, count(n) AS count
            ORDER BY count DESC
            """,
            id=dataset_id,
        ).data()
        rels = session.run(
            """
            MATCH (a)-[r]->(b) WHERE a._ds = $id AND b._ds = $id
            RETURN type(r) AS type, count(r) AS count
            ORDER BY count DESC
            """,
            id=dataset_id,
        ).data()
    return {"labels": labels, "relationships": rels}
