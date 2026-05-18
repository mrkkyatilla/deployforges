"""T0: deterministic manifest builder."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.analysis.engine import AnalysisEngine
from core.analysis.service_resolver import resolve_services
from core.manifest.builder import build_deployment_manifest

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.mark.asyncio
async def test_build_manifest_from_monorepo() -> None:
    root = FIXTURES / "monorepo_fullstack"
    engine = AnalysisEngine()
    root_fp = (await engine.analyze(str(root))).to_dict()
    resolved, _ = resolve_services(str(root), root_fp)
    svc_fps = {}
    for s in resolved:
        if s.type == "database":
            continue
        fp = await engine.analyze_service(str(root), s.root_path, service_name=s.name)
        svc_fps[s.name] = fp.to_dict()

    manifest = build_deployment_manifest(
        root_fingerprint=root_fp,
        resolved_services=[s.to_dict() for s in resolved],
        service_fingerprints=svc_fps,
        source_type="git",
    )
    assert manifest.deployment_manifest_version == "1"
    assert len(manifest.services) >= 2
    assert manifest.validation.primary_service
