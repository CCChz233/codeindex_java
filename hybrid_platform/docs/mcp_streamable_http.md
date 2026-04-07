# MCP Streamable HTTP（远程、无 stdio）

**交付总览与多索引策略**见 [mcp_delivery_handbook.md](./mcp_delivery_handbook.md)。

本仓库提供 **原生远程 MCP 服务**：传输层为 MCP 规范中的 **Streamable HTTP**（由 FastMCP + Uvicorn 提供），适用于部署在云主机、K8s 等环境，**不依赖 stdio 子进程**。

## 与 stdio 入口的关系

| 入口 | 模块 | 传输 | 典型场景 |
|------|------|------|----------|
| 本地 IDE | `python -m hybrid_platform.mcp_server` | stdio | Cursor / Claude Desktop 拉起子进程 |
| 云端 / 跨网 | `python -m hybrid_platform.mcp_streamable_server` | Streamable HTTP | Agent 通过 URL 连接 MCP |

**工具集合一致**：均通过 `mcp_tools_registry.register_codeindex_tools` 注册 `semantic_query`、`find_symbol`、`symbol_graph`（**不**注册 `code_graph_explore`；多跳图请用同一索引上的 HTTP `serve` `/graph/*`）。业务逻辑均为 `CodeindexMcpRuntime`（SQLite + 检索 + 图）。

## 鉴权与读写分层

1. **传输层（HTTP）**  
   - 环境变量 **`HYBRID_MCP_BEARER_TOKEN`**（可选）：若设置，所有到达 MCP 端点的请求必须带  
     `Authorization: Bearer <token>`。  
   - 未设置则**不校验**（仅适合内网或开发）；生产务必配合 TLS 与密钥。

2. **工具层（MCP）**  
   - 上述三工具均为 **只读**（`ToolAnnotations.readOnlyHint=true`）：不写索引、不 purge、不删向量。  
   - **写操作**（如 `purge-chunks`）仍在 **`cli serve`** 的 **`/admin/*`**，使用 **`HYBRID_ADMIN_TOKEN`**，与 MCP Bearer **分开**，避免 Agent 误拿管理权限。

## 环境变量

| 变量 | 说明 |
|------|------|
| `HYBRID_DB` | 必填，SQLite 索引路径 |
| `HYBRID_CONFIG` | 可选，JSON 配置 |
| `HYBRID_MCP_HOST` | 默认 `0.0.0.0` |
| `HYBRID_MCP_PORT` | 默认 `8765` |
| `HYBRID_MCP_PATH` | 默认 `/mcp`（Streamable 挂载路径） |
| `HYBRID_MCP_STATELESS` | 默认 `1`：每请求无状态，利于多副本；`0` 关闭 |
| `HYBRID_MCP_BEARER_TOKEN` | 可选，HTTP Bearer |
| `HYBRID_MCP_LOG_LEVEL` | Uvicorn 日志，默认 `info` |

## 短期测试（关闭 Bearer）

仅内网/临时验证时：**不要**设置 `HYBRID_MCP_BEARER_TOKEN`（或 `unset HYBRID_MCP_BEARER_TOKEN`），重启进程后 `bearer_required=False`，客户端 **无需** `Authorization` 头。

仓库内可复制示例（改好 `HYBRID_DB` 路径后）：

```bash
cd /path/to/hybrid_platform
set -a && source examples/mcp_streamable_test.env.example && set +a
myenv/bin/python -m hybrid_platform.mcp_streamable_server
```

公网或长期运行请务必重新启用 Bearer + TLS。

## 启动方式

```bash
export HYBRID_DB=/path/to/index.db
export HYBRID_MCP_BEARER_TOKEN=your-long-secret
python -m hybrid_platform.mcp_streamable_server
```

或使用 CLI：

```bash
python -m hybrid_platform.cli mcp-streamable --db /path/to/index.db
```

对外 URL 形如：`https://<host>:8765/mcp`（路径以 `HYBRID_MCP_PATH` 为准）。

## 客户端配置

具体 JSON 取决于你使用的 MCP Client（Claude Code、自研 Agent 等）。一般需要：

- **Transport**：`streamable-http` / `http`（以客户端文档为准）  
- **URL**：`https://.../mcp`  
- **Headers**：若启用 Bearer，配置 `Authorization: Bearer ...`

Server 在 `initialize` 响应中带 **`instructions`**（`MCP_STREAMABLE_INSTRUCTIONS`），与 [mcp_metadata_and_errors.md](./mcp_metadata_and_errors.md) 中的工具契约一致。

## 公网入口 + 内网转发

仅暴露 **443**（TLS），MCP 进程监听 **127.0.0.1** 或内网，由 Nginx / Caddy / Ingress 反代：见 **[deploy_public_mcp_edge_proxy.md](./deploy_public_mcp_edge_proxy.md)**（含示例配置与验证脚本 `scripts/verify_mcp_edge_proxy.sh`）。

## ASGI 工厂（自定义部署）

若需挂到已有 **Gunicorn / Uvicorn Worker / K8s Ingress**：

```python
from hybrid_platform.mcp_streamable_server import build_streamable_app
app = build_streamable_app()
```

`build_streamable_app()` 已根据环境变量套好可选 Bearer；进程启动前需设好 `HYBRID_DB` 等。

## 按 repo + commit 命名 `.db` 与 MCP 子路径

用于批量测试或多版本索引并列时，可用统一 slug：``{sanitize(repo)}_{commit_sha}``（commit 为小写 hex，长度 7–40）。

- **Python**：`hybrid_platform.index_slug` 中 `repo_commit_slug`、`index_db_path`、`mcp_http_path`。
- **一键构建**（Java：`index-java` → `build-code-graph` → `chunk` → `embed`）：`scripts/index_build_repo_commit.sh`  
  推荐命令行：`--config`、`--repo-name`、`--commit`、`--repo-root`；可选 `--output-dir`、`--build-tool`、**`--java-home`**（指定 JDK，如 Java 21）；仍可用同名环境变量（命令行优先）。`--` 之后为 `index-java` 的编译参数（如 `-DskipTests`）。
- **先克隆再构建**、**linux/amd64 + JDK 版本**：见 **[java_index_repo_setup.md](./java_index_repo_setup.md)**（含 `clone_repo_at_commit.sh`）。
- **一键 clone → 构建 → 启动 MCP**：`scripts/repo_commit_to_mcp.sh`（stderr 中 `PIPELINE_STAGE_*` 标记各阶段；失败搜 `PIPELINE_STAGE_FAILED`）。
- **启动 MCP**：`scripts/mcp_start_repo_commit.sh --config … --repo-name … --commit …`（推导 `HYBRID_DB` 与 `HYBRID_MCP_PATH=/mcp/<slug>`；等价于 `cli mcp-streamable --db --mcp-path`）。

## 相关源码

- `hybrid_platform/mcp_streamable_server.py` — 入口与 Uvicorn  
- `hybrid_platform/mcp_streamable_asgi.py` — Bearer ASGI 包装  
- `hybrid_platform/mcp_tools_registry.py` — 工具注册  
- `hybrid_platform/mcp_env_runtime.py` — 共享运行时懒加载  
