# hybrid-codeindex MCP：工具元数据、输入 Schema、返回与错误契约

**MCP 交付清单、部署对照与「多索引库不由 Agent 选择」的规则**见 [mcp_delivery_handbook.md](./mcp_delivery_handbook.md)。

**Agent 正式协议面 + 集成/运维分册**见 [mcp_agent_integration_delivery.md](./mcp_agent_integration_delivery.md)（Part I：`initialize`·`tools/list`·`tools/call`；Part II：环境变量与部署）。

本文档与实现保持一致：

- Server `instructions`：`hybrid_platform/mcp_server_instructions.py` → `MCP_SERVER_INSTRUCTIONS`（经 FastMCP 暴露给客户端，供 LLM 阅读）。
- 工具注册与注解：`hybrid_platform/mcp_tools_registry.py`（`ToolAnnotations.readOnlyHint=true`；stdio/Streamable 共用）。
- 错误码与序列化：`hybrid_platform/mcp_errors.py`；业务：`hybrid_platform/agent_mcp_handlers.py`。

---

## 1. 全局约定

### 1.1 工具返回值载体

每个 MCP 工具返回 **一个字符串**；内容必须为 **JSON**（UTF-8）。客户端应 `JSON.parse` 后：

1. 读 `ok`（boolean）。
2. `ok === true`：读 `tool` 与业务字段（见各工具）。
3. `ok === false`：读 `error`（见 §3）。

### 1.2 副作用（所有工具）

| 项目 | 说明 |
|------|------|
| 索引 SQLite | **只读**（SELECT 及内部只读路径），不执行 ingest / purge |
| 向量库 | 查询为主；**不**删除表、不 purge chunks |
| 网络 | `hybrid` / `semantic` / 图 explore 可能调用 **embedding 等上游**（由配置决定） |
| 消息/通知 | **无** |

管理面（删 chunk 等）在 **HTTP `serve` 的 `/admin/*`**，**不在** MCP 工具列表中。

### 1.3 与 HTTP 的关系

- **MCP Streamable HTTP**（`mcp_streamable_server`）：与 stdio MCP **同一套工具与 JSON 返回**，仅传输改为 HTTP；见 [mcp_streamable_http.md](./mcp_streamable_http.md)。
- **REST `serve`**：跨机 Agent 也可直接调 `/query` 等，见 [mcp_tools_remote_agent.md](./mcp_tools_remote_agent.md)。REST 的 JSON **未必**带 `ok`/`tool` 包装；MCP 始终带 `ok` 与 `tool`（配置错误时 `tool` 可为 `null`）。

---

## 2. 工具一览（稳定 name）

| name | 一句话职责 | 何时用 | 何时不用 |
|------|------------|--------|----------|
| `semantic_query` | 语义检索代码块与符号；`query` 宜为英文自然语言 | 自然语言需求 | 已有精确 `symbol_id` 且只关心定义/引用/调用 → `symbol_graph` |
| `find_symbol` | 按类型与名称解析 `symbol_id` | 用户给了类/接口/方法名 | 纯自然语言描述 → `semantic_query` |
| `symbol_graph` | 单符号：定义/引用/调用者/被调者 | 已有 `symbol_id` | 多跳图探索 → **HTTP** `/graph/*`（非 MCP） |
| `code_graph_explore` | **不在 MCP**；多跳 / intent / explore 走 HTTP | 已建 code graph | 用 `serve` 的 `/graph/code/subgraph` 等；`handle_code_graph_explore` 仍可用于内部/测试 |

---

## 3. 错误对象（所有失败路径统一形状）

当 `ok: false` 时，`error` 为对象，字段如下：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `code` | string | 是 | 稳定枚举，见 §3.1 |
| `message` | string | 是 | 人类可读说明（路径中的 DB 路径可能被替换为 `<db>`） |
| `retryable` | boolean | 是 | 客户端是否适合自动重试 |
| `suggested_next_steps` | string[] | 否 | 建议的下一步（如改参数、换工具） |
| `details` | object | 否 | 调试信息，如 `exception_type` |

### 3.1 稳定错误码 `error.code`

| code | 语义 | retryable 典型值 |
|------|------|------------------|
| `CONFIG_INVALID` | 未配置 `HYBRID_DB` 或文件不存在 | false |
| `INPUT_VALIDATION` | 缺字段、空白、非法枚举（如非法 `entity_type`） | false |
| `UNSUPPORTED_OPERATION` | 操作名不在支持集合（如非法 `op`、`graph_mode`） | false |
| `RESOURCE_NOT_FOUND` | 资源不存在（预留；多数查询用空列表表示无结果） | false |
| `PERMISSION_DENIED` | 未授权（本 MCP 进程通常不出现） | false |
| `TIMEOUT` | 超时 | true |
| `RATE_LIMITED` | 限流 | true |
| `UPSTREAM_ERROR` | 网络/连接/上游 5xx 等 | true |
| `INTERNAL_ERROR` | 未分类异常 | false |

### 3.2 错误示例 JSON

**配置无效（工具调用前即失败，`tool` 为 null）：**

```json
{
  "ok": false,
  "tool": null,
  "error": {
    "code": "CONFIG_INVALID",
    "message": "Set HYBRID_DB to an existing SQLite index file path; optionally set HYBRID_CONFIG.",
    "retryable": false,
    "suggested_next_steps": [
      "Set environment variable HYBRID_DB to an absolute path of an existing SQLite index file.",
      "Optionally set HYBRID_CONFIG to your JSON config path.",
      "If the agent cannot spawn this process, use the REST serve API documented for remote agents."
    ]
  }
}
```

**输入校验（空 query）：**

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
      "If you know an exact type name, use find_symbol then symbol_graph."
    ]
  }
}
```

**不支持的操作（symbol_graph 的 op）：**

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

**临时/上游类（示例结构，具体 message 随异常变化）：**

```json
{
  "ok": false,
  "tool": "semantic_query",
  "error": {
    "code": "RATE_LIMITED",
    "message": "...",
    "retryable": true,
    "suggested_next_steps": [
      "Reduce concurrency or wait before retrying; verify API quota and credentials."
    ],
    "details": {
      "exception_type": "SomeSDKError"
    }
  }
}
```

---

## 4. 各工具：JSON Schema（inputSchema 等价）

以下为 MCP/Agent 侧 **`tools/list` 暴露参数** 的 **JSON Schema draft 风格** 描述（FastMCP 从注册函数签名生成；与 Python 一致）。`semantic_query` / `symbol_graph` 的 `mode`、`top_k`、`include_code` 等由服务端固定（`semantic_query` 在 MCP 上固定为 **`mode=semantic`**），**不在** MCP 工具参数中出现；HTTP `POST /query` 等仍可传这些字段。

### 4.1 `semantic_query`

```json
{
  "type": "object",
  "title": "semantic_query",
  "description": "语义检索（只读）；MCP 仅 query；宜为英文自然语言；服务端固定 top_k 等",
  "properties": {
    "query": { "type": "string", "description": "英文自然语言检索文本，必填且非空白" }
  },
  "required": ["query"]
}
```

**成功体示例：**

```json
{
  "ok": true,
  "tool": "semantic_query",
  "results": [
    {
      "id": "repo:commit:path:ast:symbol…:line-line:part",
      "type": "chunk",
      "score": 1.489262,
      "explain": { "keyword": 7.44 },
      "payload": {
        "document_id": "repo:commit:path",
        "path": "module/src/main/java/…/Foo.java"
      }
    }
  ]
}
```

`type` 还可能是 `symbol` 等；`payload` 字段随类型变化。

---

### 4.2 `find_symbol`

```json
{
  "type": "object",
  "title": "find_symbol",
  "description": "按类型与名称解析 symbol_id（只读）",
  "properties": {
    "entity_type": {
      "type": "string",
      "description": "如 class, interface, method, type, enum, field, constructor, variable, type_parameter, any"
    },
    "name": { "type": "string" },
    "match": { "type": "string", "enum": ["exact", "contains"], "default": "contains" },
    "package_contains": { "type": "string", "default": "" },
    "limit": { "type": "integer", "default": 50, "minimum": 1, "maximum": 500 }
  },
  "required": ["entity_type", "name"]
}
```

**成功体示例：**

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

`count === 0` 仍为成功，表示无匹配。

---

### 4.3 `symbol_graph`

```json
{
  "type": "object",
  "title": "symbol_graph",
  "description": "单符号图查询：定义/引用/调用（只读）；MCP 仅 op + symbol_id；服务端固定 top_k 等",
  "properties": {
    "op": {
      "type": "string",
      "enum": ["def_of", "refs_of", "callers_of", "callees_of"]
    },
    "symbol_id": { "type": "string" }
  },
  "required": ["op", "symbol_id"]
}
```

**成功体示例：**

```json
{
  "ok": true,
  "tool": "symbol_graph",
  "op": "def_of",
  "symbol_id": "scip-cpp demo main().",
  "results": [
    {
      "id": "…",
      "type": "symbol",
      "score": 0.0,
      "explain": {},
      "payload": { "path": "src/main.cc" }
    }
  ]
}
```

---

### 4.4 `code_graph_explore`（仅 HTTP `serve`，**不在** `tools/list`）

下列 Schema 对应 REST 请求体形态参考；**MCP 客户端不会**再收到该 tool。

```json
{
  "type": "object",
  "title": "code_graph_explore",
  "description": "代码图/意图图探索（只读；依赖已构建图）",
  "properties": {
    "graph_mode": { "type": "string", "enum": ["code", "intent", "explore"] },
    "seed_ids": { "type": "array", "items": { "type": "string" }, "description": "graph_mode=code 时使用，如 method:<symbol_id>" },
    "hops": { "type": ["integer", "null"] },
    "edge_type": { "type": "string", "default": "calls" },
    "community_ids": { "type": "array", "items": { "type": "string" } },
    "query": { "type": ["string", "null"] },
    "symbol": { "type": ["string", "null"] },
    "module_top_k": { "type": ["integer", "null"] },
    "function_top_k": { "type": ["integer", "null"] },
    "semantic_top_k": { "type": ["integer", "null"] },
    "seed_fusion": { "type": ["string", "null"] },
    "module_seed_member_top_k": { "type": ["integer", "null"] },
    "explore_default_hops_module": { "type": ["integer", "null"] },
    "explore_default_hops_function": { "type": ["integer", "null"] },
    "min_seed_score": { "type": ["number", "null"] },
    "embedding_version": { "type": ["string", "null"] }
  },
  "required": ["graph_mode"]
}
```

**成功体示例（结构随模式变化，仅为示意）：**

```json
{
  "ok": true,
  "tool": "code_graph_explore",
  "graph_mode": "code",
  "data": {
    "nodes": [{ "node_id": "method:scip-cpp demo main().", "label": "…" }],
    "edges": [{ "src": "…", "dst": "…", "type": "calls" }],
    "explain": {}
  }
}
```

---

## 5. Server instructions（摘要）

完整文本见 `mcp_server_instructions.py`。要点：**只读**、推荐 **find_symbol → symbol_graph** 流水线、**错误码与 retryable** 解读、**semantic_query** 仅参数 `query`（宜英文自然语言），可按需 **find_symbol** 降级。

---

## 6. 变更约定

- 新增或重命名 `error.code`、或成功体顶层字段 → 视为 **破坏性变更**，需同步本文档与依赖测试。
- 仅增加可选字段或 `suggested_next_steps` 文案 → **非破坏性**。
