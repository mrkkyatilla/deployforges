"""Unit tests for Dockerfile plan helpers."""

from __future__ import annotations

from core.ai.dockerfile_generator import (
    _dockerfile_is_plausible,
    _normalize_plain_dockerfile_response,
    _plan_is_usable,
)


def test_plan_is_usable_requires_base_and_stages() -> None:
    assert _plan_is_usable(None) is False
    assert _plan_is_usable({}) is False
    assert _plan_is_usable({"base_image": "", "stages": ["a"]}) is False
    assert _plan_is_usable({"base_image": "python:3.12-slim", "stages": []}) is False
    assert _plan_is_usable({"base_image": "python:3.12-slim", "stages": ["deps", "runtime"]}) is True


def test_normalize_plain_body_strips_preamble_and_fence() -> None:
    raw = "Here is the Dockerfile:\n\n```dockerfile\nFROM node:20-bookworm-slim\nRUN echo hi\n```\n"
    out = _normalize_plain_dockerfile_response(raw)
    assert out.startswith("FROM ")
    assert _dockerfile_is_plausible(out)


def test_normalize_plain_body_json_wrapper() -> None:
    raw = '{"dockerfile": "FROM alpine:3.19\\nRUN true\\n"}'
    out = _normalize_plain_dockerfile_response(raw)
    assert out.startswith("FROM alpine")
    assert _dockerfile_is_plausible(out)


def test_normalize_plain_body_leading_comment_then_from() -> None:
    raw = "# build\nFROM scratch AS empty\n"
    out = _normalize_plain_dockerfile_response(raw)
    assert out.startswith("FROM scratch")
    assert _dockerfile_is_plausible(out)
