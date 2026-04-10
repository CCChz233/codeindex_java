# 仅用于在容器内运行 scip-java 生成 index.scip；ingest / chunk / embed 在宿主机 hybrid_platform Python 环境执行。
# 构建：docker build -t hybrid-scip-build:local -f Dockerfile .
#
# 多 JDK：可改为 eclipse-temurin:21-jdk-jammy 或并行建多个 tag。

FROM eclipse-temurin:17-jdk-jammy

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    git curl ca-certificates unzip \
    maven \
  && rm -rf /var/lib/apt/lists/*

ARG GRADLE_VERSION=8.10.2
RUN curl -fsSL "https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip" -o /tmp/gradle.zip \
  && unzip -q /tmp/gradle.zip -d /opt \
  && rm /tmp/gradle.zip \
  && ln -sf "/opt/gradle-${GRADLE_VERSION}/bin/gradle" /usr/local/bin/gradle

ARG COURSIER_VERSION=2.1.10
ARG SCIP_JAVA_VERSION=0.12.3
RUN curl -fsSL "https://github.com/coursier/coursier/releases/download/v${COURSIER_VERSION}/cs-x86_64-pc-linux.gz" \
  | gzip -d > /usr/local/bin/cs \
  && chmod +x /usr/local/bin/cs

# 稳定入口：不依赖 cs install 生成的脚本名
RUN printf '%s\n' \
  '#!/bin/sh' \
  'exec /usr/local/bin/cs launch com.sourcegraph:scip-java_2.13:'"${SCIP_JAVA_VERSION}"' -- "$@"' \
  > /usr/local/bin/scip-java \
  && chmod +x /usr/local/bin/scip-java

WORKDIR /work
