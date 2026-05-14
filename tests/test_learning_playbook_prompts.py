"""Unit tests for structured ``error_analysis`` v1, playbook static hints, and pyproject prompt rules."""

from __future__ import annotations

from core.ai.playbook_hints import playbook_redis_key, static_hints_for_fingerprint
from core.ai.prompts.dockerfile import (
    build_generation_metadata_prompt,
    build_dockerfile_body_only_prompt,
)
from core.learning.build_error_analysis import (
    ERROR_ANALYSIS_SCHEMA_VERSION,
    build_error_analysis_v1,
    success_error_analysis_v1,
)


def test_build_error_analysis_v1_shape() -> None:
    payload = build_error_analysis_v1(
        classified_errors=[
            {
                "name": "setuptools_description_file_missing",
                "fix_strategy": "add_description_file_copy",
                "auto_fixable": True,
                "error_type": "build",
            }
        ],
        pipeline_policy={"tier": "standard", "mode": "auto", "signals": ["x"]},
        fixes_applied=["strategy:add_copy"],
        deploy_error_excerpt="E" * 3000,
    )
    assert payload["schema_version"] == ERROR_ANALYSIS_SCHEMA_VERSION
    assert payload["type"] == "setuptools_description_file_missing"
    assert "setuptools_description_file_missing" in payload["summary"]
    assert len(payload["classified"]) == 1
    assert payload["classified"][0]["fix_strategy"] == "add_description_file_copy"
    assert payload["pipeline_policy"]["tier"] == "standard"
    assert payload["fixes_applied"] == ["strategy:add_copy"]
    assert len(payload["deploy_error_excerpt"]) == 2000


def test_success_error_analysis_v1() -> None:
    doc = success_error_analysis_v1()
    assert doc["schema_version"] == ERROR_ANALYSIS_SCHEMA_VERSION
    assert doc["outcome"] == "success"


def test_static_hints_python_pyproject_manager() -> None:
    fp = {
        "language": {"primary": "python"},
        "dependencies": {"manager": "pyproject"},
    }
    hints = static_hints_for_fingerprint(fp)
    assert hints
    assert any("README" in h or "readme" in h.lower() for h in hints)


def test_playbook_redis_key_stable() -> None:
    k = playbook_redis_key("python", "Flask", "setuptools_description_file_missing")
    assert k.startswith("df:playbook:v1:python:flask:setuptools_description_file_missing")


def test_pyproject_copy_rule_in_metadata_prompt() -> None:
    fp = {
        "language": {"primary": "python"},
        "dependencies": {"manager": "uv"},
        "file_tree": {"nodes": [{"path": "pyproject.toml"}]},
    }
    text = build_generation_metadata_prompt(fp, playbook_hints=["Test hint one"])
    assert "Python pyproject packaging" in text
    assert "Test hint one" in text


def test_pyproject_rule_absent_for_node_only() -> None:
    fp = {"language": {"primary": "node"}, "dependencies": {"manager": "npm"}}
    text = build_dockerfile_body_only_prompt(
        fp, template=None, plan=None, metadata={"base_image": "node:20"}
    )
    assert "Python pyproject packaging" not in text
