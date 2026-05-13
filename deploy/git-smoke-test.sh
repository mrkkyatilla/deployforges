#!/usr/bin/env bash
# Tek parça: desteklenen dil/ecosistem için örnek Git URL'leri → POST /projects/
#
# Kullanım:
#   export BASE=https://deploy.wrupup.com/api/v1
#   export API_KEY='df_live_...'
#   bash deploy/git-smoke-test.sh
#
# Not: Güvenlik tarayıcısı depoda .env / .env.* içinde KEY=değer satırı bulursa projeyi reddeder.
#      Bazı şablon repolar (ör. Laravel, çok sayıda Heroku örneği) bu yüzden elenebilir.

set -euo pipefail

: "${BASE:?Önce export BASE=https://.../api/v1}"
: "${API_KEY:?Önce export API_KEY=df_live_...}"

extract_id() {
  # jq yoksa grep ile UUID çek
  local json=$1
  if command -v jq >/dev/null 2>&1; then
    echo "$json" | jq -r '.id // empty'
  else
    echo "$json" | grep -oE '"id"\s*:\s*"[a-f0-9-]{36}"' | head -1 | grep -oE '[a-f0-9-]{8}-[a-f0-9-]{4}-[a-f0-9-]{4}-[a-f0-9-]{4}-[a-f0-9-]{12}'
  fi
}

# format: etiket|repo_url|dal
TESTS=(
  "01-minimal-readme|https://github.com/octocat/Hello-World.git|main"
  "02-python-packaging|https://github.com/pypa/sampleproject.git|main"
  "03-node-express|https://github.com/expressjs/express.git|master"
  "04-typescript-fastify|https://github.com/fastify/fastify.git|main"
  "05-go-examples|https://github.com/golang/example.git|master"
  "06-rust-cli-fd|https://github.com/sharkdp/fd.git|master"
  "07-java-maven-spring-guide|https://github.com/spring-guides/gs-maven.git|main"
  "08-php-composer-lib|https://github.com/FriendsOfPHP/Goutte.git|master"
  "09-ruby-sinatra|https://github.com/sinatra/sinatra.git|main"
  "10-csharp-polly|https://github.com/App-vNext/Polly.git|main"
  "11-elixir-plug|https://github.com/elixir-plug/plug.git|main"
)

echo "BASE=$BASE"
echo "İstekler gönderiliyor..."
echo ""

for row in "${TESTS[@]}"; do
  IFS='|' read -r label url branch <<<"$row"
  printf '=== %s ===\n  url=%s branch=%s\n' "$label" "$url" "$branch"
  resp=$(curl -sS -w "\n%{http_code}" -X POST "${BASE%/}/projects/" \
    -H "X-API-Key: ${API_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"source\":{\"type\":\"git\",\"url\":\"${url}\",\"branch\":\"${branch}\"}}")
  code=$(echo "$resp" | tail -n1)
  body=$(echo "$resp" | sed '$d')
  http_body=$(echo "$body" | tail -n +1)
  echo "  HTTP $code"
  if [[ "$code" != "202" ]]; then
    echo "  Gövde: $http_body"
    echo ""
    continue
  fi
  pid=$(extract_id "$http_body")
  echo "  project_id=$pid"
  echo "  Durum: curl -sS \"\$BASE/projects/$pid\" -H \"X-API-Key: \$API_KEY\""
  echo "  Sonuç: curl -sS \"\$BASE/projects/$pid/result\" -H \"X-API-Key: \$API_KEY\""
  echo ""
done

echo "Bitti. Celery log: docker compose -f deploy/docker-compose.vps.yml --env-file .env logs celery-worker -f"
