# Docker 内仅生成 SCIP

> 当前目录是**开发/过渡方案**，只解决「容器内编译并产出 `.scip`」这一步。
> 它不会把 `ingest / build-code-graph / chunk / embed / serve / MCP` 一并标准化，因此**不保证跨机器完全一致**。
> 需要标准化的 Linux/amd64 全容器路径时，请优先使用 [../full-stack/README.md](../full-stack/README.md)。

目标：镜像内只含 **JDK + Maven + Gradle + scip-java**，完成编译并产出 `index.scip`；**ingest / build-code-graph / chunk / embed** 仍在宿主机使用 [hybrid_platform](../../) 的 Python 虚拟环境。

## 构建镜像

在仓库内执行：

```bash
cd hybrid_platform/docker/scip-build
docker build --build-arg BASE_IMAGE=eclipse-temurin:17-jdk-jammy -t hybrid-scip-build:jdk17 -f Dockerfile .
```

常用多版本构建示例：

```bash
docker build --build-arg BASE_IMAGE=eclipse-temurin:11-jdk-jammy -t hybrid-scip-build:jdk11 -f Dockerfile .
docker build --build-arg BASE_IMAGE=eclipse-temurin:17-jdk-jammy -t hybrid-scip-build:jdk17 -f Dockerfile .
docker build --build-arg BASE_IMAGE=eclipse-temurin:21-jdk-jammy -t hybrid-scip-build:jdk21 -f Dockerfile .
docker build --build-arg BASE_IMAGE=eclipse-temurin:23-jdk -t hybrid-scip-build:jdk23 -f Dockerfile .
```

注意：`eclipse-temurin:23-jdk-jammy` 不存在，JDK 23 请用 `eclipse-temurin:23-jdk`。

## 生成 `.scip`

```bash
./docker_scip_build.sh /abs/path/to/git-checkout [/abs/path/to/index.scip] -- --build-tool maven
```

`--` 之后原样传给 `scip-java`（与宿主机 `index-java` 在 `--` 后传 Maven/Gradle 参数相同）。默认输出为 `<repo>/index.scip`。

## 宿主机续跑索引

```bash
cd hybrid_platform
source myenv/bin/activate # 或按项目规则指定解释器

./scripts/index_build_repo_commit.sh \
  --config ./config/default_config.json \
  --repo-name owner/repo \
  --commit deadbeefdeadbeefdeadbeefdeadbeefdeadbeef \
  --repo-root /abs/path/to/git-checkout \
  --prebuilt-scip /abs/path/to/index.scip
```

或使用一键 clone脚本（在 `--prebuilt-scip` 指向**宿主机上**已存在的 `.scip`）：

```bash
./scripts/repo_commit_to_index.sh \
  --config ./config/default_config.json \
  --repo-name owner/repo \
  --commit deadbeefdeadbeefdeadbeefdeadbeefdeadbeef \
  --dest /abs/path/to/git-checkout \
  --git-url https://github.com/owner/repo.git \
  --prebuilt-scip /abs/path/to/index.scip
```

## 说明

- 源码挂载为可写（`/work`），因 Maven/Gradle 需在仓库内写 `target/`、`build/` 等。
- 脚本默认会挂载 `/data1/qadong/.m2/repository`、`/data1/qadong/.gradle`、`/data1/qadong/tmp` 到容器内，避免把缓存和临时文件写回根分区。
- `JAVA_TOOL_OPTIONS`、`MAVEN_OPTS`、`GRADLE_USER_HOME` 会通过 `docker run -e` 传入容器。

## 批量跑 manifest

已提供正式脚本：

```bash
cd hybrid_platform
source myenv/bin/activate

./scripts/docker_manifest_build.sh \
  --manifest "/data1/qadong/codeindex_java/JAVA test/test_java_agent_manifest_size_ge_100000.jsonl" \
  --config ./var/server_vllm_generic_config.json \
  --overrides ./var/java_eval_overrides.json \
  --build-images
```

默认只跑 `targets.json` 中 `index_status == missing` 的目标。可用 `--slug`、`--limit`、`--dry-run` 缩小范围验证。
