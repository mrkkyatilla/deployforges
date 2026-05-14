"""Dockerfile linter: Gunicorn WSGI target syntax."""

from __future__ import annotations

from core.ai.dockerfile_linter import DockerfileLinter


def test_df009_flags_create_app_with_parens() -> None:
    df = """FROM python:3.11-slim
USER nobody
EXPOSE 8000
HEALTHCHECK CMD curl -f http://localhost:8000/ || exit 1
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "flaskr:create_app()"]
"""
    r = DockerfileLinter().lint(df, port=8000)
    codes = {i.rule for i in r.issues}
    assert "DF009" in codes
    assert r.fixed_dockerfile
    assert "flaskr:create_app()" not in r.fixed_dockerfile
    assert "flaskr:create_app" in r.fixed_dockerfile


def test_df009_no_false_positive_without_parens() -> None:
    df = """FROM python:3.11-slim
USER nobody
EXPOSE 8000
HEALTHCHECK CMD curl -f http://localhost:8000/ || exit 1
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "flaskr:create_app"]
"""
    r = DockerfileLinter().lint(df, port=8000)
    assert "DF009" not in {i.rule for i in r.issues}
