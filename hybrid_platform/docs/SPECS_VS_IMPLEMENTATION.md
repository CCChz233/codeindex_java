# Specs 与当前实现对照

本文档对照 `hybrid_platform/specs/` 下 **P0–P6** 规格说明与仓库内 **Python 实现**（`hybrid_platform/` 包、`eval/` 脚本），标注：**已实现**、**部分实现**、**未实现 / 规格外**。  
**结论日期**：以仓库当前状态为准（2026-04-07 检视）。

---

## 总览

| 规格 | 主题 | 总体完成度 |
|------|------|------------|
| P0 | Schema & Query DSL | 高：核心模型与 DSL 已落地；缺 `semantic_code`、结构化 `scope`/`depth`、显式 `schema_version`、性能 SLO 未在运行时强制 |
| P1 | Ingestion & Parser | 中高：流式解析、批写、重试、二进制/ndjson 已支持；缺质量门禁、死信队列、分钟级增量 ingest |
| P2 | 结构化索引与图 | 高：表结构 + 核心查询 + `code_graph` 派生；二级索引与增量/TTL 与规格有差距 |
| P3 | Embedding 管线 | 高：符号边界切块、版本化、批量重试、SQLite 暴力检索 + LanceDB；IVF/HNSW 显式切换与向量检索按 repo 分片未对齐 |
| P4 | 检索与服务 | 高：结构 + FTS5 + 向量 + linear/RRF + explain；混合模式下向量失败时隐式退化为空语义路，无显式降级率指标 |
| P5 | 评测与可观测 | 中：离线 Recall/MRR/NDCG、基础 MetricsRecorder、embedding 运行时统计；在线 QPS/命中率/治理项大量未做 |
| P6 | Leiden & repair-calls | 高：Leiden 可选依赖 + 回退、repair-calls + fan 更新 + 可选 isolated 重跑；evidence 字段命名与规格文案略有差异 |

---

## P0 — Schema & Query DSL

### 已实现

- **Canonical 实体**：`RepoSnapshot`、`ScipDocument`、`SymbolNode`、`OccurrenceEdge`、`RelationEdge`、`Chunk` 与规格字段基本对齐（`models.py`）；`ScipDocument` 额外含 `content`；`SymbolNode`/`OccurrenceEdge` 有扩展字段（如 `enclosing_symbol`、`syntax_kind`、enclosing range）。
- **结构化 DSL 工厂**：`symbol_exact`、`def_of`、`refs_of`、`callers_of`、`callees_of`、`semantic_text`、`hybrid`，`blend_strategy` 支持 `linear` / `rrf`（`dsl.py`）。
- **查询结果可解释**：`QueryResult.explain` 记录各路子分数（`retrieval.py`）。
- **SCIP → canonical**：`parser.py` 中 ndjson 与二进制 `.scip` 流式产出上述实体。

### 部分实现

- **`callers_of` / `callees_of` 的 depth**：规格写有 `depth` 参数；实现以 `top_k` 限制结果规模，无独立图深度参数（`storage`/`HybridRetrievalService`）。
- **`def_of` / `refs_of` 等的 scope**：规格写有 `scope`；`Query.filters` 存在但结构化路径未系统性用于限定仓库/文件范围。
- **关系类型命名**：规格为 `inherits`；SCIP 关系映射里使用 `extends` 等字符串（`parser.py`），语义接近但枚举名不一致。
- **失败恢复**：SQLite 有 `_migrate_schema` 做列级兼容，无全局 `schema_version` 字段与版本化读取策略（规格 6 节）。

### 未实现 / 未验证

- **`semantic_code(query, …)`**：`dsl.py` 中无独立 API；语义检索统一走自然语言 `Query.text`。
- **性能预算**：规格 `P95 structure < 300ms`、`P95 hybrid < 800ms` — 代码侧无统一限流/SLO 校验（仅 `eval/run_acceptance.py` 等可手工测延迟）。
- **验收「10+ 查询样例」**：见 `examples/queries_20.json` 等，是否覆盖「结构化+语义+混合」全类型需自行核对。

---

## P1 — Ingestion & Parser

### 已实现

- **输入**：`*.scip.ndjson` / `.ndjson` / `.jsonl` 与二进制 `.scip`（`parse_scip_stream`）。
- **流式 + 分批写入**：`IngestionPipeline` + 可配置 `batch_size`（`ingestion.py`）。
- **重试**：解析循环带 `retries` / `retry_backoff_s`（`ingestion.py`）。
- **幂等（同 repo+commit）**：`delete_repo_snapshot` 后全量重灌（`ingestion.py` + `storage.py`）。
- **文件级容错**：ndjson 单行 JSON 错误跳过（`parser.py`）。
- **失败统计**：`IngestionStats.failures`（整次解析失败计数，非 per-file 死信）。

### 部分实现

- **文件级错误隔离**：坏行跳过；整文件/二进制解析失败仍可能中断或由重试抛出（与「失败样本入队」不同）。

### 未实现

- **质量门禁**：空文档比例、symbol 总量异常、relation 缺失比等阈值检测与拒收/告警 — 代码中未见。
- **死信队列**：失败样本持久化队列 — 未见。
- **分钟级增量 ingest**：当前为按 `repo+commit` 快照删除后全量导入，非「仅变更文档」局部更新（规格 4 节「增量」指向 ingest 侧；图局部重建见 P2）。

---

## P2 — Structured Index & Graph

### 已实现

- **存储表**：`documents`、`symbols`、`occurrences`、`relations`、`chunks`、FTS5 `chunks_fts`、`embeddings`（`storage.py`）。
- **核心二级索引**：`repo+commit`、`symbol_id`、`document_id`、`display_name` 等（`SCHEMA_SQL`）。
- **图派生**：`code_graph.py` 从 occurrence/relation 构建 `code_nodes` / `code_edges`，含置信度与 `evidence_json`。
- **查询**：`def_of` / `refs_of` / `callers_of` / `callees_of` 在 `SqliteStore` + `HybridRetrievalService` 可用。
- **附加图服务**：`GraphService` 子图、intent 探索等（`graph_service.py`，超出 P2 最小集但增强调用图能力）。

### 部分实现

- **规格所列二级索引**：`kind`、`file_path` 专用索引 — `symbols` 无 `kind` 索引；路径通过 `documents.relative_path` 关联查询，非单独 `file_path` 列索引。
- **增量策略**：按 `repo+commit` 删除快照并重建关联数据可视为版本分层；**非**「仅重建受影响文档」的局部增量。
- **旧版本 TTL 惰性清理**：无自动 TTL；依赖显式 `delete_repo_snapshot` 或管理面操作。

---

## P3 — Embedding Pipeline

### 已实现

- **Chunk 策略**：符号/AST 边界优先、超长切分与 overlap、`primary_symbol_ids`（`embedding.py`）。
- **版本化**：`embedding_version` 贯穿 chunk、embedding 存储与检索。
- **批量计算与重试**：embed 批处理与失败批重试、流式写入与 commit 节奏（`embedding.py`）。
- **幂等写入**：同版本 upsert（SQLite / LanceDB merge_insert）。
- **ANN / 暴力**：`SqliteVectorStore` 全表点积排序；`LanceDbVectorStore` 使用 Lance 索引检索（`vector_store.py`、`vector_store_lancedb.py`）。

### 部分实现

- **默认 brute-force cosine**：SQLite 路径为点积；若向量未单位化则与「cosine」字面略有差异（取决于 embedder 是否归一化）。
- **HNSW/IVF**：未暴露规格式「可切换 IVF/HNSW」配置；LanceDB 内部实现视版本而定。

### 未实现

- **向量检索按 repo/namespace 分片**：Lance 查询主要按 `embedding_version` 过滤，未见规格中的分片策略。

---

## P4 — Retrieval & Serving

### 已实现

- **多路召回**：结构化（`symbol_exact` 等）、关键词（FTS/BM25）、语义向量（`HybridRetrievalService.query`）。
- **融合**：`linear` 与 `rrf`（`retrieval.py`）。
- **explain**：各路子分数写入 `QueryResult.explain`；RRF 仍保留子分数信息。
- **服务接口**：HTTP `POST /query`、`/query/structured` 等（`service_api.py`）；MCP 工具链（`mcp_server.py`、`agent_mcp_handlers.py` 等）。
- **向量不可用时的行为**：`semantic_search` 在 query 向量失败时返回空列表，混合检索仍融合结构与关键词 — 属于**隐式降级**，无单独「降级率」或错误码向客户端标明「语义路失败」（对比规格「可降级」表述）。

### 部分实现

- **统一 `query(mode, text, filters, …)`**：HTTP 与内部 `Query` 对齐；`structured_op` 通过单独端点或 DSL 分支表达。

### 未实现

- **规格级错误码矩阵**：有 `bad_request` / `service_not_ready` 等，未见完整「检索子系统失败」细分码。

---

## P5 — Evaluation & Observability

### 已实现

- **离线评测数据格式**：`hybrid_platform/eval.py` 中 `samples` 含 `query` 与 `relevant_ids`。
- **指标**：Recall@K、MRR、NDCG@10（`eval.py`）。
- **可重复脚本**：`eval/run_acceptance.py`、`eval/run_embedding_benchmark.py`、`eval/run_grep_query_jsonl.py`；Spring 相关 `spring_semantic_eval.py`、`spring_jsonl_semantic_eval.py`。
- **基础观测**：`observability.MetricsRecorder`（计数与 P95）；acceptance 脚本对其使用；`/stats/embedding` 暴露 embedding 运行时（`service_api.py`）。

### 部分实现

- **按查询类型分桶**：评测脚本未内置结构化分桶统计（需自建数据集或改脚本）。

### 未实现

- **在线观测**：ingestion/retrieval 的 QPS、P95、命中率、降级率 — 无集中 metrics 导出（如 Prometheus）。
- **quality proxy**：点击反馈、人工标注对齐率 — 未见。
- **治理**：敏感信息扫描与脱敏、索引生命周期管理、审计日志与变更记录 — 未见系统化实现。

---

## P6 — Leiden Upgrade & Repair Calls

### 已实现

- **Leiden（L1）**：尝试 `igraph` + `leidenalg`，`RBConfigurationVertexPartition`，失败则走连通分量等 **fallback**；`CommunityStats` 含 `algorithm_used`、`fallback_reason`（`community.py`）。
- **参数**：`resolution` 与候选边权重；边权重组合使用 **`alpha` / `beta` / `gamma`**（与规格一致，在 `build/run` 流程中可配置）。
- **Repair calls（R1）**：针对 `code_nodes.isolated_type = 'MissingEdge'`，用语义向量 + 路径先验，top-k、阈值、`max_edges_per_node`；写入 `calls` 边，`evidence_json` 含 `source: repair_missing_edge`（规格文案为 evidence 标记 `repair_missing_edge`，语义一致）；**跳过自环**（`dst == src`）；随后更新 `fan_in`/`fan_out`，可选 **`IsolatedNodePolicy` 重跑**（`repair_calls.py`）。

### 部分实现

- **双向边**：实现为对每条候选同时插入正向与略低权重的反向边（`repair_missing_edge_reverse`），规格主要描述单向低置信 calls；若产品上只要单向需在配置或代码层收敛。

---

## 关键代码索引（便于跳转）

| 领域 | 路径 |
|------|------|
| 模型与 DSL | `hybrid_platform/models.py`, `hybrid_platform/dsl.py` |
| 解析与 ingest | `hybrid_platform/parser.py`, `hybrid_platform/ingestion.py` |
| 存储与结构化检索 | `hybrid_platform/storage.py` |
| 调用图 | `hybrid_platform/code_graph.py` |
| 图 API | `hybrid_platform/graph_service.py` |
| 嵌入与切块 | `hybrid_platform/embedding.py` |
| 向量存储 | `hybrid_platform/vector_store.py`, `vector_store_lancedb.py` |
| 混合检索 | `hybrid_platform/retrieval.py` |
| HTTP 服务 | `hybrid_platform/service_api.py` |
| 离线评测 | `hybrid_platform/eval.py`, `eval/run_acceptance.py` |
| 指标 primitives | `hybrid_platform/observability.py` |
| 社区 / Leiden | `hybrid_platform/community.py` |
| repair-calls | `hybrid_platform/repair_calls.py` |

---

## 建议的后续优先级（可选）

1. **P1**：ingest 质量门禁 + 失败样本落盘/死信，便于达标「解析成功率」可运营。  
2. **P0/P4**：补齐 `semantic_code` 或文档化「与 semantic_text 合并」；混合检索在语义路失败时返回显式 `degraded` 标记。  
3. **P2**：按需增加 `symbols(kind)`、`documents(relative_path)` 索引；评估真正的文档级增量 ingest。  
4. **P5**：接入统一 metrics 导出与检索降级计数，满足在线观测闭环。  

---

*本文随 specs 或实现变更可能过时；更新时请同步修改本节日期与表格。*
