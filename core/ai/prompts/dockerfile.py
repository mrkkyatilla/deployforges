import json
from typing import Any


def build_plan_prompt(fingerprint: dict, template: str | None = None) -> str:
    sections = ["## Project fingerprint\n"]
    sections.append("```json")
    sections.append(json.dumps(fingerprint, indent=2, default=str))
    sections.append("```\n")
    if template:
        sections.append("## Reference skeleton (for alignment only)\n")
        sections.append(f"```dockerfile\n{template}\n```\n")
    sections.append(
        "## Task\n"
        "Propose a concise multi-stage build **plan** as JSON only (schema in system instructions). "
        "Align base_image and stages with the fingerprint language/framework and the skeleton if present."
    )
    return "\n".join(sections)


def build_generation_prompt(
    fingerprint: dict,
    template: str | None = None,
    plan: dict[str, Any] | None = None,
) -> str:
    sections = ["## Project Analysis Result\n"]
    sections.append("```json")
    sections.append(json.dumps(fingerprint, indent=2, default=str))
    sections.append("```\n")

    if plan:
        sections.append("## Approved build plan\n")
        sections.append("Follow this plan unless the fingerprint clearly contradicts it:\n")
        sections.append("```json")
        sections.append(json.dumps(plan, indent=2, default=str))
        sections.append("```\n")

    if template:
        sections.append("## Skeleton Template\n")
        sections.append("Use this as a starting point and customize it for the project:\n")
        sections.append(f"```dockerfile\n{template}\n```\n")

    sections.append("## Task\n")
    sections.append(
        "Based on the analysis above, generate a production-ready Dockerfile:\n"
        "1. Customize the template (if provided) for this specific project\n"
        "2. Add all required system dependencies for native packages\n"
        "3. Set correct build and start commands\n"
        "4. Ensure proper layer caching for dependencies\n"
        "5. Add security layers (non-root user, healthcheck)\n"
        "6. Generate an optimized .dockerignore\n"
        "7. Include `#` comment lines before each major stage explaining intent\n\n"
        "Respond with valid JSON matching the schema in your system instructions."
    )

    return "\n".join(sections)


def build_generation_metadata_prompt(
    fingerprint: dict,
    template: str | None = None,
    plan: dict[str, Any] | None = None,
) -> str:
    """Phase 1 of two-phase generation: JSON without embedded Dockerfile string."""
    sections = ["## Project Analysis Result\n"]
    sections.append("```json")
    sections.append(json.dumps(fingerprint, indent=2, default=str))
    sections.append("```\n")

    if plan:
        sections.append("## Approved build plan\n")
        sections.append("Follow this plan unless the fingerprint clearly contradicts it:\n")
        sections.append("```json")
        sections.append(json.dumps(plan, indent=2, default=str))
        sections.append("```\n")

    if template:
        sections.append("## Skeleton Template\n")
        sections.append("Use this as a starting point and customize it for the project:\n")
        sections.append(f"```dockerfile\n{template}\n```\n")

    sections.append("## Task\n")
    sections.append(
        "Emit JSON only per the schema: analysis_summary, dockerignore, warnings, exposed_ports, "
        "optional estimated_image_size_mb and requires_env_vars.\n"
        "**Do not** include a dockerfile field — the Dockerfile will be produced in a separate plain-text step.\n"
        "Escape double-quotes inside every JSON string value."
    )
    return "\n".join(sections)


def build_dockerfile_body_only_prompt(
    fingerprint: dict,
    template: str | None,
    plan: dict[str, Any] | None,
    metadata: dict[str, Any],
) -> str:
    """Phase 2: plain Dockerfile only, guided by fingerprint, plan, template, and phase-1 metadata."""
    sections = ["## Project Analysis Result\n", "```json\n"]
    sections.append(json.dumps(fingerprint, indent=2, default=str))
    sections.append("```\n")

    if plan:
        sections.append("## Approved build plan\n```json\n")
        sections.append(json.dumps(plan, indent=2, default=str))
        sections.append("```\n")

    if template:
        sections.append("## Skeleton Template\n```dockerfile\n")
        sections.append(template or "")
        sections.append("\n```\n")

    sections.append("## Phase-1 metadata (must align)\n```json\n")
    sections.append(json.dumps(metadata, indent=2, default=str))
    sections.append(
        "```\n## Task\n"
        "Write **only** the Dockerfile source. First substantive line must be `FROM ` with a pinned tag. "
        "Honor the plan, metadata ports/warnings, and security rules from your system instructions.\n"
    )
    return "".join(sections)


def build_critic_prompt(dockerfile: str, fingerprint: dict) -> str:
    sections = ["## Dockerfile to review\n", "```dockerfile\n", dockerfile, "\n```\n"]
    pruned = {
        k: fingerprint[k]
        for k in ("language", "framework", "dependencies", "entrypoint", "port")
        if k in fingerprint
    }
    if pruned:
        sections.append("## Project context\n```json\n")
        sections.append(json.dumps(pruned, indent=2, default=str))
        sections.append("\n```\n")
    sections.append(
        "## Task\nReturn JSON only: summary string and issues array per system instructions."
    )
    return "".join(sections)


def build_refine_from_critic_prompt(
    dockerfile: str,
    dockerignore: str,
    critic: dict[str, Any],
    fingerprint: dict,
) -> str:
    sections = [
        "## Current Dockerfile\n```dockerfile\n",
        dockerfile,
        "\n```\n## Current .dockerignore\n```\n",
        dockerignore or "",
        "\n```\n## Critic findings\n```json\n",
        json.dumps(critic, indent=2, default=str),
        "\n```\n## Fingerprint (subset)\n```json\n",
        json.dumps(
            {k: fingerprint[k] for k in fingerprint if k != "critical_files"},
            indent=2,
            default=str,
        ),
        "\n```\n## Task\n"
        "Rewrite the Dockerfile and .dockerignore to address every critic issue. "
        "Return full JSON per generation schema.\n",
    ]
    return "".join(sections)


def build_fix_prompt(
    dockerfile: str,
    error_context: str,
    fingerprint: dict | None = None,
    attempt_number: int = 1,
) -> str:
    sections = [f"## Fix Attempt #{attempt_number}\n"]

    sections.append("## Current Dockerfile\n")
    sections.append(f"```dockerfile\n{dockerfile}\n```\n")

    sections.append("## Build Error\n")
    sections.append(f"```\n{error_context}\n```\n")

    if fingerprint:
        sections.append("## Project Context\n")
        sections.append("```json")
        pruned = {
            k: fingerprint[k]
            for k in ("language", "framework", "dependencies", "entrypoint", "port")
            if k in fingerprint
        }
        sections.append(json.dumps(pruned, indent=2, default=str))
        sections.append("```\n")

    sections.append(
        "## Task\n"
        "Fix the Dockerfile to resolve the build error above. "
        "Make minimal changes — only fix what's broken. "
        "Respond with valid JSON matching the schema in your system instructions."
    )

    return "\n".join(sections)
