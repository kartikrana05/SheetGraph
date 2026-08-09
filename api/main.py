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
import re
import time
import traceback
import uuid
from collections import OrderedDict
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Load api/.env for local development, before anything reads os.getenv.
# override=False means a real environment variable always wins, so this is
# inert on Zerops, where the platform injects the variables directly.
load_dotenv(Path(__file__).parent / ".env", override=False)

import endpoints  # noqa: E402
import graphdb  # noqa: E402  — imported after the environment is populated
import profiler  # noqa: E402
import query  # noqa: E402
import schema_infer  # noqa: E402

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_FILES = 10
# The prompt carries every sheet's column profile, so this bounds context size
# as much as it bounds the upload.
MAX_SHEETS = 8
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


class DraftEndpointRequest(BaseModel):
    datasetId: str
    prompt: str


class SaveEndpointRequest(BaseModel):
    datasetId: str
    name: str
    description: str = ""
    cypher: str
    params: list[dict] = []


# ─────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────


@app.get("/api/health")
def health():
    """Liveness for the Zerops health check. Never raises — a 200 with a
    degraded body is more useful than a 500 during the slow Docker VM boot."""
    try:
        with graphdb.open_session() as session:
            session.run("RETURN 1").consume()
        neo4j_state = "connected"
    except Exception as exc:
        neo4j_state = f"unavailable: {exc}"

    return {
        "status": "ok",
        "neo4j": neo4j_state,
        "llmConfigured": bool(os.getenv("GROQ_API_KEY")),
    }


@app.get("/api/llm-check")
def llm_check():
    """
    Prove the language model actually works.

    /api/health only reports whether GROQ_API_KEY is present, which says
    nothing about whether calls succeed — a retired model name passes that
    check and fails every real request.
    """
    import llm

    result: dict = {
        "configuredModel": llm.MODEL,
        "keyPresent": bool(os.getenv("GROQ_API_KEY")),
    }

    try:
        models = llm.available_models()
        result["availableModels"] = models[:20]
        result["configuredModelAvailable"] = llm.MODEL in models
    except Exception as exc:
        result["availableModels"] = []
        result["modelListError"] = f"{type(exc).__name__}: {exc}"

    result["resolvedModel"] = llm.resolve_model()

    started = time.time()
    try:
        reply = llm.complete("Reply with the single word: ok", "ping",
                             temperature=0, max_tokens=5)
        result["testCall"] = "ok"
        result["reply"] = reply[:80]
    except Exception as exc:
        result["testCall"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["tookMs"] = round((time.time() - started) * 1000)

    return result


# ─────────────────────────────────────────────
# Step 1 — upload and profile
# ─────────────────────────────────────────────


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    """
    Accept one or more spreadsheets.

    Every tab of every workbook becomes its own table, because a multi-tab
    workbook is the most common way related tables already travel together —
    and those tabs are exactly what we want to join in the graph.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files received.")
    if len(files) > MAX_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Please upload at most {MAX_FILES} files at once.",
        )

    profiles: list[dict] = []
    sheets: dict[str, list[dict]] = {}
    rejected: list[dict] = []
    total_bytes = 0

    for upload_file in files:
        filename = upload_file.filename or "upload.csv"
        raw = await upload_file.read()

        if not raw:
            rejected.append({"file": filename, "reason": "the file is empty"})
            continue

        total_bytes += len(raw)
        if total_bytes > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Total upload exceeds {MAX_UPLOAD_BYTES // 1024 // 1024}MB.",
            )

        if not filename.lower().endswith((".csv", ".tsv", ".txt", ".xlsx", ".xlsm")):
            rejected.append({"file": filename, "reason": "unsupported file type"})
            continue

        try:
            tables = profiler.read_tables(raw, filename)
        except Exception as exc:
            rejected.append({"file": filename, "reason": f"could not be read ({exc})"})
            continue

        if not tables:
            rejected.append({
                "file": filename,
                "reason": "no table with at least 2 columns and 1 row was found",
            })
            continue

        for sheet_name, frame in tables:
            # Sheet names are the identity the schema references, so they must
            # be unique across the whole upload even if two files share a name.
            unique = sheet_name
            suffix = 2
            while unique in sheets:
                unique = f"{sheet_name} ({suffix})"
                suffix += 1

            profiles.append(
                profiler.profile_dataframe(frame, filename, sheet_name=unique)
            )
            sheets[unique] = profiler.frame_to_records(frame)

    if not profiles:
        detail = "None of those files could be read."
        if rejected:
            detail += " " + "; ".join(f"{r['file']}: {r['reason']}" for r in rejected)
        raise HTTPException(status_code=400, detail=detail)

    if len(profiles) > MAX_SHEETS:
        rejected.extend(
            {"file": p["sheetName"], "reason": f"beyond the {MAX_SHEETS}-table limit"}
            for p in profiles[MAX_SHEETS:]
        )
        for extra in profiles[MAX_SHEETS:]:
            sheets.pop(extra["sheetName"], None)
        profiles = profiles[:MAX_SHEETS]

    upload_id = uuid.uuid4().hex[:12]
    _pending[upload_id] = {"profiles": profiles, "sheets": sheets, "at": time.time()}
    _evict_stale()

    return {"uploadId": upload_id, "profiles": profiles, "rejected": rejected}


# ─────────────────────────────────────────────
# Step 2 — propose and refine the schema
# ─────────────────────────────────────────────


@app.post("/api/schema/propose")
def propose(req: ProposeRequest):
    entry = _get_pending(req.uploadId)
    try:
        schema, warnings = schema_infer.propose_schema(entry["profiles"], req.hint)
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(
            status_code=502,
            detail=f"Schema inference failed ({type(exc).__name__}): {exc}",
        )
    return {"schema": schema, "warnings": warnings}


@app.post("/api/schema/refine")
def refine(req: RefineRequest):
    entry = _get_pending(req.uploadId)
    try:
        schema, warnings = schema_infer.refine_schema(
            entry["profiles"], req.schema_, req.instruction
        )
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(
            status_code=502,
            detail=f"Could not apply that change ({type(exc).__name__}): {exc}",
        )
    return {"schema": schema, "warnings": warnings}


# ─────────────────────────────────────────────
# Step 3 — seed the graph
# ─────────────────────────────────────────────


@app.post("/api/schema/apply")
def apply(req: ApplyRequest):
    entry = _get_pending(req.uploadId)
    profiles = entry["profiles"]

    # Re-validate before touching the database: the schema came back from the
    # browser and may have been edited there.
    try:
        schema, warnings = schema_infer.validate_schema(req.schema_, profiles)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    dataset_id = uuid.uuid4().hex[:12]

    try:
        counts = graphdb.ingest(schema, entry["sheets"], dataset_id)
        graphdb.save_dataset(
            dataset_id, schema["datasetName"], schema, profiles, counts
        )
    except Exception as exc:
        # The traceback goes to the server log; the client gets the message,
        # which now names the node or relationship that failed.
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"Ingest failed. {exc}")

    _pending.pop(req.uploadId, None)

    return {
        "datasetId": dataset_id,
        "name": schema["datasetName"],
        "counts": counts,
        "schema": schema,
        "warnings": warnings + counts.get("warnings", []),
        "joins": schema_infer.join_report(schema),
    }


# ─────────────────────────────────────────────
# Datasets
# ─────────────────────────────────────────────


@app.get("/api/datasets")
def datasets():
    try:
        return {"datasets": graphdb.list_datasets()}
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}")


@app.get("/api/datasets/{dataset_id}")
def dataset(dataset_id: str):
    found = graphdb.get_dataset(dataset_id)
    if not found:
        raise HTTPException(status_code=404, detail="Dataset not found")

    # Same shape /api/schema/apply returns, so the explore view can open a
    # previously seeded dataset without a separate code path.
    return {
        "datasetId": found["id"],
        "name": found.get("name"),
        "schema": found["schema"],
        "sheets": found.get("sheets", []),
        "counts": {
            "nodes": found.get("nodeCount", 0),
            "relationships": found.get("relCount", 0),
            "joins": found.get("joins", []),
        },
        "createdAt": found.get("createdAt"),
    }


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


# ─────────────────────────────────────────────
# Saved endpoints — a prompt becomes a REST API
# ─────────────────────────────────────────────


def _base_url(request: Request) -> str:
    """
    Public base for the curl example.

    Behind Zerops the app sits behind a TLS-terminating balancer, so the scheme
    the app sees is http while callers must use https. Trust the forwarded
    header when present, otherwise the request's own scheme.
    """
    forwarded = request.headers.get("x-forwarded-proto")
    base = str(request.base_url).rstrip("/")
    if forwarded:
        base = re.sub(r"^https?", forwarded.split(",")[0].strip(), base)
    return base


@app.post("/api/endpoints/draft")
def draft_endpoint(req: DraftEndpointRequest, request: Request):
    """Turn a description into a parameterised query, without saving it."""
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="Describe what the endpoint should return.")
    try:
        drafted = endpoints.draft(req.datasetId, req.prompt.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"Could not draft that endpoint. {exc}")

    # Run it once with defaults so the user sees real rows before committing.
    preview, preview_error = [], None
    try:
        args = endpoints.resolve_args({**drafted, "datasetId": req.datasetId}, {})
        with graphdb.open_session() as session:
            preview = session.run(drafted["cypher"], **args).data()[:5]
    except Exception as exc:
        preview_error = str(exc)

    return {**drafted, "preview": preview, "previewError": preview_error}


@app.post("/api/endpoints")
def create_endpoint(req: SaveEndpointRequest, request: Request):
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Give the endpoint a name.")
    try:
        saved = endpoints.save(
            req.datasetId, req.name.strip(), req.description.strip(),
            req.cypher.strip(), req.params,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"Could not save that endpoint. {exc}")

    base = _base_url(request)
    return {
        **saved,
        "url": f"{base}/api/data/{saved['slug']}",
        "curl": endpoints.curl_for(saved, base),
    }


@app.get("/api/datasets/{dataset_id}/endpoints")
def list_endpoints(dataset_id: str, request: Request):
    base = _base_url(request)
    items = endpoints.list_for_dataset(dataset_id)
    for item in items:
        item["url"] = f"{base}/api/data/{item['slug']}"
        item["curl"] = endpoints.curl_for({**item, "datasetId": dataset_id}, base)
    return {"endpoints": items}


@app.delete("/api/endpoints/{slug}")
def remove_endpoint(slug: str):
    if not endpoints.delete(slug):
        raise HTTPException(status_code=404, detail=f"No endpoint named '{slug}'")
    return {"deleted": slug}


@app.get("/api/data/{slug}")
def run_endpoint(slug: str, request: Request):
    """
    The public data endpoint.

    Every query-string value is bound as a Cypher parameter — none is ever
    concatenated into the query — and the stored Cypher is re-validated as
    read-only on each call.
    """
    supplied = dict(request.query_params)
    try:
        return endpoints.run(slug, supplied)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"Query failed. {exc}")
