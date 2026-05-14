"""Tests for ``core.ai.json_response``."""

from core.ai.json_response import (
    extract_balanced_json_object,
    parse_model_json,
    parse_model_json_from_ai_response,
    strip_markdown_json_fence,
)


def test_parse_model_json_from_ai_response_uses_sdk_parsed():
    bad = '{"dockerfile": "FROM x'  # invalid JSON in text
    pr = parse_model_json_from_ai_response(
        bad,
        {"dockerfile": "FROM alpine:3.20", "dockerignore": "", "warnings": []},
    )
    assert pr.data is not None
    assert pr.strategy == "sdk_parsed"
    assert "alpine" in pr.data.get("dockerfile", "")


def test_try_recover_dict_with_raw_decode_trailing_junk():
    from core.ai.json_response import try_recover_dict_with_raw_decode

    blob = 'Here is JSON:\n{"a": 1, "b": 2}\n trailing garbage { not json'
    r = try_recover_dict_with_raw_decode(blob)
    assert r is not None
    assert r.data == {"a": 1, "b": 2}
    assert r.strategy == "raw_decode_prefix"


def test_parse_model_json_with_local_recovery_after_failed_parse():
    from core.ai.json_response import parse_model_json_with_local_recovery

    bad = '{"x": 1'  # truncated
    pr = parse_model_json_with_local_recovery(bad, None)
    assert pr.data is None

    ok_prefix = 'noise {"dockerfile": "FROM scratch", "dockerignore": ""} tail'
    pr2 = parse_model_json_with_local_recovery(ok_prefix, None)
    assert pr2.data is not None
    assert pr2.strategy in ("raw_decode_prefix", "balanced_brace", "strip_fence", "raw")


def test_parse_model_json_with_local_recovery_sdk_parsed_wins():
    from core.ai.json_response import parse_model_json_with_local_recovery

    pr = parse_model_json_with_local_recovery("not json", {"k": 1})
    assert pr.data == {"k": 1}
    assert pr.strategy == "sdk_parsed"


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
