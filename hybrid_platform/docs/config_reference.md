# Config 参数说明

本文档逐项说明 `config/default_config.json` 中的所有参数：含义、默认值、影响命令和常见取值建议。

## 总体规则

- 配置入口：`--config <path>`
- 优先级：`命令行参数 > config 文件 > 内置默认值`
- 本文的“作用范围”对应 CLI 子命令，例如 `ingest/chunk/embed/query/...`

---

## ingest

### `ingest.index_version`（默认：`v1`）
- **含义**：索引版本标签，用于标记当前入库批次的索引方案版本。
- **作用范围**：`ingest`
- **建议**：当解析策略/字段语义有变化时升级版本号。

### `ingest.batch_size`（默认：`1000`）
- **含义**：入库批大小（文档、符号、关系等批量写入粒度）。
- **作用范围**：`ingest`
- **建议**：内存充足时可提高到 `2000~5000`；若 SQLite 写入抖动大可调小。

### `ingest.retries`（默认：`2`）
- **含义**：ingest 失败时重试次数。
- **作用范围**：`ingest`
- **建议**：I/O 不稳定环境可提高到 `3~5`。

### `ingest.source_root`（默认：空字符串）
- **含义**：源码根目录；提供后可读取源文件内容用于后续 chunk/snippet。
- **作用范围**：`ingest`
- **建议**：强烈建议设置为仓库根目录。

---

## chunk

当前切块策略为：**`ast` 优先**。优先使用 SCIP `enclosing_range` 的 AST 节点范围切块；Java 在缺少可靠 AST 范围时，可用 `tree-sitter-java` 兜底；最后才回退到 definition-span。

默认只对“代码文件”执行 chunk；当前实现会基于 `relative_path` 后缀与 `language` 字段做过滤，跳过非代码文档（如普通文本、配置、说明文档等）。

### `chunk.target_tokens`（默认：`512`）
- **含义**：单个 chunk 的目标 token 预算（近似 token 计数）。
- **作用范围**：`chunk`
- **建议**：常用范围 `256~512`；函数很长时可适当提高。

### `chunk.overlap_tokens`（默认：`48`）
- **含义**：相邻 chunk 的 token 重叠预算，用于保留上下文连续性。
- **作用范围**：`chunk`
- **建议**：一般设为 `target_tokens` 的 `10%~20%`。

### `chunk.strategy`（默认：`ast`）
- **含义**：切分策略。
- **可选值**：`ast | definition_span`
- **作用范围**：`chunk`

### `chunk.java_treesitter_fallback`（默认：`true`）
- **含义**：Java 缺少可靠 SCIP AST 范围时，是否使用 `tree-sitter-java` 兜底补 AST 边界。
- **作用范围**：`chunk`

### `chunk.java_container_policy`（默认：`leaf_preferred`）
- **含义**：Java AST chunk 中，type/container 声明与成员声明重叠时的保留策略。
- **可选值**：
  - `all`：保留 container 块和成员块
  - `leaf_preferred`：优先保留更叶子的成员块，移除包裹它们的 container 块
- **作用范围**：`chunk`

### `chunk.fallback_to_definition_span`（默认：`true`）
- **含义**：当 AST 路径无可用节点时是否回退到 definition-span。
- **作用范围**：`chunk`

### `chunk.ast_min_lines`（默认：`1`）
- **含义**：AST 节点最小行数，过短节点会被跳过。
- **作用范围**：`chunk`

### `chunk.include_leading_doc_comment`（默认：`true`）
- **含义**：是否将定义前注释（Javadoc/docstring）并入 chunk。
- **作用范围**：`chunk`

### `chunk.include_call_graph_context`（默认：`true`）
- **含义**：是否注入调用图上下文（当前仅 incoming calls）。
- **作用范围**：`chunk`

### `chunk.call_context_max_each`（默认：`8`）
- **含义**：每个方向最多注入多少调用关系名称。
- **作用范围**：`chunk`

### `chunk.leading_doc_max_lookback_lines`（默认：`120`）
- **含义**：向上回看注释最大行数。
- **作用范围**：`chunk`

### `chunk.function_level_only`（默认：`true`）
- **含义**：为 `true` 时，AST 切块**最小粒度为函数级**（构造函数、方法、函数）；字段、属性、常量等不再单独成块，其所在行通过「父容器」（类/接口/枚举/record 等）在扣除子函数与内层类型后的**剩余行区间**生成补充 chunk（`chunk_id` 前缀 `ast_parent:`），避免短字段块与丢文本。
- **为 `false`**：恢复旧行为，与 `field`/`property`/`constant` 及类型容器等一并参与 `_legacy_should_chunk_symbol_kind` 候选，并仍受 `java_container_policy` 约束。
- **作用范围**：`chunk`（含 AST 主路径与 `definition_span` 回退路径的符号过滤）

---

## embed

### `embed`（默认：`{}`）
- **含义**：预留段；当前 embedding 主要由 `embedding.*` 统一管理。
- **作用范围**：`embed`

---

## embedding

### `embedding.version`（默认：`v1`）
- **含义**：全局 embedding 版本号。
- **作用范围**：`chunk/embed/query/eval/serve`
- **建议**：切换 embedding 模型、维度、清洗策略时升级版本号。

### `embedding.provider`（默认：`llamaindex`）
- **含义**：embedding 提供方类型。
- **可选值**：
  - `deterministic`：本地确定性 baseline（联调/回归）
  - `local`：本地 `sentence-transformers`
  - `voyage`：Voyage API
  - `http`：自部署 HTTP embedding 服务
  - `llamaindex`：通过 LlamaIndex embedding 适配层调用模型
- **作用范围**：`embed/query/eval/serve`

### `embedding.model`（默认：`voyage-code-3`）
- **含义**：模型标识。
- **作用范围**：`embed/query/eval/serve`
- **建议**：
  - `local`：填写本地/HF 模型名（如 `BAAI/bge-large-en-v1.5`）
  - `voyage`：填写 Voyage 模型名（如 `voyage-code-3`）
  - `http`：填写你服务端识别的模型名

### `embedding.dim`（默认：`256`）
- **含义**：向量维度（主要用于 deterministic 路径和一致性约束）。
- **作用范围**：`embed/query/eval/serve`
- **建议**：应与真实模型维度一致（例如 768/1024）。

### `embedding.api_base`（默认：空字符串）
- **含义**：远端 embedding 服务基地址。
- **作用范围**：`voyage/http`
- **示例**：
  - Voyage：`https://api.voyageai.com`
  - 自部署：`http://<host>:<port>`

### `embedding.api_key`（默认：空字符串）
- **含义**：远端服务鉴权 key。
- **作用范围**：`voyage/http`

### `embedding.timeout_s`（默认：`30`）
- **含义**：单次 embedding HTTP 请求超时时间（秒）。
- **作用范围**：`voyage/http`

### `embedding.endpoint`（默认：`/embeddings`）
- **含义**：`http` provider 的路径后缀。
- **作用范围**：`http`
- **完整 URL**：`{api_base}{endpoint}`

### `embedding.batch_size`（默认：`64`）
- **含义**：每批提交给 embedder 的文本条数。
- **作用范围**：`embed`
- **建议**：远端模型优先调这个参数；常见 `32/64/128`。

### `embedding.max_workers`（默认：`4`）
- **含义**：并行批任务线程数。
- **作用范围**：`embed`
- **建议**：I/O 型远端服务可提高；本地 GPU 推理通常不要过大。

### `embedding.max_retries`（默认：`2`）
- **含义**：单个 embedding batch 失败后的最大重试次数。
- **作用范围**：`embed`
- **建议**：远端服务波动明显时可适当调高。

### `embedding.retry_backoff_s`（默认：`0.5`）
- **含义**：embedding batch 重试的指数退避起始秒数。
- **作用范围**：`embed`
- **说明**：第 `n` 次重试会等待 `retry_backoff_s * 2^n` 秒。

### `embedding.stream_fetch_limit`（默认：`0` 表示自动）
- **含义**：从 SQLite 分页拉取「待嵌 chunk」时每页条数上限；`0` 时用 `max(512, batch_size * max(4, max_workers*2))`。
- **作用范围**：`embed`
- **建议**：内存紧张时调小；过大则单页文本峰值更高。

### `embedding.stream_write_buffer_chunks`（默认：`0`）
- **含义**：写向量库前在内存中缓冲的 chunk 条数。`0` 表示每成功一个 embedding batch 立即写 SQLite+LanceDB；设为 `4096` 等则凑满再写，减少 LanceDB/SQLite 往返，用内存换时间。
- **作用范围**：`embed`
- **建议**：粗估向量缓冲约 `条数 × dim × 8` 字节（外加 Python 开销）；OOM 时调回 `0` 或减小。

### `embedding.stream_commit_every_batches`（默认：`0` 表示 `2000`）
- **含义**：每完成多少次**向量写库 flush**（每次把一批向量交给 `upsert` 算一次）后对 SQLite 执行 `commit`。未缓冲时与「每 N 个 embedding batch」一致；启用 `stream_write_buffer_chunks` 后一次 flush 可能含多条 chunk。
- **作用范围**：`embed`
- **建议**：过小会严重拖慢；过大则崩溃时未提交事务更长；缓冲写库时可酌情略减小。

### `embedding.provider_max_concurrency`（默认：`8`）
- **含义**：provider 实际并发请求上限；会限制离线 embed 与在线 query 对外部 embedding 服务的总并发。
- **作用范围**：`embed/query/serve/graph-query`

### `embedding.online_max_concurrency`（默认：`8`）
- **含义**：在线 query/query-embedding 的并发门闩。
- **作用范围**：`query/serve/graph-query`

### `embedding.online_query_max_retries`（默认：`2`）
- **含义**：在线 query embedding 的最大重试次数。
- **作用范围**：`query/serve/graph-query`

### `embedding.online_query_cache_size`（默认：`1024`）
- **含义**：query embedding 的 LRU 缓存容量。
- **作用范围**：`query/serve/graph-query`

### `embedding.online_query_cache_ttl_s`（默认：`300`）
- **含义**：query embedding 缓存 TTL（秒）。
- **作用范围**：`query/serve/graph-query`

### `embedding.fail_open_on_query`（默认：`true`）
- **含义**：在线 query 发生 provider 故障时，是否降级为空语义结果而不是直接报错。
- **作用范围**：`query/serve/graph-query`

### `embedding.retryable_status_codes`（默认：`[]`）
- **含义**：额外视为可重试的 HTTP 状态码列表。
- **作用范围**：`embed/query/serve/graph-query`

### `embedding.input_type`（默认：`document`）
- **含义**：Voyage 输入类型。
- **作用范围**：`voyage`
- **常见值**：`document`（索引端）、`query`（检索端）

### `embedding.device`（默认：`cpu`）
- **含义**：本地 embedding 模型运行设备。
- **作用范围**：`local`
- **常见值**：`cpu`、`cuda`

### `embedding.llama.class_path`（默认：`llama_index.embeddings.voyageai.VoyageEmbedding`）
- **含义**：LlamaIndex embedding 类全路径。
- **作用范围**：`llamaindex`

### `embedding.llama.kwargs`（默认：`{}`）
- **含义**：初始化 `class_path` 时透传的参数对象（如 `model`、`api_base`、`api_key`）。
- **作用范围**：`llamaindex`
- **当前默认**：为 Voyage 预留了 `model_name`、`voyage_api_key`、`embed_batch_size`、`output_dimension` 槽位

### `embedding.llama.common_arg_map`（默认：Voyage 示例映射）
- **含义**：把顶层通用配置键映射到 `llama.kwargs` 的构造参数名，用于私有化部署或自定义 LlamaIndex embedding 类。
- **作用范围**：`llamaindex`
- **支持的来源键**：`model`、`api_base`、`api_key`、`timeout_s`、`batch_size`、`dim`
- **示例**：
  - Voyage 默认：`model -> model_name`，`api_key -> voyage_api_key`
  - 私有服务可改成：`api_base -> base_url` 或 `api_base -> api_base`
- **说明**：如果目标类本身不支持某个参数，就把对应映射值留空字符串即可

### `embedding.llama.query_method`（默认：`query`）
- **含义**：query 向量优先使用的方法语义。
- **可选值**：`query | text`
- **作用范围**：`llamaindex`

### `embedding.llama.document_method`（默认：`text`）
- **含义**：文档/chunk 向量优先使用的方法语义。
- **可选值**：`text | query`
- **作用范围**：`llamaindex`

### `embedding.llama.allow_batch_fallback`（默认：`true`）
- **含义**：当底层类缺少原生 batch API 时，是否允许退化为逐条调用。
- **作用范围**：`llamaindex`

### `embedding.llama.serialize_calls`（默认：`false`）
- **含义**：是否串行化单模型实例调用，适用于底层 SDK/客户端线程安全不明确的情况。
- **作用范围**：`llamaindex`

---

## vector

### `vector.backend`（默认：`lancedb`）
- **含义**：语义检索读取后端。
- **可选值**：`sqlite | lancedb`
- **作用范围**：`query/eval/serve/graph-query`

### `vector.write_mode`（默认：`dual`）
- **含义**：embedding 写入模式。
- **可选值**：`sqlite_only | dual | lancedb_only`
- **作用范围**：`embed`

### `vector.lancedb.uri`（默认：空字符串，运行时自动派生为 `<db_path>.lancedb`）
- **含义**：LanceDB 存储路径或 URI；未显式配置时，会按当前 SQLite DB 路径自动派生默认目录。
- **作用范围**：`lancedb` 相关模式

### `vector.lancedb.table`（默认：`chunk_vectors`）
- **含义**：LanceDB 表名。
- **作用范围**：`lancedb` 相关模式

### `vector.lancedb.metric`（默认：`cosine`）
- **含义**：向量检索距离度量。
- **作用范围**：`lancedb` 相关模式

---

## query

### `query.mode`（默认：`hybrid`）
- **含义**：检索模式。
- **可选值**：`structure | semantic | hybrid`
- **作用范围**：`query`

### `query.top_k`（默认：`10`）
- **含义**：返回结果条数上限。
- **作用范围**：`query`

### `query.blend_strategy`（默认：`linear`）
- **含义**：混合检索融合策略。
- **可选值**：`linear | rrf`
- **作用范围**：`query`

### `query.include_code`（默认：`false`）
- **含义**：是否在结果中附带代码片段。
- **作用范围**：`query`

### `query.max_code_chars`（默认：`1200`）
- **含义**：附带代码片段的最大字符数。
- **作用范围**：`query`

---

## eval

### `eval.mode`（默认：`hybrid`）
- **含义**：离线评测检索模式。
- **作用范围**：`eval`

### `eval.top_k`（默认：`10`）
- **含义**：评测时每条 query 的候选条数。
- **作用范围**：`eval`

---

## server

### `server.host`（默认：`0.0.0.0`）
- **含义**：HTTP 服务监听地址。
- **作用范围**：`serve`

### `server.port`（默认：`9301`）
- **含义**：HTTP 服务端口。
- **作用范围**：`serve`

---

## intent

说明：本项目的业务配置统一通过 `config` 文件与 CLI 参数传入；`intent`/`embedding` 等运行参数不再从环境变量读取兜底值。

### `intent.intent_pipeline_version`（默认：`llm-v1`）
- **含义**：函数意图生成流水线版本标签（缓存/追溯用）。
- **作用范围**：`build-intent-fn`

### `intent.intent_prompt_version`（默认：`p1`）
- **含义**：prompt 版本标签（缓存/追溯用）。
- **作用范围**：`build-intent-fn`

### `intent.neighbor_top_k`（默认：`5`）
- **含义**：函数级 intent 构建时，纳入 prompt 的 caller/callee 邻居上限。
- **作用范围**：`build-intent-fn`
- **建议**：常用 `5~12`；过大可能增加 token 消耗与噪声。

### `intent.model`（默认：空字符串）
- **含义**：LLM 模型标识；为空时退化为规则式 fallback intent。
- **作用范围**：`build-intent-fn`

### `intent.api_base`（默认：空字符串）
- **含义**：LLM 网关地址（可选）。
- **作用范围**：`build-intent-fn`

### `intent.api_key`（默认：空字符串）
- **含义**：LLM 鉴权 key。
- **作用范围**：`build-intent-fn`

### `intent.timeout_s`（默认：`30`）
- **含义**：LLM 请求超时（秒）。
- **作用范围**：`build-intent-fn`

### `intent.temperature`（默认：`0.0`）
- **含义**：LLM 温度。
- **作用范围**：`build-intent-fn`

### `intent.max_tokens`（默认：`200`）
- **含义**：LLM 最大输出 token。
- **作用范围**：`build-intent-fn`

---

## community

### `community.alpha`（默认：`0.5`）
- **含义**：语义相似度权重。
- **作用范围**：`build-intent-module`

### `community.beta`（默认：`0.4`）
- **含义**：拓扑关系权重。
- **作用范围**：`build-intent-module`

### `community.gamma`（默认：`0.1`）
- **含义**：路径先验/其他先验权重。
- **作用范围**：`build-intent-module`

### `community.semantic_top_k`（默认：`20`）
- **含义**：每个节点语义近邻候选数量。
- **作用范围**：`build-intent-module`

### `community.resolution`（默认：`1.0`）
- **含义**：Leiden 分辨率（单值模式）。
- **作用范围**：`build-intent-module`

### `community.resolutions`（默认：`[]`）
- **含义**：多分辨率列表（非空时优先于 `resolution`）。
- **作用范围**：`build-intent-module`
- **示例**：`[0.6, 1.0, 1.4]`

### `community.edge_min_weight`（默认：`0.05`）
- **含义**：建图时边保留的最小权重阈值。
- **作用范围**：`build-intent-module`

### `community.fallback_threshold`（默认：`0.35`）
- **含义**：fallback 聚类或弱连接接受阈值。
- **作用范围**：`build-intent-module`

---

## isolated_policy

### `isolated_policy.force_threshold_default`（默认：`0.55`）
- **含义**：孤立节点并入社区的默认强制阈值。
- **作用范围**：`apply-isolated-policy`

### `isolated_policy.force_threshold_uncertain`（默认：`0.65`）
- **含义**：`Uncertain` 类型节点的阈值。
- **作用范围**：`apply-isolated-policy`

### `isolated_policy.force_threshold_entrypoint`（默认：`0.60`）
- **含义**：`Entrypoint` 类型节点的阈值。
- **作用范围**：`apply-isolated-policy`

---

## repair_calls

### `repair_calls.top_k`（默认：`6`）
- **含义**：每个缺失调用节点最多候选补边数量。
- **作用范围**：`repair-calls`

### `repair_calls.sim_threshold`（默认：`0.58`）
- **含义**：补边相似度阈值。
- **作用范围**：`repair-calls`

### `repair_calls.max_edges_per_node`（默认：`3`）
- **含义**：每个节点最终新增边上限。
- **作用范围**：`repair-calls`

### `repair_calls.reclassify`（默认：`false`）
- **含义**：补边后是否重新分类孤立节点。
- **作用范围**：`repair-calls`

---

## graph_query

### `graph_query.graph_mode`（默认：`code`）
- **含义**：图查询模式。
- **可选值**：`code | intent | explore`
- **作用范围**：`query-graph`

### `graph_query.hops`（默认：`1`）
- **含义**：子图扩展跳数。
- **作用范围**：`query-graph`（`code` 模式）

### `graph_query.edge_type`（默认：`calls`）
- **含义**：边类型过滤。
- **作用范围**：`query-graph`（`code` 模式）

### `graph_query.module_top_k`（默认：`5`）
- **含义**：`explore` 模式中模块级意图召回的社区数量上限。
- **作用范围**：`query-graph`（`explore` 模式）

### `graph_query.function_top_k`（默认：`8`）
- **含义**：`explore` 模式中函数级意图召回的候选函数上限。
- **作用范围**：`query-graph`（`explore` 模式）

### `graph_query.semantic_top_k`（默认：`8`）
- **含义**：`explore` 模式中语义召回的 chunk 候选上限。
- **作用范围**：`query-graph`（`explore` 模式）

### `graph_query.seed_fusion`（默认：`rrf`）
- **含义**：多路 seed 召回融合策略。
- **可选值**：`rrf | linear`
- **作用范围**：`query-graph`（`explore` 模式）

### `graph_query.module_seed_member_top_k`（默认：`3`）
- **含义**：每个命中模块社区映射为 code graph seed 时保留的代表函数数量。
- **作用范围**：`query-graph`（`explore` 模式）

### `graph_query.explore_default_hops_module`（默认：`2`）
- **含义**：命中模块级社区时的默认扩图跳数。
- **作用范围**：`query-graph`（`explore` 模式）

### `graph_query.explore_default_hops_function`（默认：`1`）
- **含义**：仅命中函数级/语义 seed 时的默认扩图跳数。
- **作用范围**：`query-graph`（`explore` 模式）

### `graph_query.min_seed_score`（默认：`0.0`）
- **含义**：seed 融合后的最小保留分数阈值。
- **作用范围**：`query-graph`（`explore` 模式）

