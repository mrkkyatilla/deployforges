# DeployForge operations runbook

This document ties together **Celery task wall-clock limits**, **API client polling**, and the **VPS smoke script** so operators can reason about end-to-end latency in one place.

## Celery pipeline task

The full LangGraph run (clone → analysis → Dockerfile generation → lint → pre-build → builds/fix loops → optional deploy) executes inside a single Celery task: `deployforge.run_pipeline`.

| Setting | Meaning |
|--------|---------|
| `DF_CELERY_PIPELINE_TASK_TIME_LIMIT_SECONDS` | Hard kill after this many seconds (default 7200). |
| `DF_CELERY_PIPELINE_TASK_SOFT_TIME_LIMIT_SECONDS` | Soft limit; worker raises before hard kill (default 6600). |

These must exceed the worst case of roughly:

`DF_MAX_BUILD_ATTEMPTS × DF_BUILD_TIMEOUT_SECONDS` plus Gemini retries, pre-build checks, and Cloud Run deploy when enabled.

## API: pipeline enqueue (Celery vs in-process)

| `DF_PIPELINE_ENQUEUE_MODE` | Behaviour |
|---------------------------|-----------|
| `auto` (default in `.env.example`) | Try Celery first; if Redis/broker is down, run `run_pipeline` in the API process after the HTTP response (no 503 on create). |
| `celery` | Strict: project creation returns 503 if the task cannot be queued (production stacks with a worker). |
| `background` | Always run in the API process (no Celery; no worker-side retries for transient Gemini errors). |

Docker Compose VPS sets `DF_PIPELINE_ENQUEUE_MODE=celery` on the **api** service so the worker always receives jobs when Redis is healthy.

## Build attempts and Docker timeout

| Setting | Default | Notes |
|--------|---------|------|
| `DF_MAX_BUILD_ATTEMPTS` | 5 | LangGraph may loop build → classify → fix. |
| `DF_BUILD_TIMEOUT_SECONDS` | 600 | Per `docker build` / Kaniko attempt. |

## Client polling (`scripts/vps-smoke.sh`)

Smoke targets **https://deploy.wrupup.com** by default with `API_VERSION=v2` (manifest result).

| Variable | Default | Meaning |
|----------|---------|---------|
| `BASE` | `https://deploy.wrupup.com` | API origin |
| `API_VERSION` | `v2` | `v2`, `v1`, or `both` |
| `REPO_PROFILE` | `simple` | `simple` (Flask) or `monorepo` (full-stack template) |
| `SMOKE_QUICK` | `0` | `1` → shorter poll budget |
| `PRINT_FULL` | `0` | `1` → print all Dockerfiles / compose |
| `POLL_INTERVAL` | 45 (30 if quick) | Seconds between polls |
| `MAX_POLLS` | 80 (50 if quick) | Maximum polls |

Terminal statuses include `success`, `partial`, and `failed` (v2 multi-service).

Increase poll budget for large repos or high `DF_MAX_BUILD_ATTEMPTS` / `DF_BUILD_TIMEOUT_SECONDS`.

## SSE events

`GET /api/v1/projects/{id}/events` streams JSON payloads. Steps now include `progress_text` (bilingual TR/EN) and `elapsed_ms` per `step_complete` where instrumented.

## Regression suite

Deterministic regression cases live under `tests/fixtures/` and are listed in `tests/regression_manifest.yaml`. Run:

```bash
cd deployforges && pytest tests/test_regression_deterministic.py -m regression
```

CI runs this marker on a schedule (see `.github/workflows/regression.yml`).

## Build ``error_analysis`` (schema v1)

Each ``Build`` row stores JSON in ``error_analysis`` (also exposed on ``GET .../builds`` as ``error_analysis`` on each item). Version **1** uses:

| Field | Meaning |
|-------|---------|
| ``schema_version`` | Always ``"1"``. |
| ``type`` / ``summary`` | First error name and a short joined summary (backwards compatible with older clients). |
| ``classified`` | List of ``{ name, fix_strategy, auto_fixable, error_type? }`` from the classifier. |
| ``fixes_applied`` | Optional string list (e.g. ``strategy:add_copy``) after deterministic auto-fixes. |
| ``pipeline_policy`` | Snapshot: ``tier``, ``mode``, ``signals`` when available. |
| ``deploy_error_excerpt`` | Optional short log excerpt. |
| ``outcome`` | On successful end-to-end runs the latest successful build row may be updated to ``outcome: success`` (with ``pipeline_policy`` preserved when present). |

## Playbook hints (YAML + Redis)

| Variable | Default | Meaning |
|----------|---------|---------|
| ``DF_AI_PLAYBOOK_HINTS_ENABLED`` | true | Master switch for injecting curated hints into Dockerfile generation prompts. |
| ``DF_AI_PLAYBOOK_HINT_TTL_SECONDS`` | 604800 | Redis TTL for reinforced keys; **0** skips Redis read/write (YAML-only hints). |
| ``DF_AI_PLAYBOOK_RAG_ENABLED`` | false | **Reserved** — vector/RAG playbook retrieval is not implemented; keep false. |
