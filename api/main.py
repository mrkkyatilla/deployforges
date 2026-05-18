from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.config import settings
from api.middleware.rate_limit import RateLimitMiddleware
from api.middleware.security import RequestValidationMiddleware, SecurityHeadersMiddleware
from api.routers import admin, auth, billing, builds, projects, projects_v2, traces, webhooks


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.workspace_base_path.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="DeployForge API",
    version=settings.app_version,
    description="""
# DeployForge API

AI-Powered Universal Project Deployment Engine.

## Overview

DeployForge analyzes any project (Python, Node.js, Go, Rust, Java, PHP, Ruby, .NET, Elixir)
and generates production-ready Dockerfiles using AI. It then validates the build in a sandbox
and returns the result.

## Authentication

All endpoints require an API key passed via the `X-API-Key` header:

```
X-API-Key: df_live_your_api_key_here
```

## Quick Start

1. Create a project: `POST /api/v1/projects`
2. Stream events: `GET /api/v1/projects/{id}/events` (SSE)
3. Get result: `GET /api/v1/projects/{id}/result`

## Rate Limits

Per `X-API-Key`, sliding window (`DF_RATE_LIMIT_WINDOW_SECONDS`, default 3600s). Tiers come from Redis (`tier:<api_key>`) or default to **free**:

- Free: `DF_RATE_LIMIT_FREE` (default 50 requests/hour)
- Pro: `DF_RATE_LIMIT_PRO` (default 100 requests/hour)
- Enterprise: 10000 requests/hour (fixed in code)
""",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=[
        {"name": "projects", "description": "Project creation and management (v1 — deprecated)"},
        {"name": "projects-v2", "description": "Manifest-centric projects (v2)"},
        {"name": "builds", "description": "Build attempts and logs"},
        {"name": "billing", "description": "Credits, usage, and transactions"},
        {"name": "webhooks", "description": "Inbound and outbound webhook management"},
        {"name": "auth", "description": "User registration and API key management"},
        {"name": "health", "description": "System health and metadata"},
        {"name": "admin", "description": "Internal administration (X-Admin-Key only — not X-API-Key): stats, monitoring, AI traces, reporter"},
        {"name": "traces", "description": "Pipeline trace observability"},
    ],
    responses={
        401: {"description": "Invalid or missing API key"},
        429: {"description": "Rate limit exceeded"},
    },
)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestValidationMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RateLimitMiddleware)

app.include_router(auth.router, prefix="/api/v1")
app.include_router(projects.router, prefix="/api/v1")
app.include_router(projects_v2.router, prefix="/api/v2")
app.include_router(builds.router, prefix="/api/v1")
app.include_router(billing.router, prefix="/api/v1")
app.include_router(webhooks.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")
app.include_router(traces.router, prefix="/api/v1")


@app.get("/api/v1/health", tags=["health"])
async def health_check():
    return {
        "status": "ok",
        "version": settings.app_version,
        "api_versions": ["v1", "v2"],
        "pipeline_mode": settings.pipeline_mode,
        "features": {
            "deployment_manifest_v1": True,
            "multi_service_pipeline": settings.pipeline_mode == "multi_service",
        },
    }


@app.get("/api/v1/languages", tags=["health"])
async def supported_languages():
    return {
        "supported": [
            "python", "javascript", "typescript", "go", "rust",
            "java", "php", "ruby", "csharp", "elixir",
        ]
    }
