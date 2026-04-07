# Codeindex MCP 工具说明与跨机 Agent 调用

本文说明 **三个只读 MCP 工具**（`semantic_query` / `find_symbol` / `symbol_graph`）的入参与返回 JSON 结构；多跳图 **`code_graph_explore` 不通过 MCP 暴露**，请走 HTTP `serve` 的 `/graph/*`。并说明在 **索引服务部署在独立机器**（示例：`45.78.221.74`）时，**另一台服务器上的 Agent 插件**应如何调用等价能力。

**工具元数据、JSON Schema、统一错误码与示例**：见 [mcp_metadata_and_errors.md](./mcp_metadata_and_errors.md)（含 `instructions`、`error.retryable`、`suggested_next_steps`）。

---

## 1. 重要概念：MCP 与 HTTP 是两条通道

| 通道 | 传输 | 典型场景 |
|------|------|----------|
| **MCP stdio**（`python -m hybrid_platform.mcp_server`） | **stdio**（标准输入/输出上的 JSON-RPC） | Cursor 等客户端**在本机或 SSH 会话里拉起子进程**，与进程对话 |
| **MCP Streamable HTTP**（`python -m hybrid_platform.mcp_streamable_server`） | **HTTP**（默认路径 `/mcp`，见 `HYBRID_MCP_PATH`） | **云端远程 MCP**；可选 `HYBRID_MCP_BEARER_TOKEN`；说明见 [mcp_streamable_http.md](./mcp_streamable_http.md) |
| **HTTP serve**（`python -m hybrid_platform.cli serve --db ...`） | **TCP HTTP**（默认配置里端口常为 **8080**，以 `config` / 命令行为准） | **任意机器**上的 Agent 用 `http://45.78.221.74:<端口>/...` 发 JSON（非 MCP 协议，为 REST 式 API） |

**结论（跨机 Agent）**：

- 另一台服务器上的 Agent **不能**像访问 REST 那样直接「连接 MCP URL」——本仓库的 MCP **没有**内置 SSE/HTTP 传输，**只有 stdio**。
- 若索引与进程都在 `45.78.221.74` 上，**推荐**在该机启动 **`hybrid_platform` 的 HTTP `serve`**，Agent 插件通过 **HTTPS（建议反代 + 鉴权）** 调用下文中的 HTTP 路径，语义与 MCP 工具对齐（管理面 `purge` 等仍用 `/admin/*`，勿暴露给不可信 Agent）。

---

## 2. 部署在 `45.78.221.74` 上的建议形态

1. 在该机准备：SQLite 索引 `.db`、`config/default_config.json`（或你的配置）、虚拟环境与依赖（见仓库规则中的 `myenv`）。
2. 启动检索服务（示例，端口请按你方配置修改）：

   ```bash
   export HYBRID_DB=/path/to/index.db
   /path/to/myenv/bin/python -m hybrid_platform.cli serve --db "$HYBRID_DB" --host 0.0.0.0 --port 8080
   ```

3. **安全**（生产必做）：
   - 用 Nginx/Caddy 等做 **TLS** 与访问控制；
   - 防火墙仅对 Agent 所在网段或固定 IP 开放端口；
   - **`POST /admin/purge-chunks`** 需请求头 `X-Admin-Token`，与进程环境变量 `HYBRID_ADMIN_TOKEN` 一致，**不要**交给通用 Agent 工具列表。
   - **`POST /admin/index-jobs`**（异步一键构建：scip-java → ingest → build-code-graph → chunk → embed）同样使用 `X-Admin-Token`；**不提供 MCP 工具**，仅给后端/运维调用。可选环境变量 **`HYBRID_ADMIN_INDEX_ALLOW_PREFIXES`**：逗号分隔的绝对路径前缀，`repo_root` 与 `db_path` 解析后须落在某一前缀之下（未设置则不校验前缀）。

4. Agent 侧基址示例：`http://45.78.221.74:8765`（若前面有反代，则为 `https://your-domain`）。

5. 健康检查：`GET /health` → `{"ok": true/false, "service_ready": true/false}`。

6. **管理面：异步全量索引构建**（不经 MCP）  
   - **`POST /admin/index-jobs`**，JSON body 必填：`repo_root`（本地绝对路径，须已处于目标 `commit` 的工作树）、`repo`、`commit`、`db_path`（输出 SQLite 绝对路径）；**`config_path`** 与 **`config`**（内联对象）二选一，内联会与默认配置深度合并（`AppConfig.merge_with_defaults`）。  
   - 响应：`{"job_id": "<uuid>", "poll_url_hint": "/admin/index-jobs/<uuid>"}`。  
   - **`GET /admin/index-jobs`**：最近任务列表；**`GET /admin/index-jobs/<job_id>`**：`status`（`queued` \| `running` \| `succeeded` \| `failed`）、`current_stage`、`percent`（0–100，由阶段与 chunk/embed 进度推导）、`stage_stats` / `result` / `error`（失败时含 `type`、`message`、`traceback`）。单任务 URL 可加 **`?verbose=1`** 以附带 `request` 与 `last_messages`。  
   - 若 **`db_path`** 与当前 `serve --db` 打开的库为同一路径，请求会在启动任务前被拒绝，避免并行写库损坏。构建完成后由运维切换 `HYBRID_DB` 或重启指向新库。

---

## 3. 三个 MCP 工具：入参与返回（JSON 字符串）

MCP 层每个 tool 的返回值是 **字符串**；内容均为 **JSON**，解析后统一习惯：

- 成功：`"ok": true`，并带 `"tool"` 与业务字段；
- 失败：`"ok": false`，`"error": {"code": "...", "message": "..."}`（校验失败常见 `VALIDATION`，配置问题可能为 `CONFIG`）；
- 未设置 `HYBRID_DB` 或库文件不存在时，可能返回无 `"tool"` 的 `CONFIG` 错误。

### 3.1 `semantic_query`（与 MCP `tools/call` 同名；REST 仍为 `POST /query`）

**含义**：MCP 侧表述为 **语义检索**；在索引上找相关 chunk/符号。`query` **宜使用英文自然语言**（与 `tools/list` 中 `description` 一致）。

**MCP `tools/call` 入参（刻意收窄）**

| 参数 | 类型 | 说明 |
|------|------|------|
| `query` | string | 必填；**英文**自然语言问句或检索短语为佳 |

服务端固定：`mode=semantic`、`top_k=10`、`blend_strategy=linear`、`include_code=false`、`max_code_chars=1200`、默认 `embedding_version`。需要 `hybrid`/`structure`、调 `top_k` 或附带代码片段时，请用下方 **HTTP** `POST /query`，勿在 MCP 里传这些字段。

**HTTP `POST /query` 仍可传的扩展参数（非 MCP 工具面）**

| 参数 | 类型 | 说明 |
|------|------|------|
| `mode` | string | `hybrid`（默认）\|`semantic`\|`structure`（符号表子串等，与 MCP 工具集不同） |
| `top_k` | int | 默认 10 |
| `blend_strategy` | string | 默认 `linear` |
| `include_code` | bool | 是否在结果中带代码片段 |
| `max_code_chars` | int | 代码片段最大长度 |
| `embedding_version` | string \| null | 可选，覆盖默认向量版本 |

**成功返回字段**：`tool`, `results`。`results[]` 每项大致为：

- `id`, `type`（如 `chunk`、`symbol`）, `score`, `explain`, `payload`（常含 `path`、`document_id` 等）

**HTTP 等价**：`POST /query`，JSON body 示例：

```json
{
  "query": "ApplicationContext",
  "mode": "hybrid",
  "top_k": 20,
  "blend_strategy": "linear",
  "include_code": false,
  "max_code_chars": 1200
}
```

返回：`{"results": [...]}`（字段与扩展参数表一致；未传 `embedding_version` 时使用服务默认向量版本）。

---

### 3.2 `find_symbol`

**含义**：按实体类型与名称在 `symbols` 中查找 `symbol_id`。

**入参**

| 参数 | 类型 | 说明 |
|------|------|------|
| `entity_type` | string | 如 `class`, `interface`, `method`, `type`, `any` 等 |
| `name` | string | 名称（必填） |
| `match` | string | `contains`（默认）或 `exact` |
| `package_contains` | string | 包路径子串过滤 |
| `limit` | int | 默认 50 |

**成功返回字段**：`tool`, `entity_type`, `name`, `match`, `count`, `entities[]`, `supported_types[]`。  
`entities[]` 每项：`symbol_id`, `display_name`, `kind`, `package`, `language`, `enclosing_symbol`。

**HTTP 等价**：`POST /find-entity`，JSON body 示例：

```json
{
  "entity_type": "interface",
  "name": "ApplicationContext",
  "match": "exact",
  "package_contains": "",
  "limit": 10
}
```

返回：与 MCP 成功体类似，但 **无** `ok`/`tool` 包装（为历史 JSON 形状）；Agent 应直接读 `entities` / `count`。

---

### 3.3 `symbol_graph`

**含义**：对给定 `symbol_id` 做结构化图查询（与索引中关系一致）。

**MCP `tools/call` 入参（刻意收窄）**

| 参数 | 类型 | 说明 |
|------|------|------|
| `op` | string | `def_of` \| `refs_of` \| `callers_of` \| `callees_of` |
| `symbol_id` | string | 完整符号 ID |

服务端固定：`top_k=10`、`include_code=false`、`max_code_chars=1200`、默认 `embedding_version`。需要调这些时请用 **HTTP** `POST /query/structured`。

**HTTP `POST /query/structured` 仍可传的扩展参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| `top_k` | int | 默认 10 |
| `include_code` | bool | 是否附带代码 |
| `max_code_chars` | int | 代码长度上限 |
| `embedding_version` | string \| null | 可选 |

**成功返回字段**：`tool`, `op`, `symbol_id`, `results`（元素形状同 `semantic_query` 的 `results`）。

**HTTP 等价**：`POST /query/structured`，JSON body 示例：

```json
{
  "op": "def_of",
  "symbol_id": "semanticdb maven ... ApplicationContext#",
  "top_k": 5,
  "include_code": false,
  "max_code_chars": 1200
}
```

返回：`{"results": [...]}`（同样无 MCP 外层 `ok`/`tool`）。

---

### 3.4 多跳图 / `code_graph_explore`（**非 MCP**，仅 HTTP）

**含义**：与 MCP `tools/call` **无关**；服务端已不在 `tools/list` 中注册 `code_graph_explore`。在 **代码图 / 意图图** 上探索需调 **`serve` 的 REST**；依赖已构建的图数据（如执行过 CLI 的 `build-code-graph`）。未建图时可能报错或结果为空。

**入参（按 `graph_mode` 选用）**

| `graph_mode` | 必填/常用参数 | 说明 |
|--------------|----------------|------|
| `code` | `seed_ids`（如 `method:<symbol_id>`） | 从种子节点扩子图 |
| `intent` | `community_ids` | 按意图社区取子图 |
| `explore` | `query` 和/或 `symbol` | 自然语言与/或符号探索 |

其余可选参数与配置中 `graph_query` 一致，例如：`hops`, `edge_type`, `module_top_k`, `function_top_k`, `semantic_top_k`, `seed_fusion` 等（HTTP body 里均可选，缺省读配置）。

**成功返回**：REST 为图服务 JSON（随模式变化；**无** MCP 的 `ok`/`tool` 包装，与 `POST /query` 类似）。

**HTTP 路径对照**（原 MCP 三模式对应下列接口）：

| 原 `graph_mode` | HTTP 方法与路径 | 说明 |
|------------------|-----------------|------|
| `code` | `POST /graph/code/subgraph` | body 含 `seed_ids`, `hops`, `edge_type` |
| `intent` | `POST /graph/intent/subgraph` | body 含 `community_ids` |
| `explore` | `POST /graph/intent/explore` | body 含 `query`, `symbol` 及各 `*_top_k` 等 |

`POST /graph/code/subgraph` 示例：

```json
{
  "seed_ids": ["method:scip-cpp demo main()."],
  "hops": 2,
  "edge_type": "calls"
}
```

---

## 4. 跨机 Agent 插件实现要点

1. **把「工具调用」映射为 HTTP**：在 Agent 侧定义与 MCP 同名的 *逻辑* 工具（如 `semantic_query` → `POST /query`），内部用 `requests`/`httpx`/`fetch` 调用上表 **基址 + 路径**，请求体用 JSON，`Content-Type: application/json`。
2. **错误处理**：HTTP 5xx / 连接失败应重试或降级；业务错误可能在 JSON 的 `error` 字段（若你封装层统一成 MCP 形状，可自行包一层 `ok:false`）。
3. **与 Cursor 本地 MCP 的差异**：本地 MCP 返回带 `ok`/`tool` 的整包 JSON 字符串；HTTP `/query`、`/query/structured`、`/find-entity` 部分接口返回体**没有**完全相同的包装，Agent 适配层建议做 **薄封装**，使插件侧仍见统一结构。
4. **embedding / 网络**：`hybrid` / `semantic` 可能调用外部 Embedding；确保 `45.78.221.74` 出网与密钥配置正确，或与运维约定仅用 `structure` 模式。
5. **可选进阶**：若必须坚持 **远程 MCP 协议**（stdio 以外的 MCP 传输），需要单独引入 **支持 SSE/WebSocket 的 MCP 网关** 或自写桥接；**本仓库未提供**，不在本文展开。

---

## 5. 快速对照表

| MCP 工具 | HTTP 方法 | 路径 |
|----------|-----------|------|
| `semantic_query`（逻辑） | POST | `/query` |
| `find_symbol` | POST | `/find-entity` |
| `symbol_graph` | POST | `/query/structured` |
| 多跳图（code，仅 HTTP） | POST | `/graph/code/subgraph` |
| 多跳图（intent，仅 HTTP） | POST | `/graph/intent/subgraph` |
| 多跳图（explore，仅 HTTP） | POST | `/graph/intent/explore` |

---

## 6. 代码参考

- MCP 注册与参数：`hybrid_platform/mcp_server.py`
- 返回 JSON 组装：`hybrid_platform/agent_mcp_handlers.py`
- 检索结果序列化：`hybrid_platform/runtime_factory.py` 中 `format_query_results_for_json`
- HTTP 路由：`hybrid_platform/service_api.py`

文档中的 **IP `45.78.221.74`** 仅为占位示例；端口、HTTPS 域名、鉴权以你方实际部署为准。
