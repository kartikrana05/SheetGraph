#!/usr/bin/env bash
# Diagnose the deployed SheetGraph API and print a verdict.
#
#   ./diagnose.sh [upload-id]
#
# Checks configuration, CORS preflight, endpoint latency and the LLM path, then
# says which one is broken rather than leaving four raw outputs to interpret.

set -uo pipefail

API="${API_URL:-https://api-2b30-8000.prg1.zerops.app}"
WEB="${WEB_URL:-https://web-2b30.prg1.zerops.app}"
UPLOAD_ID="${1:-}"

G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; O=$'\033[0m'
step() { printf "\n%s==>%s %s\n" "$G" "$O" "$1"; }
pass() { printf "  %sPASS%s  %s\n" "$G" "$O" "$1"; }
fail() { printf "  %sFAIL%s  %s\n" "$R" "$O" "$1"; }
warn() { printf "  %sWARN%s  %s\n" "$Y" "$O" "$1"; }

VERDICT=""

# ── 1. Reachability and configuration ───────────────────────
step "1. Is the API up and configured?"
HEALTH="$(curl -s --max-time 20 "$API/api/health" 2>&1)"

if [ -z "$HEALTH" ]; then
  fail "No response from $API"
  VERDICT="The API is unreachable. Check the subdomain is enabled and the service is running."
else
  printf "  %s%s%s\n" "$D" "$HEALTH" "$O"
  case "$HEALTH" in
    *'"neo4j":"connected"'*) pass "Neo4j connected" ;;
    *) fail "Neo4j NOT connected — set the NEO4J_* project variables and restart api"
       VERDICT="Neo4j is not reachable from the API." ;;
  esac
  case "$HEALTH" in
    *'"llmConfigured":true'*) pass "GROQ_API_KEY is set" ;;
    *) fail "GROQ_API_KEY is missing — schema inference cannot work at all"
       VERDICT="GROQ_API_KEY is not set on the api service." ;;
  esac
fi

# ── 2. CORS preflight ───────────────────────────────────────
# /api/upload sends multipart/form-data, which the browser treats as a simple
# request with no preflight. /api/schema/propose sends application/json, which
# is preflighted. A broken preflight therefore breaks exactly one of them, and
# surfaces in the browser as a bare "Failed to fetch".
step "2. Does the CORS preflight succeed?"
PREFLIGHT="$(curl -s -i --max-time 20 -X OPTIONS "$API/api/schema/propose" \
  -H "origin: $WEB" \
  -H 'access-control-request-method: POST' \
  -H 'access-control-request-headers: content-type' 2>&1)"

PF_STATUS="$(printf '%s' "$PREFLIGHT" | head -1)"
printf "  %s%s%s\n" "$D" "$PF_STATUS" "$O"

if printf '%s' "$PREFLIGHT" | grep -qi 'access-control-allow-origin'; then
  pass "Preflight returns access-control-allow-origin"
  printf "  %s%s%s\n" "$D" "$(printf '%s' "$PREFLIGHT" | grep -i 'access-control-' | tr -d '\r' | sed 's/^/        /')" "$O"
else
  fail "Preflight has NO access-control-allow-origin header"
  warn "This breaks every application/json request from the browser while"
  warn "leaving multipart uploads working — exactly the split you are seeing."
  VERDICT="CORS preflight is failing. Set CORS_ORIGINS=* on the api service, or check the balancer forwards OPTIONS."
fi

# ── 3. Endpoint latency ─────────────────────────────────────
step "3. Does the endpoint answer promptly?"
ID="${UPLOAD_ID:-doesnotexist}"
START=$(date +%s)
RESP="$(curl -s --max-time 130 -o /tmp/sg_probe.txt -w '%{http_code}|%{time_total}' \
  -X POST "$API/api/schema/propose" \
  -H 'content-type: application/json' \
  -H "origin: $WEB" \
  --data-raw "{\"uploadId\":\"$ID\",\"hint\":null}" 2>&1)"
END=$(date +%s)

CODE="${RESP%%|*}"
TIME="${RESP##*|}"
BODY="$(head -c 400 /tmp/sg_probe.txt 2>/dev/null)"

printf "  %shttp=%s time=%ss%s\n" "$D" "$CODE" "$TIME" "$O"
printf "  %s%s%s\n" "$D" "$BODY" "$O"

if [ "$CODE" = "000" ]; then
  fail "Connection dropped after $((END-START))s — the request never completed"
  VERDICT="${VERDICT:-The request is being dropped, most likely a balancer timeout on a slow LLM call.}"
elif [ "$CODE" = "404" ]; then
  pass "Endpoint healthy (404 = that upload id is not in memory, which is expected here)"
  if [ -z "$UPLOAD_ID" ]; then
    warn "Re-run with a real upload id to exercise the LLM path:  ./diagnose.sh <uploadId>"
  fi
elif [ "$CODE" = "502" ]; then
  warn "502 — the API answered, but the LLM call failed. The body above says why."
  VERDICT="${VERDICT:-Schema inference reached the model and failed; see the message above.}"
elif [ "$CODE" = "200" ]; then
  pass "Schema inference succeeded in ${TIME}s"
else
  warn "Unexpected status $CODE"
fi

# ── Verdict ─────────────────────────────────────────────────
step "Verdict"
if [ -n "$VERDICT" ]; then
  printf "  %s%s%s\n\n" "$R" "$VERDICT" "$O"
else
  printf "  %sNothing broken found from outside. If the browser still fails,%s\n" "$G" "$O"
  printf "  %sthe next place to look is:  zcli service log api --limit 60%s\n\n" "$G" "$O"
fi
