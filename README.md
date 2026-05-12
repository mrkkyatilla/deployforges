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

Tek VPS için adımlar: [deploy/VPS-DEPLOY.md](deploy/VPS-DEPLOY.md).

Yerel makinede SSH anahtarın sunucuya ekliyse (`ssh root@188.132.197.229` parolasız açılıyorsa):

```bash
chmod +x deploy/push-and-boot.sh
./deploy/push-and-boot.sh
```

Varsayılan hedef `root@188.132.197.229:/opt/deployforges`; alan adı `deploy/Caddyfile` içinde `deploy.wrupup.com`.

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
| `DF_GCP_PROJECT_ID` | For Build | Google Cloud project ID |
| `DF_GCP_REGION` | For Build | Cloud Run region |
| `DF_ADMIN_API_KEY` | No | For admin endpoints |
| `DF_DEBUG` | No | Debug mode (default: false) |

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