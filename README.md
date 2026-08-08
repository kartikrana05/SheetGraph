# SheetGraph

**Turn any spreadsheet into a knowledge graph you can ask questions of.**

Upload a reporting sheet. An LLM reads the *statistical shape* of your columns and
proposes a property-graph schema — which columns become entities, which stay as
properties, and how they relate. You refine that schema in plain English, seed it into
Neo4j, then explore the graph visually and query it in natural language.

Built for [The Zerops Challenge](https://www.wemakedevs.org/hackathons/zerops),
8–9 August 2026.

---

## The idea

Most spreadsheet-to-database tools make you define the schema yourself. Most
text-to-SQL tools only work against a schema someone hardcoded in advance.

SheetGraph does neither. **The schema is inferred at upload time and stored alongside
the data**, then injected into the query prompt at question time. That's what lets the
same deployment answer questions about a project tracker, a sales pipeline and a
support-ticket export without a line of code changing between them.

```
   sheet.xlsx
       │
       ▼
  ┌─────────────┐   column profiling — type, cardinality, fill rate, samples
  │  profiler   │   (a few KB, regardless of how big the sheet is)
  └──────┬──────┘
         ▼
  ┌─────────────┐   LLM proposes labels, keys, properties, relationships
  │ schema_infer│   → validated and repaired against the real column list
  └──────┬──────┘
         ▼
   ◀── user refines in natural language ──▶
         ▼
  ┌─────────────┐   idempotent MERGE, batched, scoped by dataset id
  │   ingest    │
  └──────┬──────┘
         ▼
  ┌─────────────┐   NL → Cypher → results → NL, with the schema injected
  │    query    │   from the dataset, not hardcoded
  └─────────────┘
```

---

## Architecture on Zerops

Three services in one Zerops project, talking over the private network:

```
                    ┌────────────────────────────┐
   browser ────────▶│  web   ·  static (nginx)   │   React 18 + Vite + Cytoscape.js
                    │        SPA fallback on     │   subdomain: enabled
                    └─────────────┬──────────────┘
                                  │ HTTPS
                    ┌─────────────▼──────────────┐
                    │  api   ·  python@3.12      │   FastAPI + uvicorn
                    │        port 8000, http     │   subdomain: enabled
                    └──────┬──────────────┬──────┘
            bolt://graph:7687             │ HTTPS
                    ┌──────▼──────┐  ┌────▼─────────┐
                    │ graph ·     │  │ Groq         │
                    │ docker@26.1 │  │ Llama 3.3 70B│
                    │ neo4j:5.26  │  └──────────────┘
                    └─────────────┘
```

### Why the database is a Docker service

Zerops has no managed graph database — the native catalogue is PostgreSQL, MariaDB,
Valkey, Elasticsearch, Qdrant, ClickHouse and friends. Neo4j therefore runs in a
**Zerops Docker service**, which is a full VM rather than a container. Three
consequences shaped the config in `zerops.yaml`:

| Constraint | What the code does about it |
|---|---|
| `--network=host` is mandatory; `-p` port mapping is not supported | Ports are declared in `run.ports`, and `docker run` uses `--network=host` |
| `prepareCommands` output is cached, so `:latest` would never refresh | The image tag is pinned to `neo4j:5.26.0` |
| A deploy replaces `/var/www`, and Zerops does not contractually guarantee VM disk durability | The Neo4j volume lives at `/var/lib/neo4j/data`, **outside** `/var/www`. Independently, **ingest is fully idempotent** — every write is a `MERGE` on `(key, datasetId)` — so re-seeding after a wipe is safe and cheap rather than catastrophic |
| VM boot is slow (full kernel start) | `healthCheck.failureTimeout` is 240s, and the API returns a *degraded* 200 from `/api/health` rather than crash-looping while the database comes up |

Shared Storage was deliberately **not** used for the data volume — the Zerops docs
explicitly warn it corrupts databases, because file locks are per-mount only.

### Other platform features used

- **Private networking** — the API reaches Neo4j at `bolt://graph:7687` by hostname.
  Bolt is never exposed publicly.
- **Project-level environment variables** — `NEO4J_PASSWORD` is generated at import
  time by the YAML preprocessor (`<@generateRandomString(<24>)>`) and referenced by
  both the `graph` and `api` services, so the password exists nowhere in this repo.
- **`envSecrets`** — `GROQ_API_KEY` is a blurred secret, set in the GUI.
- **Readiness vs health checks** — readiness gates the deploy on `/api/health`;
  the health check monitors it continuously afterwards.
- **Build cache** — `vendor/` for pip and `web/node_modules` for npm.
- **Vendored Python dependencies** — `pip install --target=./vendor` so the runtime
  container needs no network access at start-up.

---

## Saved endpoints — a prompt becomes a REST API

Any question you can ask, you can also freeze into a parameterised HTTP endpoint.

Describe it in plain English ("top distributors by revenue, filterable by city and
tier") and the model writes read-only Cypher with **named parameters**, declaring
each filter's name, type, whether it is required and its default. You see a live
preview of real rows before saving. Saving stores it under a slug and hands back a
ready-to-run curl:

```bash
curl 'https://your-api.zerops.app/api/data/top-distributors-by-revenue?city=Mumbai&tier=Gold&limit=10'
```

```json
{
  "endpoint": "top-distributors-by-revenue",
  "params": {"city": "Mumbai", "tier": "Gold", "minRevenue": null, "limit": 10},
  "count": 2,
  "tookMs": 34,
  "data": [
    {"distributor": "Sharma Distributors", "city": "Mumbai", "tier": "Gold", "revenue": 1284300},
    {"distributor": "Balaji Traders", "city": "Mumbai", "tier": "Gold", "revenue": 1109870}
  ]
}
```

What makes it a real endpoint rather than a saved result:

- **Optional filters widen rather than break.** An omitted parameter resolves to
  `null`, not to absent, so the `($city IS NULL OR d.city = $city)` idiom in the
  query does the right thing. Omitting the key entirely makes Neo4j raise
  `ParameterMissing`.
- **Declared and used parameters must agree.** A parameter declared but never used
  in the query is rejected, because it would present the caller with a filter that
  silently does nothing. The reverse is rejected too.
- **Values are always Cypher parameters**, never concatenated, and coerced to their
  declared type — `?minRevenue=abc` returns `Parameter 'minRevenue': expected a
  number, got 'abc'` rather than a 500.
- **Unknown parameters are an error**, not ignored: `?citty=Mumbai` tells you so,
  instead of quietly returning unfiltered data.
- **`limit` is capped** at 1000 regardless of what the caller asks for, and the
  stored Cypher is re-validated as read-only on **every** call — stored queries are
  data, and data reaching a database deserves the same scrutiny twice.

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/endpoints/draft` | Prompt → Cypher + params + preview, without saving |
| POST | `/api/endpoints` | Save it, get back the URL and curl |
| GET | `/api/datasets/{id}/endpoints` | List saved endpoints for a dataset |
| DELETE | `/api/endpoints/{slug}` | Remove one |
| GET | `/api/data/{slug}` | **The public data endpoint** |

---

## Deploy it yourself

**Full runbook: [DEPLOY.md](DEPLOY.md)** — including troubleshooting and the
Aura fallback. The short version:

### 1. Provision the project

```bash
curl -L https://zerops.io/zcli/install.sh | sh
zcli login <your-access-token>          # app.zerops.io → Settings → Token Management
zcli project project-import zerops-project-import.yaml
```

This creates all three services. `NEO4J_PASSWORD` is generated automatically.

### 2. Set the Groq API key

In the Zerops GUI: **api → Environment variables → `GROQ_API_KEY`**.
Free tier at [console.groq.com](https://console.groq.com).

### 3. Push the code

```bash
zcli push --serviceId <graph-service-id>   # starts Neo4j (slow first boot — a VM)
zcli push --serviceId <api-service-id>
```

### 4. Wire the frontend to the API

The `api` service now has a public subdomain (**api → Subdomain & domain & IP access**).
Copy it, then set the **project-level** environment variable `API_URL` to that URL and
push the frontend:

```bash
zcli push --serviceId <web-service-id>
```

`VITE_API_URL` is baked in at build time, which is why `web` is pushed last. If you
skip this step the frontend loads and tells you exactly what's missing rather than
failing with silent 404s.

---

## Local development

```bash
docker compose -f docker-compose.dev.yml up -d      # Neo4j on 7474 / 7687

cd api
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                                # add your GROQ_API_KEY
uvicorn main:app --reload --port 8000

cd ../web
npm install && npm run dev                          # proxies /api → :8000
```

Run the offline test suite (no database required):

```bash
cd api && python3 test_ingest.py
```

---

## Guardrails

The LLM writes Cypher that runs against a live database, so there are four layers
between the model and the data:

1. **Prompt level** — the model is told the query is read-only and must return
   `{"cypher": null, ...}` for anything off-topic.
2. **Identifier allow-listing** — node labels and relationship types cannot be
   parameterised in Cypher, so they are string-concatenated. Every one is matched
   against `^[A-Za-z][A-Za-z0-9_]*$` before it touches a query. This is the layer that
   stops a creative model from escaping a backtick.
3. **Statement validation** — a word-boundary blocklist (`create`, `delete`, `set`,
   `merge`, `drop`, `remove`, `detach`, `load csv`, `apoc`, …), an allowed-start check,
   and a multi-statement check. Word boundaries matter: a property called `createdAt`
   must not trip the `create` filter, and it doesn't.
4. **Values are always parameters** — user data never enters a query string.

Plus: results are capped at 100 rows to the client and 12 to the model, uploads at
15 MB, and every query is scoped to a single dataset id.

For production you would add a fifth layer — a read-only Neo4j user — which is a
database configuration concern rather than an application one.

---

## Project structure

```
sheetgraph/
├── zerops.yaml                   # build + run for all three services
├── zerops-project-import.yaml    # provisions the project
├── docker-compose.dev.yml        # local Neo4j only
├── api/
│   ├── main.py                   # FastAPI routes
│   ├── profiler.py               # sheet → column profile
│   ├── schema_infer.py           # profile → graph schema, + validation/repair
│   ├── graphdb.py                # driver, idempotent ingest, read queries
│   ├── query.py                  # NL → Cypher → NL, guardrails
│   ├── llm.py                    # Groq wrapper, JSON extraction
│   └── test_ingest.py            # 13 offline checks
└── web/
    └── src/
        ├── App.jsx               # three-step wizard
        ├── UploadStep.jsx        # drop zone + column profile
        ├── SchemaStep.jsx        # schema preview + NL refinement
        ├── ExploreView.jsx       # graph + chat + API tab
        ├── ApiPanel.jsx          # build and manage saved endpoints
        └── GraphCanvas.jsx       # Cytoscape rendering
```

## Tech stack

| Layer | Choice |
|---|---|
| Platform | Zerops — static, python@3.12 and docker@26.1 services |
| Graph database | Neo4j 5.26 |
| API | FastAPI, Python 3.12, pandas, openpyxl |
| LLM | Groq — Llama 3.3 70B Versatile |
| Frontend | React 18, Vite 6, Cytoscape.js (cose-bilkent layout) |

## AI usage disclosure

Claude was used as a pair-programming assistant during this build, in line with the
hackathon's disclosed-AI policy. The architecture, the schema-inference approach, the
validation-and-repair strategy and the Zerops service topology are the author's design
decisions, and every file here is understood and explicable by the author.
