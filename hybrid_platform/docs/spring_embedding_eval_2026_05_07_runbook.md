# Spring Embedding 测评操作记录（2026-05-07）

本文记录 2026-05-07 对 Spring Framework 评测集进行多 embedding 模型对比的实际操作流程、产物路径与结果。通用说明见 [embedding_model_comparison.md](./embedding_model_comparison.md)；评测命令与数据集格式见 [retrieval_compare_eval.md](./retrieval_compare_eval.md)。

## 目标

固定同一份 Spring Framework 代码索引与同一批 chunks，只更换 embedding 模型，比较 dense retrieval 效果。

这样做可以避免因重新 chunk / 重新 ingest 引入差异，保证对比只反映 embedding 模型变化。

## 固定实验条件

- 仓库：`spring-projects/spring-framework`
- Spring commit：`6ec2455e2491650fbeb7efaf78615a72700995ad`
- Spring 源码路径：`/data1/qadong/workspace/spring-framework`
- 评测集：`/data1/qadong/codeindex_java/JAVA test/spring_framework_eval_v1_reviewed.verify.jsonl`
- 代码索引 DB：`/data1/qadong/codeindex_java/hybrid_platform/var/spring-eval/index/spring-6ec2455e.db`
- source backend：`tree-sitter-java`
- 评测命令：`eval-retrieval-compare`
- 评测 top-k：`5`、`10`
- 默认过滤：只评测 `repo_sha == --commit` 的样本

本次 JSONL 共加载 `200` 条，其中主 commit 样本 `80` 条参与评测，另 `120` 条因 commit mismatch 被跳过。

## 目录结构

```text
hybrid_platform/var/spring-eval/
  index/
    spring-6ec2455e.db
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
    summary_2026-05-07.md
    summary_2026-05-07.json
```

每个模型单独保存 `config.json`、LanceDB 向量目录与 `report.json`。共享 DB 只保存代码结构、chunks、BM25 所需 FTS 等。

## 为什么换模型不用重建代码索引

当前实验的索引分两层：

- 代码结构 / chunk 层：来自源码、符号、关系与 chunk 策略，和 embedding 模型无关。
- 向量层：来自 embedding 模型，对同一批 chunks 重新计算向量即可。

因此换 embedding 模型时，只需要：

```text
共享 spring-6ec2455e.db
  + 新模型 config.json
  + 新模型独立 lancedb/
  + embed
  + eval-retrieval-compare
```

只有更换 Spring commit、source backend、chunk 参数，或修复 ingest/chunk 逻辑时，才需要重新 `build-java-index`。

## 首次建库流程

首次为 Spring commit 建共享 DB，并用第一个模型写入向量：

```bash
cd /data1/qadong/codeindex_java/hybrid_platform

myenv/bin/python -m hybrid_platform.cli \
  --config /data1/qadong/codeindex_java/hybrid_platform/var/spring-eval/models/qwen3-emb-8b/config.json \
  build-java-index \
  --repo-root /data1/qadong/workspace/spring-framework \
  --repo spring-projects/spring-framework \
  --commit 6ec2455e2491650fbeb7efaf78615a72700995ad \
  --db /data1/qadong/codeindex_java/hybrid_platform/var/spring-eval/index/spring-6ec2455e.db \
  --source-backend tree-sitter-java
```

本次建库结果：

- documents：`9198`
- symbols：`218711`
- relations：`300813`
- chunks：`42853`
- attempted embedding chunks：`42830`
- embedded chunks：`42830`
- failed batches：`0`
- Qwen3 全量耗时约：`7165979 ms`
- Qwen3 embedding 吞吐约：`6.04 chunks/s`

## 新增模型流程

新增模型时使用脚本生成配置。脚本会自动调用 embedding 服务探测向量维度，并写入 `models/<name>/config.json`。

```bash
cd /data1/qadong/codeindex_java/hybrid_platform

scripts/create_embedding_model_config.sh \
  bge-code-v1 \
  /data_nvme0/models/embedding/bge-code-v1 \
  http://118.196.65.175:8000/v1
```

本次 `bge-code-v1` 探测到的维度：

```text
dim=1536
```

生成的配置路径：

```text
hybrid_platform/var/spring-eval/models/bge-code-v1/config.json
```

如果配置已存在但需要重写：

```bash
FORCE=1 scripts/create_embedding_model_config.sh \
  bge-code-v1 \
  /data_nvme0/models/embedding/bge-code-v1 \
  http://118.196.65.175:8000/v1
```

## 跑单个模型测评

使用 `run_one_model_eval.sh` 对已有共享 DB 追加该模型向量并评测：

```bash
cd /data1/qadong/codeindex_java/hybrid_platform

SPRING_EVAL_ROOT="$PWD/var/spring-eval" scripts/run_one_model_eval.sh \
  bge-code-v1 \
  "$PWD/var/spring-eval/index/spring-6ec2455e.db" \
  "/data1/qadong/codeindex_java/JAVA test/spring_framework_eval_v1_reviewed.verify.jsonl" \
  spring-projects/spring-framework \
  6ec2455e2491650fbeb7efaf78615a72700995ad \
  --top-k 5 --top-k 10
```

脚本会执行两步：

```text
embed --db spring-6ec2455e.db
eval-retrieval-compare --output models/<name>/report.json
```

## 汇总多个模型结果

```bash
cd /data1/qadong/codeindex_java/hybrid_platform

myenv/bin/python scripts/aggregate_retrieval_compare_reports.py \
  var/spring-eval/models/*/report.json \
  --json-out var/spring-eval/aggregate/summary_$(date +%F).json \
  > var/spring-eval/aggregate/summary_$(date +%F).md
```

当前汇总文件：

- `hybrid_platform/var/spring-eval/aggregate/summary_2026-05-07.md`
- `hybrid_platform/var/spring-eval/aggregate/summary_2026-05-07.json`

## 本次结果

BM25 是固定基线，因共享同一 DB 和评测集，在两个模型报告中保持一致。

```text
Model          Dense Recall@5   Dense MRR@5   Dense Recall@10   Dense MRR@10
bge-code-v1    45.00%           0.298333      58.75%            0.317103
qwen3-emb-8b   56.25%           0.475833      63.75%            0.485689
BM25 baseline  35.00%           0.264792      40.00%            0.271473
```

结论：

- `qwen3-emb-8b` 在本次 Spring 主 commit 的 `80` 条样本上优于 `bge-code-v1`。
- 差距最明显的是排序质量：`qwen3-emb-8b` 的 `MRR@5` / `MRR@10` 明显更高。
- 两个 dense 模型都优于 BM25 baseline。

## 常见问题

### 1. 为什么 `summary` 只评了 80 条？

评测集有多个 `repo_sha`。默认命令只评测与 `--commit` 一致的样本。本次 commit 是：

```text
6ec2455e2491650fbeb7efaf78615a72700995ad
```

因此 `evaluated_cases=80`，`skipped_commit_mismatch=120` 是预期行为。

### 2. 什么时候必须重新建共享 DB？

以下情况需要重新 `build-java-index`：

- Spring 源码 commit 变化。
- source backend 变化，例如从 `tree-sitter-java` 换成 `scip-java`。
- chunk 策略或 chunk 参数变化。
- 修复了 ingest、symbol、relation、chunk 相关逻辑。

仅更换 embedding 模型时，不需要重建共享 DB。

### 3. 为什么每个模型独立 LanceDB 目录？

LanceDB 的向量列维度在建表时固定。不同模型可能维度不同，例如：

- `qwen3-emb-8b`：`4096`
- `bge-code-v1`：`1536`

所以每个模型应使用自己的：

```text
models/<name>/lancedb/
```

避免维度冲突，也便于删除和重跑。

### 4. 以后换模型最短命令是什么？

```bash
cd /data1/qadong/codeindex_java/hybrid_platform

scripts/create_embedding_model_config.sh \
  NEW_NAME \
  /path/to/new/model \
  http://118.196.65.175:8000/v1

SPRING_EVAL_ROOT="$PWD/var/spring-eval" scripts/run_one_model_eval.sh \
  NEW_NAME \
  "$PWD/var/spring-eval/index/spring-6ec2455e.db" \
  "/data1/qadong/codeindex_java/JAVA test/spring_framework_eval_v1_reviewed.verify.jsonl" \
  spring-projects/spring-framework \
  6ec2455e2491650fbeb7efaf78615a72700995ad \
  --top-k 5 --top-k 10
```

然后重新汇总：

```bash
myenv/bin/python scripts/aggregate_retrieval_compare_reports.py \
  var/spring-eval/models/*/report.json \
  --json-out var/spring-eval/aggregate/summary_$(date +%F).json \
  > var/spring-eval/aggregate/summary_$(date +%F).md
```
