# Java 索引：克隆指定 commit 与指定 JDK 版本

`index-java` 在 **`--repo-root`** 下调用 **scip-java**，由 **Maven/Gradle 使用当前进程的 JDK** 编译并生成 SemanticDB。因此：

- **必须先**在磁盘上有「已检出目标 commit」的源码树（本仓库提供 `scripts/clone_repo_at_commit.sh`）。
- **JDK 版本**通过 **`JAVA_HOME`**（或构建脚本 **`--java-home`**）指定；与 hybrid 的 **Python 虚拟环境**无关。

## 1. 平台

- 建议在 **linux/amd64** 上跑索引（与常见 CI / 容器一致，避免 ARM 上原生工具链差异）。
- 若用 Docker：镜像选 **`linux/amd64`** + **JDK 21**（如 `eclipse-temurin:21-jdk`），在容器内执行 `clone_repo_at_commit.sh` 与 `index_build_repo_commit.sh`。

## 2. 克隆指定 commit

```bash
cd /path/to/hybrid_platform

./scripts/clone_repo_at_commit.sh \
  --git-url https://github.com/ls1intum/Artemis.git \
  --commit 7364749a8de08befd5f96e9dfecf6d13e241944a \
  --dest /data/worktrees/Artemis-7364749 \
  --recurse-submodules
```

- **`--repo-name`**（给索引用的逻辑名）可与 GitHub 一致，例如 **`ls1intum/Artemis`**（与 `index_slug`、`.db` 文件名一致）。
- 若仓库依赖 **子模块**，请加 **`--recurse-submodules`**。
- 若目标 commit 在 **非默认分支** 上：默认 **完整 clone** 再 `checkout` 一般最稳。
- **浅 clone**：`clone_repo_at_commit.sh --shallow`（或一键脚本 `--clone-shallow`）用 `git fetch --depth 1 origin <commit>`，体积小，但依赖远端是否允许按 SHA 浅取；失败时请去掉该参数改完整 clone。

## 3. 指定 JDK（例如 Java 21）

任选其一即可（构建脚本里 **命令行优先**）：

```bash
# 方式 A：环境变量（对当前 shell 下所有子进程生效）
export JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
export PATH="$JAVA_HOME/bin:$PATH"
java -version

# 方式 B：仅本次构建
./scripts/index_build_repo_commit.sh \
  --config ./config/default_config.json \
  --repo-name ls1intum/Artemis \
  --commit 7364749a8de08befd5f96e9dfecf6d13e241944a \
  --repo-root /data/worktrees/Artemis-7364749 \
  --java-home /usr/lib/jvm/java-21-openjdk-amd64 \
  --build-tool gradle \
  -- -DskipTests
```

- **Artemis** 在 scip-java 侧会按 **Gradle** 工程识别；若写 `--build-tool maven` 会报错：`none of them match the explicitly provided flag '--build-tool=maven'`。请改为 **`gradle`**，或**不传** `--build-tool`（由本仓库 `detect_build_tool` 与 scip-java 再对齐；多构建系统并存时以 scip-java 提示为准）。
- **Debian/Ubuntu**：先安装 **`openjdk-21-jdk`**（或你们认可的 JDK 21）；路径以本机为准，`ls /usr/lib/jvm`。**不要**写不存在的目录，否则 scip-java / Maven 会报 `JAVA_HOME is set to an invalid directory`。可用下面命令核对：
  ```bash
  readlink -f "$(command -v java)"
  dirname "$(dirname "$(readlink -f "$(command -v java)")")"   # 常可作为 JAVA_HOME
  ```
- 构建脚本会在 `--java-home` 无效但 `PATH` 上仍有 `java` 时，**尝试**从 `which java` 反推 `JAVA_HOME` 并打日志；若仍失败则退出并提示安装。
- **SDKMAN / 自定义安装**：指向该 JDK 根目录（内含 `bin/java`）。
- **scip-java** 需与项目 JDK 兼容；若构建失败，检查 `scip-java` 版本与官方对 Java 21 的支持说明。

## 4. 与「任务名 / 分支标签」的关系

例如 `.ls1intum__Artemis-11249` 仅为你方 **任务或分支的命名**；索引侧需要稳定的是：

- **`--repo-name`**：建议固定为 **`ls1intum/Artemis`**（或你们统一的逻辑 repo 字符串）。
- **`--commit`**：该任务对应的 **base commit**（如 `7364749a8d…`）。

任务 id 不必写进 `repo_name`，除非你们刻意按任务维度拆分索引命名。

## 5. 一键：clone → 索引 → MCP

**完整交接说明（索引流水线、metadata、8765 网关、故障排查）见 [HANDOVER_JAVA_INDEX_AND_MCP.md](./HANDOVER_JAVA_INDEX_AND_MCP.md)。**

当前推荐一键脚本：

- [scripts/repo_commit_to_index.sh](../scripts/repo_commit_to_index.sh)：**clone（可选）→ 构建 → 写入 `var/index_metadata.json`**；**不**在本脚本内启动常驻 MCP。
- [scripts/repo_commit_to_mcp.sh](../scripts/repo_commit_to_mcp.sh)：等同调用 `repo_commit_to_index.sh`。
- 构建完成后在本机起多库网关：[scripts/start_mcp_gateway_8765.sh](../scripts/start_mcp_gateway_8765.sh)（依赖系统 `nginx`）。

stderr 中会打印阶段标记，失败时搜索 **`PIPELINE_STAGE_FAILED`**（如 `clone_git`、`index-java`、`build-code-graph`、`chunk`、`embed`、`metadata_upsert`）。

- 源码已在 `--dest` 且已是目标 commit：加 **`--skip-clone`**，此时**不要**再传 `--git-url`。

构建子步骤的详细标记由 [index_build_repo_commit.sh](../scripts/index_build_repo_commit.sh) 输出（`PIPELINE_STAGE_START|OK|FAILED`）。

## 6. 相关脚本

| 脚本 | 作用 |
|------|------|
| [scripts/repo_commit_to_index.sh](../scripts/repo_commit_to_index.sh) | **一键** clone（可选）→ 构建 → `index_metadata` upsert |
| [scripts/repo_commit_to_mcp.sh](../scripts/repo_commit_to_mcp.sh) | 同上（兼容入口名） |
| [scripts/start_mcp_gateway_8765.sh](../scripts/start_mcp_gateway_8765.sh) | 读 metadata，多 MCP + Nginx 监听 8765 |
| [scripts/stop_mcp_gateway_8765.sh](../scripts/stop_mcp_gateway_8765.sh) | 停止上述网关 |
| [scripts/clone_repo_at_commit.sh](../scripts/clone_repo_at_commit.sh) | `git clone` / `fetch` + `checkout` |
| [scripts/index_build_repo_commit.sh](../scripts/index_build_repo_commit.sh) | `index-java` → `build-code-graph` → `chunk` → `embed` |
| [scripts/mcp_start_repo_commit.sh](../scripts/mcp_start_repo_commit.sh) | 单库按 env 起 MCP（无网关时可用） |
