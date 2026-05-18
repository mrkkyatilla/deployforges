#!/usr/bin/env bash
# DeployForge VPS smoke — deploy.wrupup.com (veya BASE) üzerinde hızlı uçtan uca test.
#
# v2 (varsayılan): DeploymentManifest + multi-service pipeline
# v1: klasik tek Dockerfile sonucu (deprecated header'lı /result)
#
# Repo kökünden:
#   cd /path/to/deployforges && bash scripts/vps-smoke.sh
#
# Hızlı test (deploy.wrupup.com):
#   bash scripts/vps-smoke.sh
#
# Monorepo profili:
#   REPO_PROFILE=monorepo bash scripts/vps-smoke.sh
#
# Sadece v1:
#   API_VERSION=v1 bash scripts/vps-smoke.sh
#
# Ortam değişkenleri:
#   BASE              — API kökü (varsayılan https://deploy.wrupup.com)
#   API_VERSION       — v2 | v1 | both (varsayılan v2)
#   DF_TEST_API_KEY   — varsa register atlanır
#   REPO_PROFILE      — simple | monorepo (varsayılan simple)
#   REPO              — REPO_PROFILE'ı geçersiz kılar
#   SMOKE_QUICK       — 1 ise daha kısa poll aralığı (sunucu yüküne göre)
#   PRINT_FULL        — 1 ise tüm Dockerfile/compose basılır
#   POLL_INTERVAL, MAX_POLLS — bekleme bütçesi
#
set -euo pipefail

BASE="${BASE:-https://deploy.wrupup.com}"
BASE="${BASE%/}"
API_VERSION="${API_VERSION:-v2}"
REPO_PROFILE="${REPO_PROFILE:-simple}"
DF_TEST_API_KEY="${DF_TEST_API_KEY:-}"
export DF_TEST_API_KEY

case "$REPO_PROFILE" in
  simple)
    DEFAULT_REPO="https://github.com/pallets/flask.git"
    ;;
  monorepo)
  # backend + frontend — multi-service pipeline doğrulaması
    DEFAULT_REPO="https://github.com/tiangolo/full-stack-fastapi-template.git"
    ;;
  *)
    echo "WARN: bilinmeyen REPO_PROFILE=$REPO_PROFILE, simple kullanılıyor" >&2
    DEFAULT_REPO="https://github.com/pallets/flask.git"
    ;;
esac
REPO="${REPO:-$DEFAULT_REPO}"

if [[ "${SMOKE_QUICK:-0}" == "1" ]]; then
  POLL_INTERVAL="${POLL_INTERVAL:-30}"
  MAX_POLLS="${MAX_POLLS:-50}"
else
  POLL_INTERVAL="${POLL_INTERVAL:-45}"
  MAX_POLLS="${MAX_POLLS:-80}"
fi
PRINT_FULL="${PRINT_FULL:-0}"
FALLBACK_V1_ON_V2_ERROR="${FALLBACK_V1_ON_V2_ERROR:-1}"

die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo ">>> $*" >&2; }
warn() { echo "WARN: $*" >&2; }

is_uuid() {
  local s="$1"
  [[ "$s" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]
}

print_http_error_body() {
  if [[ -n "$__BODY_FILE" && -f "$__BODY_FILE" ]]; then
    echo "--- response body ---" >&2
    head -c 4000 "$__BODY_FILE" >&2 || true
    echo "" >&2
    echo "--- end body ---" >&2
  fi
}

# Prints body and returns 1 (does not exit) — caller decides fallback vs die.
fail_http() {
  local ctx="$1" code="$2"
  print_http_error_body
  cleanup_body
  echo "ERROR: ${ctx} HTTP ${code}" >&2
  if [[ "$code" == "500" && "$ctx" == v2* ]]; then
    echo "ERROR: v2 sunucuda hazır değil (eski imaj veya DB migration eksik)." >&2
    echo "  VPS: cd /opt/deployforges && git pull && alembic upgrade head \\" >&2
    echo "       && docker compose -f deploy/docker-compose.vps.yml build --no-cache api celery-worker \\" >&2
    echo "       && docker compose -f deploy/docker-compose.vps.yml up -d" >&2
    echo "  .env: DF_PIPELINE_MODE=multi_service" >&2
    echo "  Geçici test: API_VERSION=v1 bash scripts/vps-smoke.sh" >&2
    echo "  Otomatik v1: FALLBACK_V1_ON_V2_ERROR=1 (varsayılan)" >&2
  fi
  return 1
}

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

read_body() { cat "$__BODY_FILE"; }

cleanup_body() {
  rm -f "$__BODY_FILE"
  __BODY_FILE=""
}

api_prefix() {
  local ver="$1"
  echo "${BASE}/api/${ver}"
}

ensure_api_key() {
  if [[ -n "$DF_TEST_API_KEY" ]]; then
    info "Mevcut DF_TEST_API_KEY kullanılıyor (${DF_TEST_API_KEY:0:20}...)"
    return
  fi
  info "Kayıt (POST /api/v1/auth/register)"
  http_request POST "${BASE}/api/v1/auth/register" "{\"email\":\"smoke-$(date +%s)@example.com\"}"
  if [[ "$__HTTP_CODE" == "429" ]]; then
    read_body >&2
    cleanup_body
    die "Rate limit (429) — bekleyin veya DF_RATE_LIMIT_FREE artırın"
  fi
  if [[ "$__HTTP_CODE" != "2"* ]]; then
    read_body >&2
    cleanup_body
    die "register HTTP $__HTTP_CODE"
  fi
  DF_TEST_API_KEY="$(read_body | python3 -c "import sys,json; print(json.load(sys.stdin)['api_key'])")"
  export DF_TEST_API_KEY
  cleanup_body
  info "Yeni API key: ${DF_TEST_API_KEY:0:32}..."
}

health_check() {
  info "Health: GET ${BASE}/api/v1/health"
  http_request GET "${BASE}/api/v1/health"
  if [[ "$__HTTP_CODE" != "2"* ]]; then
    read_body >&2
    cleanup_body
    die "health HTTP $__HTTP_CODE — BASE doğru mu? ($BASE)"
  fi
  read_body | python3 -c "
import sys, json
j = json.load(sys.stdin)
print('  status:', j.get('status'), '| version:', j.get('version', '?'))
feats = j.get('features') or {}
if feats:
    print('  features:', feats)
if j.get('pipeline_mode'):
    print('  server pipeline_mode:', j.get('pipeline_mode'))
if j.get('api_versions'):
    print('  api_versions:', j.get('api_versions'))
" >&2
  cleanup_body
}

check_v2_ready() {
  [[ "$API_VERSION" == "v1" ]] && return 0
  info "v2 hazırlık: OpenAPI + health features"
  http_request GET "${BASE}/openapi.json"
  local has_v2=0
  if [[ "$__HTTP_CODE" == "2"* ]] && grep -q '"/api/v2/projects/' "$__BODY_FILE" 2>/dev/null; then
    has_v2=1
    info "  OpenAPI: /api/v2/projects bulundu"
  else
    warn "  OpenAPI: /api/v2/projects yok (eski API imajı)"
  fi
  cleanup_body
  http_request GET "${BASE}/api/v1/health"
  local has_manifest=0
  if [[ "$__HTTP_CODE" == "2"* ]]; then
    if read_body | python3 -c "import sys,json; j=json.load(sys.stdin); exit(0 if (j.get('features') or {}).get('deployment_manifest_v1') else 1)" 2>/dev/null; then
      has_manifest=1
      info "  health: deployment_manifest_v1=true"
    else
      warn "  health: deployment_manifest_v1 yok (eski API — yeniden build gerekir)"
    fi
    cleanup_body
  else
    cleanup_body
  fi
  if [[ "$has_v2" -eq 0 || "$has_manifest" -eq 0 ]]; then
    warn "Sunucu v2 için güncel değil; create muhtemelen 500 döner."
    return 1
  fi
  return 0
}

create_project() {
  local ver="$1"
  local prefix
  prefix="$(api_prefix "$ver")"
  info "Proje oluştur [$ver]: $REPO"
  local payload
  payload="$(python3 -c "import json; print(json.dumps({'source': {'type': 'git', 'url': '''${REPO}'''}}))")"
  http_request POST "${prefix}/projects/" "$payload"
  if [[ "$__HTTP_CODE" == "429" ]]; then
    fail_http "create project $ver (rate limit)" "429"
    return 1
  fi
  if [[ -z "$__HTTP_CODE" ]]; then
    echo "ERROR: create project $ver: curl failed (HTTP code boş)" >&2
    return 1
  fi
  if [[ "$__HTTP_CODE" != "2"* ]]; then
    fail_http "create project $ver" "$__HTTP_CODE"
    return 1
  fi
  local body pid
  body="$(read_body)"
  cleanup_body
  pid="$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")"
  echo "$body" | python3 -c "
import sys, json
j = json.load(sys.stdin)
print('  id:', j.get('id'))
print('  status:', j.get('status'))
if j.get('pipeline_mode'):
    print('  pipeline_mode:', j.get('pipeline_mode'))
if j.get('manifest_version'):
    print('  manifest_version:', j.get('manifest_version'))
links = j.get('links') or {}
if links.get('result'):
    print('  result:', links.get('result'))
" >&2
  if ! is_uuid "$pid"; then
    die "create project [$ver]: geçersiz project id (stdout kirlenmiş olabilir): '$pid'"
  fi
  echo "$pid"
}

poll_until_terminal() {
  local ver="$1"
  local pid="$2"
  if ! is_uuid "$pid"; then
    die "poll: geçersiz PID '$pid' — create adımı başarısız olmuş olabilir"
  fi
  local prefix
  prefix="$(api_prefix "$ver")"
  local st="unknown"
  local i
  for i in $(seq 1 "$MAX_POLLS"); do
    http_request GET "${prefix}/projects/${pid}"
    if [[ "$__HTTP_CODE" == "429" ]]; then
      read_body >&2
      cleanup_body
      die "Rate limit (429) during poll"
    fi
    if [[ "$__HTTP_CODE" != "2"* ]]; then
      read_body >&2
      cleanup_body
      die "poll [$ver] HTTP $__HTTP_CODE"
    fi
    st="$(read_body | python3 -c "
import sys, json
j = json.load(sys.stdin)
extra = []
if j.get('service_count') is not None:
    extra.append('services=' + str(j.get('service_count')))
if j.get('primary_service'):
    extra.append('primary=' + str(j.get('primary_service')))
if j.get('is_monorepo'):
    extra.append('monorepo')
suffix = (' (' + ', '.join(extra) + ')') if extra else ''
print(j.get('status', '?') + suffix)
")"
    cleanup_body
    echo "[${i}/${MAX_POLLS}] status=$st"
    if [[ "$st" == success* || "$st" == failed* || "$st" == partial* ]]; then
      # strip suffix for terminal check
      case "$st" in
        success*|failed*|partial*) break ;;
      esac
    fi
    sleep "$POLL_INTERVAL"
  done
  case "$st" in
    success*|partial*|failed*) ;;
    *)
      approx=$(( (MAX_POLLS - 1) * POLL_INTERVAL ))
      die "Zaman aşımı (~${approx}s). Son: $st — MAX_POLLS/POLL_INTERVAL artırın; worker loglarına bakın"
      ;;
  esac
  # normalize status token
  if [[ "$st" == success* ]]; then echo "success"
  elif [[ "$st" == partial* ]]; then echo "partial"
  else echo "failed"
  fi
}

print_error_summary() {
  local ver="$1"
  local pid="$2"
  local prefix
  prefix="$(api_prefix "$ver")"
  info "Hata özeti (GET ${prefix}/projects/${pid})"
  http_request GET "${prefix}/projects/${pid}"
  if [[ "$__HTTP_CODE" == "2"* ]]; then
    read_body | python3 -c "
import sys, json
j = json.load(sys.stdin)
es = j.get('error_summary')
print(es if es else '(error_summary boş — worker güncel mi?)')
"
    cleanup_body
  else
    cleanup_body
  fi
}

print_result_v1() {
  local pid="$1"
  info "Sonuç [v1] GET /api/v1/projects/${pid}/result"
  http_request GET "${BASE}/api/v1/projects/${pid}/result"
  if [[ "$__HTTP_CODE" != "2"* ]]; then
    read_body >&2
    cleanup_body
    die "v1 result HTTP $__HTTP_CODE"
  fi
  read_body | python3 -c "
import sys, json, os
r = json.load(sys.stdin)
print('status:', r.get('status'))
if r.get('error_summary'):
    print('error_summary:', r.get('error_summary'))
res = r.get('result') or {}
df = res.get('dockerfile')
compose = res.get('docker_compose')
if compose and os.environ.get('PRINT_FULL') == '1':
    print()
    print('========== docker-compose (v1) ==========')
    print(compose[:12000])
if df:
    if os.environ.get('PRINT_FULL') == '1':
        print()
        print('========== Dockerfile (primary, v1) ==========')
        print(df)
    else:
        lines = df.strip().splitlines()
        print('dockerfile:', len(lines), 'lines,', len(df), 'bytes')
        print('  first:', lines[0][:120] if lines else '')
        print('  last:', lines[-1][:120] if lines else '')
else:
    print('Dockerfile yok.')
    print(json.dumps(r, indent=2)[:4000])
"
  cleanup_body
}

print_result_v2() {
  local pid="$1"
  info "Sonuç [v2] GET /api/v2/projects/${pid}/result"
  http_request GET "${BASE}/api/v2/projects/${pid}/result"
  if [[ "$__HTTP_CODE" != "2"* ]]; then
    read_body >&2
    cleanup_body
    die "v2 result HTTP $__HTTP_CODE (proje hâlâ processing olabilir — 409)"
  fi
  PRINT_FULL="$PRINT_FULL" read_body | python3 -c "
import sys, json, os
r = json.load(sys.stdin)
print('status:', r.get('status'))
if r.get('error_summary'):
    print('error_summary:', r.get('error_summary'))
m = r.get('deployment_manifest')
if not m:
    print('deployment_manifest: (yok — DF_PIPELINE_MODE=multi_service ve worker güncel mi?)')
    print(json.dumps(r, indent=2)[:4000])
    sys.exit(0)
print('manifest_version:', m.get('deployment_manifest_version'))
proj = m.get('project') or {}
print('project:', 'monorepo=' + str(proj.get('is_monorepo')), 'langs=', proj.get('languages'))
val = m.get('validation') or {}
print('validation:', 'primary=', val.get('primary_service'),
      'levels=', val.get('levels_passed'),
      'cloud_run=', val.get('cloud_run_service'),
      'url=', val.get('deploy_url'))
services = m.get('services') or []
print('services (%d):' % len(services))
for s in services:
    print('  -', s.get('name'), s.get('type'), s.get('root_path'),
          s.get('language'), s.get('framework'), 'port=', s.get('port'))
arts = (m.get('artifacts') or {})
dfs = arts.get('dockerfiles') or {}
print('artifacts: dockerfiles=%d compose=%s bytes' % (
    len(dfs),
    len(arts.get('compose_yml') or ''),
))
for name, art in dfs.items():
    df = (art.get('dockerfile') or '') if isinstance(art, dict) else ''
    method = art.get('generation_method', '?') if isinstance(art, dict) else '?'
    if os.environ.get('PRINT_FULL') == '1':
        print()
        print('--- Dockerfile:', name, '(%s)' % method, '---')
        print(df[:16000])
    else:
        n = len(df.splitlines()) if df else 0
        print('  ', name, ':', method, ',', n, 'lines')
compose = arts.get('compose_yml') or ''
if compose and os.environ.get('PRINT_FULL') == '1':
    print()
    print('========== compose.yml ==========')
    print(compose[:16000])
elif compose:
    print('  compose.yml:', len(compose.splitlines()), 'lines')
warn = m.get('warnings') or []
if warn:
    print('warnings (%d):' % len(warn))
    for w in warn[:8]:
        print('  -', str(w)[:200])
"
  cleanup_body
}

run_smoke_version() {
  local ver="$1"
  info "=== Smoke API $ver === (BASE=$BASE REPO_PROFILE=$REPO_PROFILE)"
  local pid
  if ! pid="$(create_project "$ver")"; then
    return 1
  fi
  if ! is_uuid "$pid"; then
    die "create_project $ver returned non-uuid: '$pid'"
  fi
  info "PID=$pid"
  local st
  st="$(poll_until_terminal "$ver" "$pid")"
  if [[ "$st" == "failed" ]]; then
    print_error_summary "$ver" "$pid"
  fi
  if [[ "$ver" == "v2" ]]; then
    print_result_v2 "$pid"
  else
    print_result_v1 "$pid"
  fi
  if [[ "$st" == "failed" ]]; then
    echo ""
    info "failed — sunucuda teşhis:"
    echo "  ssh root@<vps> 'cd /opt/deployforges && docker compose -f deploy/docker-compose.vps.yml --env-file .env logs --tail=120 celery-worker'"
    echo "  # multi-service: .env içinde DF_PIPELINE_MODE=multi_service"
    return 1
  fi
  return 0
}

# --- main ---
info "DeployForge VPS smoke"
info "BASE=$BASE API_VERSION=$API_VERSION REPO=$REPO"
export PRINT_FULL

health_check
ensure_api_key
check_v2_ready || true

rc=0
case "$API_VERSION" in
  v2)
    if ! run_smoke_version v2; then
      if [[ "$FALLBACK_V1_ON_V2_ERROR" == "1" ]]; then
        warn "v2 smoke başarısız — v1 fallback deneniyor (FALLBACK_V1_ON_V2_ERROR=0 ile kapatılır)"
        run_smoke_version v1 || rc=1
      else
        rc=1
      fi
    fi
    ;;
  v1)
    run_smoke_version v1 || rc=1
    ;;
  both)
    run_smoke_version v2 || rc=1
    echo ""
    run_smoke_version v1 || rc=1
    ;;
  *)
    die "API_VERSION must be v1, v2, or both (got: $API_VERSION)"
    ;;
esac

info "Bitti (exit $rc)"
exit "$rc"
