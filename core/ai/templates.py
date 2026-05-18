from __future__ import annotations

import re

TEMPLATES: dict[str, dict[str, str]] = {
    "python": {
        "default": """\
# ===== BUILDER =====
FROM python:{version}-slim AS builder

WORKDIR /app

RUN apt-get update && \\
    apt-get install -y --no-install-recommends {system_deps} && \\
    rm -rf /var/lib/apt/lists/*

COPY {dep_file} .
RUN pip install --no-cache-dir --prefix=/install -r {dep_file}

# ===== RUNTIME =====
FROM python:{version}-slim

RUN groupadd -r appgroup && useradd -r -g appgroup -d /app -s /sbin/nologin appuser

WORKDIR /app

COPY --from=builder /install /usr/local
COPY . .

RUN chown -R appuser:appgroup /app
USER appuser

EXPOSE {port}

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \\
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:{port}/health')" || exit 1

CMD {start_command}
""",
        "python_uv": """\
# ===== BUILDER =====
FROM python:{version}-slim AS builder

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev

COPY . .

# ===== RUNTIME =====
FROM python:{version}-slim

RUN groupadd -r appgroup && useradd -r -g appgroup -d /app -s /sbin/nologin appuser

WORKDIR /app

COPY --from=builder /app /app
ENV PATH="/app/.venv/bin:$PATH"

RUN chown -R appuser:appgroup /app
USER appuser

EXPOSE {port}

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \\
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:{port}/health')" || exit 1

CMD {start_command}
""",
        "static_analysis": """\
# ===== BUILDER =====
FROM python:{version}-slim AS builder
WORKDIR /app
COPY {dep_file} .
RUN pip install --no-cache-dir --prefix=/install -r {dep_file}

# ===== RUNTIME =====
FROM python:{version}-slim
RUN groupadd -r app && useradd -r -g app -d /app app
WORKDIR /app
COPY --from=builder /install /usr/local
COPY . .
USER app
EXPOSE {port}
CMD {start_command}
""",
    },
    "javascript": {
        "default": """\
# ===== BUILDER =====
FROM node:{version}-alpine AS builder

WORKDIR /app

COPY package*.json ./
RUN npm ci

COPY . .
{build_step}

# ===== RUNTIME =====
FROM node:{version}-alpine

RUN addgroup -S appgroup && adduser -S appuser -G appgroup

WORKDIR /app

COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/{build_output} ./{build_output}
COPY --from=builder /app/package.json .

RUN chown -R appuser:appgroup /app
USER appuser

EXPOSE {port}

HEALTHCHECK --interval=30s --timeout=3s \\
    CMD wget -qO- http://localhost:{port}/health || exit 1

CMD {start_command}
""",
        "static": """\
FROM node:{version}-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/{build_output} /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
""",
    },
    "typescript": {
        "default": """\
FROM node:{version}-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
{build_step}

FROM node:{version}-alpine
RUN addgroup -S appgroup && adduser -S appuser -G appgroup
WORKDIR /app
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/{build_output} ./{build_output}
COPY --from=builder /app/package.json .
USER appuser
EXPOSE {port}
CMD {start_command}
""",
    },
    "go": {
        "default": """\
FROM golang:{version}-alpine AS builder

WORKDIR /app

COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /app/server {main_path}

FROM gcr.io/distroless/static-debian12

COPY --from=builder /app/server /server

EXPOSE {port}

ENTRYPOINT ["/server"]
""",
    },
    "rust": {
        "default": """\
FROM rust:{version}-slim AS builder
WORKDIR /app
COPY Cargo.toml Cargo.lock ./
RUN mkdir src && echo "fn main() {{}}" > src/main.rs && \\
    cargo build --release && \\
    rm -rf src
COPY . .
RUN cargo build --release

FROM gcr.io/distroless/cc-debian12
COPY --from=builder /app/target/release/{binary_name} /app/server
EXPOSE {port}
ENTRYPOINT ["/app/server"]
""",
    },
    "java": {
        "default": """\
FROM eclipse-temurin:{version}-jdk AS builder
WORKDIR /app
COPY . .
RUN {build_command}

FROM eclipse-temurin:{version}-jre
RUN groupadd -r app && useradd -r -g app app
WORKDIR /app
COPY --from=builder /app/{jar_path} app.jar
USER app
EXPOSE {port}
ENTRYPOINT ["java", "-jar", "app.jar"]
""",
    },
    "php": {
        "default": """\
FROM composer:{composer_version} AS vendor
WORKDIR /app
COPY composer.json composer.lock ./
RUN composer install --no-dev --no-scripts --no-autoloader
COPY . .
RUN composer dump-autoload --optimize

FROM php:{version}-fpm-alpine
RUN docker-php-ext-install pdo pdo_mysql opcache {php_extensions}
RUN addgroup -S app && adduser -S app -G app
WORKDIR /var/www/html
COPY --from=vendor /app .
USER app
EXPOSE 9000
CMD ["php-fpm"]
""",
    },
    "ruby": {
        "default": """\
FROM ruby:{version}-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends build-essential {system_deps} && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY Gemfile Gemfile.lock ./
RUN bundle config set --local deployment true && bundle config set --local without development test && bundle install
COPY . .
{build_step}

FROM ruby:{version}-slim
RUN apt-get update && apt-get install -y --no-install-recommends {runtime_deps} && rm -rf /var/lib/apt/lists/*
RUN groupadd -r app && useradd -r -g app app
WORKDIR /app
COPY --from=builder /app .
USER app
EXPOSE {port}
CMD {start_command}
""",
    },
    "csharp": {
        "default": """\
FROM mcr.microsoft.com/dotnet/sdk:{version} AS builder
WORKDIR /app
COPY *.csproj ./
RUN dotnet restore
COPY . .
RUN dotnet publish -c Release -o out

FROM mcr.microsoft.com/dotnet/aspnet:{version}
RUN addgroup --system app && adduser --system --ingroup app app
WORKDIR /app
COPY --from=builder /app/out .
USER app
EXPOSE {port}
ENTRYPOINT ["dotnet", "{assembly}.dll"]
""",
    },
    "elixir": {
        "default": """\
FROM elixir:{version}-alpine AS builder
RUN apk add --no-cache build-base git {system_deps}
WORKDIR /app
ENV MIX_ENV=prod
RUN mix local.hex --force && mix local.rebar --force
COPY mix.exs mix.lock ./
RUN mix deps.get --only prod && mix deps.compile
COPY . .
RUN mix compile && mix release

FROM alpine:3.19
RUN apk add --no-cache libstdc++ openssl ncurses-libs
RUN addgroup -S app && adduser -S app -G app
WORKDIR /app
COPY --from=builder /app/_build/prod/rel/{app_name} .
USER app
EXPOSE {port}
CMD ["bin/{app_name}", "start"]
""",
    },
}


def select_template(language: str, variant: str = "default") -> str | None:
    lang_templates = TEMPLATES.get(language)
    if not lang_templates:
        return None
    return lang_templates.get(variant)


def _service_root_path(
    project_path: str,
    fingerprint: dict,
    service_root: str | None,
) -> Path:
    from pathlib import Path

    base = Path(project_path)
    if service_root:
        return base / service_root.strip().lstrip("./")
    env = fingerprint.get("environment") or {}
    rel = env.get("service_root_rel")
    if isinstance(rel, str) and rel.strip():
        return base / rel.strip().lstrip("./")
    return base


def fingerprint_allows_template_first(
    project_path: str,
    fingerprint: dict,
    service_root: str | None = None,
) -> bool:
    """Strong signal: servicable tree with requirements.txt, package.json, or uv lockfile."""
    root = _service_root_path(project_path, fingerprint, service_root)
    if not root.is_dir():
        return False

    if not service_root and not (fingerprint.get("environment") or {}).get("service_root_rel"):
        if fingerprint.get("is_monorepo") and len(fingerprint.get("services") or []) > 1:
            return False
        if len(fingerprint.get("services") or []) > 1:
            return False

    lang = (fingerprint.get("language") or {}).get("primary") or ""
    deps = fingerprint.get("dependencies") or {}
    manager = str(deps.get("manager") or "")

    if lang == "python":
        if (root / "uv.lock").is_file() and (root / "pyproject.toml").is_file():
            return True
        if (root / "Pipfile").is_file():
            return False
        if manager == "pip" and (root / "requirements.txt").is_file():
            return True
        if (root / "pyproject.toml").is_file() and manager in ("pip", "poetry", "uv"):
            return bool((root / "requirements.txt").is_file() or (root / "uv.lock").is_file())
        return False

    if lang in ("javascript", "typescript"):
        if not (root / "package.json").is_file():
            return False
        if (root.parent / "pnpm-workspace.yaml").is_file() and service_root is None:
            return False
        return True

    return False


def render_template_dockerfile(
    project_path: str,
    fingerprint: dict,
    service_root: str | None = None,
) -> str | None:
    """Fill a stock template from fingerprint + on-disk files. Returns None if not supported."""
    root = _service_root_path(project_path, fingerprint, service_root)
    lang = (fingerprint.get("language") or {}).get("primary") or "python"
    fw = fingerprint.get("framework") or {}
    fw_name = (fw.get("name") or "").lower() if isinstance(fw, dict) else str(fw).lower()

    variant = "default"
    if lang == "python" and (root / "uv.lock").is_file():
        variant = "python_uv"
    template = select_template(lang, variant)
    if not template and variant != "default":
        template = select_template(lang)
    if not template:
        return None

    start_cmd = "uvicorn main:app --host 0.0.0.0 --port 8000"
    if isinstance(fw, dict) and fw.get("start_command"):
        start_cmd = str(fw["start_command"])

    port_info = fingerprint.get("port") or {}
    port = 8080
    if isinstance(port_info, dict):
        try:
            port = int(port_info.get("value") or 8080)
        except (TypeError, ValueError):
            port = 8080

    lang_ver = (fingerprint.get("language") or {}).get("version") or "3.12"
    if lang in ("javascript", "typescript"):
        raw_ver = (fingerprint.get("language") or {}).get("version") or "20"
        lang_ver = re.sub(r"[^0-9.]+.*$", "", str(raw_ver)).strip(".") or "20"

    if lang == "python" and variant == "python_uv":
        if not (root / "pyproject.toml").is_file():
            return None
        return template.format(
            version=lang_ver,
            port=port,
            start_command=start_cmd,
        )

    if lang == "python":
        dep_file = "requirements.txt"
        if not (root / dep_file).is_file():
            return None
        sys_block = fingerprint.get("dependencies") or {}
        sys_pkgs = sys_block.get("system_packages_needed") or {}
        debian_pkgs = sys_pkgs.get("debian") or sys_pkgs.get("default") or []
        system_deps = " ".join(str(p) for p in debian_pkgs) or "gcc"
        return template.format(
            version=lang_ver,
            system_deps=system_deps,
            dep_file=dep_file,
            port=port,
            start_command=start_cmd,
        )

    if lang in ("javascript", "typescript"):
        build_step = "RUN npm run build" if "next" in fw_name or "nuxt" in fw_name else ""
        build_output = "dist" if "vite" in fw_name else "build"
        if "next" in fw_name:
            build_output = ".next"
        return template.format(
            version=lang_ver,
            build_step=build_step,
            build_output=build_output,
            port=port,
            start_command=start_cmd or '["npm", "start"]',
        )

    return None
