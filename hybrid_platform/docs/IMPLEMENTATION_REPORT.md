# SCIP-Clang Hybrid Retrieval Platform 实施报告

**项目路径**：`scip-clang/hybrid_platform`  
**报告日期**：2025-03-12  
**版本**：基于当前代码库与规格文档（P0–P6）的完整实施总结

---

## 1. 项目概述

本平台是基于 **SCIP（Symbol-based Code Index Protocol）** 的混合检索落地实现，面向 C/C++ 等语言的代码索引与检索场景。实现覆盖从 `.scip` 解析、结构化/语义索引、到混合召回与图查询的完整流水线，并支持双层图（CodeGraph + IntentGraph）与 Leiden 社区发现、孤立节点策略及调用边修复等进阶能力。

### 1.1 目标与范围

- **输入**：`.scip` 二进制或 ndjson 导出、repo + commit 标识
- **输出**：可解释的混合检索结果、图子图、评测指标
- **规格阶段**：P0（Schema/DSL）→ P1（解析/入库）→ P2（图索引）→ P3（Chunk/Embedding）→ P4（混合召回/服务）→ P5（评测/可观测）→ P6（Leiden/Repair Calls）

---

## 2. 架构与模块总览

### 2.1 目录与依赖

| 路径/文件 | 说明 |
|-----------|------|
| `hybrid_platform/` | 运行时代码包 |
| `config/default_config.json` | 默认配置（可被 `--config` 覆盖） |
| `docs/config_reference.md` | 配置参数字典 |
| `specs/` | P0–P6 规格文档 |
| `examples/` | 示例 SCIP ndjson、评测数据集 |
| `pyproject.toml` | 项目元数据，依赖 `litellm` |

**可选依赖**：`numpy`（向量加速）、`sentence-transformers`（本地 embedding）、`igraph` + `leidenalg`（Leiden 社区发现）、`hnswlib`（社区构建时语义候选加速）。

### 2.2 核心模块一览

| 模块 | 文件 | 职责 |
|------|------|------|
| 数据模型 | `models.py` | RepoSnapshot, ScipDocument, SymbolNode, OccurrenceEdge, RelationEdge, Chunk, QueryResult |
| 配置 | `config.py` | AppConfig，支持 JSON 配置加载与深合并，命令行优先 |
| 解析 | `parser.py` | ndjson / 二进制 .scip 流式解析，产出规范化 document/symbol/occurrence/relation |
| 入库 | `ingestion.py` | IngestionPipeline，批写入 SqliteStore，支持重试 |
| 存储 | `storage.py` | SqliteStore：文档/符号/occurrence/relation/chunk/embedding/FTS、图表、意图表及查询 API |
| Query DSL | `dsl.py` | Query 数据类，symbol_exact / semantic_text / hybrid，校验 mode/blend_strategy |
| 向量 | `embedding.py` | DeterministicEmbedder、Http/Voyage/Local embedder，EmbeddingPipeline（chunk 构建、批 embed、语义检索） |
| 检索 | `retrieval.py` | HybridRetrievalService：structure + keyword + semantic 三路召回，linear/RRF 融合，def_of/refs_of/callers_of/callees_of |
| 代码图 | `code_graph.py` | CodeGraphBuilder：code_nodes/code_edges，直接 calls + 文档内推断 calls，度更新 |
| 函数意图 | `intent_builder.py` | FunctionIntentBuilder：LLM 生成 intent，deterministic 语义向量 + 拓扑向量，缓存 key 防重复 |
| 社区 | `community.py` | IntentCommunityBuilder：加权图（拓扑+语义+路径），Leiden 或 fallback 连通分量，模块 intent LLM 摘要 |
| 孤立策略 | `isolated_policy.py` | IsolatedNodePolicy：孤立节点分类（BoundaryExternal/Entrypoint/TrueLeaf/MissingEdge/NoiseNode/Uncertain），强制归并社区 |
| 补边 | `repair_calls.py` | CallsRepairer：MissingEdge 节点按语义+路径先验补 calls 边，可选重跑 isolated_policy |
| 图服务 | `graph_service.py` | GraphService：code_subgraph、intent_subgraph、explore（模块+函数+语义 seed，RRF/linear 融合） |
| 图评测 | `graph_eval.py` | GraphEvaluator：孤立/不确定/强制分配比例、社区与单例数 |
| 评测 | `eval.py` | Evaluator：基于 JSON 数据集的 recall@k、MRR、nDCG@10 |
| 服务 | `service_api.py` | HTTP 服务：/query、/graph/code/subgraph、/graph/intent/subgraph、/graph/intent/explore |
| CLI | `cli.py` | 全命令入口，统一 --config，子命令 ingest/chunk/embed/query/eval/build-code-graph/build-intent-fn/build-intent-module/apply-isolated-policy/repair-calls/query-graph/eval-graph/serve |
| Prompt | `prompt.py` | 函数级/模块级 intent 的 system/user prompt 模板 |

---

## 3. 分阶段实施详情

### 3.1 P0：Schema 与 Query DSL

**规格**：`specs/p0-schema.md`

**实施要点**：

- **Canonical 模型**：`models.py` 中 RepoSnapshot、ScipDocument、SymbolNode、OccurrenceEdge、RelationEdge、Chunk 与规格一一对应；document_id 为 `repo:commit:path`，symbol 含 symbol_fingerprint。
- **Query DSL**：`dsl.py` 提供 `Query(text, mode, top_k, blend_strategy, filters)`，`validate()` 校验 mode∈{structure,semantic,hybrid}、blend_strategy∈{linear,rrf}。工厂函数：`symbol_exact`、`semantic_text`、`hybrid`。
- **结构化查询**：通过 `SqliteStore` 实现 `def_of`、`refs_of`、`callers_of`、`callees_of`、`symbol_exact`，与 DSL 配合使用。

### 3.2 P1：.scip 流式解析与规范化

**规格**：`specs/p1-ingestion-parser.md`

**实施要点**：

- **解析**：`parser.py` 的 `parse_scip_stream(path, repo, commit, source_root)` 支持：
  - **ndjson**：`*.ndjson` / `*.jsonl` / `*.scip.ndjson`，按行 JSON，type∈{document,symbol,occurrence,relation}，单行解析失败仅跳过该行。
  - **二进制 .scip**：依赖 `scip_pb2` 或 `scip.scip_pb2`，解析 Index.documents，occurrences/symbols/relationships 映射为 occurrence/relation；source_root 用于读取源码 content。
- **入库**：`ingestion.py` 的 `IngestionPipeline` 按批（batch_size）写入 documents/symbols/occurrences/relations，支持 retries 与退避，最终返回 IngestionStats（documents/symbols/occurrences/relations/failures）。

### 3.3 P2：结构化索引与关系图查询

**规格**：`specs/p2-graph-index.md`

**实施要点**：

- **SQLite Schema**：`storage.py` 中 documents、symbols、occurrences、relations 表及索引；`code_graph.py` 中 `GRAPH_SCHEMA_SQL` 定义 code_nodes（node_id, symbol_id, path, signature, fan_in, fan_out, is_isolated, isolated_type 等）、code_edges（src_node, dst_node, edge_type, weight, confidence, evidence_json）。
- **CodeGraph 构建**：`CodeGraphBuilder.build(repo, commit)`：从 symbols+occurrences(definition)+documents 生成 code_nodes；从 relations(relation_type=calls) 生成直接 calls 边；同文档内 definition→reference 推断 calls（置信度上限 0.49）；刷新 fan_in/fan_out。
- **图查询**：`GraphService.code_subgraph(seed_ids, hops, edge_type)` 做 BFS 扩展；`intent_subgraph(community_ids)` 查 intent_communities + module_intents。

### 3.4 P3：Chunk + Embedding + 向量检索

**规格**：`specs/p3-embedding.md`

**实施要点**：

- **Chunk**：`EmbeddingPipeline.build_chunks` 按文档、以 definition occurrence 为跨度做函数级切分，超长按 `target_tokens`/`overlap_tokens` 二次切块（`_chunks_for_span`），chunk_id 形如 `{doc_id}:fn:{symbol_id}:p{part}`，primary_symbol_ids 写入 chunks 表。
- **Embedding**：支持 provider：`deterministic`（hash 基线）、`http`、`voyage`、`local`（sentence-transformers）。批处理由 `batch_size`、`max_workers` 控制；向量存 embeddings 表（chunk_id, embedding_version, vector_json）。
- **向量检索**：`EmbeddingPipeline.semantic_search(query, embedding_version, top_k)`：query 向量与库内向量余弦相似度排序返回 (chunk_id, score)。

### 3.5 P4：混合召回与重排服务

**规格**：`specs/p4-retrieval-serving.md`

**实施要点**：

- **三路召回**：`HybridRetrievalService.query` 同时拉取：structure（`symbol_exact`）、keyword（FTS5 `chunks_fts`）、semantic（`semantic_search`）。
- **融合**：`_linear_fusion`（权重 structure 0.5 / keyword 0.2 / semantic 0.3）与 `_rrf_fusion`（k=60），按 `Query.blend_strategy` 选择；结果带 `explain` 子分数。
- **附加代码**：`_attach_code` 根据 `include_code`/`max_code_chars` 为 chunk 或 symbol 结果附加 path、code 片段、truncated 标记。
- **服务**：`service_api.py` 提供 `POST /query`（body：query, mode, top_k, blend_strategy, include_code, max_code_chars），返回 results 列表（id, type, score, explain, payload）。

### 3.6 P5：评测与可观测性

**规格**：`specs/p5-eval-observability.md`

**实施要点**：

- **检索评测**：`eval.py` 的 `Evaluator.run(dataset_path, mode, top_k)` 读取 JSON 数据集（samples[].query + relevant_ids），对每条 query 调用 `HybridRetrievalService.query`，计算 recall@k、MRR、nDCG@10，`format_metrics` 输出格式化指标。
- **图评测**：`graph_eval.py` 的 `GraphEvaluator.run()` 统计：isolated_ratio、uncertain_ratio、forced_assignment_ratio、singleton_communities、communities。
- **CLI**：`eval`、`eval-graph` 子命令；可选 `observability.py` 中的 MetricsRecorder/_Timer 用于耗时统计（若接入）。

### 3.7 P6：Leiden 升级与 Repair Calls

**规格**：`specs/p6-leiden-repair-calls.md`

**实施要点**：

- **Leiden**：`community.py` 中 `_components_by_leiden` 使用 `igraph` + `leidenalg.RBConfigurationVertexPartition`，resolution 可配；若导入或运行失败则回退 `_components_from_weights`（基于 threshold 的连通分量），并记录 algorithm_used / fallback_reason。
- **多分辨率**：`IntentCommunityBuilder.build` 支持 `resolutions` 列表，对每个 resolution 跑 Leiden/fallback，按 objective（cohesion、stability、singleton_ratio）选最优，并写入 intent_community_runs / intent_community_members_history。
- **Repair Calls**：`repair_calls.py` 的 `CallsRepairer.run` 针对 `code_nodes.isolated_type='MissingEdge'` 的节点，用 function_intents 的 semantic_vec 计算与其它节点的相似度，叠加路径先验，取 top_k 且 score≥sim_threshold，插入双向 calls 边（evidence 标记 repair_missing_edge），并更新 fan_in/fan_out；可选 `reclassify=True` 再跑 `IsolatedNodePolicy`。

---

## 4. 配置与运维

### 4.1 配置优先级与入口

- **优先级**：命令行参数 > 配置文件（`--config`）> 代码内默认值。
- **默认配置**：`config/default_config.json`，各段含义见 `docs/config_reference.md`。
- **主要配置段**：ingest、chunk、embedding、query、eval、server、intent、community、isolated_policy、repair_calls、graph_query。

### 4.2 典型流水线命令

```bash
# 入库
python -m hybrid_platform.cli ingest --repo demo/repo --commit abc123 \
  --input examples/sample.scip.ndjson --db examples/demo.db

# Chunk + Embed
python -m hybrid_platform.cli chunk --db examples/demo.db --repo demo/repo --commit abc123
python -m hybrid_platform.cli embed --db examples/demo.db

# 混合检索
python -m hybrid_platform.cli query --db examples/demo.db --query "parse command line options" \
  --mode hybrid --include-code --max-code-chars 1200

# 双层图
python -m hybrid_platform.cli build-code-graph --db examples/demo.db --repo demo/repo --commit abc123
python -m hybrid_platform.cli build-intent-fn --db examples/demo.db
python -m hybrid_platform.cli build-intent-module --db examples/demo.db --resolution 1.0
python -m hybrid_platform.cli apply-isolated-policy --db examples/demo.db
python -m hybrid_platform.cli repair-calls --db examples/demo.db --reclassify
python -m hybrid_platform.cli eval-graph --db examples/demo.db

# 图查询
python -m hybrid_platform.cli query-graph --db examples/demo.db --graph-mode code \
  --seed-ids "fn:scip-cpp demo main()." --hops 2
python -m hybrid_platform.cli query-graph --db examples/demo.db --graph-mode explore --query "options parser"

# 服务
python -m hybrid_platform.cli serve --db examples/demo.db --port 9301
```

### 4.3 HTTP 接口摘要

| 路径 | 方法 | 说明 |
|------|------|------|
| `/query` | POST | 混合检索，body: query, mode, top_k, blend_strategy, include_code, max_code_chars |
| `/graph/code/subgraph` | POST | seed_ids, hops, edge_type |
| `/graph/intent/subgraph` | POST | community_ids |
| `/graph/intent/explore` | POST | query/symbol, module_top_k, function_top_k, semantic_top_k, seed_fusion, hops 等 |

---

## 5. 数据流与表结构摘要

### 5.1 核心表

- **documents**：document_id, repo, commit_hash, relative_path, language, occurrence_count, content  
- **symbols**：symbol_id, display_name, kind, package, signature_hash, symbol_fingerprint  
- **occurrences**：document_id, symbol_id, range_*, role  
- **relations**：from_symbol, to_symbol, relation_type, confidence, evidence_document_id  
- **chunks**：chunk_id, document_id, content, primary_symbol_ids, span_*, embedding_version  
- **chunks_fts**：FTS5 虚拟表，content  
- **embeddings**：(chunk_id, embedding_version), vector_json  
- **code_nodes**：node_id, symbol_id, node_type, path, signature, fan_in, fan_out, is_isolated, isolated_type, isolation_confidence, isolation_reason, meta_json  
- **code_edges**：edge_id, src_node, dst_node, edge_type, weight, confidence, evidence_json  
- **function_intents**：node_id, intent_text, intent_tags_json, quality_score, role_in_chain, fan_in, fan_out, chain_depth, semantic_vec_json, topology_vec_json, fused_vec_json, model_version, prompt_version, cache_key  
- **intent_communities**：community_id, node_id, cohesion_score, assign_score, assignment_mode  
- **module_intents**：community_id, module_intent, module_tags_json, backbone_json, cohesion_score, size  
- **intent_community_runs** / **intent_community_members_history**：多分辨率 run 记录与成员历史

### 5.2 示例数据

`examples/sample.scip.ndjson` 包含 document（main.cc/options.cc）、symbol（add, main, parse_options）、occurrence（definition/reference）、relation（main calls add），与当前实现完全兼容。

---

## 6. 技术要点与设计选择

- **多输入**：同时支持 ndjson 与二进制 .scip，便于从不同索引管线接入。  
- **可解释性**：检索结果带 explain（structure/keyword/semantic 子分），图结果带 seed_nodes、seed_communities、explain。  
- **降级**：无向量时可用 structure+keyword；无 LLM 时 intent 用规则 fallback；无 Leiden 时用连通分量 fallback。  
- **版本化**：index_version、embedding_version、intent_pipeline_version、intent_prompt_version 支持回溯与 A/B。  
- **可扩展**：Embedding 通过 provider 切换；Prompt 集中在 `prompt.py`；配置与 CLI 统一，便于生产调参。

---

## 7. 验收与后续建议

### 7.1 当前实现与规格对应

| 规格 | 状态 | 备注 |
|------|------|------|
| P0 Schema/DSL | ✅ | 模型与 DSL 完整，结构化查询可用 |
| P1 解析/入库 | ✅ | ndjson + binary scip，批写入与重试 |
| P2 图索引/查询 | ✅ | CodeGraph 构建，code/intent 子图 |
| P3 Chunk/Embed | ✅ | 函数优先切块，多 provider embedding，语义检索 |
| P4 混合召回/服务 | ✅ | 三路召回、linear/RRF、HTTP /query |
| P5 评测/可观测 | ✅ | recall/MRR/nDCG、图指标、CLI eval/eval-graph |
| P6 Leiden/Repair | ✅ | Leiden+fallback，MissingEdge 补边，可选 reclassify |

### 7.2 建议后续工作

1. **性能**：大库下 embedding 全量内存加载可改为分页或外部向量库（如 HNSW 索引）。  
2. **评测**：补充端到端验收脚本（如 README 中的 `eval/run_acceptance.py`）与标准数据集格式说明。  
3. **可观测**：将 observability 与 eval/serve 打通，输出 Prometheus/日志指标。  
4. **多 repo/commit**：当前存储支持多 repo/commit，可明确文档化增量索引与查询过滤策略。

### 7.3 MCP 子系统交付（补充）

- **stdio MCP**、**Streamable HTTP MCP**、工具/错误契约、REST 对照与 **多索引库固定规则**（不由 Agent 选库）已写入 **[mcp_delivery_handbook.md](./mcp_delivery_handbook.md)**（MCP 交付手册）。  
- **Agent 产品集成**（**Part I** 正式协议面：`initialize` / `tools/list` / `tools/call`；**Part II** 集成/运维）见 **[mcp_agent_integration_delivery.md](./mcp_agent_integration_delivery.md)**。  
- 工具 Schema、错误码与 JSON 示例见 [mcp_metadata_and_errors.md](./mcp_metadata_and_errors.md)；远程 HTTP 部署见 [mcp_streamable_http.md](./mcp_streamable_http.md)。

---

**报告结束。** 本文档与代码库及 `specs/`、`docs/config_reference.md` 保持一致，可作为交付与维护的参考依据；**MCP 专项交付** 请同时查阅 `docs/mcp_delivery_handbook.md`。
