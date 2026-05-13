FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# Hatchling needs package sources (not only pyproject.toml) to build the wheel.
COPY pyproject.toml README.md LICENSE ./
COPY api ./api
COPY cli ./cli
COPY core ./core
COPY db ./db
COPY workers ./workers
COPY sdk ./sdk

RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim

ARG TARGETARCH=amd64
ARG DOCKER_STATIC_VERSION=27.3.1
ARG INSTALL_GCLOUD=0

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git ca-certificates curl gnupg; \
    case "${TARGETARCH}" in \
      amd64) DARCH=x86_64 ;; \
      arm64) DARCH=aarch64 ;; \
      *) DARCH=x86_64 ;; \
    esac; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${DARCH}/docker-${DOCKER_STATIC_VERSION}.tgz" \
      | tar xz --strip-components=1 -C /usr/local/bin docker/docker; \
    if [ "${INSTALL_GCLOUD}" = "1" ]; then \
      echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
        > /etc/apt/sources.list.d/google-cloud-sdk.list; \
      curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
        | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg; \
      apt-get update; \
      apt-get install -y --no-install-recommends google-cloud-cli; \
    fi; \
    rm -rf /var/lib/apt/lists/*

RUN groupadd -r app && useradd -r -g app -d /app app

WORKDIR /app

COPY --from=builder /install /usr/local
COPY . ./

RUN chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health')" || exit 1

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
