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


def _manifest_cases() -> list[dict]:
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    cases = (data or {}).get("cases") or []
    out: list[dict] = []
    for c in cases:
        rel = c.get("path")
        if not rel:
            continue
        root = FIXTURES / str(rel)
        if not root.is_dir():
            raise AssertionError(f"missing fixture: {root}")
        out.append({"root": root, "service": c.get("service"), "id": c.get("id", rel)})
    assert len(out) >= 8, "regression manifest should list at least 8 cases"
    return out


@pytest.mark.regression
@pytest.mark.asyncio
async def test_regression_analyze_lint_prebuild_policy() -> None:
    engine = AnalysisEngine()
    linter = DockerfileLinter()
    validator = PreBuildValidator()

    for case in _manifest_cases():
        root = case["root"]
        svc = case.get("service")
        label = case.get("id", root.name)
        if svc:
            fp_obj = await engine.analyze_service(str(root), str(svc), service_name=str(svc))
            fp = fp_obj.to_dict()
            work_root = str(root)
            allows = fingerprint_allows_template_first(work_root, fp, str(svc))
            render = lambda: render_template_dockerfile(work_root, fp, service_root=str(svc))
            pre_root = str((root / str(svc)).resolve())
        else:
            fp = (await engine.analyze(str(root))).to_dict()
            assert fp.get("language")
            allows = fingerprint_allows_template_first(str(root), fp)
            render = lambda: render_template_dockerfile(str(root), fp)
            pre_root = str(root)

        if allows:
            df = render()
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

        assert not check_dockerfile_policy(df), f"policy violations for {label}"
        port = None
        pi = fp.get("port")
        if isinstance(pi, dict) and pi.get("value") is not None:
            try:
                port = int(pi["value"])
            except (TypeError, ValueError):
                port = None
        lint = linter.lint(df, port=port)
        body = lint.fixed_dockerfile or df
        assert lint.is_valid or lint.fixed_dockerfile, f"lint failed for {label}"

        pre = await validator.validate(pre_root, body)
        assert pre.can_build, f"prebuild failed for {label}: {[e.details for e in pre.errors if e.is_error]}"


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
