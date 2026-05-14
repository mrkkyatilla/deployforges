"""Tests for orchestrator crash-path error summaries shown to API users."""

from core.ai.orchestrator import _pipeline_crash_error_summary


def test_503_high_demand_message():
    inner = Exception(
        "503 UNAVAILABLE. This model is currently experiencing high demand."
    )
    try:
        raise RuntimeError("Gemini API failed after 3 retries") from inner
    except Exception as exc:
        msg = _pipeline_crash_error_summary(exc)
    assert "503" in msg
    assert "peak load" in msg or "Retry" in msg


def test_429_resource_exhausted():
    inner = Exception("429 RESOURCE_EXHAUSTED quota")
    try:
        raise RuntimeError("failed") from inner
    except Exception as exc:
        msg = _pipeline_crash_error_summary(exc)
    assert "rate limit" in msg.lower() or "quota" in msg.lower()


def test_generic_uses_root_cause_line():
    try:
        raise ValueError("something broke in node X") from OSError("disk full")
    except Exception as exc:
        msg = _pipeline_crash_error_summary(exc)
    assert "disk full" in msg
