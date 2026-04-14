# 仅用于在容器内运行 scip-java 生成 index.scip；ingest / chunk / embed 在宿主机 hybrid_platform Python 环境执行。
# 构建：docker build --build-arg BASE_IMAGE=eclipse-temurin:17-jdk-jammy -t hybrid-scip-build:jdk17 -f Dockerfile .
#
# 多 JDK：11/17/21 可用 *-jdk-jammy，23 起通常直接用 eclipse-temurin:23-jdk 之类的通用 tag。

ARG BASE_IMAGE=eclipse-temurin:17-jdk-jammy
FROM ${BASE_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    git curl ca-certificates unzip \
    make gcc g++ \
  && rm -rf /var/lib/apt/lists/*

ARG MAVEN_VERSION=3.9.9
RUN curl -fsSL "https://archive.apache.org/dist/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz" \
  | tar -xz -C /opt \
  && ln -sf "/opt/apache-maven-${MAVEN_VERSION}/bin/mvn" /usr/local/bin/mvn
ENV MAVEN_HOME="/opt/apache-maven-${MAVEN_VERSION}"

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
