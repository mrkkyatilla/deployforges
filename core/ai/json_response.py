"""Parse Gemini JSON outputs: markdown fences, whitespace, embedded JSON objects."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ParseResult:
    """Result of trying to decode a model string as JSON."""

    data: dict | None
    strategy: str
    raw_text: str
    error: str | None = None


def strip_markdown_json_fence(text: str) -> str:
    """Remove optional ``` / ```json wrappers and BOM (including text before first fence)."""
    t = text.strip()
    if t.startswith("\ufeff"):
        t = t.lstrip("\ufeff").strip()
    fence = t.find("```")
    if fence < 0:
        return t
    after = t[fence + 3 :].lstrip()
    if after.lower().startswith("json"):
        after = after[4:].lstrip()
    # drop optional newline after ```json
    close = after.rfind("```")
    if close >= 0:
        after = after[:close]
    return after.strip()


def extract_balanced_json_object(text: str) -> str | None:
    """Return substring from first '{' through matching '}' respecting strings."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    quote: str | None = None
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                in_str = False
                quote = None
        else:
            if ch in ('"', "'"):
                in_str = True
                quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    return None


def _try_load(candidate: str, strategy: str, raw_text: str) -> ParseResult | None:
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return ParseResult(None, strategy, raw_text, str(exc))
    if not isinstance(data, dict):
        return ParseResult(None, strategy, raw_text, "JSON root is not an object")
    return ParseResult(data, strategy, raw_text, None)


def parse_model_json(text: str) -> ParseResult:
    """
    Multi-step parse: raw -> strip fences -> extract balanced `{...}`.

    Returns first successful dict parse, or last error-bearing ParseResult.
    """
    raw_text = text or ""
    if not raw_text.strip():
        return ParseResult(None, "empty", raw_text, "empty response")

    strategies: list[tuple[str, str]] = [
        ("raw", raw_text),
        ("strip_fence", strip_markdown_json_fence(raw_text)),
    ]
    unfenced = strategies[1][1]
    balanced = extract_balanced_json_object(unfenced)
    if balanced and balanced != unfenced.strip():
        strategies.append(("balanced_brace", balanced))

    last_err: str | None = "no candidate"
    for name, candidate in strategies:
        candidate = candidate.strip()
        if not candidate:
            continue
        res = _try_load(candidate, name, raw_text)
        if res.data is not None:
            return res
        last_err = res.error
    return ParseResult(None, "failed", raw_text, last_err)


def parse_model_json_from_ai_response(
    text: str,
    parsed_dict: dict | None,
) -> ParseResult:
    """Prefer string parse; if it fails and Gemini SDK filled ``parsed``, use that dict."""
    pr = parse_model_json(text)
    if pr.data is not None:
        return pr
    if parsed_dict is not None and isinstance(parsed_dict, dict):
        return ParseResult(parsed_dict, "sdk_parsed", text or "", None)
    return pr


def try_recover_dict_with_raw_decode(text: str) -> ParseResult | None:
    """If the string contains a complete leading JSON object, decode it (ignores trailing junk)."""
    decoder = json.JSONDecoder()
    raw = (text or "").strip()
    if not raw:
        return None
    candidates: list[str] = [raw]
    fenced = strip_markdown_json_fence(raw)
    if fenced.strip() != raw:
        candidates.append(fenced.strip())
    for cand in candidates:
        cand = cand.strip()
        if not cand:
            continue
        start = cand.find("{")
        if start < 0:
            continue
        tail = cand[start:]
        try:
            obj, _end = decoder.raw_decode(tail)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return ParseResult(obj, "raw_decode_prefix", text or "", None)
    return None


def parse_model_json_with_local_recovery(
    text: str,
    parsed_dict: dict | None,
) -> ParseResult:
    """Full local path: AI response parse, then ``raw_decode`` salvage (no LLM)."""
    pr = parse_model_json_from_ai_response(text, parsed_dict)
    if pr.data is not None:
        return pr
    recovered = try_recover_dict_with_raw_decode(text or "")
    if recovered is not None:
        return recovered
    return pr


def truncate_for_log(text: str, max_chars: int, *, head_frac: float = 0.5) -> str:
    """Return head + tail excerpt when text exceeds max_chars."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head_n = int(max_chars * head_frac)
    tail_n = max_chars - head_n - 3
    if tail_n < 1:
        return text[:max_chars] + "..."
    return text[:head_n] + "..." + text[-tail_n:]


def mask_secrets(text: str, patterns: list[str]) -> str:
    """Apply regex patterns (e.g. from settings.secret_patterns) to redact lines."""
    out = text
    for pat in patterns:
        try:
            out = re.sub(pat, "[REDACTED]", out, flags=re.MULTILINE)
        except re.error:
            continue
    return out
