# Deploying SheetGraph to Zerops

Three services in one project: a static React frontend, a FastAPI backend, and
Neo4j running in a Zerops Docker service. Start to finish this takes about
fifteen minutes, most of it waiting for the Neo4j VM to boot.

---

## Before you start

- A Zerops account and an access token —
  [app.zerops.io](https://app.zerops.io) → **Settings → Token Management** →
  *Generate a new access token*
- A Groq API key — free at [console.groq.com](https://console.groq.com)
- This repository pushed to GitHub, **public**

> **The repository must be public.** Zerops clones it anonymously during the
> import. A private repo fails at clone time with the build ending `FAILED` and
> **no build log**, which is a genuinely unpleasant thing to debug. Check
> GitHub → *Settings → General → Change visibility* before you begin.

---

## 1. Push the code

```bash
cd sheetgraph
git add -A
git commit -m "Deploy"
git branch -M main
git push -u origin main
```

If git complains that a lock file exists, clear the stale locks first:

```bash
find .git -name '*.lock' -delete
find .git/objects -name 'tmp_obj_*' -delete
```

---

## 2. Install the CLI and log in

```bash
curl -L https://zerops.io/zcli/install.sh | sh
zcli login <your-access-token>
```

---

## 3. Create the project

```bash
zcli project project-import zerops-project-import.yaml
```

This provisions **and builds** all three services in one command. Each carries
`buildFromGit`, so Zerops clones the repository and runs the build itself —
there is no separate `zcli push` step for the first deploy.

What you get:

| Service | Type | What it runs |
|---|---|---|
| `graph` | `docker@26.1.5` | `neo4j:5.26.0`, ports 7474 and 7687 |
| `api` | `python@3.12` | `uvicorn main:app` on port 8000 |
| `web` | `static` | the built React SPA on nginx |

`NEO4J_PASSWORD` is generated during the import by the YAML preprocessor, so it
exists nowhere in this repository.

---

## 4. Set the environment variables

Zerops GUI → **your project → Environment variables** → paste the contents of
`zerops.env`.

Project-level variables are inherited by every service, in both build and
runtime, so this single paste configures all three at once.

> **Do not paste a value for `NEO4J_PASSWORD`.** The import already generated
> one and the `graph` service is using it. Overwriting it breaks the database
> that just started.

Leave `API_URL` empty for now — you cannot know it until `api` has a subdomain.

---

## 5. Wait for the database

`graph` is a Docker service, which on Zerops means a **full VM**: kernel boot,
then the Neo4j image pull, then Neo4j's own start-up. Budget five minutes.

```bash
zcli service log graph --follow
```

While it comes up, `api` is already running and reports the database as
unavailable. That is expected, not a failure — `/api/health` deliberately
returns a degraded `200` rather than crash-looping, so the service stays up
while its dependency starts.

Check it:

```bash
curl https://<api-subdomain>/api/health
```

You want:

```json
{"status":"ok","neo4j":"connected","llmConfigured":true}
```

---

## 6. Point the frontend at the API

1. Zerops GUI → **api → Subdomain & domain & IP access** → copy the URL
   (looks like `https://api-1a2b-8000.prg1.zerops.app`)
2. **Project → Environment variables** → set `API_URL` to it, no trailing slash
3. Rebuild `web`:

```bash
zcli scope project <project-id>
zcli service push web
```

`VITE_API_URL` is baked into the bundle at build time, which is why `web` is
deployed last and must be rebuilt after `API_URL` changes. Skip this and the
frontend loads but tells you exactly what is missing, rather than failing with
silent 404s.

Open the `web` subdomain. Upload `sample/fmcg_operations.xlsx`.

---

## If the Neo4j Docker service will not stabilise

Running a database in a Zerops Docker service is off the documented path —
there is no official recipe for it, and Zerops' own ELK recipe deliberately
uses a native service for the stateful component. If `graph` will not go
healthy after about ten minutes, do not sink the evening into it.

Paste `zerops-aura.env` instead — it swaps the four `NEO4J_*` variables for a
managed [Neo4j Aura](https://console.neo4j.io) instance — then redeploy `api`:

```bash
zcli service push api
```

No code changes. The application reads all four names from the environment, and
accepts `NEO4J_USER` or `NEO4J_USERNAME` because Aura's connection snippet uses
the latter.

---

## Troubleshooting

**Build fails immediately with no log** — the repository is private, or the
`buildFromGit` URL has a trailing `.git`. Both fail identically and silently.

**`api` reports `neo4j: unavailable`** — check the URI matches the target:
`bolt://graph:7687` for the in-project service (plain `bolt://`; there is no
TLS on the private network, and `https://` between services fails), or
`neo4j+s://` for Aura, where TLS is required.

**Aura connects but every query returns nothing** — `NEO4J_DATABASE` is unset.
Aura issues an instance-specific database name; leaving it blank queries the
server default instead, which succeeds and finds nothing.

**Frontend loads but every request 404s** — `API_URL` was set after `web` was
built. Rebuild `web`.

**`llmConfigured: false`** — `GROQ_API_KEY` is missing from the project
variables.

**Seeding fails with a constraint error** — it should not; a rejected
constraint degrades to a warning and the load continues. If it does, the error
names the node label and offending row.

---

## What this uses from Zerops

- **Three service types** — `static`, `python@3.12`, `docker@26.1.5`
- **Docker service for Neo4j**, since Zerops has no managed graph database:
  `--network=host` with ports declared in `zerops.yaml`, a pinned image tag
  because `prepareCommands` output is cached, and the data volume outside
  `/var/www` so a deploy does not replace it
- **Private networking** — `api` reaches Neo4j at `bolt://graph:7687` by
  hostname; Bolt is never exposed publicly
- **Project-level variables**, inherited by every service, which is what makes
  the database target swappable without touching code or `zerops.yaml`
- **YAML preprocessor** — `<@generateRandomString(<24>)>` for the database
  password, so no credential lives in the repository
- **`buildFromGit`** — provisioning and first deploy in a single command
- **Readiness and health checks** — readiness gates the deploy on
  `/api/health`; the health check monitors continuously, with a patient
  240-second failure timeout on `graph` because VM boot is slow
- **Build caching** — `vendor/` for pip, `web/node_modules` for npm
- **Vendored dependencies** — `pip install --target=./vendor`, so the runtime
  container needs no network access at start-up
- **`.deployignore`** — keeps local `.env` files and virtualenvs out of the
  container
