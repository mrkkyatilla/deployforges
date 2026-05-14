#!/usr/bin/env bash
# DeployForge VPS smoke: register (optional) -> create project -> poll -> print Dockerfile.
# Uses curl WITHOUT -f so 429 bodies are visible. Safe with set -u (DF_TEST_API_KEY defaults to "").
#
# Run from the deployforges repo root (always up to date):
#   cd /path/to/deployforges && bash scripts/vps-smoke.sh
# If you keep a copy in another folder (e.g. out_of_stack), re-copy after git pull:
#   cp /path/to/deployforges/scripts/vps-smoke.sh ./
#
# Usage:
#   export BASE=https://deploy.wrupup.com
#   export DF_TEST_API_KEY=df_live_...   # optional: skip register
#   export REPO=https://github.com/pallets/flask.git
#   # optional: longer poll (default ~1h wall): export MAX_POLLS=100 POLL_INTERVAL=40
#   bash scripts/vps-smoke.sh
#
set -euo pipefail

BASE="${BASE:-https://deploy.wrupup.com}"
REPO="${REPO:-https://github.com/pallets/flask.git}"
# Wall-clock budget for GET /projects/{id} polling. Pipeline may run several docker builds
# (DF_MAX_BUILD_ATTEMPTS × DF_BUILD_TIMEOUT_SECONDS) plus Gemini — defaults are ~1h.
POLL_INTERVAL="${POLL_INTERVAL:-45}"
MAX_POLLS="${MAX_POLLS:-80}"
DF_TEST_API_KEY="${DF_TEST_API_KEY:-}"
export DF_TEST_API_KEY

die() { echo "ERROR: $*" >&2; exit 1; }

# GET/POST JSON API. Writes body to temp file; prints HTTP code on last line of captured output.
# Returns body via stdout (without code line) — caller uses temp file. Simpler: use global __HTTP_CODE.
__HTTP_CODE=""
__BODY_FILE=""

http_request() {
  local method="$1" url="$2" data="${3:-}"
  local tmp
  tmp="$(mktemp)"
  if [[ "$method" == "GET" ]]; then
    __HTTP_CODE="$(curl -sS -o "$tmp" -w "%{http_code}" "$url" -H "X-API-Key: ${DF_TEST_API_KEY}")" || true
  else
    __HTTP_CODE="$(curl -sS -o "$tmp" -w "%{http_code}" -X "$method" "$url" \
      -H "X-API-Key: ${DF_TEST_API_KEY}" \
      -H "Content-Type: application/json" \
      -d "$data")" || true
  fi
  __BODY_FILE="$tmp"
}

read_body() {
  cat "$__BODY_FILE"
}

cleanup_body() {
  rm -f "$__BODY_FILE"
  __BODY_FILE=""
}

if [[ -z "$DF_TEST_API_KEY" ]]; then
  echo ">>> Kayıt (1 istek) — sonra: export DF_TEST_API_KEY='...' ile tekrar çalıştırın"
  http_request POST "${BASE}/api/v1/auth/register" "{\"email\":\"smoke-$(date +%s)@example.com\"}"
  if [[ "$__HTTP_CODE" == "429" ]]; then
    read_body >&2
    cleanup_body
    die "Rate limit (429) — bekle veya DF_RATE_LIMIT_FREE artır / yeni saat penceresi"
  fi
  if [[ "$__HTTP_CODE" != "2"* ]]; then
    read_body >&2
    cleanup_body
    die "register HTTP $__HTTP_CODE"
  fi
  DF_TEST_API_KEY="$(read_body | python3 -c "import sys,json; print(json.load(sys.stdin)['api_key'])")"
  export DF_TEST_API_KEY
  cleanup_body
  echo ">>> API key (başlangıç): ${DF_TEST_API_KEY:0:32}..."
else
  echo ">>> Mevcut DF_TEST_API_KEY kullanılıyor"
fi

echo ">>> Proje oluştur: $REPO"
http_request POST "${BASE}/api/v1/projects/" "{\"source\":{\"type\":\"git\",\"url\":\"${REPO}\"}}"
if [[ "$__HTTP_CODE" == "429" ]]; then
  read_body >&2
  cleanup_body
  die "Rate limit (429)"
fi
if [[ "$__HTTP_CODE" != "2"* ]]; then
  read_body >&2
  cleanup_body
  die "create project HTTP $__HTTP_CODE"
fi
PID="$(read_body | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")"
cleanup_body
echo ">>> PID=$PID"

ST="unknown"
for i in $(seq 1 "$MAX_POLLS"); do
  http_request GET "${BASE}/api/v1/projects/${PID}"
  if [[ "$__HTTP_CODE" == "429" ]]; then
    read_body >&2
    cleanup_body
    die "Rate limit (429) during poll"
  fi
  if [[ "$__HTTP_CODE" != "2"* ]]; then
    read_body >&2
    cleanup_body
    die "poll HTTP $__HTTP_CODE"
  fi
  ST="$(read_body | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")"
  cleanup_body
  echo "[${i}/${MAX_POLLS}] status=$ST"
  if [[ "$ST" == "success" || "$ST" == "failed" ]]; then
    break
  fi
  sleep "$POLL_INTERVAL"
done

if [[ "$ST" != "success" && "$ST" != "failed" ]]; then
  approx_wait=$(( (MAX_POLLS - 1) * POLL_INTERVAL ))
  die "Zaman aşımı — son status=$ST (~${approx_wait}s beklendi). MAX_POLLS veya POLL_INTERVAL artırın; sunucuda DF_CELERY_PIPELINE_TASK_TIME_LIMIT_SECONDS ve worker loglarına bakın"
fi

if [[ "$ST" == "failed" ]]; then
  echo ">>> Hata özeti (GET /projects/{id} → error_summary)"
  http_request GET "${BASE}/api/v1/projects/${PID}"
  if [[ "$__HTTP_CODE" == "2"* ]]; then
    read_body | python3 -c "import sys,json; j=json.load(sys.stdin); es=j.get('error_summary'); print(es if es else '(error_summary boş — worker kodu güncel mi? Sunucuda: docker compose -f deploy/docker-compose.vps.yml build --no-cache celery-worker && ... up -d)')"
    cleanup_body
  else
    cleanup_body
  fi
fi

echo ">>> Sonuç"
http_request GET "${BASE}/api/v1/projects/${PID}/result"
if [[ "$__HTTP_CODE" != "2"* ]]; then
  read_body >&2
  cleanup_body
  die "result HTTP $__HTTP_CODE"
fi
read_body | python3 -c "
import sys, json
r = json.load(sys.stdin)
print('status:', r.get('status'))
es = r.get('error_summary')
if es:
    print('error_summary:', es)
res = r.get('result') or {}
df = res.get('dockerfile')
if df:
    print()
    print('========== Dockerfile ==========')
    print(df)
else:
    print('Dockerfile yok.')
    print(json.dumps(r, indent=2)[:8000])
"
cleanup_body
echo ">>> Bitti"

if [[ "$ST" == "failed" ]]; then
  echo ""
  echo ">>> failed — teşhis (sunucuda):"
  echo "    docker compose -f deploy/docker-compose.vps.yml --env-file .env logs --tail=150 celery-worker"
  echo "    docker compose -f deploy/docker-compose.vps.yml --env-file .env exec -T db psql -U postgres -d deployforge -c \"SELECT error_summary FROM projects WHERE id='${PID}';\""
fi
