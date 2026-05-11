# Java Index 与 Spring 评测统一操作手册

本文是日常操作的统一入口。其他文档只作为背景、配置参考或历史实验记录。

## 什么时候重建 DB

先判断你改了什么：

| 变化 | 操作 |
|------|------|
| 只换 embedding 模型、维度、provider、LanceDB URI | 复用同一 SQLite DB，只跑 `embed` + `eval-retrieval-compare` |
| Spring commit 变了 | 重新 `build-java-index` |
| `source_backend` 变了，例如 `tree-sitter-java` 换 `scip-java` | 重新 `build-java-index` |
| ingest / symbols / occurrences / relations 逻辑变了 | 重新 `build-java-index` |
| chunk 策略或 chunk 参数变了 | 重新 `build-java-index`，并重算所有模型向量 |
| `chunk.symbol_context_enabled` / `chunk.symbol_cards_enabled` / `chunk.symbol_context_max_tokens` 变了 | 重新 `build-java-index`，并重算所有模型向量 |

2026-05-08 的 annotation、`annotated_with`、`field_type`、symbol context、symbol card 改动属于 ingest/chunk 逻辑变化。不要在 2026-05-07 的旧 DB 上只重跑 `embed`。

## 推荐目录

```text
hybrid_platform/var/spring-eval/
  index/
    spring-6ec2455e-ts-symbolctx.db
  models/
    qwen3-emb-8b/
      config.json
      lancedb/
      report.json
    bge-code-v1/
      config.json
      lancedb/
      report.json
  aggregate/
    summary_YYYY-MM-DD.md
    summary_YYYY-MM-DD.json
```

旧 DB 和旧报告可以保留作 baseline，不要覆盖。

## 固定实验参数

Spring 主 commit：

```text
6ec2455e2491650fbeb7efaf78615a72700995ad
```

评测集：

```text
/data1/qadong/codeindex_java/JAVA test/spring_framework_eval_v1_reviewed.verify.jsonl
```

默认只评 `repo_sha == --commit` 的样本。当前主 commit 样本通常是 80 条。

## 1. 准备配置

日常推荐直接使用大一统脚本。它会自动探测模型维度、生成 `models/<name>/config.json`、按需重建 DB、跑评测并汇总：

```bash
cd /data1/qadong/codeindex_java/hybrid_platform

# 代码索引 / chunk 逻辑改过：加 --rebuild
scripts/run_spring_eval.sh \
  bge-code-v1 \
  /data_nvme0/models/embedding/bge-code-v1 \
  --rebuild

# 只是重跑同一个模型或只换 embedding：不加 --rebuild
scripts/run_spring_eval.sh \
  bge-code-v1 \
  /data_nvme0/models/embedding/bge-code-v1
```

默认参数：

- DB：`var/spring-eval/index/spring-6ec2455e-ts-symbolctx.db`
- API base：`http://118.196.65.175:8000/v1`
- dataset：`/data1/qadong/codeindex_java/JAVA test/spring_framework_eval_v1_reviewed.verify.jsonl`
- top-k：`5`、`10`

下面是脚本背后的手动流程，排错时再看。

每个模型一份配置，建议从 `config/default_config.json` 复制，只改 `embedding` 和 `vector`。

必须确认：

```json
{
  "java_index": {
    "source_backend": "tree-sitter-java"
  },
  "chunk": {
    "symbol_context_enabled": true,
    "symbol_cards_enabled": true,
    "symbol_context_max_tokens": 220
  },
  "query": {
    "blend_strategy": "linear"
  }
}
```

每个模型单独设置：

```json
{
  "embedding": {
    "version": "qwen3-emb-8b",
    "provider": "http",
    "model": "/data_nvme0/models/embedding/Qwen3-Embedding-8B",
    "dim": 4096,
    "api_base": "http://118.196.65.175:8000/v1",
    "endpoint": "/embeddings"
  },
  "vector": {
    "backend": "lancedb",
    "write_mode": "lancedb_only",
    "lancedb": {
      "uri": "/data1/qadong/codeindex_java/hybrid_platform/var/spring-eval/models/qwen3-emb-8b/lancedb",
      "table": "chunk_vectors",
      "metric": "cosine"
    }
  }
}
```

不同维度模型必须使用不同 LanceDB 目录。

## 2. 重建 tree-sitter Java 索引

在 2026-05-08 新索引逻辑下，先新建共享 DB：

```bash
cd /data1/qadong/codeindex_java/hybrid_platform

myenv/bin/python -m hybrid_platform.cli \
  --config var/spring-eval/models/qwen3-emb-8b/config.json \
  build-java-index \
  --repo-root /data1/qadong/workspace/spring-framework \
  --repo spring-projects/spring-framework \
  --commit 6ec2455e2491650fbeb7efaf78615a72700995ad \
  --db var/spring-eval/index/spring-6ec2455e-ts-symbolctx.db \
  --source-backend tree-sitter-java
```

这一步会执行：

```text
source backend -> ingest -> build-code-graph -> chunk -> embed
```

第一个配置里的模型会顺带写入第一份向量。

## 3. 为其他模型追加向量并评测

推荐使用脚本：

```bash
cd /data1/qadong/codeindex_java/hybrid_platform

SPRING_EVAL_ROOT="$PWD/var/spring-eval" scripts/run_one_model_eval.sh \
  bge-code-v1 \
  "$PWD/var/spring-eval/index/spring-6ec2455e-ts-symbolctx.db" \
  "/data1/qadong/codeindex_java/JAVA test/spring_framework_eval_v1_reviewed.verify.jsonl" \
  spring-projects/spring-framework \
  6ec2455e2491650fbeb7efaf78615a72700995ad \
  --top-k 5 --top-k 10
```

脚本会跑：

```text
embed
eval-retrieval-compare
```

手动命令等价于：

```bash
myenv/bin/python -m hybrid_platform.cli \
  --config var/spring-eval/models/bge-code-v1/config.json \
  embed \
  --db var/spring-eval/index/spring-6ec2455e-ts-symbolctx.db

myenv/bin/python -m hybrid_platform.cli \
  --config var/spring-eval/models/bge-code-v1/config.json \
  eval-retrieval-compare \
  --db var/spring-eval/index/spring-6ec2455e-ts-symbolctx.db \
  --repo spring-projects/spring-framework \
  --commit 6ec2455e2491650fbeb7efaf78615a72700995ad \
  --dataset "/data1/qadong/codeindex_java/JAVA test/spring_framework_eval_v1_reviewed.verify.jsonl" \
  --top-k 5 --top-k 10 \
  --output var/spring-eval/models/bge-code-v1/report.json
```

## 4. 汇总报告

```bash
cd /data1/qadong/codeindex_java/hybrid_platform

myenv/bin/python scripts/aggregate_retrieval_compare_reports.py \
  var/spring-eval/models/*/report.json \
  --json-out var/spring-eval/aggregate/summary_$(date +%F).json \
  > var/spring-eval/aggregate/summary_$(date +%F).md
```

## 5. 看哪些指标

优先看：

```text
Hit@10
MRR@10
FileRecall@10
SymbolRecall@10
ChunkRecall@10
TargetRecall@10
nDCG@10
```

再看四路结果：

```text
dense
bm25
rrf
oracle_union
```

判断规则：

| 现象 | 下一步 |
|------|--------|
| `oracle_union` 明显高于 `dense` 和 `rrf` | 优先调 fusion/rerank |
| `oracle_union` 也不高 | 优先查索引内容、chunk 表达、gold 映射 |
| `dense` 高、BM25 低 | embedding 起主要作用，关键词表达可能不足 |
| BM25 高、`dense` 低 | dense 排序或 query embedding 可能有问题 |
| `missing_gold_files` / `missing_gold_symbols` 多 | 先查索引是否缺 gold 目标 |
| `empty_relevant` 多 | 先查评测集 gold 是否能映射到当前 DB |

分组指标在：

```text
summary.groups.source_type
summary.groups.semantic_scope
summary.groups.structure_status
summary.groups.query_source
```

## 6. MCP 使用

索引建好并启动服务后，Agent 侧仍只使用三个 MCP 工具：

```text
semantic_query
find_symbol
symbol_graph
```

`scip-java` 和 `tree-sitter-java` 索引都能用这三个工具。差异在结果精度和置信度，不在工具名。

## 相关文档

- `docs/config_reference.md`：完整配置项。
- `docs/retrieval_compare_eval.md`：评测指标和报告字段。
- `docs/ARCHITECTURE_MODULES.md`：代码模块和数据流维护说明。
- `docs/spring_embedding_eval_2026_05_07_runbook.md`：2026-05-07 旧实验记录，作为 baseline。
