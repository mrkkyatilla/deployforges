```markdown
# DeployForges

AI-Powered Universal Project Deployment Engine.

Provide a Git repo URL, get a production-ready Dockerfile. That's it.

---

## Quick Start (5 minutes)

```bash
# 1. Install dependencies
make setup

# 2. Edit the .env file — only the Gemini API key is required
nano .env
# DF_GEMINI_API_KEY=AIzaSy...  ← [https://aistudio.google.com/apikey](https://aistudio.google.com/apikey)

# 3. Start PostgreSQL + Redis
make db-up

# 4. Database tables + test user
make migrate
make seed

# 5. Start the API server
make dev

```

The server runs on `http://localhost:8000`. API docs: `http://localhost:8000/docs`

### Production VPS (SSH + Cloudflare)

Self hosted VPS deployment: [deploy/VPS-DEPLOY.md](deploy/VPS-DEPLOY.md).

if SSH key adden on local server (`ssh root@IP` running without pw):

```bash
chmod +x deploy/push-and-boot.sh
./deploy/push-and-boot.sh
```

Curent Target `root@IP:/opt/deployforges`; domain name `deploy/Caddyfile` into `deploy.wrupup.com`.

---

## First Use

### New User Registration

```bash
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com"}'

```

Response:

```json
{
  "user_id": "...",
  "email": "you@example.com",
  "api_key": "df_live_abc123...",  ← SAVE THIS!
  "tier": "free",
  "credits_balance": 5.0,
  "message": "Registration successful. Save your API key — it won't be shown again."
}

```

### Deploy a Project

```bash
# Via API:
curl -X POST http://localhost:8000/api/v1/projects \
  -H "X-API-Key: df_live_abc123..." \
  -H "Content-Type: application/json" \
  -d '{"source": {"type": "git", "url": "[https://github.com/user/repo.git](https://github.com/user/repo.git)"}}'

# or via CLI:
deployforge auth login    # Enter API key
deployforge deploy [https://github.com/user/repo.git](https://github.com/user/repo.git)

```

### Get the Result

```bash
curl http://localhost:8000/api/v1/projects/{id}/result \
  -H "X-API-Key: df_live_abc123..."

```

### VPS / production smoke (repo → Dockerfile)

From repo root (set `BASE` and optional `DF_TEST_API_KEY`):

```bash
BASE=https://your-api.example.com bash scripts/vps-smoke.sh
```

After `git pull` on the VPS, **rebuild** the worker image so intake/security fixes ship:  
`docker compose -f deploy/docker-compose.vps.yml build --no-cache celery-worker api && docker compose -f deploy/docker-compose.vps.yml up -d`

Uses `curl` without `-f`, handles `429`, and avoids `set -u` issues with an empty API key before register.

---

## Requirements

| Requirement | Version | Reason |
| --- | --- | --- |
| Python | 3.12+ | Main runtime |
| PostgreSQL | 16+ | Metadata, logs, audit |
| Redis | 7+ | Cache, queue, rate limit |
| Docker | 24+ | Local build sandbox |
| gcloud CLI | Latest | Cloud Run deploy/test (optional) |
| Gemini API Key | — | AI Dockerfile generation |

---

## Environment Variables

| Variable | Required | Description |
| --- | --- | --- |
| `DF_GEMINI_API_KEY` | Yes | Google AI Studio API key |
| `DF_DATABASE_URL` | Yes | PostgreSQL connection string |
| `DF_REDIS_URL` | Yes | Redis connection string |
| `DF_GCP_PROJECT_ID` | For Cloud Run deploy/test & Kaniko | Google Cloud project ID |
| `DF_GCP_REGION` | With GCP | Cloud Run region |
| `DF_BUILD_BACKEND` | No | `local_docker` (default), `kaniko` (needs GCP + gcloud in worker), `skip` |
| `DF_ADMIN_API_KEY` | No | For admin endpoints |
| `DF_REQUIRE_EMAIL_VERIFICATION` | No | `false` (default): instant sign-up + API key. `true`: register returns 501 until verify-by-email is implemented — use before going production. |
| `DF_DEBUG` | No | Debug mode (default: false) |
| `DF_RATE_LIMIT_FREE` | No | Max requests/hour per API key when tier is free (default: 50). |
| `DF_RATE_LIMIT_PRO` | No | Max requests/hour when tier is pro (default: 100). |
| `DF_RATE_LIMIT_WINDOW_SECONDS` | No | Sliding window length in seconds (default: 3600). |
| `DF_AI_DEBUG_IO` | No | When true, logs truncated Gemini prompts/responses (masked). |
| `DF_AI_DEBUG_MAX_CHARS` | No | Max chars per prompt/response excerpt for logs and DB (default: 8000). |
| `DF_AI_PERSIST_IO_EXCERPTS` | No | When true, stores excerpts + parse metadata on ``ai_interactions.extra``. |
| `DF_AI_DOCKERFILE_PLAN_ENABLED` | No | When true (default), runs a Flash JSON **plan** step before Dockerfile generation. |
| `DF_AI_DOCKERFILE_PLAN_JSON_REPAIR_ENABLED` | No | When true (default), one Flash **repair** pass if plan JSON fails to parse. Set false to skip that extra call. |
| `DF_AI_DOCKERFILE_TWO_PHASE_ENABLED` | No | When true (default), metadata JSON (Flash) then Dockerfile **plain text** (Pro); reduces JSON string truncation issues. |
| `DF_AI_GENERATION_OUTPUT_FLOOR_TOKENS` | No | Floor for `max_output_tokens` on generation-like steps vs budget (default 4096). |
| `DF_AI_GENERATION_OUTPUT_FLOOR_MONOREPO_TOKENS` | No | Same for monorepo / multi-deps fingerprints (default 8192). |
| `DF_AI_GENERATION_USE_FLASH_FOR_SIMPLE` | No | When true, non–high-complexity fingerprints use Flash for **legacy** single-shot JSON generation only. |
| `DF_AI_JSON_REPAIR_SECOND_ATTEMPT_ENABLED` | No | When true (default), JSON repair retries once with a tail-focused Flash prompt. |
| `DF_GEMINI_FILES_API_PROMPT_TOKEN_THRESHOLD` | No | Reserved: future Files API for huge prompts. **0** = disabled (no runtime behavior yet). |
| `DF_AI_DOCKERFILE_CRITIC_REFINE_ENABLED` | No | When true, runs critic + optional one-shot **refine** after generation (extra tokens). |
| `DF_REPORTER_LLM_ENABLED` | No | When true, ``POST /api/v1/admin/reporter/run`` may call Gemini on **aggregate** metrics only. |
| `DF_REPORTER_BEAT_ENABLED` | No | When true, Celery Beat schedules daily ``deployforge.reporter_run`` (no customer HTTP). |

---

## Admin API (``X-Admin-Key`` only)

Customer API keys (``X-API-Key``) cannot call these routes.

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/v1/admin/monitoring/report` | Token/cost report + rule-based suggestions |
| POST | `/api/v1/admin/reporter/run` | Same metrics plus optional LLM summary (`include_llm=true` and ``DF_REPORTER_LLM_ENABLED``) |

Query params for reporter: ``period_days`` (1–90), ``include_llm`` (default false).

### Large repositories and Gemini Files API

Monorepos and very large trees stress inline JSON (embedded Dockerfile strings can hit **MAX_TOKENS** or break parsing). DeployForge defaults to **two-phase** generation: a compact metadata JSON step, then a **plain-text** Dockerfile body (`DF_AI_DOCKERFILE_TWO_PHASE_ENABLED`, default on). Tighter **critical-file** budgets apply when the fingerprint looks high-complexity (monorepo, multi-deps, many services).

`DF_GEMINI_FILES_API_PROMPT_TOKEN_THRESHOLD` is a **reserved** hook: at **0** (default) nothing uses the Files API. A future version may upload oversized artifacts when estimated prompt tokens exceed this threshold, instead of pasting megabytes into the prompt.

---

## Commands

```bash
make setup        # Install dependencies + create .env
make db-up        # Start PostgreSQL + Redis
make db-down      # Stop
make migrate      # Create and apply DB migrations
make seed         # Create test user + API key
make dev          # API server (hot-reload)
make worker       # Celery worker (build jobs)
make beat         # Celery beat (periodic cleanup)
make test         # Run unit tests
make lint         # Code quality check
make gcp-setup    # Enable GCP APIs + service account
make prod-up      # Start production docker-compose
make prod-down    # Stop production

```

---

## API Endpoints

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| POST | `/api/v1/auth/register` | — | Register, get API key |
| POST | `/api/v1/auth/keys` | Key | Create a new API key |
| GET | `/api/v1/auth/keys` | Key | List keys |
| GET | `/api/v1/auth/me` | Key | User info |
| POST | `/api/v1/projects` | Key | Create a project (git URL) |
| POST | `/api/v1/projects/upload` | Key | Upload a project (zip/tar) |
| GET | `/api/v1/projects` | Key | List projects |
| GET | `/api/v1/projects/{id}` | Key | Project status |
| GET | `/api/v1/projects/{id}/result` | Key | Result (Dockerfile) |
| GET | `/api/v1/projects/{id}/events` | Key | SSE event stream |
| GET | `/api/v1/projects/{id}/builds` | Key | Build attempts |
| GET | `/api/v1/billing/credits` | Key | Credit balance |
| GET | `/api/v1/billing/usage` | Key | Usage report |
| GET | `/api/v1/health` | — | Health check |
| POST | `/api/v1/admin/reporter/run` | Admin key | Usage report + optional LLM narrative (aggregate data only) |

Full API documentation: `http://localhost:8000/docs` (Swagger) or `/redoc`

---

## Architecture

```
deployforges/
├── api/              # FastAPI — routers, middleware, schemas
├── cli/              # CLI tool (deployforge command)
├── core/
│   ├── ai/           # Gemini client, LangGraph orchestrator, templates, linter
│   ├── analysis/     # 10 language detection, fingerprint, monorepo
│   ├── builder/      # Sandbox build, Cloud Run validator, retry
│   ├── error/        # Error classification, patterns, auto-fix
│   └── intake/       # Git clone, archive extract, security scan
├── db/               # SQLAlchemy models, Alembic migrations
├── sdk/              # Python SDK (pip install)
├── workers/          # Celery build + cleanup workers
├── scripts/          # Setup & seed scripts
└── tests/            # Unit & integration tests

```

---

## Supported Languages

Python, JavaScript/TypeScript, Go, Rust, Java, PHP, Ruby, C# (.NET), Elixir — a total of 10 languages, 20+ frameworks.

---

## Production Deployment

```bash
# 1. GCP setup (one-time)
make gcp-setup

# 2. Edit .env (with production values)
cp .env.example .env
nano .env

# 3. Start production
make prod-up

# This runs the following:
#   - 2x API server (load balanced)
#   - 2x Celery worker (build jobs)
#   - 1x Celery beat (cleanup)
#   - PostgreSQL 16
#   - Redis 7

```

```

```