# Spring Framework 评测集：切换 Embedding 建索引与跑评测

本文是「使用新的 embedding 模型，对仓库根目录下 `JAVA test/spring_framework_eval_v1_reviewed.verify.jsonl` 建索引并测试」的操作说明。Dense 检索口径、评测集字段与输出字段的详细说明见 [retrieval_compare_eval.md](./retrieval_compare_eval.md)。

## 适用场景

- 你已有一份 Spring Framework 源码树，且 commit 与评测集中的 `repo_sha` 一致（见下文「主 commit」）。
- 你要更换 `embedding.provider` / `model` / `dim` 等：若 **chunks 已固定**，只需对该模型再跑 **`embed` + `eval-retrieval-compare`**（见 [embedding_model_comparison.md](./embedding_model_comparison.md) 主推流程）；从零开始或需隔离 chunks 时再 **`build-java-index`**。

## 环境

在 `codeindex_java` 仓库中执行 Python CLI 时，请使用项目内虚拟环境（见仓库根目录 `.cursor/rules` 或 README 说明），例如：

```bash
cd /data1/qadong/codeindex_java/hybrid_platform
/data1/qadong/codeindex_java/hybrid_platform/myenv/bin/python -m hybrid_platform.cli --help
```

若尚未创建 `myenv`，在 `hybrid_platform` 目录下执行：

```bash
python3 -m venv myenv
myenv/bin/python -m pip install -r requirements.txt
```

## 主 commit 与评测集路径

- 评测集文件（仓库内相对路径）：`JAVA test/spring_framework_eval_v1_reviewed.verify.jsonl`
- 该 JSONL 中主批次使用的 **`repo_sha`**：`6ec2455e2491650fbeb7efaf78615a72700995ad`（约 80 条；`eval-retrieval-compare` 默认只评与此 `--commit` 一致的样本）。

路径中含空格，Shell 里 **`--dataset` 必须加引号**。

## 步骤 1：检出 Spring Framework 源码

```bash
mkdir -p /path/to/workspace
cd /path/to/workspace

git clone https://github.com/spring-projects/spring-framework.git
cd spring-framework
git fetch --all --tags
git checkout 6ec2455e2491650fbeb7efaf78615a72700995ad
git rev-parse HEAD
```

将下文中的 `--repo-root` 替换为上述 `spring-framework` 目录的**绝对路径**。

## 步骤 2：准备专用配置文件

复制 `hybrid_platform/config/default_config.json` 为一份新文件（例如 `hybrid_platform/config/spring_eval_my_embedding.json`），避免改动仓库默认配置。

**切换 embedding 时务必修改：**

| 配置项 | 说明 |
|--------|------|
| `embedding.version` | 换新模型或维度后立即改掉（如 `v2-my-emb`），与向量写入版本一致 |
| `embedding.provider` | `deterministic` \| `local` \| `voyage` \| `http` \| `llamaindex`（见 `docs/config_reference.md`） |
| `embedding.model` | 服务端识别的模型名或 HF 模型名 |
| `embedding.dim` | **必须与模型实际输出维度一致** |
| `embedding.api_base` / `api_key` / `endpoint` | 远端或网关必填项按 provider 填写 |
| `vector.lancedb.uri` | 若使用 LanceDB，建议为新模型指定**新的 URI 目录**，避免与旧向量表混淆；多模型对比推荐 `write_mode=lancedb_only`（见对比文档） |

配置优先级：`命令行参数 > 配置文件 > 内置默认值`。评测阶段使用的配置文件应与**写入该模型向量**时**保持一致**（至少 `embedding.*` 与 `vector.*` 一致）。

## 步骤 3：建索引（首次）

**仅首次**或为全新快照时需要完整建库。`build-java-index` 会执行：`source backend → ingest → build-code-graph → chunk → embed`。完成后得到共享 `--db`；**此后若仅更换 embedding 模型**，不要在同一 chunks 上重复跑全流程（除非你刻意重建 chunks），应跳到步骤 4b（仅 `embed`）和多模型对比文档。

无编译索引示例（`tree-sitter-java`，与当前默认配置一致）：

```bash
cd /data1/qadong/codeindex_java/hybrid_platform

/data1/qadong/codeindex_java/hybrid_platform/myenv/bin/python -m hybrid_platform.cli \
  --config /data1/qadong/codeindex_java/hybrid_platform/config/spring_eval_my_embedding.json \
  build-java-index \
  --repo-root /path/to/workspace/spring-framework \
  --repo spring-projects/spring-framework \
  --commit 6ec2455e2491650fbeb7efaf78615a72700995ad \
  --db /data1/qadong/codeindex_java/hybrid_platform/examples/spring-6ec2455e-myemb.db \
  --source-backend tree-sitter-java
```

如需编译型 SCIP 后端，将 `--source-backend` 改为 `scip-java`，并在配置中设置可用的 `java_index.scip_java_cmd`（以及按需的 `build_tool` 等）。

**建议：** 首次建库可为占位 embedding；后续每个模型用独立 `config` + `vector.lancedb.uri`，在同一 `--db` 上追加向量即可（详见 [embedding_model_comparison.md](./embedding_model_comparison.md)）。

### 步骤 4b：已有 chunks，仅换模型（追加向量）

```bash
cd /data1/qadong/codeindex_java/hybrid_platform

/data1/qadong/codeindex_java/hybrid_platform/myenv/bin/python -m hybrid_platform.cli \
  --config /path/to/model_specific.json embed \
  --db /path/to/shared.db
```

再使用该 **`model_specific.json`** 运行下文中的 `eval-retrieval-compare`。也可用一键脚本：`scripts/run_one_model_eval.sh`（见 [embedding_model_comparison.md](./embedding_model_comparison.md)）。

## 步骤 4：跑 Dense vs BM25 对比评测

```bash
cd /data1/qadong/codeindex_java/hybrid_platform

/data1/qadong/codeindex_java/hybrid_platform/myenv/bin/python -m hybrid_platform.cli \
  --config /data1/qadong/codeindex_java/hybrid_platform/config/spring_eval_my_embedding.json \
  eval-retrieval-compare \
  --db /data1/qadong/codeindex_java/hybrid_platform/examples/spring-6ec2455e-myemb.db \
  --repo spring-projects/spring-framework \
  --commit 6ec2455e2491650fbeb7efaf78615a72700995ad \
  --dataset "/data1/qadong/codeindex_java/JAVA test/spring_framework_eval_v1_reviewed.verify.jsonl" \
  --top-k 5 \
  --top-k 10 \
  --output /tmp/spring_bm25_dense_report.json
```

查看汇总表（示例）：

```bash
/data1/qadong/codeindex_java/hybrid_platform/myenv/bin/python - <<'PY'
import json
r = json.load(open("/tmp/spring_bm25_dense_report.json"))
print(r["table_markdown"])
print("evaluated_cases:", r["summary"]["evaluated_cases"])
print("skipped_commit_mismatch:", r["summary"].get("skipped_commit_mismatch"))
print("embedding_runtime:", r["summary"].get("embedding_runtime"))
PY
```

多 embedding 模型横向对比与目录约定见 [embedding_model_comparison.md](./embedding_model_comparison.md)。

## 行为说明与注意事项

1. **Commit 过滤**：默认只评测 `repo_sha == --commit` 的样本；其余进入 `skipped_cases`。临时包含不匹配样本可加 `--include-commit-mismatches`，但不宜作为跨 commit 的正式指标（见 [retrieval_compare_eval.md](./retrieval_compare_eval.md) 第 6.6 节）。
2. **Dense 依赖在线 embedding**：报告里的 dense 分支需要对查询做 embedding；需保证网络与 API Key 可用，且配置与建库时一致。
3. **`embedding.version` 与向量存储**：版本用于区分写入的向量版本；换模型后版本与 DB/LanceDB 应对齐，否则可能出现检索不到或维度错误。
4. **混合检索 `eval`**：若你需要 hybrid 模式整条链路评测，数据集格式可能与本文 JSONL 不同；请以 `docs/` 下对应评测文档为准。

## 相关文档

- [retrieval_compare_eval.md](./retrieval_compare_eval.md)：`eval-retrieval-compare`、评测集格式、Spring 全流程示例
- [config_reference.md](./config_reference.md)：配置项逐项说明
- [embedding_model_comparison.md](./embedding_model_comparison.md)：多 embedding 模型对比（维度、LanceDB、汇总脚本）
- [spring_embedding_eval_2026_05_07_runbook.md](./spring_embedding_eval_2026_05_07_runbook.md)：2026-05-07 实际操作记录与结果
