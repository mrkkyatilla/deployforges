from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class LintIssue:
    rule: str
    severity: str  # "error", "warning", "info"
    message: str
    line: int | None = None
    auto_fix: str | None = None


@dataclass
class LintResult:
    is_valid: bool
    issues: list[LintIssue] = field(default_factory=list)
    fixed_dockerfile: str | None = None

    @property
    def errors(self) -> list[LintIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[LintIssue]:
        return [i for i in self.issues if i.severity == "warning"]


class DockerfileLinter:
    """Validates AI-generated Dockerfiles with deterministic checks."""

    def lint(self, dockerfile: str, port: int | None = None) -> LintResult:
        issues: list[LintIssue] = []
        lines = dockerfile.strip().splitlines()

        issues.extend(self._check_from_exists(lines))
        issues.extend(self._check_no_root(lines))
        issues.extend(self._check_no_latest_tag(lines))
        issues.extend(self._check_expose(lines, port))
        issues.extend(self._check_healthcheck(lines))
        issues.extend(self._check_no_secrets(lines))
        issues.extend(self._check_dangerous_commands(lines))
        issues.extend(self._check_cache_cleanup(lines))

        has_errors = any(i.severity == "error" for i in issues)
        fixed = self._apply_auto_fixes(dockerfile, issues) if issues else None

        return LintResult(
            is_valid=not has_errors,
            issues=issues,
            fixed_dockerfile=fixed,
        )

    def _check_from_exists(self, lines: list[str]) -> list[LintIssue]:
        has_from = any(line.strip().upper().startswith("FROM ") for line in lines)
        if not has_from:
            return [LintIssue("DF001", "error", "Dockerfile has no FROM directive")]
        return []

    def _check_no_root(self, lines: list[str]) -> list[LintIssue]:
        has_user = any(line.strip().upper().startswith("USER ") for line in lines)
        if not has_user:
            return [LintIssue(
                "DF002", "warning",
                "No USER directive — application will run as root",
                auto_fix="ADD_USER",
            )]
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.upper().startswith("USER "):
                user_val = stripped.split(None, 1)[1].strip() if len(stripped.split()) > 1 else ""
                if user_val in ("root", "0"):
                    return [LintIssue("DF002", "warning", "USER is set to root", line=i)]
        return []

    def _check_no_latest_tag(self, lines: list[str]) -> list[LintIssue]:
        issues = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.upper().startswith("FROM "):
                image = stripped.split()[1] if len(stripped.split()) > 1 else ""
                if image.endswith(":latest"):
                    issues.append(LintIssue(
                        "DF003", "warning",
                        f"Base image uses :latest tag: {image}", line=i,
                    ))
                elif ":" not in image and "@" not in image and image.upper() != "SCRATCH":
                    issues.append(LintIssue(
                        "DF003", "warning",
                        f"Base image has no version tag: {image}", line=i,
                    ))
        return issues

    def _check_expose(self, lines: list[str], port: int | None) -> list[LintIssue]:
        exposed_ports = set()
        for line in lines:
            stripped = line.strip()
            if stripped.upper().startswith("EXPOSE "):
                for part in stripped.split()[1:]:
                    try:
                        exposed_ports.add(int(part.split("/")[0]))
                    except ValueError:
                        pass

        if not exposed_ports:
            return [LintIssue("DF004", "warning", "No EXPOSE directive found")]

        if port and port not in exposed_ports:
            return [LintIssue(
                "DF004", "warning",
                f"EXPOSE ports {exposed_ports} don't include detected port {port}",
            )]
        return []

    def _check_healthcheck(self, lines: list[str]) -> list[LintIssue]:
        has_hc = any(line.strip().upper().startswith("HEALTHCHECK ") for line in lines)
        if not has_hc:
            return [LintIssue("DF005", "info", "No HEALTHCHECK directive")]
        return []

    def _check_no_secrets(self, lines: list[str]) -> list[LintIssue]:
        issues = []
        secret_patterns = [
            (r"ENV\s+.*(?:PASSWORD|SECRET|TOKEN|API_KEY)\s*=\s*\S+", "Possible secret in ENV"),
            (r"COPY\s+\.env\s", "Copying .env file into image"),
            (r"COPY\s+.*id_rsa", "Copying SSH private key into image"),
            (r"COPY\s+.*\.pem\s", "Copying PEM file into image"),
        ]
        for i, line in enumerate(lines, 1):
            for pattern, msg in secret_patterns:
                if re.search(pattern, line, re.IGNORECASE):
                    issues.append(LintIssue("DF006", "error", msg, line=i))
        return issues

    def _check_dangerous_commands(self, lines: list[str]) -> list[LintIssue]:
        issues = []
        dangerous = [
            (r"rm\s+-rf\s+/[^a-zA-Z]", "Dangerous rm -rf / command"),
            (r"chmod\s+777", "Overly permissive chmod 777"),
            (r"curl.*\|\s*sh", "Piping curl to shell — security risk"),
            (r"wget.*\|\s*sh", "Piping wget to shell — security risk"),
        ]
        for i, line in enumerate(lines, 1):
            for pattern, msg in dangerous:
                if re.search(pattern, line):
                    issues.append(LintIssue("DF007", "warning", msg, line=i))
        return issues

    def _check_cache_cleanup(self, lines: list[str]) -> list[LintIssue]:
        issues = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if "apt-get install" in stripped and "rm -rf /var/lib/apt/lists" not in stripped:
                continuation = ""
                for j in range(i, min(i + 5, len(lines))):
                    continuation += lines[j - 1]
                if "rm -rf /var/lib/apt/lists" not in continuation:
                    issues.append(LintIssue(
                        "DF008", "warning",
                        "apt-get install without cleaning cache in same RUN",
                        line=i,
                    ))
                    break
        return issues

    def _apply_auto_fixes(self, dockerfile: str, issues: list[LintIssue]) -> str:
        fixed = dockerfile
        for issue in issues:
            if issue.auto_fix == "ADD_USER" and "USER " not in fixed:
                if "\nCMD " in fixed:
                    fixed = fixed.replace(
                        "\nCMD ",
                        "\nRUN addgroup -S appgroup && adduser -S appuser -G appgroup\n"
                        "USER appuser\n\nCMD ",
                        1,
                    )
                elif "\nENTRYPOINT " in fixed:
                    fixed = fixed.replace(
                        "\nENTRYPOINT ",
                        "\nRUN addgroup -S appgroup && adduser -S appuser -G appgroup\n"
                        "USER appuser\n\nENTRYPOINT ",
                        1,
                    )
        return fixed
