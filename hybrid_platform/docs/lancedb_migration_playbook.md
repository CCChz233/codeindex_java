# LanceDB 迁移与回滚手册

本文档描述如何从 SQLite 向量检索迁移到 LanceDB，并给出 A/B 验证与回滚流程。

## 1. 配置切换原则

- `vector.backend` 控制**读路径**：`sqlite` 或 `lancedb`
- `vector.write_mode` 控制**写路径**：
  - `sqlite_only`
  - `dual`（SQLite + LanceDB 双写）
  - `lancedb_only`

推荐顺序：

1. `backend=sqlite`, `write_mode=dual`
2. 验证 LanceDB 数据与查询表现
3. 切 `backend=lancedb`
4. 稳定后可选切 `write_mode=lancedb_only`

## 2. 预热与数据回填

### 2.1 双写回填

先把配置改为双写，然后重新跑一次 `embed`：

```bash
python -m hybrid_platform.cli --config <dual_config.json> embed --db <path/to/db.sqlite>
```

说明：此步骤会把当前 `chunks` 对应向量同时写入 SQLite 与 LanceDB。

### 2.2 基础完整性检查

- SQLite 向量数：`SELECT COUNT(*) FROM embeddings WHERE embedding_version='<v>';`
- LanceDB 向量数：读取 `vector.lancedb.table` 行数
- 抽样检查 `chunk_id` 是否可在 `chunks` 表中找到元数据

## 3. A/B 验证（建议）

可使用 `eval/run_acceptance.py`：

```bash
# A: sqlite
python eval/run_acceptance.py \
  --db <db.sqlite> \
  --queries <queries.json> \
  --embedding-version <v> \
  --vector-backend sqlite

# B: lancedb
python eval/run_acceptance.py \
  --db <db.sqlite> \
  --queries <queries.json> \
  --embedding-version <v> \
  --vector-backend lancedb \
  --lancedb-uri <lancedb_uri> \
  --lancedb-table <table_name>
```

建议比较：

- `acceptance_total_ms`
- `query_ms` 的 P50/P95
- 同一查询下 Top-K 结果重叠率

## 4. 线上灰度策略

1. 先 `write_mode=dual`，`backend=sqlite`
2. 灰度环境切 `backend=lancedb`
3. 观察延迟、错误率、召回质量
4. 全量切换

## 5. 回滚方案

发生异常时立即执行：

1. 将 `vector.backend` 改回 `sqlite`
2. 保持 `vector.write_mode=dual`（可保留 LanceDB 回填）
3. 若需完全回退，改为 `write_mode=sqlite_only`

该回滚不依赖数据回灌，切配置即可生效。
