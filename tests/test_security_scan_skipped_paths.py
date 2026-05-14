"""Security scanner skips tutorial/test trees (Flask-style false positives)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.intake.security_scan import SecurityScanner, _is_skipped_scan_path


def test_is_skipped_scan_path_examples_and_tests() -> None:
    assert _is_skipped_scan_path("examples/tutorial/tests/conftest.py") is True
    assert _is_skipped_scan_path("docs/tutorial/tests.rst") is True
    assert _is_skipped_scan_path("tests/test_apps/.env") is True
    assert _is_skipped_scan_path("src/app/main.py") is False


@pytest.mark.asyncio
async def test_scan_allows_fake_secret_in_examples(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    ex = root / "examples" / "demo.txt"
    ex.parent.mkdir(parents=True)
    ex.write_text('api_key = "not-a-real-secret-for-tutorial"\n', encoding="utf-8")
    app = root / "app.py"
    app.write_text("print('ok')\n", encoding="utf-8")

    scanner = SecurityScanner()
    result = await scanner.scan(root)
    assert result.is_safe is True
    assert result.secrets_found == []


@pytest.mark.asyncio
async def test_scan_still_flags_secret_at_repo_root(tmp_path: Path) -> None:
    root = tmp_path / "proj2"
    root.mkdir()
    cfg = root / "config.py"
    cfg.write_text('DATABASE_PASSWORD = "supersecret123"\n', encoding="utf-8")

    scanner = SecurityScanner()
    result = await scanner.scan(root)
    assert result.is_safe is False
    assert result.secrets_found
