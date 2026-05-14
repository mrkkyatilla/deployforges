"""Classifier + Dockerfile patch for setuptools 'Description file … does not exist'."""

from __future__ import annotations

from core.ai import orchestrator as orch
from core.error.classifier import BuildErrorClassifier


def test_classifier_matches_description_file_error() -> None:
    log = "gunicorn_app_import_or_uri: Error: Description file README.md does not exist"
    c = BuildErrorClassifier()
    errs = c.classify(log, language="python")
    names = [e.name for e in errs]
    assert "setuptools_description_file_missing" in names
    assert any(
        e.name == "setuptools_description_file_missing" and e.fix_strategy == "add_description_file_copy"
        for e in errs
    )


def test_insert_copy_readme_before_user_final_stage() -> None:
    df = (
        "FROM python:3.10-slim AS builder\n"
        "WORKDIR /app\n"
        "FROM python:3.10-slim\n"
        "WORKDIR /app\n"
        "COPY src/ ./src/\n"
        "USER app\n"
        'CMD ["gunicorn"]\n'
    )
    out = orch._insert_copy_for_description_file(df, "README.md")
    assert "COPY README.md /app/README.md" in out
    assert out.index("COPY README.md") < out.index("USER app")


def test_rewrites_readme_copy_after_nested_workdir() -> None:
    """README must live next to pyproject (first WORKDIR), not under a later WORKDIR."""
    df = (
        "FROM python:3.10-slim\n"
        "WORKDIR /app\n"
        "COPY --chown=app:app . .\n"
        "WORKDIR /app/examples/tutorial\n"
        "COPY README.md ./\n"
        "USER app\n"
        "CMD gunicorn\n"
    )
    out = orch._insert_copy_for_description_file(df, "README.md")
    assert "COPY README.md /app/README.md" in out
    assert "COPY README.md ./" not in out


def test_insert_skips_if_already_present_at_repo_root() -> None:
    df = (
        "FROM python:3.10-slim\n"
        "WORKDIR /app\n"
        "COPY README.md /app/README.md\n"
        "USER app\n"
    )
    out = orch._insert_copy_for_description_file(df, "README.md")
    assert out.count("COPY README.md /app/README.md") == 1


def test_description_file_re_matches_setuptools_message() -> None:
    blob = "Error: Description file README.md does not exist"
    m = orch._DESCRIPTION_FILE_RE.search(blob)
    assert m is not None
    assert m.group(1) == "README.md"
