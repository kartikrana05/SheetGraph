"""
SheetGraph API — turn any spreadsheet into a queryable knowledge graph.

Flow:
  upload  →  profile columns
          →  LLM proposes a graph schema
          →  user refines it in natural language
          →  ingest into Neo4j
          →  visualise + ask questions
"""

from __future__ import annotations

import os
import time
import uuid
from collections import OrderedDict

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import graphdb
import profiler
import query
import schema_infer

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_PENDING_UPLOADS = 20
PENDING_TTL_SECONDS = 60 * 60

app = FastAPI(
    title="SheetGraph API",
    version="1.0.0",
    description="Upload a spreadsheet, get a queryable graph.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Uploaded-but-not-yet-ingested sheets live here between the upload call and
# the apply call. Bounded and TTL'd so a busy demo cannot exhaust memory.
# Single container by design (minContainers: 1) — a multi-container deployment
# would move this to object storage.
_pending: OrderedDict[str, dict] = OrderedDict()


def _evict_stale() -> None:
    now = time.time()
    for key in [k for k, v in _pending.items() if now - v["at"] > PENDING_TTL_SECONDS]:
        _pending.pop(key, None)
    while len(_pending) > MAX_PENDING_UPLOADS:
        _pending.popitem(last=False)


def _get_pending(upload_id: str) -> dict:
    _evict_stale()
    entry = _pending.get(upload_id)
    if not entry:
        raise HTTPException(
            status_code=404,
            detail="That upload expired or was never received. Please upload the sheet again.",
        )
    return entry


# ─────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────


class ProposeRequest(BaseModel):
    uploadId: str
    hint: str | None = None


class RefineRequest(BaseModel):
    uploadId: str
    schema_: dict = Field(alias="schema")
    instruction: str

    model_config = {"populate_by_name": True}


class ApplyRequest(BaseModel):
    uploadId: str
    schema_: dict = Field(alias="schema")

    model_config = {"populate_by_name": True}


class ChatRequest(BaseModel):
    datasetId: str
    message: str
    history: list[dict] = []


class ExpandRequest(BaseModel):
    nodeId: str
    limit: int = 40


# ─────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────


@app.get("/api/health")
def health():
    """Liveness for the Zerops health check. Never raises — a 200 with a
    degraded body is more useful than a 500 during the slow Docker VM boot."""
    try:
        with graphdb.driver().session() as session:
            session.run("RETURN 1").consume()
        neo4j_state = "connected"
    except Exception as exc:
        neo4j_state = f"unavailable: {exc}"

    return {
        "status": "ok",
        "neo4j": neo4j_state,
        "llmConfigured": bool(os.getenv("GROQ_API_KEY")),
    }


# ─────────────────────────────────────────────
# Step 1 — upload and profile
# ─────────────────────────────────────────────


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), sheet: str | None = Form(default=None)):
    raw = await file.read()

    if not raw:
        raise HTTPException(status_code=400, detail="That file is empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File is {len(raw) // 1024 // 1024}MB. The limit is "
                   f"{MAX_UPLOAD_BYTES // 1024 // 1024}MB.",
        )

    filename = file.filename or "upload.csv"
    if not filename.lower().endswith((".csv", ".tsv", ".txt", ".xlsx", ".xlsm")):
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Upload a .csv, .tsv, .xlsx or .xlsm file.",
        )

    try:
        frame, sheet_names = profiler.read_sheet(raw, filename, sheet)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read that file: {exc}")

    # Clean first, then validate — a trailing empty column would otherwise
    # let a single-column sheet through the shape check below.
    frame = profiler.clean_frame(frame)

    if frame.empty:
        raise HTTPException(status_code=400, detail="That sheet has no rows.")
    if len(frame.columns) < 2:
        raise HTTPException(
            status_code=400,
            detail="A graph needs at least two columns to relate entities. "
                   "This sheet has one.",
        )

    profile = profiler.profile_dataframe(frame, filename, sheet_names)
    records = profiler.frame_to_records(frame)

    upload_id = uuid.uuid4().hex[:12]
    _pending[upload_id] = {"profile": profile, "records": records, "at": time.time()}
    _evict_stale()

    return {"uploadId": upload_id, "profile": profile}


# ─────────────────────────────────────────────
# Step 2 — propose and refine the schema
# ─────────────────────────────────────────────


@app.post("/api/schema/propose")
def propose(req: ProposeRequest):
    entry = _get_pending(req.uploadId)
    try:
        schema, warnings = schema_infer.propose_schema(entry["profile"], req.hint)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Schema inference failed: {exc}")
    return {"schema": schema, "warnings": warnings}


@app.post("/api/schema/refine")
def refine(req: RefineRequest):
    entry = _get_pending(req.uploadId)
    try:
        schema, warnings = schema_infer.refine_schema(
            entry["profile"], req.schema_, req.instruction
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not apply that change: {exc}")
    return {"schema": schema, "warnings": warnings}


# ─────────────────────────────────────────────
# Step 3 — seed the graph
# ─────────────────────────────────────────────


@app.post("/api/schema/apply")
def apply(req: ApplyRequest):
    entry = _get_pending(req.uploadId)
    profile = entry["profile"]

    # Re-validate before touching the database: the schema came back from the
    # browser and may have been edited there.
    try:
        schema, warnings = schema_infer.validate_schema(req.schema_, profile)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    dataset_id = uuid.uuid4().hex[:12]

    try:
        counts = graphdb.ingest(schema, entry["records"], dataset_id)
        graphdb.save_dataset(
            dataset_id, schema["datasetName"], schema, profile, counts
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Ingest failed: {exc}")

    _pending.pop(req.uploadId, None)

    return {
        "datasetId": dataset_id,
        "name": schema["datasetName"],
        "counts": counts,
        "schema": schema,
        "warnings": warnings,
    }


# ─────────────────────────────────────────────
# Datasets
# ─────────────────────────────────────────────


@app.get("/api/datasets")
def datasets():
    try:
        return {"datasets": graphdb.list_datasets()}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}")


@app.get("/api/datasets/{dataset_id}")
def dataset(dataset_id: str):
    found = graphdb.get_dataset(dataset_id)
    if not found:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return found


@app.delete("/api/datasets/{dataset_id}")
def drop_dataset(dataset_id: str):
    deleted = graphdb.delete_dataset(dataset_id)
    return {"deleted": deleted}


@app.get("/api/datasets/{dataset_id}/graph")
def dataset_graph(dataset_id: str, nodeLimit: int = 150, edgeLimit: int = 300):
    return graphdb.overview(dataset_id, min(nodeLimit, 500), min(edgeLimit, 800))


@app.get("/api/datasets/{dataset_id}/stats")
def dataset_stats(dataset_id: str):
    return graphdb.stats(dataset_id)


@app.get("/api/datasets/{dataset_id}/suggestions")
def dataset_suggestions(dataset_id: str):
    found = graphdb.get_dataset(dataset_id)
    if not found:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return {
        "suggestions": query.suggest_questions(
            found["schema"], found.get("name") or "this dataset"
        )
    }


@app.post("/api/expand")
def expand(req: ExpandRequest):
    return graphdb.expand(req.nodeId, min(req.limit, 100))


# ─────────────────────────────────────────────
# Step 4 — ask questions
# ─────────────────────────────────────────────


@app.post("/api/chat")
def chat(req: ChatRequest):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    return query.ask(req.datasetId, req.message.strip(), req.history)
