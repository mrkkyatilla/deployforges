"""Build log excerpt helpers."""

from __future__ import annotations

from core.error.parser import extract_error_lines


def test_extract_error_lines_includes_uv_context() -> None:
    log = """Step 5/10 : RUN uv pip sync --system uv.lock
 ---> Running in abc123
error: unexpected argument
The command '/bin/sh -c uv pip sync --system uv.lock' returned a non-zero code: 2
"""
    out = extract_error_lines(log)
    assert "non-zero" in out.lower() or "unexpected argument" in out.lower()
