"""Regression: analysis + template/lint/prebuild without Gemini (see tests/regression_manifest.yaml)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.ai.dockerfile_linter import DockerfileLinter
from core.ai.templates import fingerprint_allows_template_first, render_template_dockerfile
from core.analysis.engine import AnalysisEngine
from core.builder.sandbox import PreBuildValidator
from core.security.dockerfile_policy import check_dockerfile_policy

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MANIFEST = Path(__file__).resolve().parent / "regression_manifest.yaml"


def _manifest_paths() -> list[Path]:
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    cases = (data or {}).get("cases") or []
    expected: list[Path] = []
    for c in cases:
        rel = c.get("path")
        if not rel:
            continue
        expected.append(FIXTURES / str(rel))
    missing = [p for p in expected if not p.is_dir()]
    assert not missing, f"Regression manifest lists missing fixture dirs: {[str(m) for m in missing]}"
    assert len(expected) >= 8, "regression manifest should list at least 8 cases"
    return expected


@pytest.mark.regression
@pytest.mark.asyncio
async def test_regression_analyze_lint_prebuild_policy() -> None:
    engine = AnalysisEngine()
    linter = DockerfileLinter()
    validator = PreBuildValidator()

    for root in _manifest_paths():
        fp = (await engine.analyze(str(root))).to_dict()
        assert fp.get("language")

        if fingerprint_allows_template_first(str(root), fp):
            df = render_template_dockerfile(str(root), fp)
            assert df and "FROM" in df.upper()
        else:
            df = (
                "FROM alpine:3.19\n"
                "WORKDIR /app\n"
                "RUN echo ok > /app/readme.txt\n"
                "USER nobody\n"
                "EXPOSE 8080\n"
                "HEALTHCHECK CMD true\n"
                "CMD [\"cat\", \"/app/readme.txt\"]\n"
            )

        assert not check_dockerfile_policy(df), f"policy violations for {root.name}"
        port = None
        pi = fp.get("port")
        if isinstance(pi, dict) and pi.get("value") is not None:
            try:
                port = int(pi["value"])
            except (TypeError, ValueError):
                port = None
        lint = linter.lint(df, port=port)
        body = lint.fixed_dockerfile or df
        assert lint.is_valid or lint.fixed_dockerfile, f"lint failed for {root.name}"

        pre = await validator.validate(str(root), body)
        assert pre.can_build, f"prebuild failed for {root.name}: {[e.details for e in pre.errors if e.is_error]}"


def test_compose_merge_patches() -> None:
    from core.ai.compose_generator import ComposeGenerator

    gen = ComposeGenerator()
    base = {"services": {"api": {"image": "nginx:1.27-alpine", "ports": ["8080:8080"]}}}
    patches = [
        {"service": "api", "environment": [{"name": "FOO", "value": "bar"}], "ports": ["9090:9090"]},
    ]
    gen._merge_compose_patches(base, patches)  # noqa: SLF001
    api = base["services"]["api"]
    assert api["environment"]["FOO"] == "bar"
    assert "9090:9090" in api["ports"]
