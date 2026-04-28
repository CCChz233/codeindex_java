# BM25 vs Dense Retrieval Compare 使用文档

本文说明如何使用 `eval-retrieval-compare` 对同一份评测集同时跑纯 dense 召回和 SQLite FTS5 BM25 召回。

## 1. 模块定位

`eval-retrieval-compare` 只读现有 index DB，不建索引，不修改 DB。

它对每条 query 做两次独立检索：

- `dense`：调用 `EmbeddingPipeline.semantic_search(query, embedding_version, top_k)`
- `bm25`：调用 `SqliteStore.keyword_search(query, top_k)`，底层是 `bm25(chunks_fts)`

输出包含：

- 最终表格指标：`Recall@5`、`MRR@5`、`Recall@10`、`MRR@10`
- 每个 retriever 的 `chunk_recall@k`
- 每条 case 的 dense/BM25 排名、命中文件、命中 symbol、失败原因
- `index_info`，用于确认 `source_mode`、capabilities、repo、commit

这里的 `Recall@k` 是现有表格口径：top-k 内命中任意相关 chunk 就记为 1，再对 query 求平均。

## 2. 典型命令

```bash
python -m hybrid_platform.cli --config /path/to/eval_config.json eval-retrieval-compare \
  --db /path/to/index.db \
  --repo spring-projects/spring-framework \
  --commit 6ec2455e2491650fbeb7efaf78615a72700995ad \
  --dataset "/path/to/spring_framework_eval_v1_reviewed.verify.jsonl" \
  --top-k 5 \
  --top-k 10 \
  --output /path/to/bm25_dense_report.json
```

如果评测集的 `repo_sha` 是混合的，默认只评测 `repo_sha == --commit` 的样本。不匹配的样本会进入 `skipped_cases`，并统计到 `summary.skipped_commit_mismatch`。

临时粗测全量样本可以加：

```bash
--include-commit-mismatches
```

## 3. 评测集格式

主要支持 Spring reviewed flat JSONL：

```json
{"sample_id":"case-1","query":"where is byte buffer capacity expanded","gold_files":"src/main/java/a/A.java | src/main/java/b/B.java","gold_symbols":"org.example.A#grow/0","repo_sha":"6ec2455e2491650fbeb7efaf78615a72700995ad"}
```

也支持 `ground_truth` 或 `expected` 包装：

```json
{"id":"case-2","query":"where is parser configured","ground_truth":{"gold_files":["src/main/java/a/A.java"],"gold_symbols":["org.example.A#parse/1"]}}
```

相关性使用 `gold_files ∪ gold_symbols ∪ gold_chunks`：

- `gold_files`：文件下所有 chunks 都算相关
- `gold_symbols`：按现有 Spring symbol-to-chunk 逻辑展开，并用 chunk primary symbols 做直接匹配兜底
- `gold_chunks`/`chunk_ids`：直接按 chunk_id 判断

## 4. 输出说明

报告核心字段：

```json
{
  "summary": {
    "loaded_cases": 200,
    "evaluated_cases": 80,
    "skipped_cases": 120,
    "skipped_commit_mismatch": 120,
    "dense": {"recall@5": 0.6875, "mrr@5": 0.5339},
    "bm25": {"recall@5": 0.1875, "mrr@5": 0.07396}
  },
  "table_markdown": "| Metric | Dense | BM25 | ...",
  "cases": [],
  "skipped_cases": [],
  "index_info": {}
}
```

每条 case 的失败原因可能是：

- `empty_relevant`：gold 无法在当前 DB/repo/commit 下展开成 chunk
- `no_results`：该 retriever 没有返回结果
- `no_relevant_hit`：有结果，但 top-k 内没有命中相关 chunk
- `case_error`：检索过程中抛出异常，错误细节会写入 `error`

## 5. 服务器运行建议

BM25 只需要 SQLite DB；dense 还需要能访问 query embedding 服务，并且配置里的 `embedding.version`、维度、vector backend 要和建索引时一致。

如果 index DB 和 LanceDB 都在服务器文件系统上，建议直接在服务器运行 CLI，再把 JSON report 下载回 Mac 查看。

## 6. Spring Framework reviewed 评测集：从拉仓库到跑评测

你的评测集：

```text
/Users/chz/workspace/codeindex_java/JAVA test/spring_framework_eval_v1_reviewed.verify.jsonl
```

这份 JSONL 没有 repo 字段，只有 `repo_sha`。其中主 commit 是：

```text
6ec2455e2491650fbeb7efaf78615a72700995ad
```

这个 commit 对应 80 条样本。建议第一轮只建这个 commit 的 index，因为 `eval-retrieval-compare` 默认会按 `repo_sha == --commit` 过滤样本，指标更可信。

### 6.1 在服务器拉仓库并 checkout

```bash
mkdir -p /data/codeindex/workspace
cd /data/codeindex/workspace

git clone https://github.com/spring-projects/spring-framework.git
cd spring-framework
git fetch --all --tags
git checkout 6ec2455e2491650fbeb7efaf78615a72700995ad
git rev-parse HEAD
```

### 6.2 上传评测集

在 Mac 上执行：

```bash
scp "/Users/chz/workspace/codeindex_java/JAVA test/spring_framework_eval_v1_reviewed.verify.jsonl" \
  user@server:/data/codeindex/evals/spring_framework_eval_v1_reviewed.verify.jsonl
```

### 6.3 准备服务器配置

在服务器上创建：

```bash
mkdir -p /data/codeindex/config /data/codeindex/indices /data/codeindex/evals
vim /data/codeindex/config/spring_eval_config.json
```

如果向量写入 SQLite，配置示例：

```json
{
  "java_index": {
    "scip_java_cmd": "/data/codeindex_java/hybrid_platform/scripts/run-scip-java-spring62.sh",
    "build_tool": "gradle",
    "output": "index.scip",
    "fallback_mode": "syntax"
  },
  "embedding": {
    "provider": "http",
    "version": "v1",
    "model": "your-embedding-model",
    "dim": 1024,
    "api_base": "http://your-embedding-host:port/v1",
    "endpoint": "/embeddings",
    "api_key": "",
    "timeout_s": 30,
    "batch_size": 128,
    "max_workers": 8
  },
  "vector": {
    "backend": "sqlite",
    "write_mode": "sqlite_only",
    "lancedb": {}
  }
}
```

需要替换：

- `scip_java_cmd`：服务器上实际可用的 scip-java 或 Spring 6.2 包装脚本路径
- `embedding.model`：你的 embedding 模型名
- `embedding.dim`：模型输出维度，必须和实际输出一致
- `embedding.api_base` / `endpoint`：服务器可访问的 embedding 服务地址

如果向量写入 LanceDB，把 `vector` 改成：

```json
{
  "backend": "lancedb",
  "write_mode": "lancedb_only",
  "lancedb": {
    "uri": "/data/codeindex/indices/spring-framework-6ec2455e.lancedb",
    "table": "chunk_vectors",
    "metric": "cosine"
  }
}
```

### 6.4 建 Spring Framework index

在服务器代码目录执行：

```bash
cd /data/codeindex_java/hybrid_platform

python -m hybrid_platform.cli --config /data/codeindex/config/spring_eval_config.json build-java-index \
  --repo-root /data/codeindex/workspace/spring-framework \
  --repo spring-projects/spring-framework \
  --commit 6ec2455e2491650fbeb7efaf78615a72700995ad \
  --db /data/codeindex/indices/spring-framework-6ec2455e.db \
  --build-tool gradle \
  --fallback-mode syntax
```

`build-java-index` 会跑完整链路：

```text
scip-java -> ingest -> code graph -> chunk -> embed
```

因此建完以后不需要再单独跑 `embed`。

如果 scip-java 或 Gradle 失败，且 `fallback_mode=syntax`，命令会降级为 tree-sitter syntax index。报告里的 `index_info.source_mode` 会显示最终模式。对 BM25/dense 召回评测来说，syntax fallback 仍可评测文件/chunk 召回，但结构能力会弱于 SCIP。

### 6.5 跑 BM25 vs Dense 评测

```bash
python -m hybrid_platform.cli --config /data/codeindex/config/spring_eval_config.json eval-retrieval-compare \
  --db /data/codeindex/indices/spring-framework-6ec2455e.db \
  --repo spring-projects/spring-framework \
  --commit 6ec2455e2491650fbeb7efaf78615a72700995ad \
  --dataset /data/codeindex/evals/spring_framework_eval_v1_reviewed.verify.jsonl \
  --top-k 5 \
  --top-k 10 \
  --output /data/codeindex/evals/bm25_dense_report.json
```

查看最终表：

```bash
python - <<'PY'
import json
r = json.load(open("/data/codeindex/evals/bm25_dense_report.json"))
print(r["table_markdown"])
print("loaded_cases:", r["summary"]["loaded_cases"])
print("evaluated_cases:", r["summary"]["evaluated_cases"])
print("skipped_commit_mismatch:", r["summary"]["skipped_commit_mismatch"])
PY
```

### 6.6 关于评完整 200 条

这份评测集是混合 commit。上面的流程只评测主 commit 的 80 条。

如果要评完整 200 条，需要按每个 `repo_sha` 分别：

1. checkout 到对应 commit
2. 建对应 DB
3. 用同一份 JSONL 跑 `eval-retrieval-compare`
4. 聚合多个 report

不要直接用一个 commit 的 DB 加 `--include-commit-mismatches` 当作正式指标；这只能用于粗略排查，因为 gold 文件和 symbol 可能来自不同代码快照。
