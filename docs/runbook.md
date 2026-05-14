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

## Build attempts and Docker timeout

| Setting | Default | Notes |
|--------|---------|------|
| `DF_MAX_BUILD_ATTEMPTS` | 5 | LangGraph may loop build → classify → fix. |
| `DF_BUILD_TIMEOUT_SECONDS` | 600 | Per `docker build` / Kaniko attempt. |

## Client polling (`scripts/vps-smoke.sh`)

The smoke script polls `GET /api/v1/projects/{id}` until status is terminal.

| Variable | Default | Meaning |
|----------|---------|---------|
| `POLL_INTERVAL` | 45 | Seconds between polls. |
| `MAX_POLLS` | 80 | Maximum polls (~1 hour at defaults). |

Increase both when running against large repos or when `DF_MAX_BUILD_ATTEMPTS` / `DF_BUILD_TIMEOUT_SECONDS` are raised above defaults.

## SSE events

`GET /api/v1/projects/{id}/events` streams JSON payloads. Steps now include `progress_text` (bilingual TR/EN) and `elapsed_ms` per `step_complete` where instrumented.

## Regression suite

Deterministic regression cases live under `tests/fixtures/` and are listed in `tests/regression_manifest.yaml`. Run:

```bash
cd deployforges && pytest tests/test_regression_deterministic.py -m regression
```

CI runs this marker on a schedule (see `.github/workflows/regression.yml`).
