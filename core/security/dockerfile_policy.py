"""Deterministic Dockerfile policy checks (security / unsafe patterns)."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PolicyViolation:
    code: str
    message: str
    line: int | None = None


_PRIVILEGED = re.compile(r"(?i)\s--privileged\b")
_HOST_NETWORK = re.compile(r"(?i)network_mode:\s*host|\b--network=host\b")
_DOCKER_SOCK = re.compile(r"(?i)/var/run/docker\.sock|docker\.sock")
_ADD_HOST_DOCKER = re.compile(r"(?i)\s--add-host=host\.docker\.internal")
_CURL_PIPE_SH = re.compile(r"(?i)curl[^|\n]*\|\s*(ba)?sh\b")


def check_dockerfile_policy(dockerfile: str) -> list[PolicyViolation]:
    violations: list[PolicyViolation] = []
    if not (dockerfile or "").strip():
        return violations

    for i, line in enumerate(dockerfile.splitlines(), 1):
        if _PRIVILEGED.search(line):
            violations.append(
                PolicyViolation("POL001", "privileged containers are not allowed", i),
            )
        if _HOST_NETWORK.search(line):
            violations.append(
                PolicyViolation("POL002", "host network mode is not allowed", i),
            )
        if _DOCKER_SOCK.search(line):
            violations.append(
                PolicyViolation("POL003", "mounting Docker socket is not allowed", i),
            )
        if _ADD_HOST_DOCKER.search(line):
            violations.append(
                PolicyViolation(
                    "POL004",
                    "host.docker.internal / extra_hosts patterns require review",
                    i,
                ),
            )
        if _CURL_PIPE_SH.search(line):
            violations.append(
                PolicyViolation("POL005", "curl|sh style install is not allowed", i),
            )
    return violations
