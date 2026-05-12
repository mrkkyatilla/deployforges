from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class AutoFix:
    description: str
    dockerfile_patch: str | None = None
    line_to_replace: str | None = None
    replacement: str | None = None
    insert_before_pattern: str | None = None
    insert_content: str | None = None


@dataclass
class ResolverResult:
    fixed: bool
    dockerfile: str
    fixes_applied: list[str] = field(default_factory=list)
    needs_ai: bool = False


SYSTEM_PACKAGE_MAP = {
    "postgresql-dev": {"alpine": "postgresql-dev", "debian": "libpq-dev"},
    "libpq-dev": {"alpine": "postgresql-dev", "debian": "libpq-dev"},
    "openssl-dev": {"alpine": "openssl-dev", "debian": "libssl-dev"},
    "libssl-dev": {"alpine": "openssl-dev", "debian": "libssl-dev"},
    "libffi-dev": {"alpine": "libffi-dev", "debian": "libffi-dev"},
    "jpeg-dev": {"alpine": "jpeg-dev", "debian": "libjpeg-dev"},
    "zlib-dev": {"alpine": "zlib-dev", "debian": "zlib1g-dev"},
    "freetype-dev": {"alpine": "freetype-dev", "debian": "libfreetype6-dev"},
    "vips-dev": {"alpine": "vips-dev", "debian": "libvips-dev"},
    "libxml2-dev": {"alpine": "libxml2-dev", "debian": "libxml2-dev"},
    "libxslt-dev": {"alpine": "libxslt-dev", "debian": "libxslt-dev"},
    "cairo-dev": {"alpine": "cairo-dev", "debian": "libcairo2-dev"},
    "pango-dev": {"alpine": "pango-dev", "debian": "libpango1.0-dev"},
    "gcc": {"alpine": "gcc", "debian": "gcc"},
    "g++": {"alpine": "g++", "debian": "g++"},
    "make": {"alpine": "make", "debian": "make"},
    "musl-dev": {"alpine": "musl-dev", "debian": "libc6-dev"},
    "python3-dev": {"alpine": "python3-dev", "debian": "python3-dev"},
    "build-base": {"alpine": "build-base", "debian": "build-essential"},
}


class ErrorResolver:
    """Deterministic auto-fixer for common Docker build errors."""

    def resolve(self, dockerfile: str, errors: list[dict]) -> ResolverResult:
        current = dockerfile
        fixes: list[str] = []
        needs_ai = False

        for error in errors:
            strategy = error.get("fix_strategy", "")
            auto_fixable = error.get("auto_fixable", False)

            if not auto_fixable:
                needs_ai = True
                continue

            handler = self._STRATEGY_MAP.get(strategy)
            if handler:
                fix = handler(self, current, error)
                if fix:
                    current = fix.dockerfile
                    fixes.extend(fix.fixes_applied)
            else:
                needs_ai = True

        return ResolverResult(
            fixed=len(fixes) > 0,
            dockerfile=current,
            fixes_applied=fixes,
            needs_ai=needs_ai,
        )

    def _fix_add_system_package(self, dockerfile: str, error: dict) -> ResolverResult | None:
        suggested = error.get("suggested_fix") or error.get("fix")
        if not suggested:
            match_text = error.get("match_text", "")
            pkg = self._extract_package_name(match_text)
            if not pkg:
                return None
            suggested = pkg

        packages = suggested.split() if isinstance(suggested, str) else suggested
        base_type = self._detect_base_type(dockerfile)

        resolved_packages = []
        for pkg in packages:
            mapped = SYSTEM_PACKAGE_MAP.get(pkg, {}).get(base_type, pkg)
            resolved_packages.append(mapped)

        pkg_str = " ".join(resolved_packages)
        new_df = self._inject_system_packages(dockerfile, pkg_str, base_type)

        if new_df != dockerfile:
            return ResolverResult(
                fixed=True,
                dockerfile=new_df,
                fixes_applied=[f"Added system packages: {pkg_str}"],
            )
        return None

    def _fix_permissions(self, dockerfile: str, error: dict) -> ResolverResult | None:
        if "USER " in dockerfile and "chown" not in dockerfile.lower():
            lines = dockerfile.splitlines()
            new_lines = []
            for line in lines:
                new_lines.append(line)
                if line.strip().startswith("COPY . .") or line.strip().startswith("COPY --from"):
                    if "COPY . ." in line and "chown" not in line:
                        new_lines.append("RUN chown -R appuser:appgroup /app")

            new_df = "\n".join(new_lines)
            if new_df != dockerfile:
                return ResolverResult(
                    fixed=True,
                    dockerfile=new_df,
                    fixes_applied=["Added chown after COPY to fix permission issues"],
                )
        return None

    def _fix_copy_path(self, dockerfile: str, error: dict) -> ResolverResult | None:
        match_text = error.get("match_text", "")
        file_match = re.search(r"(?:not found|cache key).*?['\"]?([^\s'\"]+)['\"]?", match_text)
        if not file_match:
            return None

        missing_file = file_match.group(1)
        lines = dockerfile.splitlines()
        new_lines = []
        fixed = False

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("COPY") and missing_file in stripped:
                new_lines.append(f"# WARNING: {missing_file} not found, COPY commented out")
                new_lines.append(f"# {line}")
                fixed = True
            else:
                new_lines.append(line)

        if fixed:
            return ResolverResult(
                fixed=True,
                dockerfile="\n".join(new_lines),
                fixes_applied=[f"Commented out COPY of missing file: {missing_file}"],
            )
        return None

    def _fix_base_image(self, dockerfile: str, error: dict) -> ResolverResult | None:
        lines = dockerfile.splitlines()
        new_lines = []
        fixed = False

        for line in lines:
            stripped = line.strip()
            if stripped.upper().startswith("FROM ") and ":latest" in stripped:
                parts = stripped.split()
                if len(parts) >= 2:
                    image = parts[1]
                    base = image.split(":")[0]
                    new_image = f"{base}:stable"
                    new_line = stripped.replace(image, new_image)
                    new_lines.append(new_line)
                    fixed = True
                    continue
            new_lines.append(line)

        if fixed:
            return ResolverResult(
                fixed=True,
                dockerfile="\n".join(new_lines),
                fixes_applied=["Replaced :latest tag with :stable"],
            )
        return None

    def _fix_optimize_memory(self, dockerfile: str, error: dict) -> ResolverResult | None:
        additions = []
        new_df = dockerfile

        if "node" in dockerfile.lower() and "NODE_OPTIONS" not in dockerfile:
            new_df = new_df.replace(
                "COPY . .",
                'ENV NODE_OPTIONS="--max-old-space-size=3072"\nCOPY . .',
                1,
            )
            additions.append("Set NODE_OPTIONS max-old-space-size=3072")

        if "java" in dockerfile.lower() and "JAVA_OPTS" not in dockerfile:
            new_df = new_df.replace(
                "ENTRYPOINT",
                'ENV JAVA_OPTS="-Xmx2g -Xms512m"\nENTRYPOINT',
                1,
            )
            additions.append("Set JAVA_OPTS with memory limits")

        if additions:
            return ResolverResult(
                fixed=True,
                dockerfile=new_df,
                fixes_applied=additions,
            )
        return None

    def _detect_base_type(self, dockerfile: str) -> str:
        lower = dockerfile.lower()
        if "alpine" in lower or "apk add" in lower:
            return "alpine"
        return "debian"

    def _extract_package_name(self, error_text: str) -> str | None:
        patterns = [
            r"No such file.*?(\w+\.h)",
            r"cannot find -l(\w+)",
            r"Package '(\w+)' not found",
            r"Unable to locate package (\w+)",
        ]
        for pattern in patterns:
            m = re.search(pattern, error_text)
            if m:
                name = m.group(1)
                if name.endswith(".h"):
                    header_map = {
                        "Python.h": "python3-dev",
                        "ffi.h": "libffi-dev",
                        "openssl/ssl.h": "openssl-dev",
                        "pg_config.h": "postgresql-dev",
                    }
                    return header_map.get(name, f"{name.replace('.h', '')}-dev")
                return name
        return None

    def _inject_system_packages(self, dockerfile: str, packages: str, base_type: str) -> str:
        install_cmd = "apk add --no-cache" if base_type == "alpine" else "apt-get install -y --no-install-recommends"

        lines = dockerfile.splitlines()
        for i, line in enumerate(lines):
            if install_cmd in line:
                clean = line.rstrip()
                if clean.endswith("\\"):
                    clean = clean[:-1].rstrip()
                lines[i] = f"{clean} {packages} \\"
                return "\n".join(lines)

        for i, line in enumerate(lines):
            if line.strip().startswith("WORKDIR"):
                if base_type == "alpine":
                    inject = f"RUN apk add --no-cache {packages}\n"
                else:
                    inject = (
                        f"RUN apt-get update && \\\n"
                        f"    apt-get install -y --no-install-recommends {packages} && \\\n"
                        f"    rm -rf /var/lib/apt/lists/*\n"
                    )
                lines.insert(i, inject)
                return "\n".join(lines)

        return dockerfile

    _STRATEGY_MAP = {
        "add_system_package": _fix_add_system_package,
        "fix_permissions": _fix_permissions,
        "fix_copy_path": _fix_copy_path,
        "fix_base_image": _fix_base_image,
        "optimize_memory": _fix_optimize_memory,
    }
