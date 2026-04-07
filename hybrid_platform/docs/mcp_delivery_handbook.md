# MCP 子系统交付手册

本文档面向 **交付与运维**：汇总 MCP 相关能力、文档索引、部署方式，并明确 **多索引库** 下由谁决定「用哪个库」（**不由 Agent/LLM 选择**）。

---

## 1. 交付范围（近期已实现）

| 项 | 说明 | 入口 / 源码 |
|----|------|-------------|
| stdio MCP | Cursor 等本地拉起子进程，三只读工具（无 `code_graph_explore`） | `python -m hybrid_platform.mcp_server` → `mcp_server.py` |
| Streamable HTTP MCP | 云端远程，无 stdio，MCP 标准 HTTP 传输 | `python -m hybrid_platform.mcp_streamable_server` 或 `cli mcp-streamable` → `mcp_streamable_server.py` |
| 工具注册复用 | stdio 与 HTTP 共用同一套工具与 `CodeindexMcpRuntime` | `mcp_tools_registry.py`、`mcp_env_runtime.py` |
| 传输层鉴权（HTTP） | 可选 `HYBRID_MCP_BEARER_TOKEN` + `Authorization: Bearer` | `mcp_streamable_asgi.py` |
| 工具元数据与错误契约 | `instructions`、`readOnlyHint`、统一 `error.code` / `retryable` / `suggested_next_steps` | `mcp_server_instructions.py`、`mcp_errors.py`、`agent_mcp_handlers.py` |
| REST 等价 API | 跨机非 MCP 协议调用 | `cli serve` → `service_api.py`，对照见下表 |
| 自动化测试 | 样例库 MCP 场景、Spring 黄金场景、Streamable Bearer ASGI | `tests/test_mcp_agent_tools.py`、`test_mcp_agent_spring.py`、`test_mcp_streamable_asgi.py` |

### 1.1 文档索引

| 文档 | 内容 |
|------|------|
| **[mcp_agent_integration_delivery.md](./mcp_agent_integration_delivery.md)** | **Part I：Agent 正式协议面**（`initialize` / `tools/list` / `tools/call`）；**Part II：集成/运维**（环境变量、鉴权、部署）；自包含 |
| [mcp_metadata_and_errors.md](./mcp_metadata_and_errors.md) | 工具名、输入 Schema、成功/失败 JSON、错误码枚举 |
| [mcp_streamable_http.md](./mcp_streamable_http.md) | Streamable HTTP 环境变量、启动、ASGI 工厂、读写分层 |
| [mcp_tools_remote_agent.md](./mcp_tools_remote_agent.md) | MCP vs REST、远程 Agent、路径对照表 |
| [deploy_public_mcp_edge_proxy.md](./deploy_public_mcp_edge_proxy.md) | **公网 TLS + 内网转发**（Nginx/Caddy/Ingress 示例、多库路径、验证脚本） |
| [java_index_repo_setup.md](./java_index_repo_setup.md) | **Java 索引**：克隆指定 commit、`repo_commit_to_mcp.sh` 一键到 MCP、`PIPELINE_STAGE_*` 阶段日志 |

---

## 2. 部署形态一览

| 形态 | 绑定索引方式 | 典型用途 |
|------|----------------|----------|
| stdio MCP | 进程环境变量 `HYBRID_DB` | 本机 IDE |
| Streamable HTTP MCP | 进程环境变量 `HYBRID_DB`（每进程一个路径） | 云上 Agent、多副本 |
| REST `serve` | 启动参数 `--db` 或等价配置 | 自定义 Agent HTTP 客户端、管理面 |

**管理写操作**（purge 等）仅走 REST `serve` 的 `/admin/*` + `HYBRID_ADMIN_TOKEN`，与 MCP Bearer **分离**，见 [mcp_streamable_http.md](./mcp_streamable_http.md)。

---

## 3. 多索引库：固定规则（核心）

### 3.1 当前产品行为

- 每个 MCP（或 REST serve）进程在启动时只读取 **一个** SQLite 路径：环境变量 **`HYBRID_DB`**（REST 为 `--db`）。
- **工具参数中没有「索引库 id / 别名」字段**：LLM **无法**也 **不应**在单次连接上「选择库」。

因此：**索引选择 = 连接哪一个已部署好的后端实例**，而不是对话里选一个库名。

### 3.2 推荐固定规则（不让 Agent 选库）

由 **平台 / 运维 / 产品配置** 在 Agent **外侧**决定，常见做法如下（可组合）：

1. **一库一实例（最清晰）**  
   - Spring 索引 → 部署 A：`HYBRID_DB=/data/index/spring.db`，对外 URL `https://mcp.example.com/spring`（Ingress 路由到 Service A）。  
   - JDK 索引 → 部署 B：`HYBRID_DB=.../jdk.db`，对外 URL `https://mcp.example.com/jdk`。  
   - **Agent 配置文件**（或 Cursor MCP 配置）里写死连接哪一个 URL；**同一段对话只接一个 MCP**，则自然只查一个库。

2. **一业务线一 Profile**  
   - 「Spring 代码助手」Profile 只注册 Spring 的 MCP Server；「基础库助手」只注册 JDK 的 MCP。用户选助手 = 选库，**不是 LLM 在 tool 参数里选库**。

3. **网关按租户固定路由（仍不由 LLM 填）**  
   - 若必须统一入口，可在 API 网关根据 **JWT / mTLS / 内网源 IP** 等将请求转发到对应后端；后端进程仍各自一个 `HYBRID_DB`。  
   - 可选在网关注入只读 Header（如 `X-Internal-Tenant`）仅供审计，**不**暴露给模型当自由文本参数。

### 3.3 反模式（交付时应避免）

- 让模型在 prompt 里「选一个库名」再拼进某个 tool 参数（**当前实现不支持**，且易产生注入与误用）。  
- 多个互不信任租户共用一个 MCP 进程、仅靠模型自觉（无隔离）。

### 3.4 若未来要做「单进程多库」（扩展，非当前实现）

若产品明确要求单入口多库，应在 **服务端** 增加**确定性**映射（例如配置文件 `alias → db 路径`），并由 **网关或 sidecar** 注入固定 `alias`（Header），**仍不**由 LLM 填写；需单独设计与安全评审。当前代码未实现该模式。

---

## 4. 交付检查清单（简版）

- [ ] `HYBRID_DB` 指向生产可读、已 ingest 的 `.db`  
- [ ] `HYBRID_CONFIG` 中 embedding 等密钥与出网策略已确认  
- [ ] Streamable 部署：`HYBRID_MCP_BEARER_TOKEN` + TLS 反代（公网）  
- [ ] 多索引场景：已为每索引明确 **URL/实例** 与 **Agent 侧连接配置**，而非依赖模型选库  
- [ ] 管理面：`HYBRID_ADMIN_TOKEN` 仅运维持有，未配置进 Agent MCP  

---

## 5. 与总报告的关系

平台级实现与规格对照仍以 [IMPLEMENTATION_REPORT.md](./IMPLEMENTATION_REPORT.md) 为准；**MCP 交付与多库策略** 以 **本文档** 为单一事实来源（随 MCP 变更同步更新）。

**语言**：本仓库 **`.md` 文档保留中文** 便于交付阅读；**进程日志、stderr、pytest 输出中的说明**，以及 MCP 暴露给 Agent 的 `instructions`、工具 `title`/`description`、返回 JSON 中的 `error.message` / `suggested_next_steps` 为 **English**（见对应 `.py` 与 `tests/`）。
