"""Pre-build Dockerfile checks (context COPY vs multi-stage --from)."""

from __future__ import annotations

from pathlib import Path

from core.builder.sandbox import PreBuildValidator


def _referenced_file_errors(result) -> list[str]:
    return [e.details for e in result.errors if e.is_error and e.name == "referenced_files"]


async def test_copy_from_stage_paths_not_checked_against_workspace(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname=x\n")
    dockerfile = (
        "FROM alpine:3.19 AS builder\n"
        "RUN echo hi > /opt/artifact\n"
        "FROM alpine:3.19\n"
        "COPY --from=builder /opt/artifact /artifact\n"
    )
    v = PreBuildValidator()
    r = await v.validate(str(tmp_path), dockerfile)
    assert not _referenced_file_errors(r)


async def test_copy_missing_context_file_still_fails(tmp_path: Path) -> None:
    dockerfile = "FROM alpine:3.19\nCOPY nope.txt /\n"
    v = PreBuildValidator()
    r = await v.validate(str(tmp_path), dockerfile)
    errs = _referenced_file_errors(r)
    assert errs and any("nope.txt" in e for e in errs)


async def test_add_url_not_checked_as_file(tmp_path: Path) -> None:
    dockerfile = "FROM alpine:3.19\nADD https://example.com/x.txt /tmp/x\n"
    v = PreBuildValidator()
    r = await v.validate(str(tmp_path), dockerfile)
    assert not _referenced_file_errors(r)
