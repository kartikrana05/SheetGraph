#!/usr/bin/env bash
# Start SheetGraph locally: FastAPI on :8000, Vite on :5173.
#
#   ./run-local.sh          use api/.env as-is
#   ./run-local.sh --docker start a local Neo4j container first
#
# Ctrl-C stops both processes.

set -euo pipefail
cd "$(dirname "$0")"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'
info() { printf "%s==>%s %s\n" "$GREEN" "$OFF" "$1"; }
warn() { printf "%s!! %s%s\n" "$YELLOW" "$1" "$OFF"; }
die()  { printf "%s!! %s%s\n" "$RED" "$1" "$OFF"; exit 1; }

# ── Python ──────────────────────────────────────────────────
# pandas 2.2.3 has no wheel for 3.13+, so prefer 3.12 when it exists.
PY=""
for candidate in python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
[ -n "$PY" ] || die "No python3 found. Install Python 3.12."

PY_VER="$($PY -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
info "Using $PY (Python $PY_VER)"
case "$PY_VER" in
  3.10|3.11|3.12) ;;
  *) warn "Python $PY_VER has no pandas 2.2.3 wheel. If pip fails, install 3.12:
       brew install python@3.12  &&  ./run-local.sh" ;;
esac

# ── Config ──────────────────────────────────────────────────
[ -f api/.env ] || die "api/.env is missing. Copy api/.env.example and fill it in."

# shellcheck disable=SC1091
set -a; source api/.env; set +a

[ -n "${GROQ_API_KEY:-}" ] || die "GROQ_API_KEY is not set in api/.env"
case "${GROQ_API_KEY}" in gsk_xxx*) die "GROQ_API_KEY is still the placeholder." ;; esac
info "Neo4j target: ${NEO4J_URI:-unset}"

# ── Optional local Neo4j ────────────────────────────────────
if [ "${1:-}" = "--docker" ]; then
  command -v docker >/dev/null 2>&1 || die "Docker not found. Start Docker Desktop, or drop --docker to use Aura."
  info "Starting Neo4j (first run pulls ~600MB)…"
  docker compose -f docker-compose.dev.yml up -d
  printf "    waiting for Neo4j"
  for _ in $(seq 1 60); do
    if curl -sf http://localhost:7474 >/dev/null 2>&1; then printf " ready\n"; break; fi
    printf "."; sleep 2
  done
fi

# ── Backend ─────────────────────────────────────────────────
if [ ! -d api/venv ]; then
  info "Creating virtualenv…"
  "$PY" -m venv api/venv
fi

# --no-cache-dir matters more than it looks: pip otherwise writes every wheel
# to ~/Library/Caches/pip as well as installing it, so a ~250MB dependency set
# briefly needs ~500MB. On a full disk that is the difference between working
# and an Errno 28 halfway through.
info "Installing Python dependencies…"
if ! ./api/venv/bin/pip install -q --no-cache-dir --upgrade pip \
  || ! ./api/venv/bin/pip install -q --no-cache-dir -r api/requirements.txt; then
  printf "\n"
  warn "Dependency install failed."
  warn "If that was 'No space left on device', reclaim some and re-run:"
  cat <<'HINT'

    rm -rf api/venv                                   # clear the half-built venv
    pip cache purge 2>/dev/null || rm -rf ~/Library/Caches/pip
    rm -rf ~/Documents/backend-graphdb/backend/venv   # ~242MB, recreatable
    rm -rf ~/Documents/backend-graphdb/frontend/node_modules  # ~51MB

    df -h /                                           # check what you got back

HINT
  exit 1
fi

info "Running the offline test suite…"
(cd api && ../api/venv/bin/python test_ingest.py) || die "Tests failed — stopping before we start anything."

info "Starting API on :8000"
(cd api && ../api/venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload) &
API_PID=$!

cleanup() {
  printf "\n"
  info "Shutting down…"
  kill "$API_PID" 2>/dev/null || true
  [ -n "${WEB_PID:-}" ] && kill "$WEB_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

# ── Health gate ─────────────────────────────────────────────
printf "    waiting for the API"
for _ in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8000/api/health >/dev/null 2>&1; then printf " up\n"; break; fi
  printf "."; sleep 1
done

HEALTH="$(curl -s http://127.0.0.1:8000/api/health || echo '{}')"
printf "%s    %s%s\n" "$DIM" "$HEALTH" "$OFF"
case "$HEALTH" in
  *'"neo4j":"connected"'*) info "Neo4j reachable" ;;
  *) warn "Neo4j is NOT reachable — upload will work, seeding will not. Check NEO4J_URI in api/.env." ;;
esac

# ── Frontend ────────────────────────────────────────────────
if [ ! -d web/node_modules ]; then
  info "Installing npm dependencies…"
  (cd web && npm install --silent)
fi

info "Starting web on :5173"
(cd web && npm run dev) &
WEB_PID=$!

printf "\n%s==>%s Open %shttp://localhost:5173%s   (Ctrl-C to stop)\n\n" "$GREEN" "$OFF" "$GREEN" "$OFF"
wait
