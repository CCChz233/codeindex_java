# Docker 内仅生成 SCIP

> 当前目录是**开发/过渡方案**，只解决「容器内编译并产出 `.scip`」这一步。
> 它不会把 `ingest / build-code-graph / chunk / embed / serve / MCP` 一并标准化，因此**不保证跨机器完全一致**。
> 需要标准化的 Linux/amd64 全容器路径时，请优先使用 [../full-stack/README.md](../full-stack/README.md)。

目标：镜像内只含 **JDK + Maven + Gradle + scip-java**，完成编译并产出 `index.scip`；**ingest / build-code-graph / chunk / embed** 仍在宿主机使用 [hybrid_platform](../../) 的 Python 虚拟环境。

## 构建镜像

在仓库内执行：

```bash
cd hybrid_platform/docker/scip-build
docker build -t hybrid-scip-build:local -f Dockerfile .
```

需要 **JDK 21 / 23** 时，可修改 `Dockerfile` 第一行为 `eclipse-temurin:21-jdk-jammy` 等并重建 tag。

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
- 可选：为加速依赖下载增加 `-v maven-repo:/root/.m2` 等命名卷。
- `JAVA_TOOL_OPTIONS`、`MAVEN_OPTS` 会通过 `docker run -e` 传入容器。
