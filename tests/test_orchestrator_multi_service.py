"""T3: multi-service graph compiles and resolve node."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.ai.orchestrator_multi import build_multi_service_graph, resolve_services_node
from core.analysis.engine import AnalysisEngine

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_multi_service_graph_compiles() -> None:
    g = build_multi_service_graph()
    assert g is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_services_node() -> None:
    root = FIXTURES / "monorepo_fullstack"
    engine = AnalysisEngine()
    fp = (await engine.analyze(str(root))).to_dict()
    state = {
        "project_id": "test-proj",
        "project_path": str(root),
        "fingerprint": fp,
    }
    out = await resolve_services_node(state)  # type: ignore[arg-type]
    assert len(out.get("resolved_services") or []) >= 2
    assert out.get("primary_service")
