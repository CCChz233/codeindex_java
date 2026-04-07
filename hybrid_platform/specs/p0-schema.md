# P0 Schema & Query DSL Specification

## 1. 目标

定义统一 canonical schema 与 query DSL，保证 `repo+commit` 级可追溯性与版本化演进能力。

## 2. 输入输出契约

- 输入：`.scip` 解析结果与业务查询需求
- 输出：
  - Canonical entities
  - Query DSL（结构化、语义、混合）

## 3. Canonical Data Model

### 3.1 RepoSnapshot

- `repo`: string
- `commit`: string
- `index_version`: string
- `ingested_at_epoch_ms`: integer

### 3.2 ScipDocument

- `document_id`: string (`repo:commit:path`)
- `relative_path`: string
- `language`: string
- `occurrence_count`: integer

### 3.3 SymbolNode

- `symbol_id`: string (原始 SCIP symbol 主键)
- `display_name`: string
- `kind`: string
- `package`: string
- `signature_hash`: string
- `symbol_fingerprint`: string（跨 commit 弱稳定匹配）

### 3.4 OccurrenceEdge

- `document_id`: string
- `symbol_id`: string
- `range_start_line`: integer
- `range_start_col`: integer
- `range_end_line`: integer
- `range_end_col`: integer
- `role`: enum(`definition`, `reference`, `unknown`)

### 3.5 RelationEdge

- `from_symbol`: string
- `to_symbol`: string
- `relation_type`: enum(`calls`, `inherits`, `implements`, `references`)
- `confidence`: float `[0,1]`
- `evidence_document_id`: string

### 3.6 Chunk

- `chunk_id`: string
- `document_id`: string
- `content`: string
- `primary_symbol_ids`: string[]
- `span_start_line`: integer
- `span_end_line`: integer
- `embedding_version`: string

## 4. Query DSL

### 4.1 精确符号查询

- `symbol_exact(name, filters)`
- `def_of(symbol_id, scope)`
- `refs_of(symbol_id, scope)`
- `callers_of(symbol_id, depth, scope)`
- `callees_of(symbol_id, depth, scope)`

### 4.2 语义查询

- `semantic_text(query, top_k, filters)`
- `semantic_code(query, top_k, filters)`

### 4.3 混合查询

- `hybrid(query, top_k, filters, blend_strategy)`
- `blend_strategy`: `linear`, `rrf`

## 5. 性能预算

- `P95 structure < 300ms`
- `P95 hybrid < 800ms`

## 6. 失败模式与恢复

- schema 演进：采用 `schema_version` + 兼容读取
- 数据不一致：以 `repo+commit+symbol_id` 回溯修复
- 上游异常：按文件隔离，失败样本入死信队列

## 7. 验收

- 10+ 查询样例无歧义表达
- 样例 `.scip` 无损映射到 canonical schema
