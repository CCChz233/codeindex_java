# 交接文档：Java 代码索引与远程 MCP（codeindex_java / hybrid_platform）

本文面向 **运维与后续开发**，说明从源码到 SQLite 索引、再到公网/内网 **Streamable HTTP MCP** 的完整链路、关键代码位置与常见故障。仓库根目录以 `hybrid_platform/` 为准（Python 包名 `hybrid_platform`）。

---

## 1. 环境与运行约定

### 1.1 Python 虚拟环境

- 在 `codeindex_java` 下执行 Python / CLI 时，应使用 **`hybrid_platform/myenv`**，不要用未激活的 conda base（仓库 `.cursor/rules` 中有说明）。
- 推荐：
  ```bash
  cd /path/to/codeindex_java/hybrid_platform
  ./myenv/bin/python -m hybrid_platform.cli --help
  ```
- 安装依赖：`./myenv/bin/python -m pip install -r requirements.txt`（若 venv 从别处复制，勿用错 shebang 的 `pip` 脚本）。

### 1.2 系统依赖（索引侧）

- **JDK**：与目标仓库一致（常见 Java 21）；`JAVA_HOME` 必须指向含 `bin/java` 的根目录。
- **scip-java**：由配置 `java_index.scip_java_cmd` 指定，默认可指向仓库内包装脚本（如 `scripts/run-scip-java-spring62.sh`）。
- **构建工具**：Maven 或 Gradle，须与项目一致（例如 Artemis 用 Gradle）。

### 1.3 系统依赖（MCP 网关侧）

- **nginx**：`start_mcp_gateway_8765.sh` 依赖 PATH 中的 `nginx`；网关用 **`nginx -p <runtime_dir> -c nginx.conf`** 独立前缀启动，不污染系统主配置。
- 公网访问需在安全组/防火墙放行对应端口（如 **8765** 或仅放行边缘 **443** 由反代转发）。

---

## 2. 核心概念与命名

### 2.1 逻辑仓库名与 commit

- **`repo`（逻辑名）**：字符串，如 `ls1intum/Artemis`，与 GitHub 组织/仓库名一致即可，**写入索引与 metadata**。
- **`commit`**：Git commit SHA，**7–40 位十六进制**；代码中统一 **小写**。

### 2.2 Slug、数据库路径、MCP URL 路径

三者由 **`hybrid_platform/index_slug.py`** 统一推导：

| 概念 | 规则 | 示例 |
|------|------|------|
| **slug** | `sanitize_repo_name(repo) + "_" + commit` | `ls1intum_Artemis_7364749a8de08befd5f96e9dfecf6d13e241944a` |
| **SQLite 文件** | `{output_dir}/{slug}.db` | `var/hybrid_indices/<slug>.db` |
| **HTTP MCP 子路径** | `/mcp/<slug>` | `/mcp/ls1intum_Artemis_7364…` |

CLI 调试：

```bash
./myenv/bin/python -m hybrid_platform.index_slug 'ls1intum/Artemis' '7364749a8de08befd5f96e9dfecf6d13e241944a'
```

**重要**：多库并存时，**每个索引对应唯一 URL 路径** `http(s)://<host>:<port>/mcp/<slug>`，客户端不可只写 `/mcp`。

---

## 3. 索引构建全流程（从 Git 到可检索 DB）

### 3.1 阶段总览（顺序固定）

流水线与 **`index_build_runner.run_java_full_index_pipeline`** 及 Shell **`scripts/index_build_repo_commit.sh`** 对齐：

1. **scip-java**：在 `--repo-root` 下编译并生成 **`.scip`**（或 SemanticDB 再转 SCIP，视配置而定）。
2. **ingest**：将 SCIP 写入 SQLite（symbols/documents/occurrences 等）。
3. **build-code-graph**：构建调用图等 **`code_edges`**，供 chunk 阶段 **call context** 使用。  
   **必须在 chunk 之前**；若跳过，chunk 仍可跑，但图相关上下文缺失。
4. **chunk**：按 AST/策略切分代码块写入库表。
5. **embed**：对 chunk 做向量化（可能调用外部 Embedding API，需配置与网络）。

Shell 脚本 **`index_build_repo_commit.sh`** 中阶段名为：`index-java` → `build-code-graph` → `chunk` → `embed`。  
其中 **`index-java` 子命令内部已包含 ingest**（见下节）。

### 3.2 代码入口对照

| 步骤 | 用户入口 | 核心实现 |
|------|----------|----------|
| scip + ingest | `python -m hybrid_platform.cli index-java ...` | `cli.cmd_index_java` → `JavaIndexer.run` → `IngestionPipeline.run` |
| 调用图 | `cli build-code-graph` | `CodeGraphBuilder` |
| 分块 | `cli chunk` | 配置驱动的 embedding pipeline 的 `build_chunks` |
| 向量 | `cli embed` | 同上 pipeline 的 `run` |
| 程序化整链 | `index_build_runner.run_java_full_index_pipeline` | 与上顺序一致，供 Admin 任务等调用 |

**`java_indexer.py`**：`JavaIndexer` 拼出 scip-java 命令（`index` 或 `index-semanticdb`），在 `repo_root` 下 `subprocess.run`，产出 `.scip` 路径。

**`ingestion.py`**：`IngestionPipeline` 读 SCIP，写入 `SqliteStore`。

**`index_build_repo_commit.sh`**（节选逻辑）：

- 解析 `--config`、`--repo-name`、`--commit`、`--repo-root`、`--output-dir`（默认 `var/hybrid_indices`）、`--java-home`、`--build-tool`。
- `commit` 转小写；用 Python 计算 `SLUG` 与 `DB_PATH`。
- 依次 `run_cli index-java ...`、`build-code-graph`、`chunk`、`embed`。
- 日志：`>>> PIPELINE_STAGE_START|OK|FAILED: <stage>`。

环境变量可跳过某步：`SKIP_CODE_GRAPH`、`SKIP_CHUNK`、`SKIP_EMBED`（仅当明确知晓影响时使用）。

### 3.3 一键：克隆 + 索引 + 注册 metadata

| 脚本 | 行为 |
|------|------|
| **`scripts/repo_commit_to_index.sh`** | 可选 `clone_repo_at_commit.sh` → `index_build_repo_commit.sh` → **`python -m hybrid_platform.index_metadata upsert`** |
| **`scripts/repo_commit_to_mcp.sh`** | 薄封装，**当前等同** `repo_commit_to_index.sh`（**不再**在脚本内启动常驻 MCP） |

完成后需 **单独启动网关**：`scripts/start_mcp_gateway_8765.sh`。

**环境变量**：

- `INDEX_METADATA_FILE`：metadata 路径，默认 `var/index_metadata.json`。
- `HYBRID_PYTHON`：可覆盖 venv 的 `python` 路径。

### 3.4 仅克隆 / 仅构建

- **克隆**：`scripts/clone_repo_at_commit.sh`（`--git-url`、`--commit`、`--dest`，可选 `--recurse-submodules`、`--shallow`）。
- **仅构建**（已有工作树）：`index_build_repo_commit.sh`，无需 git-url。

更细的 JDK、Gradle/Maven、Artemis 等说明见 **[java_index_repo_setup.md](./java_index_repo_setup.md)**（其中 §5「一键」描述略旧，**以本文与 `repo_commit_to_index.sh` 为准**）。

---

## 4. 索引注册表 `index_metadata.json`

### 4.1 作用

多份 `.db` 多进程 MCP 时，用一份 JSON 描述 **每条索引的 db 路径、对外 MCP 路径、配置路径**，供：

- **`mcp_gateway_local`** 拉起多个 `mcp-streamable` + 生成 Nginx 配置；
- 运维 **`index_metadata` CLI** 增删改查。

### 4.2 默认路径与结构

- 默认文件：**`hybrid_platform/var/index_metadata.json`**（`index_metadata.default_metadata_path()`）。
- 结构：`version` + `entries[]`，每条含 `slug`、`repo`、`commit`、`db_path`、`mcp_path`、`config_path`、`status`、`updated_at`。

### 4.3 CLI（模块 `hybrid_platform.index_metadata`）

```bash
cd hybrid_platform
./myenv/bin/python -m hybrid_platform.index_metadata upsert \
  --repo 'org/repo' --commit '<40hex>' \
  --config ./config/default_config.json \
  --output-dir ./var/hybrid_indices
# --db 可省略，按 output-dir + slug 推导
./myenv/bin/python -m hybrid_platform.index_metadata list
./myenv/bin/python -m hybrid_platform.index_metadata remove --slug '<slug>'
./myenv/bin/python -m hybrid_platform.index_metadata nginx-conf   # 打印完整 nginx http{} 到 stdout
./myenv/bin/python -m hybrid_platform.index_metadata backend-map # JSON：slug → 内网端口
```

示例 JSON：**`examples/index_metadata.example.json`**。

---

## 5. MCP 架构与流程

### 5.1 两种部署形态

**A. 单进程直连（调试或单库）**

- 命令：`python -m hybrid_platform.mcp_streamable_server` 或 `cli mcp-streamable --db ...`。
- 环境变量：`HYBRID_DB`、`HYBRID_CONFIG`、`HYBRID_MCP_HOST`、`HYBRID_MCP_PORT`、`HYBRID_MCP_PATH`、`HYBRID_MCP_STATELESS`、`HYBRID_MCP_BEARER_TOKEN`。
- 文档：**[mcp_streamable_http.md](./mcp_streamable_http.md)**。

**B. 多库 + 单端口网关（生产推荐形态）**

- **`mcp_gateway_local start`**：
  1. 读 `index_metadata.json`，筛选 **`status == "ready"` 且 `db_path` 文件存在** 的条目。
  2. 按 **slug 排序**，为第 *i* 条分配 **`127.0.0.1:(backend_base + i)`**（默认 `backend_base=28065`）。
  3. 对每个 entry `subprocess` 启动：  
     `python -m hybrid_platform.cli mcp-streamable --db ... --mcp-path ... --config ...`  
     并设置环境变量 **`HYBRID_MCP_HOST=127.0.0.1`**、**`HYBRID_MCP_PORT=<内网端口>`**、**`HYBRID_MCP_PATH=<entry.mcp_path>`**。
  4. 写 **`var/mcp_gateway/runtime/nginx.conf`**，执行 **`nginx -p <runtime> -c nginx.conf`**，监听 **`--listen`（默认 8765）**。
  5. Nginx **`location` 与 `mcp_path` 一致**，`proxy_pass` 到对应内网端口。

**关键**：反代到后端时 **`Host` 必须设为 `127.0.0.1:<内网端口>`**（已在 `index_metadata.render_nginx_gateway_conf` 中实现），否则 FastMCP 在 `127.0.0.1` 监听时会启用 DNS 重绑定校验，公网 `Host` 会导致 **421 Invalid Host header**。

### 5.2 Shell 封装

| 脚本 | 说明 |
|------|------|
| `scripts/start_mcp_gateway_8765.sh` | `mcp_gateway_local start --listen 8765`；支持 `INDEX_METADATA_FILE`、`MCP_GATEWAY_RUNTIME` |
| `scripts/stop_mcp_gateway_8765.sh` | `stop`：终止 backend pid 列表与本 runtime 下 nginx |

**`mcp_gateway_local` 行为摘要**：

- 默认 **`--no-stop-first` 未设置** 时：先停同一 `runtime-dir` 内旧进程。
- 默认在起 nginx 前 **释放监听端口**（对占用 `--listen` 的进程 `SIGTERM`）；可用 **`--no-free-listen-port`** 关闭。
- 依赖 PATH 中的 **`nginx`**。

### 5.3 MCP 工具与运行时

- **注册**：`mcp_tools_registry.register_codeindex_tools` → 只读工具 **`semantic_query`**、**`find_symbol`**、**`symbol_graph`**（不含 `code_graph_explore`；多跳图走 HTTP `serve` 的 graph API）。
- **运行时**：`mcp_env_runtime.get_mcp_runtime()` 读 **`HYBRID_DB` / `HYBRID_CONFIG`**，封装 **`CodeindexMcpRuntime`**。
- **传输**：FastMCP **Streamable HTTP**；规范要求客户端 **`Accept`** 含 **`application/json`** 与 **`text/event-stream`**（POST）；单独 GET SSE 需 **`Accept: text/event-stream`**。
- **可选 Bearer**：`mcp_streamable_asgi.compose_optional_bearer_auth`；与 Admin 的 **`HYBRID_ADMIN_TOKEN`** 分离。

### 5.4 客户端配置要点

- URL 必须带 **完整 path**：`http://<公网IP或域名>:8765/mcp/<slug>`。
- 使用 **`mcp-remote`** 等时通常需 **`--transport http-only --allow-http`**（若未上 TLS）。
- 工具契约与错误文案见 **[mcp_metadata_and_errors.md](./mcp_metadata_and_errors.md)**。

### 5.5 公网边缘与 TLS

- 仅暴露 443、内网监听 MCP 的架构与示例见 **[deploy_public_mcp_edge_proxy.md](./deploy_public_mcp_edge_proxy.md)**。

---

## 6. 关键文件索引（速查）

| 路径 | 说明 |
|------|------|
| `hybrid_platform/index_slug.py` | slug / db 路径 / `/mcp/...` 路径 |
| `hybrid_platform/index_metadata.py` | metadata 读写、nginx 配置渲染、CLI |
| `hybrid_platform/mcp_gateway_local.py` | 多后端 + nginx 网关 |
| `hybrid_platform/mcp_streamable_server.py` | 单进程 Streamable MCP |
| `hybrid_platform/mcp_streamable_asgi.py` | Bearer 包装 |
| `hybrid_platform/mcp_tools_registry.py` | MCP 工具注册 |
| `hybrid_platform/java_indexer.py` | scip-java 调用 |
| `hybrid_platform/ingestion.py` | SCIP → SQLite |
| `hybrid_platform/index_build_runner.py` | 程序化整链（与脚本顺序一致） |
| `hybrid_platform/cli.py` | 所有子命令入口（含 `index-java`、`mcp-streamable`） |
| `hybrid_platform/admin_index_jobs.py` | 异步索引任务（HTTP Admin） |
| `scripts/repo_commit_to_index.sh` | 克隆 + 构建 + upsert |
| `scripts/index_build_repo_commit.sh` | 仅构建四阶段 |
| `scripts/start_mcp_gateway_8765.sh` / `stop_mcp_gateway_8765.sh` | 网关启停 |
| `config/default_config.json` | 默认应用配置（embedding、chunk、java_index 等） |
| `var/hybrid_indices/*.db` | 索引库默认目录 |
| `var/index_metadata.json` | 网关注册表 |
| `var/mcp_gateway/runtime/` | nginx prefix、pid、日志、backend_pids.txt |

---

## 7. 故障排查清单

| 现象 | 可能原因 | 处理 |
|------|----------|------|
| 网关启动报 *no ready entries* | metadata 空、路径错、或 `.db` 不存在 | `index_metadata upsert`；确认 `db_path` 与磁盘一致 |
| **421 Invalid Host header** | 反代把公网 Host 原样传给监听在 127.0.0.1 的 FastMCP | 使用当前仓库生成的 nginx 配置（已设 `Host 127.0.0.1:<port>`） |
| **406 Not Acceptable**（JSON 提示缺 `text/event-stream`） | curl/客户端未带规范要求的 Accept | POST 带 `Accept: application/json, text/event-stream`；GET SSE 带 `Accept: text/event-stream` |
| **bind 8765 失败** | 端口被旧 nginx/其它进程占用 | `stop_mcp_gateway_8765.sh`；或网关默认会尝试 `SIGTERM` 占用端口的进程 |
| **embed 慢或限流** | 外部 Embedding API / HF | 检查配置与 `HF_TOKEN` 等 |
| scip-java / Gradle 失败 | JDK 版本、内存、`--build-tool` 错误 | 见 `java_index_repo_setup.md`、构建日志 |

日志：

- 索引各阶段：`stderr` 中 **`PIPELINE_STAGE_*`**。
- MCP 后端：`var/mcp_gateway/runtime/logs/mcp_<slug>.log`。
- nginx：`var/mcp_gateway/runtime/logs/error.log`。

---

## 8. 相关文档（延伸阅读）

- [java_index_repo_setup.md](./java_index_repo_setup.md) — 克隆、JDK、Maven/Gradle、Artemis 注意点  
- [mcp_streamable_http.md](./mcp_streamable_http.md) — 单进程 Streamable MCP、环境变量  
- [deploy_public_mcp_edge_proxy.md](./deploy_public_mcp_edge_proxy.md) — 公网反代与安全组  
- [mcp_metadata_and_errors.md](./mcp_metadata_and_errors.md) — 工具契约与错误语义  
- [mcp_delivery_handbook.md](./mcp_delivery_handbook.md) — 交付与多索引策略总览  

---

## 9. 交接检查建议（接手人可做）

1. `cd hybrid_platform && ./myenv/bin/python -m hybrid_platform.cli --help` 能运行。  
2. 对测试用小仓库跑通 `index_build_repo_commit.sh`（或 `repo_commit_to_index.sh`）。  
3. `index_metadata list` 能看到条目；`start_mcp_gateway_8765.sh` 后本机 `curl` 带正确 `Accept` 能返回 200。  
4. 阅读 `config/default_config.json` 中与 `embedding`、`java_index`、`chunk` 相关的段。  
5. 若上生产：Bearer + TLS + 防火墙与本文 §5.5 对齐。

---

*文档生成自仓库当前实现；若脚本与 md 冲突，以 `hybrid_platform` 下代码与 `scripts/*.sh` 为准。*
