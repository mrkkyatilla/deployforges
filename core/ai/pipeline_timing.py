"""Pipeline step timing labels for SSE and aggregated metrics."""

from __future__ import annotations

import time
from typing import Any

# Bilingual short labels for SSE clients (TR primary, EN secondary).
STEP_LABELS: dict[str, tuple[str, str]] = {
    "intake": ("Kaynak indiriliyor / açılıyor", "Fetching and extracting source"),
    "analyze": ("Proje analizi (deterministik)", "Deterministic project analysis"),
    "ai_analyze": ("AI ile analiz tamamlama", "AI-assisted analysis"),
    "generate_dockerfile": ("Dockerfile üretimi", "Dockerfile generation"),
    "dockerfile_critic": ("Dockerfile gözden geçirme", "Dockerfile critic"),
    "dockerfile_refine": ("Dockerfile iyileştirme", "Dockerfile refine"),
    "lint_check": ("Dockerfile lint", "Dockerfile lint"),
    "auto_fix_lint": ("Lint otomatik düzeltme", "Auto-fix lint issues"),
    "pre_build_validate": ("Ön derleme doğrulama", "Pre-build validation"),
    "policy_check": ("Güvenlik politikası", "Security policy check"),
    "build": ("Docker imaj derlemesi", "Docker image build"),
    "deploy_and_test": ("Dağıtım ve smoke test", "Deploy and smoke test"),
    "classify_error": ("Hata sınıflandırma", "Error classification"),
    "auto_fix_build": ("Deterministik derleme düzeltmesi", "Deterministic build fix"),
    "ai_fix_build": ("AI derleme düzeltmesi", "AI build fix"),
}


def step_progress_text(step: str, *, phase: str, elapsed_ms: int | None = None) -> str:
    """Human-readable one-line progress for SSE consumers."""
    tr, en = STEP_LABELS.get(step, (step, step))
    bits = [f"[{phase}] {tr}"]
    if elapsed_ms is not None:
        bits.append(f" ({elapsed_ms} ms)")
    bits.append(f" — {en}")
    return "".join(bits)


def timing_row(
    step: str,
    t0: float,
    *,
    tokens_at_start: int | None = None,
    tokens_at_end: int | None = None,
) -> dict[str, Any]:
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    row: dict[str, Any] = {"step": step, "elapsed_ms": elapsed_ms}
    if tokens_at_start is not None and tokens_at_end is not None:
        row["tokens_delta"] = max(0, tokens_at_end - tokens_at_start)
    return row


def emit_timing_fields(
    step: str,
    t0: float,
    *,
    tokens_at_start: int | None = None,
    tokens_at_end: int | None = None,
) -> dict[str, Any]:
    """Fields merged into Redis ``step_*`` payloads."""
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    out: dict[str, Any] = {
        "elapsed_ms": elapsed_ms,
        "progress_text": step_progress_text(step, phase="done", elapsed_ms=elapsed_ms),
    }
    if tokens_at_start is not None and tokens_at_end is not None:
        out["tokens_delta"] = max(0, tokens_at_end - tokens_at_start)
        out["total_tokens_used"] = tokens_at_end
    elif tokens_at_end is not None:
        out["total_tokens_used"] = tokens_at_end
    return out


def emit_step_start_fields(step: str) -> dict[str, Any]:
    return {
        "progress_text": step_progress_text(step, phase="start", elapsed_ms=None),
    }
