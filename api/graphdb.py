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


def database() -> str | None:
    """
    Which database to open sessions against.

    Self-hosted Neo4j uses the default (`neo4j`), but Aura hands out an
    instance-specific database name. Returning None lets the driver pick the
    server's default rather than guessing wrong.
    """
    return os.getenv("NEO4J_DATABASE") or None


def open_session(**kwargs):
    """Always go through here so the database name is applied consistently."""
    name = database()
    if name:
        kwargs["database"] = name
    return driver().session(**kwargs)


def driver() -> Driver:
    global _driver
    if _driver is None:
        uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        # Aura's connection snippet uses NEO4J_USERNAME; the self-hosted
        # convention is NEO4J_USER. Accept either so the same image runs
        # against a Zerops Docker service or Aura with no code change.
        user = os.getenv("NEO4J_USER") or os.getenv("NEO4J_USERNAME") or "neo4j"
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
            with open_session() as session:
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


def ensure_constraints(schema: dict) -> list[str]:
    """
    Create one uniqueness constraint per node label, scoped by dataset.

    Constraints are an optimisation and a safety net, not a correctness
    requirement: ingest MERGEs on (key, _ds) either way. Managed Neo4j tiers
    differ in what they allow — composite uniqueness in particular is not
    available everywhere — so a rejected constraint degrades to a warning
    rather than failing the whole load. Returns the warnings for the caller
    to surface.
    """
    warnings: list[str] = []

    for node in schema["nodes"]:
        label = safe_identifier(node["label"], "label")
        key = safe_identifier(node["key"], "property")

        attempts = [
            # Preferred: unique per (key, dataset), so two datasets may share a key.
            f"CREATE CONSTRAINT IF NOT EXISTS "
            f"FOR (n:`{label}`) REQUIRE (n.`{key}`, n._ds) IS UNIQUE",
            # Fallback for tiers without composite uniqueness. Only safe while a
            # single dataset is loaded per label, hence the warning below.
            f"CREATE CONSTRAINT IF NOT EXISTS "
            f"FOR (n:`{label}`) REQUIRE n.`{key}` IS UNIQUE",
        ]

        for index, statement in enumerate(attempts):
            try:
                with open_session() as session:
                    session.run(statement).consume()
                if index > 0:
                    warnings.append(
                        f"{label}: composite uniqueness is unavailable on this Neo4j, "
                        f"so a single-property constraint on {key} was used instead."
                    )
                break
            except Exception as exc:
                if index == len(attempts) - 1:
                    warnings.append(
                        f"{label}: could not create a uniqueness constraint ({exc}). "
                        f"Loading anyway — MERGE still deduplicates."
                    )

    return warnings


def _node_rows(node: dict, sheets: dict, dataset_id: str) -> tuple[list[dict], dict | None]:
    """
    Project every source sheet of one node label into deduplicated rows.

    A node fed by several sheets is the whole point of multi-sheet upload: the
    same key seen in two sheets produces ONE row whose properties are the union
    of both. Also returns per-sheet key sets so the caller can report how many
    keys actually overlapped — a join that silently matched nothing otherwise
    looks identical to one that worked.
    """
    seen: dict = {}
    keys_per_sheet: dict = {}

    for source in node["sources"]:
        records = sheets.get(source["sheet"], [])
        key_column = source["keyColumn"]
        sheet_keys: set = set()

        for row in records:
            key_value = row.get(key_column)
            if key_value is None or key_value == "":
                continue
            key_value = str(key_value).strip()
            if not key_value:
                continue
            sheet_keys.add(key_value)

            props = {}
            for prop in source["properties"]:
                value = row.get(prop["column"])
                if value is not None and value != "":
                    props[prop["name"]] = value

            # Later rows enrich earlier ones rather than replacing them, so a
            # sparse row cannot blank out properties another row supplied — and
            # a master sheet can add detail to a key first seen in a
            # transaction sheet.
            if key_value in seen:
                seen[key_value]["props"].update(props)
            else:
                seen[key_value] = {"key": key_value, "props": props, "_ds": dataset_id}

        keys_per_sheet[source["sheet"]] = sheet_keys

    overlap = None
    if len(keys_per_sheet) > 1:
        shared = set.intersection(*keys_per_sheet.values())
        overlap = {
            "sheets": list(keys_per_sheet),
            "perSheet": {k: len(v) for k, v in keys_per_sheet.items()},
            "matched": len(shared),
            "total": len(seen),
        }

    return list(seen.values()), overlap


def _rel_rows(rel: dict, node_index: dict, sheets: dict, dataset_id: str) -> list[dict]:
    """Every row of the relationship's sheet that carries both endpoints."""
    sheet = rel["sheet"]
    records = sheets.get(sheet, [])

    from_col = node_index[rel["from"]]["bySheet"][sheet]
    to_col = node_index[rel["to"]]["bySheet"][sheet]

    pairs = set()
    for row in records:
        from_value = row.get(from_col)
        to_value = row.get(to_col)
        if from_value in (None, "") or to_value in (None, ""):
            continue
        pairs.add((str(from_value).strip(), str(to_value).strip()))

    return [{"from": f, "to": t, "_ds": dataset_id} for f, t in pairs if f and t]


def ingest(schema: dict, sheets: dict, dataset_id: str) -> dict:
    """
    Load every uploaded sheet into the graph according to the schema.

    `sheets` maps sheet name -> list of row dicts. Idempotent: MERGE on
    (key, dataset) means re-running is safe, which is the mitigation for the
    Zerops Docker volume not being contractually durable across deploys.
    """
    constraint_warnings = ensure_constraints(schema)

    # keyColumn per (label, sheet), for resolving relationship endpoints.
    node_index = {
        n["label"]: {
            "key": n["key"],
            "bySheet": {s["sheet"]: s["keyColumn"] for s in n["sources"]},
        }
        for n in schema["nodes"]
    }

    counts = {"nodes": 0, "relationships": 0, "byLabel": {}, "byType": {},
              "joins": [], "warnings": constraint_warnings}

    with open_session() as session:
        for node in schema["nodes"]:
            label = safe_identifier(node["label"], "label")
            key = safe_identifier(node["key"], "property")
            rows, overlap = _node_rows(node, sheets, dataset_id)

            for start in range(0, len(rows), BATCH_SIZE):
                batch = rows[start : start + BATCH_SIZE]
                try:
                    session.run(
                        f"UNWIND $batch AS row "
                        f"MERGE (n:`{label}` {{`{key}`: row.key, _ds: row._ds}}) "
                        f"SET n += row.props",
                        batch=batch,
                    ).consume()
                except Exception as exc:
                    # Name the node and show one offending row — "ingest failed"
                    # on its own gives you nowhere to start.
                    sample = batch[0] if batch else {}
                    raise RuntimeError(
                        f"Loading :{label} failed at row {start + 1} of {len(rows)}. "
                        f"{type(exc).__name__}: {exc}. "
                        f"First row in this batch: {sample}"
                    ) from exc

            counts["byLabel"][label] = len(rows)
            counts["nodes"] += len(rows)
            if overlap:
                counts["joins"].append({"label": label, **overlap})

        for rel in schema["relationships"]:
            rel_type = safe_identifier(rel["type"], "relationship type")
            from_label = safe_identifier(rel["from"], "label")
            to_label = safe_identifier(rel["to"], "label")
            from_key = safe_identifier(node_index[rel["from"]]["key"], "property")
            to_key = safe_identifier(node_index[rel["to"]]["key"], "property")

            rows = _rel_rows(rel, node_index, sheets, dataset_id)

            for start in range(0, len(rows), BATCH_SIZE):
                batch = rows[start : start + BATCH_SIZE]
                try:
                    session.run(
                        f"UNWIND $batch AS row "
                        f"MATCH (a:`{from_label}` {{`{from_key}`: row.from, _ds: row._ds}}) "
                        f"MATCH (b:`{to_label}` {{`{to_key}`: row.to, _ds: row._ds}}) "
                        f"MERGE (a)-[r:`{rel_type}`]->(b)",
                        batch=batch,
                    ).consume()
                except Exception as exc:
                    raise RuntimeError(
                        f"Linking (:{from_label})-[:{rel_type}]->(:{to_label}) from sheet "
                        f"'{rel['sheet']}' failed at row {start + 1} of {len(rows)}. "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc

            signature = f"{rel['from']}-[:{rel_type}]->{rel['to']}"
            counts["byType"][signature] = len(rows)
            counts["relationships"] += len(rows)

    return counts


# ─────────────────────────────────────────────
# Dataset registry
# ─────────────────────────────────────────────


def save_dataset(dataset_id: str, name: str, schema: dict, profiles: list, counts: dict) -> None:
    """
    Persist the schema alongside the data.

    This is what makes the chat work on arbitrary sheets: at query time we read
    the schema back out and inject it into the prompt, instead of hardcoding a
    domain like a single-purpose app would.
    """
    import json

    with open_session() as session:
        session.run(
            """
            MERGE (d:_Dataset {id: $id})
            SET d.name = $name,
                d.schema = $schema,
                d.sheets = $sheets,
                d.joins = $joins,
                d.rowCount = $rowCount,
                d.sheetCount = $sheetCount,
                d.nodeCount = $nodeCount,
                d.relCount = $relCount,
                d.createdAt = coalesce(d.createdAt, datetime())
            """,
            id=dataset_id,
            name=name,
            schema=json.dumps(schema),
            sheets=json.dumps([
                {"name": p["sheetName"], "rows": p["rowCount"], "columns": p["columnCount"]}
                for p in profiles
            ]),
            joins=json.dumps(counts.get("joins", [])),
            rowCount=sum(p["rowCount"] for p in profiles),
            sheetCount=len(profiles),
            nodeCount=counts["nodes"],
            relCount=counts["relationships"],
        ).consume()


def get_dataset(dataset_id: str) -> dict | None:
    import json

    with open_session() as session:
        record = session.run(
            "MATCH (d:_Dataset {id: $id}) RETURN d", id=dataset_id
        ).single()

    if not record:
        return None

    data = dict(record["d"])
    data["schema"] = json.loads(data["schema"])
    data["sheets"] = json.loads(data.get("sheets") or "[]")
    data["joins"] = json.loads(data.get("joins") or "[]")
    if "createdAt" in data:
        data["createdAt"] = str(data["createdAt"])
    return data


def list_datasets() -> list[dict]:
    import json

    with open_session() as session:
        rows = session.run(
            """
            MATCH (d:_Dataset)
            OPTIONAL MATCH (e:_Endpoint {datasetId: d.id})
            RETURN d.id AS id, d.name AS name, d.sheetCount AS sheetCount,
                   d.rowCount AS rowCount, d.nodeCount AS nodeCount,
                   d.relCount AS relCount, d.sheets AS sheets,
                   count(e) AS endpointCount,
                   toString(d.createdAt) AS createdAt
            ORDER BY d.createdAt DESC
            """
        ).data()

    for row in rows:
        row["sheets"] = json.loads(row.get("sheets") or "[]")
    return rows


def delete_dataset(dataset_id: str) -> int:
    with open_session() as session:
        result = session.run(
            "MATCH (n) WHERE n._ds = $id DETACH DELETE n RETURN count(n) AS deleted",
            id=dataset_id,
        ).single()
        # Saved endpoints carry no _ds of their own, so they survive the sweep
        # above and would keep serving against data that no longer exists.
        session.run(
            "MATCH (e:_Endpoint {datasetId: $id}) DETACH DELETE e", id=dataset_id
        ).consume()
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
    with open_session() as session:
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
    with open_session() as session:
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
    with open_session() as session:
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
