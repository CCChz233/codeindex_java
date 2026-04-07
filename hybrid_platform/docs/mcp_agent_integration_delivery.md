# hybrid-codeindex MCP — Agent 集成与运维参考

# 第一部分 — Agent 正式协议面（契约）

**语言说明：** 本节**正文**（表格与解释）为中文，便于集成方阅读。下列与 **Agent / 模型直接消费** 的内容为**英文**（与实现对齐）：**`initialize.instructions` 基线**、`tools/list` 里每个工具的 **`description`**、**`inputSchema`** 中各属性的 **`description`**（若实现或本文档 JSON 中给出）。**`enum`、字段名、类型、`required` 等**保持英文。各工具小节在英文 **`description` 摘录**下附 **「中文概要」** 供人读。

## I.0 Agent 可依赖的稳定面

在 MCP 协议下，Agent（或工具调用模型）**唯一**应作为契约依赖的是：

| MCP 步骤 | 本服务端约定 |
|----------|----------------|
| **`initialize`** | 服务端响应中含 **`instructions`**（字符串）。其语义内容见下文 §I.2，定义能力、建议调用顺序与错误处理预期。 |
| **`tools/list`** | 每个工具有 **`name`**、**`title`**、**`description`**、**`inputSchema`**、**`annotations`**（含 **`readOnlyHint: true`**）。用于选工具与填参。 |
| **`tools/call`** | 每个工具：**arguments** 为符合该工具 **`inputSchema`** 的 JSON 对象；**Result** 为**单个字符串**，其内容为 **JSON 对象**（返回信封见 §I.6）。 |

凡**未**列入上表的内容（URL、端口、进程拉起方式、环境变量名、文件系统路径、Kubernetes、管理 API 等）均属**宿主/运维配置**，**不是** Agent 契约的一部分。

## I.1 明确**不属于** Agent 契约的内容

**不要**要求模型必须掌握、解析或依赖下列信息：

- 环境变量名（如索引路径、主机、端口、路径前缀）。
- HTTP URL、端口、Ingress、反代拓扑。
- MCP 服务端进程如何启动（stdio 子进程 vs 远程 HTTP）。
- Admin token、`/admin/*` 路由、ingest/purge 等运维流程。
- 服务端本地路径或数据库文件位置。
- 副本数、负载均衡或内部服务拓扑。

若 **tool 结果**或 **`error.message`** 中出现部署细节，应用 **`error.code`** 与 **`retryable`** 做逻辑分支，并升级给人类运维；**不要**根据密钥名或路径分支。

## I.2 `initialize` — 字段 `instructions`


**基线文本（语义契约；与部署无关；英文，供模型/Agent 直接使用）：**

```
You are a coding assistant using hybrid-codeindex. All tools are **read-only**: they do not modify the index, delete data, or send messages. Ingest, purge, and admin work are **not** in MCP; operators use separate admin channels outside these tools.

**Suggested call order**
1. User gives a class/interface/method name → call `find_symbol` first to get the full `symbol_id` (for Java, use `entity_type=interface` for interfaces, not `class`).
2. For "where defined", "who references", or **method-level** call direction → `symbol_graph` with `op` one of: `def_of`, `refs_of`, `callers_of`, `callees_of`.
3. For natural-language search → `semantic_query` with **only** `{"query": "..."}` — write `query` as **English** natural language; do not pass `mode`, `top_k`, or other tuning fields. For type/name lookup use `find_symbol`.

**symbol_graph: types vs call graph**
- `callers_of` / `callees_of` are **method-level** call edges (who invokes whom). Do **not** use them to mean “who uses this class or interface” at the **type** layer: that mixes references, inheritance, composition, etc., and is **not** a strict call graph.
- If the user cares about **who references a type or interface**, prefer `refs_of` on that symbol’s `symbol_id`. (A dedicated type-use summary may exist later.)
- The index is aligned with this: **call** relations are built mainly **between methods**, not as a stand-in for type-level “who uses me”.

**Minimal examples** (`tools/call` **arguments** object; each tool **result** is one **string** — `JSON.parse` it before reading fields)

- `semantic_query`: `{"query": "Where is request validation handled?"}`  
  Success: `ok`, `tool`, `results[]` with `id`, `type`, `score`, `explain`, `payload` (often `path`, `document_id` on chunk hits).

- `find_symbol`: `{"entity_type": "interface", "name": "ApplicationContext", "match": "exact", "limit": 20}`  
  Success: `entities[]` with `symbol_id`, `display_name`, `kind`, `package` — use `symbol_id` in `symbol_graph`.

- `symbol_graph`: `{"op": "def_of", "symbol_id": "<paste full symbol_id from find_symbol>"}`  
  `op` is one of: `def_of`, `refs_of`, `callers_of`, `callees_of`. Success: `results[]` (same general shape as `semantic_query` hits). No `top_k` / snippet tuning on MCP.

**Illustrative envelopes**  
- Success: `{"ok": true, "tool": "<name>", ...}`  
- Failure: `{"ok": false, "tool": "<name or null>", "error": {"code": "INPUT_VALIDATION", "message": "...", "retryable": false, "suggested_next_steps": [...]}}`

**Returns and errors**
- Each tool returns **one JSON string**. Parse JSON and read top-level `ok`.
- If `ok` is false, read `error.code` (stable enum), `error.message`, `error.retryable`, and `error.suggested_next_steps`.
- `INPUT_VALIDATION` / `UNSUPPORTED_OPERATION`: do not retry blindly; fix arguments. `CONFIG_INVALID`: operator must fix deployment; do not retry blindly. `TIMEOUT` / `RATE_LIMITED` / `UPSTREAM_ERROR`: retry only when `retryable` is true.

**Transport note**
- If the host uses HTTP or other transports, credentials and endpoints are configured **outside** the model. **Never** put secrets or connection parameters into tool arguments.

**Do not**
- Use this MCP to run shell or edit repo files; do not assume write access.
- Semantic retrieval needs embedding capability and network; on failure, fall back to `find_symbol` or a shorter query.
```

**说明：** 线上服务端**可能**追加面向运维的行（路径、token 提示等）；这些行给人/客户端配置宿主用。**工具名、`inputSchema` 与 JSON 返回信封**仍是权威契约。

## I.3 `tools/list` — 工具元数据

对每个已注册工具，Agent 依赖：

- **`name`**：`tools/call` 用的稳定标识符。
- **`title`**：短标签。
- **`description`**：何时用/不用、只读说明、返回形态提示。
- **`inputSchema`**：类 JSON Schema；**required** 属性必须始终提供。
- **`annotations`**：本交付中工具均带 **`readOnlyHint: true`**。

```json
{ "readOnlyHint": true }
```

小节 **I.4.1–I.4.3** 记录与 `tools/list` 对齐的 **`name` / `title` / `description` / `inputSchema`**（工具级与参数级说明均为**英文**，供模型填参）。

**本版范围：** 三个工具 — `semantic_query`、`find_symbol`、`symbol_graph`。未来可能增加工具；在 `tools/list` 列出之前勿假定存在。

## I.4 `tools/call` — 参数与结果

- **Arguments：** 每次调用一个 JSON 对象；键与类型须满足该 **`name`** 的 **`inputSchema`**。
- **Result：** MCP 内容为**字符串**。解析为 JSON 即**返回信封**（§I.6）。

**关于 `inputSchema`：** 线上由 FastMCP 根据注册函数签名生成 JSON Schema；**与模型相关的文案（工具级 `description`、属性 `description`）应为英文**。下文 JSON 为便于对照的**结构化摘录**（属性 `description` 为英文）；若你方 `tools/list` 响应与本文在可选字段或说明细节上略有差异，**以线上响应为准**。

**服务端固定默认（MCP 不暴露）：** `semantic_query` 内部使用 `mode=semantic`、`top_k=10`、`blend_strategy=linear`、`include_code=false`、`max_code_chars=1200`、默认 `embedding_version`；`symbol_graph` 内部使用 `top_k=10` 及相同 snippet/向量默认。需要 `hybrid`/`structure` 或其它调参请走 **HTTP** `POST /query` 或 `POST /query/structured`（见集成文档），勿在 MCP `tools/call` 传这些字段。

## I.4.1 `semantic_query`

**name:** `semantic_query`  
**title:** Semantic search over code index

**description**（英文，与 `mcp_tools_registry` / `tools/list` 一致）:

> Semantic retrieval over the ingested code index: returns relevant code chunks and symbols. Arguments: only `query` — non-empty natural language in English (questions, behavior descriptions, or search phrases work best). For exact type/method names or to obtain `symbol_id` for symbol_graph, use find_symbol instead. Read-only; may call external embedding APIs when configured. Returns a JSON string: ok=true includes results[{id,type,score,explain,payload}]; failures include an error object.

**中文概要：** 对外表述为语义检索；`query` 请用**英文**自然语言。精确符号名 / 要拿 `symbol_id` 请用 `find_symbol`。服务端内部仍有固定 `mode`/`top_k` 等（见下段）；完整调参请用 HTTP `/query`。

**inputSchema**（MCP 仅 `query`；属性说明为英文）:

```json
{
  "type": "object",
  "properties": {
    "query": { "type": "string", "description": "Non-empty search text (required)." }
  },
  "required": ["query"]
}
```

## I.4.2 `find_symbol`

**name:** `find_symbol`  
**title:** Resolve symbol_id by entity type and name

**description**（英文，与 `mcp_tools_registry` / `tools/list` 一致）:

> Look up symbols in the symbols table by entity type and name; returns full symbol_id and display metadata. Use when the user gives a class/interface/method name and you need symbol_id for symbol_graph. For vague natural language, use semantic_query instead. For Java interfaces use entity_type=interface, not class. Read-only SQLite; no side effects beyond the query. Parameters: entity_type such as class, interface, method, type, any; match is exact | contains. Returns a JSON string: ok=true includes entities[], supported_types; count may be 0 (not an error).

**中文概要：** 按类型+名称查 `symbol_id`；Java 接口用 `entity_type=interface`；模糊需求用 `semantic_query`。

**inputSchema**（属性说明为英文）:

```json
{
  "type": "object",
  "properties": {
    "entity_type": {
      "type": "string",
      "description": "e.g. class, interface, method, type, enum, field, constructor, variable, type_parameter, any"
    },
    "name": { "type": "string", "description": "Symbol name to match (required)." },
    "match": { "type": "string", "enum": ["exact", "contains"], "default": "contains" },
    "package_contains": { "type": "string", "default": "" },
    "limit": { "type": "integer", "default": 50, "minimum": 1, "maximum": 500 }
  },
  "required": ["entity_type", "name"]
}
```

## I.4.3 `symbol_graph`

**name:** `symbol_graph`  
**title:** Symbol graph: definition, references, call edges

**description**（英文，与 `mcp_tools_registry` / `tools/list` 一致）:

> Run def_of, refs_of, callers_of, or callees_of for one symbol_id; relationships match the index. Use when you already have symbol_id and need definition site, referrers, or call direction. If symbol_id is missing, call find_symbol first. Read-only; server applies fixed top_k and snippet defaults (not configurable via this tool). Arguments: `op` and `symbol_id` only. Parameter op must be one of: def_of, refs_of, callers_of, callees_of. Returns a JSON string: ok=true includes op, symbol_id, results (same item shape as semantic_query).

**中文概要：** 对已有 `symbol_id` 做定义/引用/调用关系；无 id 先 `find_symbol`。`top_k`/片段等由服务端固定；调参请用 HTTP `/query/structured`。多跳代码图不在 MCP 内，请用 HTTP `serve` 的 `/graph/*`。

**inputSchema**（MCP 仅 `op` + `symbol_id`）:

```json
{
  "type": "object",
  "properties": {
    "op": {
      "type": "string",
      "enum": ["def_of", "refs_of", "callers_of", "callees_of"]
    },
    "symbol_id": { "type": "string", "description": "Full symbol id from find_symbol or query results." }
  },
  "required": ["op", "symbol_id"]
}
```

## I.5 工具一览 — 何时用 / 何时不用

| `name` | 适用场景 | 不适用场景 |
|--------|----------|------------|
| `semantic_query` | 英文自然语言；语义检索 chunk + 符号 | 已有精确 `symbol_id` 且只要 def/refs/call → `find_symbol` + `symbol_graph` |
| `find_symbol` | 用户说出类型/方法名；需要规范 `symbol_id` | 仅模糊意图 → `semantic_query`；Java 接口 → `entity_type=interface` |
| `symbol_graph` | 已有 `symbol_id`；需要 `def_of` / `refs_of` / `callers_of` / `callees_of` | 无 `symbol_id` → 先 `find_symbol` |

## I.6 返回信封（`tools/call` 结果 JSON）

工具 **result 字符串** 解析后为 JSON 对象。

**成功：**

- `ok`：`true`
- `tool`：工具名字符串
- 工具专有字段（如 `results`、`entities`、`data` 等）

**失败：**

- `ok`：`false`
- `tool`：工具名，若未派发到工具则为 `null`
- `error`：对象

**`error` 对象：**

| 字段 | 类型 | 说明 |
|-------|------|--------|
| `code` | string | 稳定枚举（§I.8） |
| `message` | string | 人类可读；实现多为英文，**勿**用作复杂分支逻辑 |
| `retryable` | boolean | 是否适合自动重试 |
| `suggested_next_steps` | string[] | 可选提示 |
| `details` | object | 可选（如 `exception_type`） |

## I.7 入出示例（示意）

取值仅为示例；真实 `symbol_id` 与路径取决于已索引工程。

### I.7.1 `semantic_query`

**示例 A — 自然语言检索（MCP 仅传 `query`，建议英文）**  
用**英文**自然语言描述问题或要找的行为；用户给出**精确**类型或方法名时优先 **`find_symbol`**。服务端对 `query` 仍按固定默认做检索（如内部 `top_k=10`）。

Arguments:

```json
{
  "query": "Where does the CLI report invalid flag combinations to the user?"
}
```

成功（示意；`explain` 随部署变化）:

```json
{
  "ok": true,
  "tool": "semantic_query",
  "results": [
    {
      "id": "repo:abc:src/cli/errors.cc:chunk:…",
      "type": "chunk",
      "score": 0.91,
      "explain": { "semantic": 0.84, "keyword": 0.45 },
      "payload": {
        "chunk_id": "…",
        "document_id": "repo:abc:src/cli/errors.cc",
        "path": "src/cli/errors.cc",
        "language": "cpp",
        "start_line": 12,
        "end_line": 48
      }
    }
  ]
}
```

**Payload 说明：** 对 **`type: "chunk"`**，存储层至少带 **`document_id`** 与 **`path`**（相对路径）；**`chunk_id`**、**`language`**、**`start_line`**、**`end_line`** 来自元数据行（若有）。MCP 默认不在结果中附带完整代码片段（`include_code=false`）；若通过 HTTP `/query` 开启 `include_code`，会增加 **`code`**、**`truncated`** 等。对 **`type: "symbol"`** 命中，**`payload` 可能较简**（如仅 `display_name`），带代码片段时定义 **`path`** 与行范围来自片段拉取 —— 不一定含 **`document_id`**。

**示例 B — 校验失败**

Arguments: `{ "query": "   " }`

```json
{
  "ok": false,
  "tool": "semantic_query",
  "error": {
    "code": "INPUT_VALIDATION",
    "message": "query must not be empty or whitespace-only",
    "retryable": false,
    "suggested_next_steps": [
      "Provide a non-empty natural-language or symbol-related query.",
      "If you know an exact type name, use find_symbol then symbol_graph.",
      "MCP only accepts query; use HTTP /query if you need mode or top_k."
    ]
  }
}
```

### I.7.2 `find_symbol`

**示例 — Java 接口**

Arguments:

```json
{
  "entity_type": "interface",
  "name": "ApplicationContext",
  "match": "exact",
  "limit": 20
}
```

成功:

```json
{
  "ok": true,
  "tool": "find_symbol",
  "entity_type": "interface",
  "name": "ApplicationContext",
  "match": "exact",
  "count": 1,
  "entities": [
    {
      "symbol_id": "semanticdb maven … ApplicationContext#",
      "display_name": "ApplicationContext",
      "kind": "Interface",
      "package": "org/springframework/context",
      "language": "java",
      "enclosing_symbol": ""
    }
  ],
  "supported_types": ["any", "class", "constructor", "enum", "field", "interface", "method", "type", "type_parameter", "variable"]
}
```

### I.7.3 `symbol_graph`

**示例 — 非法 `op`**

Arguments:

```json
{ "op": "not_an_op", "symbol_id": "scip-cpp demo main()." }
```

```json
{
  "ok": false,
  "tool": "symbol_graph",
  "error": {
    "code": "UNSUPPORTED_OPERATION",
    "message": "op must be one of ['callees_of', 'callers_of', 'def_of', 'refs_of']; got 'not_an_op'",
    "retryable": false,
    "suggested_next_steps": [
      "def_of: definition site; refs_of: references; callers_of / callees_of: call edges.",
      "symbol_id must be the full id from find_symbol or query results."
    ]
  }
}
```

### I.7.4 工具派发前的部署失败（仅结构示意）

**示例 — `CONFIG_INVALID`（线上 `message` 可能不同）**

```json
{
  "ok": false,
  "tool": null,
  "error": {
    "code": "CONFIG_INVALID",
    "message": "Server configuration is invalid; the operator must fix deployment.",
    "retryable": false,
    "suggested_next_steps": [
      "Escalate to the operator; do not retry the same call without configuration changes."
    ]
  }
}
```

## I.8 错误码与 Agent 侧实践

| `error.code` | 含义 | 典型 `retryable` | 建议处理 |
|--------------|------|------------------|----------|
| `CONFIG_INVALID` | 部署/配置异常 | false | 升级运维；勿盲目重试 |
| `INPUT_VALIDATION` | 参数缺失或非法 | false | 修正参数 |
| `UNSUPPORTED_OPERATION` | 非法 `op` 等不支持参数 | false | 按 `suggested_next_steps` |
| `RESOURCE_NOT_FOUND` | 预留；少见 | false | 常以空列表表达 |
| `PERMISSION_DENIED` | 鉴权/策略 | false | 只读工具少见 |
| `TIMEOUT` | 超时 | true | 退避重试 |
| `RATE_LIMITED` | 限流 | true | 降低并发；退避 |
| `UPSTREAM_ERROR` | 网络/上游 5xx | true | 退避重试 |
| `INTERNAL_ERROR` | 未分类服务端错误 | false | 若有 `details` 一并升级 |

**实践：** 解析 JSON → `ok` → 按 **`error.code`** 与 **`retryable`** 分支。优先 **`find_symbol` → `symbol_graph`**。语义检索失败时可回退 **`find_symbol`** 或缩短查询。

## I.9 稳定摘要（仅 Agent 契约）

**属于契约：** `initialize.instructions`（上文语义）、`tools/list` 元数据、`tools/call` 参数与 JSON 结果信封、`error.code` 枚举、`readOnlyHint: true`。

**不属于契约：** 宿主 URL、环境变量、路径、管理面、进程拓扑 —— 除非产品**另行**在 MCP 契约外成文。

---

# 第二部分 — 集成方与运维参考


## II.1 传输形态（非 Agent 契约）

| 形态 | 集成方式（概要） |
|------|------------------|
| **stdio MCP** | 宿主配置 `command` + `args` 拉起子进程，MCP 帧在 stdin/stdout 上传输。 |
| **Streamable HTTP MCP** | 客户端按 MCP Streamable HTTP 连接服务端发布的 **URL**（由运维决定：主机、端口、路径、TLS、反代）。 |

**与第一部分的关系：** Agent 只消费 **`initialize` / `tools/list` / `tools/call`**；**如何连上服务器**完全由集成层配置。

## II.2 鉴权分层（集成方配置）

| 层级 | 说明 |
|------|------|
| **传输层（常见于 HTTP）** | 服务端可要求 `Authorization: Bearer <token>`；具体密钥名与轮换策略由运维定义（实现上常与环境变量绑定）。 |
| **工具层** | 本交付三个工具均为只读；**没有**在 tool 参数里传租户密钥、Admin 密钥或「选库」字段。 |
| **管理写操作** | 索引写入、purge、异步全量构建等走 **独立于 MCP 的 HTTP 管理面**（如 `/admin/*`）；其 token **不得**配进通用 Agent 的 MCP 客户端。 |

## II.3 环境与索引绑定（运维）

以下名称与实现常见配置对应，**属于部署契约，不是 MCP 协议里给模型的字段**：

| 变量（示例） | 用途 |
|--------------|------|
| `HYBRID_DB` | 当前 MCP 进程绑定的 SQLite 索引文件路径（stdio / Streamable 通常一致）。 |
| `HYBRID_CONFIG` | 可选 JSON 配置路径。 |
| `HYBRID_MCP_HOST` / `HYBRID_MCP_PORT` / `HYBRID_MCP_PATH` | Streamable HTTP 监听地址与 MCP 路径（若使用默认实现）。 |
| `HYBRID_MCP_BEARER_TOKEN` | 若设置，HTTP 客户端需在传输层带 Bearer（与 Admin token 不同）。 |
| `HYBRID_ADMIN_TOKEN` | 仅用于 `serve` 进程的 `/admin/*`，**不是** MCP 工具参数。 |

## II.4 作用域与多库策略

- **每个 MCP 进程一个固定索引**；**不支持**在同一条连接上运行时切换库。
- **多库** = 多个 MCP 实例或多个端点，由**宿主选连哪一个**，**不要让模型在对话里选库**。
- 若需让模型理解「当前连的是哪套代码」，应在 **`instructions`** 中增加**自然语言**描述（实例级拼接），或等价机制；**不要**做成工具可选参数。

