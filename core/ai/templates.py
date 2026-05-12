from __future__ import annotations

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
