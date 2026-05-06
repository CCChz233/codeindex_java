# 架构与模块维护说明

本文档用于维护当前代码索引平台的整体逻辑。后续如果新增索引后端、MCP 工具、评测入口或数据表，需要同步更新本文。

## 一句话概览

平台做的事情是：

```text
源码仓库
  -> source backend 建索引
  -> 统一写入 SQLite
  -> build code graph
  -> chunk
  -> embedding / vector
  -> CLI / REST / MCP 查询
  -> eval 评测
```

当前 Java 有三个同级 source backend：

```text
scip-java          编译型高精度后端
tree-sitter-java   无编译 best-effort 后端
document           最低能力文本后端
```

MCP / REST / 查询层不直接关心索引来自哪个后端，只读统一 SQLite 索引库。

## 核心数据流

### 1. 建索引

入口：

- `hybrid_platform/cli.py`
- `hybrid_platform/index_build_runner.py`

正式命令：

```bash
python -m hybrid_platform.cli build-java-index \
  --repo-root /path/to/java-repo \
  --repo demo/repo \
  --commit local \
  --db /tmp/codeindex.db \
  --source-backend tree-sitter-java
```

`build-java-index` 的流水线：

```text
选择 source_backend
  -> 运行对应 SourceIndexer
  -> prepare_index 写 index_info
  -> 写 documents / symbols / occurrences / relations
  -> build-code-graph
  -> build chunks
  -> embed chunks
```

### 2. 查询

常用入口：

- `find-entity`: 按类型和名称查符号
- `query`: semantic / hybrid / keyword 风格查询
- `query-structure`: `def-of`、`refs-of`、`callers-of`、`callees-of`
- REST: `service_api.py`
- MCP: `agent_mcp_handlers.py`、`mcp_streamable_*`

MCP 三个核心工具：

```text
semantic_query
find_symbol
symbol_graph
```

`scip-java` 和 `tree-sitter-java` 索引都能使用这三个工具。区别是结果精度：SCIP 是编译型语义索引；tree-sitter 是无编译保守解析，歧义时跳过。

## 模块划分

当前代码可以按 9 个模块维护。

### 1. CLI / 配置 / Runtime 组装

主要文件：

- `hybrid_platform/cli.py`
- `hybrid_platform/config.py`
- `hybrid_platform/runtime_factory.py`
- `config/default_config.json`

职责：

- 定义命令行入口。
- 读取配置并合并默认值。
- 创建 embedding pipeline、vector store、retrieval service 等运行时对象。
- 将 `--source-backend` 透传给构建流水线。

维护重点：

- 新增用户可见命令时优先改 `cli.py`。
- 新增配置项时同时更新 `config.py`、`config/default_config.json`、`docs/config_reference.md`。
- 不要让 CLI 默认隐式触发编译；Java 是否编译由 `source_backend=scip-java` 明确表达。

### 2. Source Backend / 建索引流水线

主要文件：

- `hybrid_platform/source_indexer.py`
- `hybrid_platform/index_build_runner.py`
- `hybrid_platform/java_indexer.py`
- `hybrid_platform/fallback_indexer.py`
- `hybrid_platform/ingestion.py`
- `hybrid_platform/parser.py`

职责：

- `source_indexer.py` 定义同级后端：
  - `ScipJavaSourceIndexer`
  - `TreeSitterJavaSourceIndexer`
  - `DocumentSourceIndexer`
- `index_build_runner.py` 编排完整构建流程。
- `java_indexer.py` 负责运行 `scip-java`。
- `fallback_indexer.py` 当前承载 tree-sitter-java 和 document 的源码扫描实现。
- `ingestion.py` 负责把 `.scip` 解析结果写入统一 SQLite。
- `parser.py` 负责 SCIP 数据解析。

维护重点：

- 新增语言或后端时，应优先扩展 `SourceIndexer` 风格，而不是在 `index_build_runner.py` 里堆分支。
- `tree-sitter-java` 不应依赖 `scip-java` 失败才触发。
- `fallback_mode` 只作为 legacy 兼容保留，新代码使用显式 `source_backend`。

### 3. 索引契约 / Schema / 存储

主要文件：

- `hybrid_platform/index_contract.py`
- `hybrid_platform/storage.py`
- `hybrid_platform/models.py`
- `hybrid_platform/index_metadata.py`
- `hybrid_platform/index_slug.py`

职责：

- 定义 `source_mode`、`source_backend`、capabilities。
- 定义 SQLite schema。
- 维护 `index_info` 元数据。
- 提供统一读写 API：
  - documents
  - symbols
  - occurrences
  - relations
  - chunks
  - embeddings

关键概念：

```text
source_backend = 索引来源实现
source_mode    = 能力类别和兼容层
capabilities   = 当前索引可暴露的查询能力
```

当前映射：

```text
scip-java        -> source_mode=scip
tree-sitter-java -> source_mode=syntax
document         -> source_mode=document
```

维护重点：

- schema 改动必须考虑旧库迁移。
- 新能力要写入 `index_info.capabilities_json`。
- 查询 payload 应携带 `source_mode` 和 `source_backend`，方便排查结果来源。

### 4. 符号查询 / 结构查询 / Code Graph

主要文件：

- `hybrid_platform/entity_query.py`
- `hybrid_platform/dsl.py`
- `hybrid_platform/code_graph.py`
- `hybrid_platform/graph_service.py`

职责：

- `find_entity` 按类型和名称查 `symbols`。
- DSL 构造结构查询：
  - `def_of`
  - `refs_of`
  - `callers_of`
  - `callees_of`
- `code_graph.py` 从 symbols / occurrences / relations 构建 `code_nodes` 和 `code_edges`。
- `graph_service.py` 提供图查询服务。

维护重点：

- `build-code-graph` 消费统一 `relations`，不应关心关系来自 SCIP 还是 tree-sitter。
- `field_refs`、`calls`、`extends`、`implements` 都应走统一 relations/code_edges。
- 如果 tree-sitter 增加新 relation 类型，要同步检查 `code_graph.py` 是否消费。

### 5. Chunk / Embedding / Vector / Retrieval

主要文件：

- `hybrid_platform/embedding.py`
- `hybrid_platform/vector_store.py`
- `hybrid_platform/vector_store_lancedb.py`
- `hybrid_platform/retrieval.py`
- `hybrid_platform/llamaindex_embedder.py`
- `hybrid_platform/query_test_signals.py`

职责：

- 按文档和符号范围切 chunk。
- 生成 embedding。
- 写 SQLite 或 LanceDB 向量。
- 提供 keyword / semantic / hybrid 检索。
- 查询时合并结构分数、BM25、向量分数等信号。

维护重点：

- 本地验证推荐 deterministic embedding + sqlite vector，避免依赖外部服务。
- 线上检索可用 Voyage/OpenAI/LanceDB 等配置。
- chunk 策略变更会影响所有后端，因为后端最终都落到统一 documents/symbols/occurrences。

### 6. REST / MCP 服务

主要文件：

- `hybrid_platform/service_api.py`
- `hybrid_platform/agent_mcp_handlers.py`
- `hybrid_platform/mcp_errors.py`
- `hybrid_platform/mcp_streamable_asgi.py`
- `hybrid_platform/mcp_streamable_server.py`
- `hybrid_platform/mcp_server.py`
- `hybrid_platform/mcp_tools_registry.py`
- `hybrid_platform/mcp_server_instructions.py`
- `hybrid_platform/mcp_gateway_local.py`
- `hybrid_platform/mcp_env_runtime.py`

职责：

- 对外暴露 REST API。
- 对 Agent 暴露 MCP 工具。
- 统一错误格式和工具注册。

MCP 核心工具：

```text
semantic_query  自然语言 / hybrid 检索
find_symbol     结构化符号查找
symbol_graph    def_of / refs_of / callers_of / callees_of
```

维护重点：

- 不要为不同索引后端新增不同 MCP 工具。
- 后端差异通过 `index_info.source_backend` 和 result payload 解释。
- 如果某个能力不可用，应通过 capabilities 返回明确错误。

### 7. 评测模块

主要文件：

- `hybrid_platform/index_accuracy_eval.py`
- `hybrid_platform/retrieval_compare_eval.py`
- `hybrid_platform/spring_jsonl_semantic_eval.py`
- `hybrid_platform/spring_semantic_eval.py`
- `hybrid_platform/entity_eval.py`
- `hybrid_platform/grep_baseline.py`
- `hybrid_platform/graph_eval.py`
- `hybrid_platform/eval.py`

职责：

- 对已建好的索引做准确度评测。
- 支持 Spring reviewed JSONL：

```text
/Users/chz/workspace/codeindex_java/JAVA test/spring_framework_eval_v1_reviewed.verify.jsonl
```

推荐入口：

```bash
python -m hybrid_platform.cli eval-index-accuracy \
  --db /tmp/codeindex.db \
  --repo spring-framework \
  --commit 6ec2455e2491650fbeb7efaf78615a72700995ad \
  --dataset "/Users/chz/workspace/codeindex_java/JAVA test/spring_framework_eval_v1_reviewed.verify.jsonl" \
  --mode hybrid \
  --top-k 10 \
  --output /tmp/spring-eval-report.json
```

对比 dense semantic 和 BM25：

```bash
python -m hybrid_platform.cli eval-retrieval-compare \
  --db /tmp/codeindex.db \
  --repo spring-framework \
  --commit 6ec2455e2491650fbeb7efaf78615a72700995ad \
  --dataset "/Users/chz/workspace/codeindex_java/JAVA test/spring_framework_eval_v1_reviewed.verify.jsonl" \
  --top-k 5 \
  --top-k 10 \
  --output /tmp/spring-retrieval-compare.json
```

维护重点：

- 比较 `scip-java` 和 `tree-sitter-java` 时，应分别建两个 DB，再对两个 DB 跑同一个 dataset。
- `eval-index-accuracy` 更适合评估“这个索引整体检索效果怎样”。
- `eval-retrieval-compare` 更适合评估“同一个索引里 dense 和 BM25 哪个召回好”。

### 8. Admin / 运维 / 观测

主要文件：

- `hybrid_platform/admin_index_jobs.py`
- `hybrid_platform/observability.py`
- `hybrid_platform/service_api.py`
- `docs/server_agent_runbook.md`
- `docs/mcp_streamable_http.md`

职责：

- 管理异步建索引任务。
- 限制 admin build 路径范围。
- 输出进度和运行状态。
- 支持服务端部署与运维手册。

维护重点：

- admin index job 最终仍应调用 `run_java_full_index_pipeline`。
- 对外服务构建索引时，必须注意 `repo_root` 和 `db_path` 的路径安全限制。

### 9. Intent / Community / Repair 扩展模块

主要文件：

- `hybrid_platform/intent_builder.py`
- `hybrid_platform/community.py`
- `hybrid_platform/isolated_policy.py`
- `hybrid_platform/repair_calls.py`
- `hybrid_platform/prompt.py`

职责：

- 给函数或模块生成 intent。
- 做 community / graph 级聚类。
- 处理孤立节点策略。
- 对 calls 关系做后处理修复。

维护重点：

- 这些模块是增强层，不是建索引主链路。
- 输入通常依赖 `code_nodes`、`code_edges`、chunks 和 embedding。

## 后端能力对照

| 能力 | scip-java | tree-sitter-java | document |
|---|---:|---:|---:|
| keyword / hybrid / semantic | yes | yes | yes |
| find_symbol / find_entity | yes | yes | no |
| definition | yes | yes | no |
| references | yes | best-effort | no |
| callers / callees | yes | best-effort | no |
| extends / implements | yes | best-effort | no |
| 编译项目 | yes | no | no |

说明：

- `scip-java` 是编译型索引，精度最高。
- `tree-sitter-java` 是无编译索引，目标是项目内高可用；歧义调用不会伪造高置信边。
- `document` 只保证文本检索，不保证符号和调用图能力。

## 常用命令

无编译 tree-sitter 建索引：

```bash
python -m hybrid_platform.cli build-java-index \
  --repo-root /path/to/java-repo \
  --repo demo/repo \
  --commit local \
  --db /tmp/tree.db \
  --source-backend tree-sitter-java
```

编译型 SCIP 建索引：

```bash
python -m hybrid_platform.cli build-java-index \
  --repo-root /path/to/java-repo \
  --repo demo/repo \
  --commit local \
  --db /tmp/scip.db \
  --source-backend scip-java
```

查符号：

```bash
python -m hybrid_platform.cli find-entity \
  --db /tmp/tree.db \
  --type class \
  --name App
```

查结构关系：

```bash
python -m hybrid_platform.cli query-structure \
  --db /tmp/tree.db \
  --op callers-of \
  --symbol-id 'symbol_id'
```

启动 REST 服务：

```bash
python -m hybrid_platform.cli serve \
  --db /tmp/tree.db \
  --host 127.0.0.1 \
  --port 9301
```

## 维护检查清单

改 source backend 时检查：

- `source_indexer.py`
- `index_build_runner.py`
- `index_contract.py`
- `storage.py`
- `tests/test_index_contract_and_fallback.py`
- `docs/config_reference.md`
- 本文档

改 schema / capabilities 时检查：

- `SCHEMA_SQL`
- `_migrate_schema`
- `IndexInfo`
- `prepare_index`
- REST / MCP payload
- 旧库兼容读取

改 MCP 工具时检查：

- `agent_mcp_handlers.py`
- `mcp_tools_registry.py`
- `mcp_errors.py`
- `docs/mcp_tools_remote_agent.md`
- `docs/mcp_streamable_http.md`

改评测时检查：

- `index_accuracy_eval.py`
- `retrieval_compare_eval.py`
- 对应 CLI 参数
- `docs/index_accuracy_eval.md`
- `docs/retrieval_compare_eval.md`

## 当前设计原则

- 后端平级：`scip-java` 和 `tree-sitter-java` 都是正式 source backend。
- 查询统一：MCP / REST 不为不同后端拆工具。
- 能力显式：索引库通过 `index_info.capabilities` 声明可用能力。
- 来源可追踪：结果 payload 带 `source_mode` 和 `source_backend`。
- 无编译优先可用：tree-sitter 后端不追求完整 Java 类型系统，但要保守、稳定、可解释。
