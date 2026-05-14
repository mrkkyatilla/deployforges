"""Unit tests for Dockerfile pipeline policy resolution and related helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.ai.dockerfile_linter import DockerfileLinter
from core.ai.dockerfile_pipeline_policy import (
    extract_stage_names_from_dockerfile_template,
    resolve_dockerfile_pipeline_policy,
)


@pytest.fixture
def legacy_settings() -> SimpleNamespace:
    return SimpleNamespace(
        ai_dockerfile_pipeline_mode="legacy",
        ai_dockerfile_plan_enabled=True,
        ai_dockerfile_plan_json_repair_enabled=True,
        ai_dockerfile_critic_refine_enabled=False,
        ai_json_repair_second_attempt_enabled=True,
    )


def test_legacy_respects_critic_master_flag(legacy_settings: SimpleNamespace) -> None:
    p = resolve_dockerfile_pipeline_policy({}, legacy_settings)
    assert p.mode == "legacy"
    assert p.critic_enabled is False
    assert p.refine_enabled is False
    legacy_settings.ai_dockerfile_critic_refine_enabled = True
    p2 = resolve_dockerfile_pipeline_policy({}, legacy_settings)
    assert p2.critic_enabled is True
    assert p2.refine_enabled is True


def test_auto_minimal_tier() -> None:
    settings = SimpleNamespace(ai_dockerfile_pipeline_mode="auto")
    fp = {
        "confidence": 0.9,
        "is_monorepo": False,
        "services": [],
        "dependencies": {"lock_file_exists": True},
        "language": {"primary": "python", "secondary": []},
        "warnings": [],
        "file_tree": {"nodes": []},
    }
    p = resolve_dockerfile_pipeline_policy(fp, settings)
    assert p.tier == "minimal"
    assert p.plan_enabled is False
    assert p.critic_enabled is False


def test_auto_thorough_low_confidence_monorepo() -> None:
    settings = SimpleNamespace(ai_dockerfile_pipeline_mode="auto")
    fp = {
        "confidence": 0.5,
        "is_monorepo": True,
        "services": [{"name": "a", "type": "web", "root_path": ".", "port": 8000}],
        "dependencies": {"lock_file_exists": False},
        "language": {"primary": "python", "secondary": []},
        "warnings": [],
        "file_tree": {"nodes": []},
    }
    p = resolve_dockerfile_pipeline_policy(fp, settings)
    assert p.tier == "thorough"
    assert p.refine_enabled is True
    assert p.json_repair_second_attempt_enabled is True


def test_auto_standard_multi_service() -> None:
    settings = SimpleNamespace(ai_dockerfile_pipeline_mode="auto")
    fp = {
        "confidence": 0.72,
        "is_monorepo": False,
        "services": [
            {"name": "api", "type": "api", "root_path": "apps/api", "port": 8080},
            {"name": "web", "type": "web", "root_path": "apps/web", "port": 3000},
        ],
        "dependencies": {"lock_file_exists": True},
        "language": {"primary": "typescript", "secondary": []},
        "warnings": [],
        "file_tree": {"nodes": []},
    }
    p = resolve_dockerfile_pipeline_policy(fp, settings)
    assert p.tier == "standard"
    assert p.plan_enabled is True
    assert p.critic_enabled is True
    assert p.refine_enabled is False


def test_extract_stage_names_from_template() -> None:
    tpl = "FROM node:20 AS builder\nWORKDIR /app\nFROM node:20-alpine AS runner\n"
    assert extract_stage_names_from_dockerfile_template(tpl) == ["builder", "runner"]


def test_linter_workdir_missing() -> None:
    linter = DockerfileLinter()
    dockerfile = "FROM python:3.12-slim\nUSER nobody\nCOPY . .\nCMD [\"python\", \"-m\", \"http.server\"]\n"
    r = linter.lint(dockerfile, port=8000)
    assert any(i.rule == "DF010" for i in r.issues)


def test_linter_copy_from_unknown_stage() -> None:
    linter = DockerfileLinter()
    dockerfile = (
        "FROM python:3.12-slim AS builder\n"
        "WORKDIR /app\n"
        "FROM python:3.12-slim\n"
        "WORKDIR /app\n"
        "COPY --from=nosuch /tmp /out\n"
        "USER nobody\n"
    )
    r = linter.lint(dockerfile, port=None)
    assert any(i.rule == "DF011" for i in r.issues)
