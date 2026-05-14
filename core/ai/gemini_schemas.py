"""Gemini ``response_schema`` (structured output) definitions for JSON responses."""

from __future__ import annotations

from google.genai import types

_T = types.Type


def schema_ai_analysis() -> types.Schema:
    """Matches ``AI_ANALYSIS_SYSTEM_PROMPT`` output shape."""
    str_item = types.Schema(type=_T.STRING)
    return types.Schema(
        type=_T.OBJECT,
        properties={
            "language": types.Schema(type=_T.STRING),
            "language_version": types.Schema(type=_T.STRING, nullable=True),
            "framework": types.Schema(type=_T.STRING, nullable=True),
            "framework_version": types.Schema(type=_T.STRING, nullable=True),
            "entrypoint_file": types.Schema(type=_T.STRING, nullable=True),
            "start_command": types.Schema(type=_T.STRING, nullable=True),
            "port": types.Schema(type=_T.INTEGER, nullable=True),
            "additional_system_deps": types.Schema(
                type=_T.ARRAY,
                items=str_item,
                nullable=True,
            ),
            "warnings": types.Schema(type=_T.ARRAY, items=str_item),
            "confidence": types.Schema(type=_T.NUMBER),
        },
        required=[
            "language",
            "warnings",
            "confidence",
        ],
    )


def schema_dockerfile_plan() -> types.Schema:
    """Structured build plan before Dockerfile JSON generation."""
    str_item = types.Schema(type=_T.STRING)
    return types.Schema(
        type=_T.OBJECT,
        properties={
            "base_image": types.Schema(
                type=_T.STRING,
                description="OCI image reference with explicit tag (not :latest).",
            ),
            "stages": types.Schema(
                type=_T.ARRAY,
                items=str_item,
                description='Short stage labels only, e.g. "deps", "builder", "runtime".',
            ),
            "copy_strategy": types.Schema(type=_T.STRING),
            "install_commands_outline": types.Schema(type=_T.ARRAY, items=str_item),
            "cmd": types.Schema(type=_T.STRING),
            "healthcheck": types.Schema(type=_T.STRING, nullable=True),
            "non_root": types.Schema(type=_T.BOOLEAN),
            "notes": types.Schema(type=_T.STRING, nullable=True),
        },
        required=[
            "base_image",
            "stages",
            "copy_strategy",
            "install_commands_outline",
            "cmd",
            "non_root",
        ],
    )


def schema_dockerfile_critic() -> types.Schema:
    """Post-generation static review issues (no full Dockerfile in output)."""
    str_item = types.Schema(type=_T.STRING)
    issue = types.Schema(
        type=_T.OBJECT,
        properties={
            "severity": types.Schema(type=_T.STRING),
            "title": types.Schema(type=_T.STRING),
            "detail": types.Schema(type=_T.STRING),
            "suggested_change": types.Schema(type=_T.STRING, nullable=True),
        },
        required=["severity", "title", "detail"],
    )
    return types.Schema(
        type=_T.OBJECT,
        properties={
            "summary": types.Schema(type=_T.STRING),
            "issues": types.Schema(type=_T.ARRAY, items=issue),
        },
        required=["summary", "issues"],
    )


def schema_reporter_analysis() -> types.Schema:
    """Admin reporter LLM output from aggregate metrics only."""
    str_item = types.Schema(type=_T.STRING)
    return types.Schema(
        type=_T.OBJECT,
        properties={
            "executive_summary": types.Schema(type=_T.STRING),
            "token_optimization_recommendations": types.Schema(
                type=_T.ARRAY, items=str_item
            ),
            "risks_or_anomalies": types.Schema(type=_T.ARRAY, items=str_item),
            "model_routing_suggestion": types.Schema(type=_T.STRING, nullable=True),
        },
        required=[
            "executive_summary",
            "token_optimization_recommendations",
            "risks_or_anomalies",
        ],
    )


def schema_dockerfile_generation_metadata() -> types.Schema:
    """Phase-1 JSON: same fields as full generation except embedded ``dockerfile`` string."""
    str_item = types.Schema(type=_T.STRING)
    int_item = types.Schema(type=_T.INTEGER)
    esc = (
        "Plain UTF-8 text only inside JSON strings. Escape every double-quote inside a string as \\\". "
        "Do not put raw newlines inside JSON strings; use \\n if needed."
    )
    return types.Schema(
        type=_T.OBJECT,
        properties={
            "analysis_summary": types.Schema(
                type=_T.STRING,
                description=f"3–5 sentence summary; Dockerfile body is emitted in a separate step. {esc}",
            ),
            "dockerignore": types.Schema(type=_T.STRING, description=f".dockerignore body. {esc}"),
            "warnings": types.Schema(type=_T.ARRAY, items=str_item),
            "exposed_ports": types.Schema(type=_T.ARRAY, items=int_item),
            "estimated_image_size_mb": types.Schema(type=_T.INTEGER, nullable=True),
            "requires_env_vars": types.Schema(type=_T.ARRAY, items=str_item, nullable=True),
        },
        required=[
            "analysis_summary",
            "dockerignore",
            "warnings",
            "exposed_ports",
        ],
    )


def schema_dockerfile_generation() -> types.Schema:
    """Matches ``MASTER_SYSTEM_PROMPT`` JSON shape."""
    str_item = types.Schema(type=_T.STRING)
    int_item = types.Schema(type=_T.INTEGER)
    return types.Schema(
        type=_T.OBJECT,
        properties={
            "analysis_summary": types.Schema(
                type=_T.STRING,
                description=(
                    "Brief technical summary. The dockerfile field must include "
                    "short explanatory # comments before each major stage (FROM, deps, app, runtime). "
                    "Inside JSON strings escape every literal double-quote as \\\" and avoid raw newlines "
                    "(use \\n). Unescaped quotes break parsing."
                ),
            ),
            "dockerfile": types.Schema(
                type=_T.STRING,
                description=(
                    "Full Dockerfile; include # comments before each major stage. "
                    "This value is a JSON string: escape internal \" as \\\" or the response becomes invalid."
                ),
            ),
            "dockerignore": types.Schema(
                type=_T.STRING,
                description="Entire .dockerignore; escape quotes if any appear in paths.",
            ),
            "warnings": types.Schema(type=_T.ARRAY, items=str_item),
            "exposed_ports": types.Schema(type=_T.ARRAY, items=int_item),
            "estimated_image_size_mb": types.Schema(type=_T.INTEGER, nullable=True),
            "requires_env_vars": types.Schema(type=_T.ARRAY, items=str_item, nullable=True),
        },
        required=[
            "analysis_summary",
            "dockerfile",
            "dockerignore",
            "warnings",
            "exposed_ports",
        ],
    )


def schema_dockerfile_fix() -> types.Schema:
    """Matches ``ERROR_FIX_SYSTEM_PROMPT`` JSON shape."""
    str_item = types.Schema(type=_T.STRING)
    esc = (
        "Inside JSON strings escape every literal double-quote as \\\" and avoid raw newlines (use \\n). "
        "Unterminated strings break parsing."
    )
    return types.Schema(
        type=_T.OBJECT,
        properties={
            "analysis_summary": types.Schema(
                type=_T.STRING,
                description=f"What failed and what changed. {esc}",
            ),
            "dockerfile": types.Schema(
                type=_T.STRING,
                description=f"Complete fixed Dockerfile. {esc}",
            ),
            "dockerignore": types.Schema(
                type=_T.STRING,
                description=f".dockerignore body. {esc}",
            ),
            "warnings": types.Schema(type=_T.ARRAY, items=str_item),
            "changes_made": types.Schema(type=_T.ARRAY, items=str_item),
        },
        required=[
            "analysis_summary",
            "dockerfile",
            "dockerignore",
            "warnings",
            "changes_made",
        ],
    )


def schema_compose_generation() -> types.Schema:
    """Compose AI: compose_yml + warnings."""
    str_item = types.Schema(type=_T.STRING)
    return types.Schema(
        type=_T.OBJECT,
        properties={
            "compose_yml": types.Schema(type=_T.STRING),
            "warnings": types.Schema(type=_T.ARRAY, items=str_item),
        },
        required=["compose_yml", "warnings"],
    )


def schema_compose_service_patches() -> types.Schema:
    """Small JSON: per-service overrides merged into template compose (not full YAML)."""
    str_item = types.Schema(type=_T.STRING)
    env_pair = types.Schema(
        type=_T.OBJECT,
        properties={
            "name": types.Schema(type=_T.STRING),
            "value": types.Schema(type=_T.STRING),
        },
        required=["name", "value"],
    )
    patch_item = types.Schema(
        type=_T.OBJECT,
        properties={
            "service": types.Schema(type=_T.STRING, description="Service name matching fingerprint"),
            "environment": types.Schema(
                type=_T.ARRAY,
                items=env_pair,
                description="Extra env vars to merge into the service",
            ),
            "ports": types.Schema(
                type=_T.ARRAY,
                items=str_item,
                description='Port mappings like "8080:8080"',
            ),
        },
        required=["service"],
    )
    return types.Schema(
        type=_T.OBJECT,
        properties={
            "patches": types.Schema(
                type=_T.ARRAY,
                items=patch_item,
                description="Overrides merged into template compose services",
            ),
            "warnings": types.Schema(type=_T.ARRAY, items=str_item),
        },
        required=["patches", "warnings"],
    )
