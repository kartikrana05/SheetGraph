#!/usr/bin/env bash
# Deploy SheetGraph to Zerops, from nothing to running.
#
#   ./deploy.sh
#
# Handles the whole chain: commit and push to GitHub, create the Zerops project
# if it does not exist, then build every service. Safe to re-run — it detects
# what is already done and skips it, so a failure halfway is not a reset.

set -uo pipefail
cd "$(dirname "$0")"

PROJECT_NAME="sheetgraph"

GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; BLUE=$'\033[34m'; DIM=$'\033[2m'; OFF=$'\033[0m'
step() { printf "\n%s==>%s %s\n" "$GREEN" "$OFF" "$1"; }
info() { printf "    %s\n" "$1"; }
warn() { printf "%s !! %s%s\n" "$YELLOW" "$1" "$OFF"; }
die()  { printf "\n%s !! %s%s\n\n" "$RED" "$1" "$OFF"; exit 1; }

# ── 1. Preflight ────────────────────────────────────────────
step "Checking prerequisites"

command -v zcli >/dev/null 2>&1 || die "zcli not found:
    curl -L https://zerops.io/zcli/install.sh | sh"
zcli project list >/dev/null 2>&1 || die "Not logged in:
    zcli login <your-access-token>"
[ -f zerops.yaml ] || die "Run this from the repo root (zerops.yaml not found)."

# api/.env inside the container would shadow the platform's own variables,
# because main.py loads it at import. Loud failure now beats silent later.
if [ -f api/.env ] && ! grep -q '^/api/.env' .deployignore 2>/dev/null; then
  die "api/.env is not excluded by .deployignore. It would override the
    platform's environment variables inside the container."
fi
info "zcli ready, local secrets excluded"

# ── 2. Git ──────────────────────────────────────────────────
step "Publishing the source to GitHub"

find .git -name '*.lock' -delete 2>/dev/null
find .git/objects -name 'tmp_obj_*' -delete 2>/dev/null

git remote get-url origin >/dev/null 2>&1 || die "No 'origin' remote. Add one:
    git remote add origin https://github.com/<you>/<repo>.git"

BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || echo main)"
if [ "$BRANCH" != "main" ]; then
  info "renaming branch '$BRANCH' to 'main'"
  git branch -M main || die "Could not rename the branch to main"
fi

if [ -n "$(git status --porcelain)" ]; then
  info "committing local changes"
  git add -A
  git commit -q -m "Deploy $(git rev-parse --short HEAD 2>/dev/null || echo initial)" \
    || die "Commit failed"
else
  info "working tree already clean"
fi

info "pushing to $(git remote get-url origin)"
git push -u origin main || die "Push failed. If this is an auth prompt, use a
    personal access token rather than your GitHub password."

# Zerops clones anonymously during the import; a private repo fails at clone
# time with the build marked FAILED and no log at all.
REMOTE_URL="$(git remote get-url origin | sed 's/\.git$//')"
info "source published"
warn "Make sure $REMOTE_URL is PUBLIC — Zerops clones it anonymously,
    and a private repo fails with no build log."

# ── 3. Project ──────────────────────────────────────────────
step "Finding or creating the Zerops project"

if zcli project list 2>/dev/null | grep -qi "$PROJECT_NAME"; then
  info "project '$PROJECT_NAME' already exists"
  EXISTING=1
else
  info "creating project '$PROJECT_NAME' and all three services"
  info "this also builds them, straight from GitHub"
  zcli project project-import zerops-project-import.yaml \
    || die "project-import failed. If it complained about an organisation,
    re-run with:  zcli project project-import zerops-project-import.yaml --org-id <id>"
  EXISTING=0
fi

step "Selecting the project"
# zcli remembers the last scope and silently reuses it — which is how an
# earlier run ended up pushing into an unrelated project called 'zcp' and
# reporting "Service [graph] not found". Reset first so the picker appears.
zcli scope reset >/dev/null 2>&1 || true
zcli scope project || die "Could not scope the project"

# Confirm the scope actually contains our services before pushing into it.
if ! zcli service list 2>/dev/null | grep -qiE '\b(api|graph|web)\b'; then
  warn "The scoped project does not appear to contain graph/api/web."
  warn "If you picked the wrong one, re-run and choose the '$PROJECT_NAME' project."
fi

# ── 4. Build ────────────────────────────────────────────────
if [ "$EXISTING" = "1" ]; then
  step "Deploying services from local code"
  info "graph first — it is a full VM and the slowest to start"
  zcli service push graph --setup graph || warn "graph deploy failed; see below for the Aura fallback"
  zcli service push api --setup api || die "api deploy failed:
    zcli service log api --limit 100"
else
  step "Builds are running"
  info "buildFromGit means the import already kicked them off — no push needed"
fi

# ── 5. What is left ─────────────────────────────────────────
step "Remaining steps (these need the GUI)"
cat <<'NEXT'

  1. Environment variables
       Zerops GUI → project → Environment variables
       Paste the contents of  zerops.env
       Do NOT set NEO4J_PASSWORD — the import generated one already.

  2. Wait for Neo4j (about 5 minutes — kernel boot, image pull, start-up)
       zcli service log graph --follow

  3. Check the API
       api → Subdomain & domain & IP access  → copy the URL
       curl https://<api-subdomain>/api/health

       want: {"status":"ok","neo4j":"connected","llmConfigured":true}

       neo4j "unavailable" for the first few minutes is expected, not a
       failure — the API stays up rather than crash-looping on a missing
       database.

  4. Frontend, last
       project → Environment variables → API_URL = the api subdomain
       zcli service push web --setup web

       VITE_API_URL is compiled into the bundle, which is why web goes last.

  If graph will not go healthy after ~10 minutes, stop fighting it:
       paste zerops-aura.env into the project variables
       zcli service push api --setup api

NEXT

printf "%s==>%s Done. Follow along with %szcli service log api --follow%s\n\n" \
  "$GREEN" "$OFF" "$BLUE" "$OFF"
