"""Tests for ``core.ai.json_response``."""

from core.ai.json_response import (
    extract_balanced_json_object,
    parse_model_json,
    strip_markdown_json_fence,
)


def test_strip_markdown_json_fence():
    raw = '```json\n{"a": 1}\n```'
    assert strip_markdown_json_fence(raw) == '{"a": 1}'


def test_parse_raw_json():
    pr = parse_model_json('{"x": "y"}')
    assert pr.data == {"x": "y"}
    assert pr.strategy == "raw"


def test_parse_fenced_json():
    body = 'Here you go:\n```json\n{"k": 2}\n```\n'
    pr = parse_model_json(body)
    assert pr.data == {"k": 2}
    assert pr.strategy == "strip_fence"


def test_extract_balanced_nested():
    text = 'prefix {"outer": {"inner": 1}} suffix'
    sub = extract_balanced_json_object(text)
    assert sub is not None
    pr = parse_model_json(sub)
    assert pr.data == {"outer": {"inner": 1}}
