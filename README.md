# SheetGraph

**Turn your spreadsheets into one knowledge graph — and a live API.**

Upload as many sheets as you like. An LLM reads the *statistical shape* of every
column, works out which entities appear in more than one sheet, and proposes a single
property-graph schema that joins them. You refine it in plain English, seed it into
Neo4j, then explore it visually, ask questions that cross every sheet at once, and
freeze any question into a REST endpoint your other systems can call.

Built for [The Zerops Challenge](https://www.wemakedevs.org/hackathons/zerops),
8–9 August 2026.

---

## The problem

Organisations run on spreadsheets, and the useful data is never in one of them.

A sales export lists orders by product code and region code. The product master sits
in another tab, the region master in a third, the field team roster in a fourth. Every
individual sheet is readable. The questions worth asking are not:

> *Which Gold-tier distributors in the West zone buy the most from the Beverages
> category, and which rep covers them?*

That question touches four sheets. Answering it today means one of three things:

1. **VLOOKUP and pivot tables** — a person spends an hour, produces a number nobody
   can reproduce, and the workbook is stale the next morning.
2. **Load it into a database** — someone has to design the schema, write the DDL, map
   every column, and maintain it. Days of work, and it starts over when the columns
   change.
3. **Ask a BI team** — a ticket, a queue, and an answer next week.

And relationship questions are precisely what spreadsheets are worst at. A pivot table
can group and sum; it cannot traverse. "Which reps have never sold Personal Care" or
"which distributors order from every category" are one hop in a graph and a manual
cross-referencing exercise in Excel.

**The existing tools each solve half of it.** Spreadsheet-to-database tools make *you*
define the schema, which is the hard part. Text-to-SQL tools query beautifully — but
only against a schema someone hardcoded in advance, so they work for one dataset and
one dataset only.

Then there is the last mile. Even when you get the answer, it dies in a chat window.
There is no way to call a pivot table from a dashboard, a cron job, or another service.

---

## What SheetGraph does about it

**The schema is inferred at upload time and stored alongside the data**, then injected
into the query prompt at question time. Nothing is hardcoded, so the same deployment
answers questions about a project tracker, an FMCG sales export and a support-ticket
dump with no code change between them.

Four things follow from that:

| | |
|---|---|
| **You never design a schema** | Columns are profiled statistically — semantic type, cardinality, fill rate, sample values — and a model proposes the labels, keys, properties and relationships. Every proposal is validated and repaired against your real columns before it can run. |
| **Separate sheets become one graph** | An entity found in more than one sheet becomes a *single node fed by several sources*, matched on meaning and overlapping values rather than identical column names — so `SKU Code` in a sales export and `Product Code` in a master are the same product. |
| **Relationship questions become one hop** | The thing spreadsheets cannot do is exactly what a graph is for. Every answer shows the Cypher it ran, so the working is checkable rather than trusted. |
| **Answers become endpoints** | Any question can be frozen into a named, parameterised, read-only URL with a ready `curl`. That is the step that turns an analysis into something a dashboard or a cron job can consume. |

### Concretely

Load the `sales_orders` sample — three tabs, 130 rows, no configuration:

```
Orders          Order ID · Order Date · Customer · Product Code · Region Code · Amount
Product Master  Product Code · Product Name · Category
Region Master   Region Code · Region Name · Zone
```

SheetGraph works out on its own that `Product Code` and `Region Code` each appear in
two tabs and joins them, promotes `Customer`, `Category`, `Zone` and `Status` to nodes,
keeps the amounts and dates as properties, and reports how many keys actually matched
across each join — so a mis-matched key is visible rather than silent.

Then this works, across all three tabs at once:

```bash
curl 'https://your-api.zerops.app/api/data/top-products-by-zone?zone=Zone%201&limit=5'
```

---

## How it works

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
