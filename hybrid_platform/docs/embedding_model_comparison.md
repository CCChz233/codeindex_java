# 不同 Embedding 模型效果对比（推荐 SOP）

本文说明如何做 **dense（向量）召回** 的多模型对比实验。**默认推荐**：**一份共享 SQLite DB（chunks 不变）+ 每个模型独立 `embed` + 独立 LanceDB + 独立报告**；仅在「从零开始」或「需要隔离 chunks」时再为每模型跑完整 `build-java-index`。

> 日常操作优先看 [JAVA_INDEX_EVAL_RUNBOOK.md](./JAVA_INDEX_EVAL_RUNBOOK.md)。本文只展开多 embedding 模型对比细节。

这里的“chunks 不变”是硬前提：如果 source backend、ingest 逻辑、relations、chunk 策略、symbol context 或 symbol card 逻辑发生变化，需要先重建共享 DB，再为每个模型重算向量。2026-05-08 的 tree-sitter annotation + symbol card 优化属于必须重建 DB 的变化。

背景实现：`chunks` 与 embedder 解耦；向量按 `(chunk_id, embedding_version)` 存在 [`embeddings`](../hybrid_platform/storage.py)；[`cmd_embed`](../hybrid_platform/cli.py) 只补缺向量；LanceDB 表维度在首次创建时固定（[`vector_store_lancedb.py`](../hybrid_platform/vector_store_lancedb.py)）。

Spring 评测集与格式见 [retrieval_compare_eval.md](./retrieval_compare_eval.md)；Spring 跑通示例见 [spring_framework_eval_embedding_runbook.md](./spring_framework_eval_embedding_runbook.md)。

---

## 1. Pre-flight：维度与服务自检

在写配置前必须确认 **真实向量维度** = `embedding.dim`。

**HTTP / OpenAI 兼容服务示例：**

```bash
curl -sS http://127.0.0.1:8000/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"model":"/path/to/model","input":["hello"]}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('dim=',len(d['data'][0]['embedding']))"
```

或用项目内客户端（在 `hybrid_platform` 目录、`myenv` 已激活）：

```bash
myenv/bin/python - <<'PY'
from hybrid_platform.embedding import HttpEmbeddingClient
c = HttpEmbeddingClient(model="YOUR_MODEL", api_base="http://127.0.0.1:8000/v1", endpoint="/embeddings", timeout_s=60)
print("dim=", len(c.embed("hello")))
PY
```

| 情况 | 建议 |
|------|------|
| **多模型维度相同** | 可共用同一 `vector.lancedb.uri`，用不同 `embedding.version` 区分（仍建议每模型独立 URI，避免误删）。 |
| **维度不同** | **必须**每模型独立 `vector.lancedb.uri`（推荐），或独立 `--db`，或使用 `sqlite_only`（见 [config_reference.md](./config_reference.md)）。 |

若某目录下已有 Lance 表且维度与当前模型不一致：换一个 `lancedb.uri` 子目录，勿强行复用。

---

## 2. 目录约定（建议固化）

以仓库内 `hybrid_platform/var/spring-eval/` 为例（可按机器调整绝对路径）：

```text
hybrid_platform/var/spring-eval/
  index/
    spring-6ec2455e-ts-symbolctx.db # 共享 SQLite：ingest + chunk + FTS（BM25）
  models/
    qwen3-emb-8b/
      config.json                   # 仅此模型的 embedding + vector 段
      lancedb/                      # 仅此模型的 LanceDB（维度隔离）
      report.json                   # eval-retrieval-compare --output
    voyage-code-3/
      config.json
      lancedb/
      report.json
  aggregate/
    summary_YYYY-MM-DD.json         # aggregate 脚本写出
```

约定：`models/<name>/config.json` 中 **`embedding.version` 与目录名 `<name>` 一致**，便于追溯。

**推荐向量写入**：`vector.write_mode=lancedb_only`。高维模型若同时写 SQLite `embeddings` 表（JSON），会把 `.db` 撑得很大；BM25 只依赖 `chunks_fts`，不依赖向量是否在 SQLite。

---

## 3. 配置模板（每模型一份）

从 [`config/default_config.json`](../config/default_config.json)（相对于 `hybrid_platform/` 目录）复制 Java/chunk 等与模型无关的段落，仅改 `embedding` 与 `vector`。如果已经固定了一份共享 DB，`java_index` 和 `chunk` 段必须与首次建库时一致：

```json
{
  "embedding": {
    "version": "qwen3-emb-8b",
    "provider": "http",
    "model": "/data_nvme0/models/embedding/Qwen3-Embedding-8B",
    "dim": 4096,
    "api_base": "http://127.0.0.1:8000/v1",
    "endpoint": "/embeddings",
    "api_key": "",
    "timeout_s": 120,
    "batch_size": 8,
    "max_workers": 2
  },
  "vector": {
    "backend": "lancedb",
    "write_mode": "lancedb_only",
    "lancedb": {
      "uri": "/ABS/PATH/hybrid_platform/var/spring-eval/models/qwen3-emb-8b/lancedb",
      "table": "chunk_vectors",
      "metric": "cosine"
    }
  }
}
```

其余 `java_index`、`chunk` 等应与**首次建库**所用配置一致（否则不应共享同一 `.db`）。

当前默认 chunk 增强项：

```json
{
  "chunk": {
    "symbol_context_enabled": true,
    "symbol_cards_enabled": true,
    "symbol_context_max_tokens": 220
  }
}
```

这些值会改变 chunk 内容或 chunk 数量；调它们时应新建 DB，并让所有模型重新 `embed`。

**硬性规则**：**写入向量**与 **`eval-retrieval-compare`** 使用**同一份**该模型 `config.json`（评测会对 query 做 embedding）。

---

## 4. 流程 A（主推）：共享 DB + 每模型只 `embed` + 评测

### 4.1 首次：完整建库一次

任选「占位」embedding（或最终第一个模型）跑 `build-java-index`，得到共享 DB，例如 `spring-6ec2455e-ts-symbolctx.db`。详见 [spring_framework_eval_embedding_runbook.md](./spring_framework_eval_embedding_runbook.md) 步骤 3。

完成后：`chunks` 与 BM25 就绪；第一个模型若在此时已 embed，其向量已在对应 `embedding.version` + LanceDB URI 下。

如果目的是对比 2026-05-08 新索引逻辑与旧结果，不要覆盖旧 DB。建议使用新 DB 名，例如 `spring-6ec2455e-ts-symbolctx.db`，旧的 `spring-6ec2455e.db` 保留为 baseline。

### 4.2 每新增一个模型（4 步）

```bash
cd /path/to/codeindex_java/hybrid_platform
PY=myenv/bin/python
ROOT=/path/to/codeindex_java/hybrid_platform/var/spring-eval
NAME=qwen3-emb-8b
CFG=$ROOT/models/$NAME/config.json
DB=$ROOT/index/spring-6ec2455e-ts-symbolctx.db

mkdir -p "$ROOT/models/$NAME/lancedb"
# 编辑 $CFG：embedding.*、vector.lancedb.uri 指向上述 lancedb/

$PY -m hybrid_platform.cli --config "$CFG" embed --db "$DB"

$PY -m hybrid_platform.cli --config "$CFG" eval-retrieval-compare \
  --db "$DB" \
  --repo spring-projects/spring-framework \
  --commit 6ec2455e2491650fbeb7efaf78615a72700995ad \
  --dataset "/path/to/codeindex_java/JAVA test/spring_framework_eval_v1_reviewed.verify.jsonl" \
  --top-k 5 --top-k 10 \
  --output "$ROOT/models/$NAME/report.json"
```

报告 `summary` 中含 `embedding_version`；若 CLI 版本已支持，另含 `embedding_runtime`（provider / model / dim 等，不含 api_key）。数据集路径含空格时需引号。

或使用一键脚本（见下文 [`scripts/run_one_model_eval.sh`](../scripts/run_one_model_eval.sh)）。

---

## 5. 流程 B（备选）：每模型独立 `build-java-index`

当需要**完全隔离**（不同 chunk 策略、不同 ingest）时使用：每模型单独 `--db` + 单独配置 + 完整流水线 + `eval-retrieval-compare`。成本高，一般不建议仅因「换 embedding」而采用。

---

## 6. 汇总多份报告

```bash
cd /path/to/codeindex_java/hybrid_platform
myenv/bin/python scripts/aggregate_retrieval_compare_reports.py \
  var/spring-eval/models/*/report.json \
  --label qwen3=var/spring-eval/models/qwen3-emb-8b/report.json \
  --label voyage=var/spring-eval/models/voyage-code-3/report.json \
  --json-out var/spring-eval/aggregate/summary.json
```

不传 `--label` 时用报告文件名作为列名。详见 `scripts/aggregate_retrieval_compare_reports.py --help`。

---

## 7. 当前代码不包含的功能

- 无单条 CLI 自动遍历多个模型（可用 shell 循环或 `run_one_model_eval.sh`）。
- 无自动校验「Lance 维度 vs 配置 dim」；依赖第 1 节自检。

更多 CLI 说明：[README.md](../README.md)、[config_reference.md](./config_reference.md)。
