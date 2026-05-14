"""Per-project Dockerfile AI pipeline policy (legacy settings vs auto tiered profile)."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from core.ai.token_manager import fingerprint_is_high_complexity

PipelineTier = Literal["minimal", "standard", "thorough"]
PipelineMode = Literal["legacy", "auto"]


@dataclass(frozen=True)
class DockerfilePipelinePolicy:
    plan_enabled: bool
    plan_json_repair_enabled: bool
    critic_enabled: bool
    refine_enabled: bool
    json_repair_second_attempt_enabled: bool
    tier: PipelineTier
    mode: PipelineMode
    signals: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["signals"] = list(self.signals)
        return d


def policy_from_dict(d: dict[str, Any] | None) -> DockerfilePipelinePolicy | None:
    """Rehydrate policy saved on graph state; returns None if missing or invalid."""
    if not d or not isinstance(d, dict):
        return None
    try:
        tier = d.get("tier", "standard")
        if tier not in ("minimal", "standard", "thorough"):
            tier = "standard"
        mode = d.get("mode", "legacy")
        if mode not in ("legacy", "auto"):
            mode = "legacy"
        sig = d.get("signals") or ()
        if isinstance(sig, list):
            sig = tuple(str(x) for x in sig)
        else:
            sig = ()
        return DockerfilePipelinePolicy(
            plan_enabled=bool(d.get("plan_enabled", True)),
            plan_json_repair_enabled=bool(d.get("plan_json_repair_enabled", True)),
            critic_enabled=bool(d.get("critic_enabled", False)),
            refine_enabled=bool(d.get("refine_enabled", False)),
            json_repair_second_attempt_enabled=bool(
                d.get("json_repair_second_attempt_enabled", True),
            ),
            tier=tier,  # type: ignore[arg-type]
            mode=mode,  # type: ignore[arg-type]
            signals=sig,
        )
    except (TypeError, ValueError):
        return None


def extract_stage_names_from_dockerfile_template(template: str | None) -> list[str]:
    """Return declared stage names from ``FROM ... AS name`` lines in a skeleton template."""
    if not template or not template.strip():
        return []
    names: list[str] = []
    for line in template.splitlines():
        s = line.strip()
        if not s.upper().startswith("FROM "):
            continue
        m = re.search(r"\s+AS\s+([a-zA-Z0-9_.-]+)\s*$", s, re.IGNORECASE)
        if m:
            names.append(m.group(1))
    return names


def fingerprint_multi_surface(fingerprint: dict | None) -> bool:
    """Heuristic: multiple Dockerfiles, or compose/workflows + Dockerfile (deployment surface)."""
    fp = fingerprint or {}
    ft = fp.get("file_tree") or {}
    nodes = ft.get("nodes") or []
    dockerfiles: list[str] = []
    has_workflow = False
    has_compose = False
    for n in nodes:
        if not isinstance(n, dict):
            continue
        p = (n.get("path") or "").replace("\\", "/")
        pl = p.lower()
        base = pl.rsplit("/", 1)[-1]
        if base == "dockerfile" or base.startswith("dockerfile."):
            dockerfiles.append(p)
        if ".github/workflows/" in pl and (pl.endswith(".yml") or pl.endswith(".yaml")):
            has_workflow = True
        if "docker-compose" in base or base in ("compose.yaml", "compose.yml"):
            has_compose = True
    if len(dockerfiles) >= 2:
        return True
    if has_workflow and len(dockerfiles) >= 1:
        return True
    if has_compose and len(dockerfiles) >= 1:
        return True
    return False


def _service_type_variety(services: list[Any]) -> int:
    types: set[str] = set()
    for s in services:
        if not isinstance(s, dict):
            continue
        t = str(s.get("type") or "").strip().lower()
        if t:
            types.add(t)
    return len(types)


def _legacy_policy(settings: Any, mode: PipelineMode) -> DockerfilePipelinePolicy:
    cre = bool(getattr(settings, "ai_dockerfile_critic_refine_enabled", False))
    return DockerfilePipelinePolicy(
        plan_enabled=bool(getattr(settings, "ai_dockerfile_plan_enabled", True)),
        plan_json_repair_enabled=bool(
            getattr(settings, "ai_dockerfile_plan_json_repair_enabled", True),
        ),
        critic_enabled=cre,
        refine_enabled=cre,
        json_repair_second_attempt_enabled=bool(
            getattr(settings, "ai_json_repair_second_attempt_enabled", True),
        ),
        tier="thorough" if cre else "minimal",
        mode=mode,
        signals=("legacy_settings",),
    )


def _tier_policy(
    tier: PipelineTier,
    mode: PipelineMode,
    signals: tuple[str, ...],
) -> DockerfilePipelinePolicy:
    if tier == "minimal":
        return DockerfilePipelinePolicy(
            plan_enabled=False,
            plan_json_repair_enabled=False,
            critic_enabled=False,
            refine_enabled=False,
            json_repair_second_attempt_enabled=False,
            tier=tier,
            mode=mode,
            signals=signals,
        )
    if tier == "standard":
        return DockerfilePipelinePolicy(
            plan_enabled=True,
            plan_json_repair_enabled=True,
            critic_enabled=True,
            refine_enabled=False,
            json_repair_second_attempt_enabled=True,
            tier=tier,
            mode=mode,
            signals=signals,
        )
    return DockerfilePipelinePolicy(
        plan_enabled=True,
        plan_json_repair_enabled=True,
        critic_enabled=True,
        refine_enabled=True,
        json_repair_second_attempt_enabled=True,
        tier="thorough",
        mode=mode,
        signals=signals,
    )


def _dedupe_signals(signals: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(signals))


def resolve_dockerfile_pipeline_policy(
    fingerprint: dict | None,
    settings: Any,
) -> DockerfilePipelinePolicy:
    """Resolve effective policy from settings mode and fingerprint (auto) or globals (legacy)."""
    raw_mode = getattr(settings, "ai_dockerfile_pipeline_mode", "legacy") or "legacy"
    mode: PipelineMode = "auto" if str(raw_mode).lower() == "auto" else "legacy"
    if mode == "legacy":
        return _legacy_policy(settings, mode)

    fp = fingerprint or {}
    signals: list[str] = []

    if fingerprint_is_high_complexity(fp):
        signals.append("high_complexity")
        return _tier_policy("thorough", mode, _dedupe_signals(signals))

    conf = float(fp.get("confidence") or 0.0)
    services = fp.get("services")
    if not isinstance(services, list):
        services = []
    lang = fp.get("language") or {}
    secondary = lang.get("secondary") if isinstance(lang, dict) else None
    if not isinstance(secondary, list):
        secondary = []
    deps = fp.get("dependencies") or {}
    lock_ok = True
    if isinstance(deps, dict):
        lock_ok = bool(deps.get("lock_file_exists", True))

    warnings = fp.get("warnings") or []
    if not isinstance(warnings, list):
        warnings = []

    fw = fp.get("framework") or {}
    is_static = bool(fw.get("is_static")) if isinstance(fw, dict) else False

    score = 0
    if fp.get("is_monorepo"):
        score += 2
        signals.append("monorepo")
    if len(services) >= 2:
        score += 2
        signals.append("multi_service")
    elif len(services) > 1:
        score += 1
        signals.append("multi_service")
    if fingerprint_multi_surface(fp):
        score += 2
        signals.append("multi_surface")
    if conf < 0.55:
        score += 3
        signals.append("low_confidence")
    elif conf < 0.65:
        score += 1
        signals.append("medium_confidence")
    if not lock_ok:
        score += 1
        signals.append("no_lockfile")
    if len(secondary) >= 1:
        score += 1
        signals.append("secondary_language")
    if _service_type_variety(services) >= 2:
        score += 1
        signals.append("mixed_service_types")
    if len(warnings) >= 3:
        score += 1
        signals.append("many_warnings")

    if is_static and conf >= 0.8 and len(services) <= 1 and not fp.get("is_monorepo"):
        score = max(0, score - 2)
        signals.append("static_simple")

    if conf < 0.55 and fp.get("is_monorepo"):
        return _tier_policy("thorough", mode, _dedupe_signals(signals))

    if score >= 5:
        return _tier_policy("thorough", mode, _dedupe_signals(signals))

    minimal_ok = (
        score == 0
        and conf >= 0.72
        and len(services) <= 1
        and not fp.get("is_monorepo")
        and lock_ok
        and not fingerprint_multi_surface(fp)
    )
    if minimal_ok:
        return _tier_policy("minimal", mode, _dedupe_signals(signals))

    return _tier_policy("standard", mode, _dedupe_signals(signals))
