# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.11

FROM python:${PYTHON_VERSION}-slim-bookworm AS python-base
ARG TARGETARCH

ENV HYBRID_ROOT=/opt/hybrid_platform \
    HYBRID_PYTHON=/usr/local/bin/python \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR ${HYBRID_ROOT}

RUN if [ "${TARGETARCH}" != "amd64" ]; then \
      echo "This container stack is standardized only for linux/amd64. Use docker buildx build --platform linux/amd64."; \
      exit 1; \
    fi

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY . .

RUN python -m pip install --upgrade pip setuptools wheel \
 && python -m pip install -r requirements.txt \
 && python -m pip install -e . \
 && chmod +x docker/full-stack/entrypoints/*.sh


FROM python-base AS runtime
CMD ["python", "-m", "hybrid_platform.cli", "--help"]


FROM python-base AS indexer
ARG TARGETARCH
ARG JDK_VERSION=21
ARG GRADLE_VERSION=8.10.2
ARG COURSIER_VERSION=2.1.10
ARG SCIP_JAVA_VERSION=0.12.3

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    curl \
    git \
    maven \
    unzip \
 && rm -rf /var/lib/apt/lists/*

RUN if [ "${TARGETARCH}" != "amd64" ]; then \
      echo "Indexer image supports only linux/amd64."; \
      exit 1; \
    fi \
 && curl -fsSL "https://api.adoptium.net/v3/binary/latest/${JDK_VERSION}/ga/linux/x64/jdk/hotspot/normal/eclipse" -o /tmp/jdk.tar.gz \
 && mkdir -p /opt/java/openjdk \
 && tar -xzf /tmp/jdk.tar.gz -C /opt/java/openjdk --strip-components=1 \
 && rm -f /tmp/jdk.tar.gz

ENV JAVA_HOME=/opt/java/openjdk \
    PATH=/opt/java/openjdk/bin:${PATH}

RUN curl -fsSL "https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip" -o /tmp/gradle.zip \
 && unzip -q /tmp/gradle.zip -d /opt \
 && rm -f /tmp/gradle.zip \
 && ln -sf "/opt/gradle-${GRADLE_VERSION}/bin/gradle" /usr/local/bin/gradle

RUN curl -fsSL "https://github.com/coursier/coursier/releases/download/v${COURSIER_VERSION}/cs-x86_64-pc-linux.gz" \
  | gzip -d > /usr/local/bin/cs \
 && chmod +x /usr/local/bin/cs \
 && printf '%s\n' \
    '#!/bin/sh' \
    'exec /usr/local/bin/cs launch com.sourcegraph:scip-java_2.13:'"${SCIP_JAVA_VERSION}"' -- "$@"' \
    > /usr/local/bin/scip-java \
 && chmod +x /usr/local/bin/scip-java

ENTRYPOINT ["/opt/hybrid_platform/docker/full-stack/entrypoints/index_repo_commit.sh"]
CMD ["--help"]
