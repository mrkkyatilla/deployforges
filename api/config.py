from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="DF_",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---
    app_name: str = "DeployForge"
    app_version: str = "0.1.0"
    debug: bool = False

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 4

    # --- Database ---
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/deployforge"
    database_pool_size: int = 20
    database_max_overflow: int = 10

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"
    # How the API starts a pipeline after creating a project:
    # - celery: always use Celery (503 if broker/worker unavailable).
    # - auto: try Celery first; on failure run in-process after the HTTP response (local dev friendly).
    # - background: always run in-process (no Celery; no transient Gemini Celery retries).
    pipeline_enqueue_mode: Literal["celery", "auto", "background"] = "auto"

    # --- Gemini AI ---
    gemini_api_key: str = ""
    gemini_pro_model: str = "gemini-2.5-pro"
    gemini_flash_model: str = "gemini-2.5-flash"
    gemini_max_retries: int = 3
    gemini_timeout: int = 60
    # Per-request backoff between Gemini retries (exponential cap). Jitter reduces herd effects.
    gemini_retry_backoff_base_seconds: float = 5.0
    gemini_retry_backoff_max_seconds: float = 120.0
    gemini_retry_backoff_jitter_ratio: float = 0.25
    # After primary model exhausts retries on transient errors, try this model once (same retry loop).
    gemini_fallback_model: str = ""

    # When the model returns HTTP 200 but no text (often FinishReason.MAX_TOKENS), bump
    # max_output_tokens up to this cap before counting as a failed outer retry.
    gemini_max_output_tokens_cap: int = 24576

    # Celery: only ``TransientGeminiError`` triggers full-pipeline retries (503/429 overload).
    celery_transient_max_retries: int = 3
    celery_transient_retry_countdown_base: int = 90
    celery_transient_retry_countdown_max: int = 600
    # Whole LangGraph pipeline (clone → AI → multiple docker builds + fixes + deploy) runs in one Celery task.
    # Must exceed worst-case: ``max_build_attempts * build_timeout_seconds`` plus Gemini / deploy slack.
    celery_pipeline_task_time_limit_seconds: int = 7200
    celery_pipeline_task_soft_time_limit_seconds: int = 6600

    # When True: log truncated Gemini prompt/response (masked). Do not enable in untrusted log sinks.
    ai_debug_io: bool = False
    # Max characters per prompt/response excerpt for logs and optional DB persistence.
    ai_debug_max_chars: int = 8000
    # When True: store prompt/response excerpts on ai_interactions.extra (still truncated).
    ai_persist_io_excerpts: bool = False

    # Dockerfile generation: optional plan JSON step before full generation (Flash).
    ai_dockerfile_plan_enabled: bool = True
    # When False: skip the extra Flash JSON-repair pass on plan parse failure (saves one call).
    ai_dockerfile_plan_json_repair_enabled: bool = True
    # Optional critic + one full refine pass after generation (Pro refine); higher cost.
    ai_dockerfile_critic_refine_enabled: bool = False
    # Two-phase generation: (1) compact metadata JSON without embedded Dockerfile string,
    # (2) Dockerfile body as plain text — reduces JSON escaping / MAX_TOKENS truncation issues.
    ai_dockerfile_two_phase_enabled: bool = True
    # Prefer fully rendered stock Dockerfile when fingerprint + tree are unambiguous (no Gemini).
    ai_dockerfile_template_first_enabled: bool = True
    # Fix prompts: ask model to touch only RUN/CMD layers when possible (aligned with two-phase body).
    ai_dockerfile_locked_skeleton_fix_enabled: bool = True
    # After N failed builds, Dockerfile fix step falls back to deterministic template (if available).
    ai_early_stop_template_after_build_attempt: int = 4
    # When True, Dockerfile body phase may use Flash for low-complexity projects (cheaper; test before prod).
    ai_generation_body_use_flash_for_simple: bool = False
    # Redis TTL (seconds) for Dockerfile plan + metadata JSON cache; 0 disables cache.
    ai_pipeline_cache_ttl_seconds: int = 3600
    # When True, compose AI returns small JSON patches merged into template YAML (not full compose file).
    ai_compose_json_patch_enabled: bool = True
    # Minimum max_output_tokens floor for Pro generation (non-monorepo); capped by budget and
    # gemini_max_output_tokens_cap. Monorepos use ai_generation_output_floor_monorepo_tokens.
    ai_generation_output_floor_tokens: int = 4096
    ai_generation_output_floor_monorepo_tokens: int = 8192
    # When True and project is not high-complexity: use Flash for legacy single-shot generation.
    ai_generation_use_flash_for_simple: bool = False
    # After first Flash JSON repair failure, retry once with a shorter tail-focused prompt.
    ai_json_repair_second_attempt_enabled: bool = True
    # Dockerfile AI step gating: ``legacy`` uses the flags above; ``auto`` picks minimal/standard/thorough
    # tiers from fingerprint (monorepo, services, confidence, lockfile, multi-surface tree, etc.).
    ai_dockerfile_pipeline_mode: Literal["legacy", "auto"] = "auto"
    # Curated playbook hints (YAML + optional Redis). When false, skip hint collection for prompts.
    ai_playbook_hints_enabled: bool = True
    # Redis TTL for reinforced playbook keys (0 = skip Redis read/write; static YAML still applies).
    ai_playbook_hint_ttl_seconds: int = 604800
    # Reserved: embedding retrieval for playbook (not implemented; keep false).
    ai_playbook_rag_enabled: bool = False
    # Reserved: if prompt-side estimated tokens exceed this threshold, consider Gemini Files API
    # (not implemented in code yet; 0 = off). See README "Large repositories".
    gemini_files_api_prompt_token_threshold: int = 0

    # Admin reporter: LLM narrative over aggregate metrics only (never customer-triggered).
    reporter_llm_enabled: bool = False
    # When True: Celery Beat schedules periodic reporter runs (still no public API).
    reporter_beat_enabled: bool = False

    # --- Pipeline mode ---
    # legacy: single Dockerfile LangGraph (v1 behavior).
    # multi_service: per-service generation, compose, DeploymentManifest v1.
    pipeline_mode: Literal["legacy", "multi_service"] = "legacy"
    max_services_per_project: int = 8
    validate_worker_build: bool = False
    cloud_run_primary_only: bool = True
    ai_compose_patch_enabled: bool = False

    # --- Token Budget ---
    default_token_budget: int = 50_000
    max_token_budget: int = 100_000

    # --- Build ---
    # local_docker: subprocess docker against mounted socket (VPS celery-worker).
    # kaniko: GCS + Cloud Run Job + Kaniko (requires gcloud in worker + DF_GCP_PROJECT_ID).
    # skip: no image build; deploy step skipped when GCP unset (see orchestrator).
    build_backend: Literal["local_docker", "kaniko", "skip"] = "local_docker"
    max_build_attempts: int = 5
    build_timeout_seconds: int = 600
    build_cpu_limit: str = "2"
    build_memory_limit: str = "4Gi"

    # --- Cloud Run ---
    gcp_project_id: str = ""
    gcp_region: str = "europe-west1"
    cloud_run_timeout: int = 300
    cloud_run_max_instances: int = 1

    # --- Storage ---
    workspace_base_path: Path = Path("/tmp/deployforge/workspaces")
    max_upload_size_mb: int = 500
    artifact_retention_hours: int = 24

    # --- Security ---
    allowed_registries: list[str] = [
        "registry.npmjs.org",
        "pypi.org",
        "proxy.golang.org",
        "crates.io",
        "repo1.maven.org",
        "packagist.org",
        "rubygems.org",
    ]
    secret_patterns: list[str] = [
        r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]+['\"]",
        r"(?i)-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----",
        r"AKIA[0-9A-Z]{16}",
        r"(?i)(ghp|gho|github_pat)_[a-zA-Z0-9_]{20,}",
    ]

    # --- Policy ---
    dockerfile_policy_enabled: bool = True
    dockerfile_policy_fail_on_violations: bool = True
    compose_policy_fail_on_violations: bool = True

    # --- Lint / pre-build strictness ---
    lint_strict_mode: bool = False
    prebuild_require_cmd_or_entrypoint_error: bool = False

    # --- Admin ---
    admin_api_key: str = ""

    # --- Auth / registration ---
    # When True: POST /auth/register returns 501 until email verification (SMTP + tokens) is implemented.
    # Self-host / staging: keep False so users get an API key immediately.
    # Production later: set True after shipping the verify flow (do not forget).
    require_email_verification: bool = False

    # --- Rate Limiting (per API key, sliding window; see api/middleware/rate_limit.py) ---
    rate_limit_free: int = 50
    rate_limit_pro: int = 100
    rate_limit_window_seconds: int = 3600

    @property
    def sync_database_url(self) -> str:
        return self.database_url.replace("+asyncpg", "")

    @model_validator(mode="after")
    def _validate_build_backend(self) -> Self:
        if self.build_backend == "kaniko" and not (self.gcp_project_id or "").strip():
            raise ValueError(
                "DF_BUILD_BACKEND=kaniko requires DF_GCP_PROJECT_ID to be set "
                "(Kaniko uses GCS and Cloud Run Jobs in that project)."
            )
        return self


settings = Settings()
