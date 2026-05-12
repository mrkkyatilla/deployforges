from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="DF_",
        case_sensitive=False,
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

    # --- Gemini AI ---
    gemini_api_key: str = ""
    gemini_pro_model: str = "gemini-2.5-pro"
    gemini_flash_model: str = "gemini-2.5-flash"
    gemini_max_retries: int = 3
    gemini_timeout: int = 60

    # --- Token Budget ---
    default_token_budget: int = 50_000
    max_token_budget: int = 100_000

    # --- Build ---
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
    ]

    # --- Admin ---
    admin_api_key: str = ""

    # --- Rate Limiting ---
    rate_limit_free: int = 10
    rate_limit_pro: int = 100
    rate_limit_window_seconds: int = 3600

    @property
    def sync_database_url(self) -> str:
        return self.database_url.replace("+asyncpg", "")


settings = Settings()
