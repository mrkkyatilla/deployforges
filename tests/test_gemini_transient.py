"""Gemini transient detection and orchestrator crash classification."""

from core.ai.gemini_client import is_transient_gemini_http_error, _retry_delay_seconds
from core.ai.orchestrator import _is_transient_gemini_crash


def test_is_transient_503_high_demand():
    exc = Exception(
        "503 UNAVAILABLE. This model is currently experiencing high demand."
    )
    assert is_transient_gemini_http_error(exc) is True


def test_is_transient_429():
    assert is_transient_gemini_http_error(Exception("429 RESOURCE_EXHAUSTED")) is True


def test_is_transient_not_400():
    assert is_transient_gemini_http_error(Exception("400 INVALID_ARGUMENT")) is False


def test_is_transient_none():
    assert is_transient_gemini_http_error(None) is False


def test_retry_delay_bounded():
    d = _retry_delay_seconds(5, Exception("503 UNAVAILABLE high demand"))
    assert 1.0 <= d <= 160.0


def test_transient_crash_chain():
    inner = Exception("503 UNAVAILABLE high demand")
    try:
        raise RuntimeError("Gemini API failed after 3 retries") from inner
    except Exception as exc:
        assert _is_transient_gemini_crash(exc) is True
