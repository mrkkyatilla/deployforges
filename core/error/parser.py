from __future__ import annotations

import re


def extract_error_lines(build_log: str, max_lines: int = 50) -> str:
    """Extract the most relevant error lines from a build log."""
    lines = build_log.splitlines()
    error_lines: list[str] = []

    error_indicators = [
        r"(?i)^error",
        r"(?i)^E:",
        r"(?i)failed",
        r"(?i)fatal",
        r"(?i)cannot find",
        r"(?i)not found",
        r"(?i)permission denied",
        r"(?i)no such file",
        r"(?i)ModuleNotFoundError",
        r"(?i)ImportError",
        r"(?i)SyntaxError",
        r"(?i)TypeError",
        r"(?i)npm ERR!",
        r"(?i)COPY failed",
        r"(?i)returned a non-zero code",
        r"(?i)non-zero exit",
        r"(?i)exit code",
        r"(?i)\buv\b",
        r"(?i)usage:",
        r"(?i)error:",
    ]

    compiled = [re.compile(p) for p in error_indicators]

    for i, line in enumerate(lines):
        for pattern in compiled:
            if pattern.search(line):
                start = max(0, i - 2)
                end = min(len(lines), i + 3)
                for ctx_line in lines[start:end]:
                    if ctx_line not in error_lines:
                        error_lines.append(ctx_line)
                break

    if not error_lines:
        error_lines = lines[-max_lines:]

    return "\n".join(error_lines[:max_lines])


def get_last_n_lines(log: str, n: int = 30) -> str:
    return "\n".join(log.splitlines()[-n:])
