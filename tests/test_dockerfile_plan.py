"""Unit tests for Dockerfile plan helpers."""

from __future__ import annotations

from core.ai.dockerfile_generator import _plan_is_usable


def test_plan_is_usable_requires_base_and_stages() -> None:
    assert _plan_is_usable(None) is False
    assert _plan_is_usable({}) is False
    assert _plan_is_usable({"base_image": "", "stages": ["a"]}) is False
    assert _plan_is_usable({"base_image": "python:3.12-slim", "stages": []}) is False
    assert _plan_is_usable({"base_image": "python:3.12-slim", "stages": ["deps", "runtime"]}) is True
