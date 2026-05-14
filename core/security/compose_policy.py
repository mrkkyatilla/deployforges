"""Deterministic docker-compose policy checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml


@dataclass(frozen=True)
class ComposePolicyViolation:
    code: str
    message: str
    path: str


def check_compose_policy(compose_yml: str) -> list[ComposePolicyViolation]:
    violations: list[ComposePolicyViolation] = []
    if not (compose_yml or "").strip():
        return violations
    try:
        data = yaml.safe_load(compose_yml) or {}
    except yaml.YAMLError as exc:
        return [
            ComposePolicyViolation("CPY001", f"invalid compose YAML: {exc}", ""),
        ]
    if not isinstance(data, dict):
        return violations

    services = data.get("services")
    if not isinstance(services, dict):
        return violations

    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        base = f"services.{name}"
        if svc.get("privileged") is True:
            violations.append(
                ComposePolicyViolation("CPO001", "privileged: true is not allowed", base),
            )
        if str(svc.get("network_mode", "")).lower() == "host":
            violations.append(
                ComposePolicyViolation("CPO002", "network_mode: host is not allowed", base),
            )
        vols = svc.get("volumes")
        if isinstance(vols, list):
            for idx, v in enumerate(vols):
                s = str(v).lower()
                if "docker.sock" in s:
                    violations.append(
                        ComposePolicyViolation(
                            "CPO003",
                            "docker socket bind is not allowed",
                            f"{base}.volumes[{idx}]",
                        ),
                    )
        ports = svc.get("ports")
        if isinstance(ports, list):
            for idx, p in enumerate(ports):
                s = str(p).strip()
                if s.startswith("0.0.0.0:"):
                    violations.append(
                        ComposePolicyViolation(
                            "CPO010",
                            "binding published port to 0.0.0.0 is high risk; prefer 127.0.0.1",
                            f"{base}.ports[{idx}]",
                        ),
                    )
    return violations


def compose_risk_heuristic(
    *,
    service_count: int,
    exposes_host_ports: bool,
    has_database: bool,
) -> dict[str, Any]:
    """Lightweight risk summary for API preview (no LLM)."""
    score = 0
    reasons: list[str] = []
    if service_count >= 4:
        score += 2
        reasons.append("many_services")
    elif service_count >= 2:
        score += 1
        reasons.append("multi_service")
    if exposes_host_ports:
        score += 1
        reasons.append("published_ports")
    if has_database:
        score += 1
        reasons.append("database_service")
    level = "low"
    if score >= 4:
        level = "high"
    elif score >= 2:
        level = "medium"
    return {"risk_level": level, "score": score, "reasons": reasons}
