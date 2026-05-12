from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

PATTERNS_DIR = Path(__file__).parent / "patterns"


@dataclass
class ClassifiedError:
    error_type: str
    name: str
    severity: str
    auto_fixable: bool
    fix_strategy: str
    match_text: str
    context: str
    line_number: int | None = None
    suggested_fix: str | None = None


class BuildErrorClassifier:
    """Classifies build errors using pattern matching against known error signatures."""

    def __init__(self):
        self._patterns: dict[str, list[dict]] = {}
        self._load_patterns()

    def _load_patterns(self) -> None:
        if not PATTERNS_DIR.exists():
            return
        for yml_file in PATTERNS_DIR.glob("*.yml"):
            lang = yml_file.stem
            try:
                data = yaml.safe_load(yml_file.read_text())
                self._patterns[lang] = data.get("patterns", [])
            except Exception:
                logger.warning("Failed to load pattern file: %s", yml_file, exc_info=True)

    def classify(self, build_log: str, language: str | None = None) -> list[ClassifiedError]:
        errors: list[ClassifiedError] = []

        pattern_sets = {}
        if language and language in self._patterns:
            pattern_sets[language] = self._patterns[language]
        else:
            pattern_sets = self._patterns

        for lang, patterns in pattern_sets.items():
            for pattern_def in patterns:
                regex = pattern_def.get("regex", "")
                if not regex:
                    continue

                try:
                    for match in re.finditer(regex, build_log, re.MULTILINE | re.IGNORECASE):
                        start = max(0, match.start() - 200)
                        end = min(len(build_log), match.end() + 200)
                        context = build_log[start:end]

                        line_num = build_log[:match.start()].count("\n") + 1

                        errors.append(ClassifiedError(
                            error_type=lang,
                            name=pattern_def.get("name", "unknown"),
                            severity=pattern_def.get("severity", "medium"),
                            auto_fixable=pattern_def.get("auto_fixable", False),
                            fix_strategy=pattern_def.get("fix_strategy", "unknown"),
                            match_text=match.group(),
                            context=context,
                            line_number=line_num,
                            suggested_fix=pattern_def.get("fix"),
                        ))
                except re.error:
                    logger.warning("Invalid regex pattern: %s", regex)

        _severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        errors.sort(key=lambda e: _severity_order.get(e.severity, 4))

        return errors
