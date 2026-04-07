# SCIP Hybrid Retrieval Platform

该目录实现了基于 `.scip` 的 SDD 落地版本，当前已开始向 Java / `scip-java` 主链路演进，覆盖：

- P0: schema 与 query DSL
- P1: `.scip` 流式解析与规范化
- P2: 结构化索引与关系图查询
- P3: chunk + embedding + 向量检索
- P4: 混合召回与重排服务
- P5: 评测与可观测性

## 目录结构

- `specs/`: 各阶段规格文档
- `src/hybrid_platform/`: 运行时代码
- `examples/`: 示例数据与评测样本

## 快速开始

1. 准备 Python 3.10+
2. 可选安装依赖（仅在你希望用 numpy 加速向量计算时）：

```bash
pip install numpy litellm
```

如果你要使用本地 embedding 模型（`embedding.provider=local`）：

```bash
pip install sentence-transformers
```

3. 运行示例：

```bash
python -m hybrid_platform.cli ingest \
  --repo demo/repo \
  --commit abc123 \
  --input examples/sample.scip.ndjson \
  --db examples/demo.db

python -m hybrid_platform.cli embed \
  --db examples/demo.db

python -m hybrid_platform.cli query \
  --db examples/demo.db \
  --query "parse command line options" \
  --mode hybrid \
  --include-code \
  --max-code-chars 1200
```

**实体查询（封装 `symbols` 表）**：用逻辑类型 + 名称查找 `symbol_id`，避免手写 SQL。例如查找名为 `AbstractByteBuf` 的类：

```bash
python -m hybrid_platform.cli find-entity \
  --db examples/netty.db \
  --type class \
  --name AbstractByteBuf \
  --match exact
```

支持 `--type`：`class`、`interface`、`enum`、`type`（类/接口/枚举）、`method`、`field`、`constructor`、`variable`、`type_parameter`、`any`。可选 `--package-contains` 缩小包路径。

代码中：`find_entity(store, type="class", name="AbstractByteBuf", match="exact")` 或 `SqliteStore.find_entities(...)`。

**实体评测**：数据集每条为 `entity_query`（与 `find_entity` 参数一致）+ `relevant_ids`（期望 `symbol_id`），命令：

```bash
python -m hybrid_platform.cli eval-entity \
  --db examples/netty.db \
  --dataset examples/netty_entity_eval_dataset.json \
  --top-k 10
```

与 `eval`（混合检索）不同：这里衡量的是 **find_entity 在结构化条件能否覆盖标注的符号**；若 ground truth 与查询条件一致，可出现 1.0（一致性检查）；若要测「模糊检索」仍用 `eval` + 自然语言/关键词 query。

默认 JSON 含 `summary` 与 `queries`（每条含 `entity_query`、`results` 排名列表、`is_relevant`）；只要汇总指标可加 `--no-per-query`。

**grep baseline 对比**：在**源码树**上用 `rg`（无则 `grep`）按固定规则搜 `class Foo` / `interface Foo` / 限定 glob 下的 `\\bmethod\\b`，与索引侧 `find_entity` 对比：

```bash
python -m hybrid_platform.cli eval-baseline-compare \
  --repo-root /path/to/netty \
  --dataset examples/netty_entity_eval_dataset.json \
  --db examples/netty.db \
  --top-k 10
```

输出含：**同一 `k`**（与 `--top-k` 一致）下 `find_entity_returned_count` / `grep_matched_total_count`、`find_entity_recall@k` / `grep_recall@k`（grep 为路径**字典序**前 `k` 个文件能否覆盖 GT 源文件）、以及不截断的 `grep_recall_files`。`gap` 中对比 **mean** 两边的 `recall@k`。`k` 较小时若 GT 符号/文件排序靠后，两侧 recall 会明显下降，便于和「全搜出来」区分。

Java 项目也可以直接通过 `scip-java` 建索引并自动 ingest：

```bash
python -m hybrid_platform.cli index-java \
  --repo-root /path/to/java-repo \
  --repo demo/java-repo \
  --commit abc123 \
  --db examples/java.db
```

如果 `scip-java index` 自动模式失败，可以显式走 SemanticDB 兜底：

```bash
python -m hybrid_platform.cli index-java \
  --repo-root /path/to/java-repo \
  --repo demo/java-repo \
  --commit abc123 \
  --db examples/java.db \
  --semanticdb-targetroot /path/to/semanticdb-targetroot
```

**enclosing_range 支持**：SCIP 的 `occurrence.enclosing_range` 用于更准确地推断 calls/field_refs。scip-java 自 PR #838（2026-02）起开始填充该字段。若需更高 calls 推断准确率，可从 [scip-java main](https://github.com/sourcegraph/scip-java) 构建并指定：

```bash
python -m hybrid_platform.cli index-java \
  --repo-root /path/to/java-repo \
  --repo demo/java-repo \
  --commit abc123 \
  --db examples/java.db \
  --scip-java-cmd /path/to/scip-java-src/scip-java/target/pack/bin/scip-java
```

4. 运行离线评测：

```bash
python -m hybrid_platform.cli eval \
  --db examples/demo.db \
  --dataset examples/eval_dataset.json

python eval/run_acceptance.py
```

## 统一配置文件

默认配置文件路径：`config/default_config.json`

完整参数字典见：`docs/config_reference.md`

Prompt 集中管理文件：`hybrid_platform/prompt.py`（函数级与模块级 intent 的 system/user prompt 都在这里改）。

你可以复制一份自定义配置（例如 `config/local_config.json`），然后所有命令通过 `--config` 统一读取：

```bash
python -m hybrid_platform.cli --config config/local_config.json query --db examples/demo.db --query "parse options"
```

配置优先级：`命令行参数 > config 文件 > 内置默认值`。

`intent` 段通过 LiteLLM 调用模型，核心字段：

- `intent_pipeline_version`: 意图生成流水线版本（用于缓存/追溯）
- `intent_prompt_version`: 提示词版本（用于缓存/追溯）
- `model`: 目标模型（如 `openai/gpt-4o-mini` 或兼容网关模型名）
- `api_base`: 可选，自建网关地址
- `api_key`: API Key
- `timeout_s` / `temperature` / `max_tokens`

`embedding` 段用于统一管理 embedding 模型配置（当前默认已切到 `llamaindex + voyage`）：

- `version`: 全局 embedding 版本（`chunk/embed/query/eval/serve` 共用）
- `provider`: `deterministic | local | voyage | http | llamaindex`
- `model`: embedding 模型标识（默认 `voyage-code-3`）
- `dim`: 向量维度（会影响 `chunk/embed/query/eval` 全链路）
- `api_base` / `api_key` / `timeout_s` / `endpoint`: 线上服务配置
- `batch_size` / `max_workers`: 批处理和并发配置（用于提升大规模 embedding 吞吐）
- `device`: 本地模型运行设备（`cpu`/`cuda`）
- `input_type`: voyage 输入类型（默认 `document`）

如果你要接自部署 embedding 服务，使用 `provider=http`，接口兼容以下任一返回格式：

- `{"embedding": [...]}`
- `{"data":[{"embedding":[...]}]}`
- `{"data":[{"vector":[...]}]}`

如果你要直连 Voyage，使用 `provider=voyage`，请求会走 `POST {api_base}/v1/embeddings`。

如果你要通过 LlamaIndex 走 Voyage，使用 `provider=llamaindex`，并把 `embedding.llama.class_path` 设为 `llama_index.embeddings.voyageai.VoyageEmbedding`。

`chunk` 段默认采用**函数定义优先 + token 预算切块**：

- `target_tokens`: chunk 目标 token 预算
- `overlap_tokens`: 相邻 chunk token 重叠预算

## `.scip` 输入支持

优先支持两类输入：

- 原生 `.scip`（若环境存在 `scip_pb2` 或 `scip.scip_pb2`）
- ndjson 导出格式（`*.scip.ndjson`），每行一个对象，字段与内部 schema 对齐

对于超大仓库，建议将全量 `.scip` 先切分为多个文件并并行执行 ingest。

## Java / `scip-java`

新增 `index-java` 命令用于打通 `Java repo -> scip-java -> ingest`：

- 自动识别 `Maven` / `Gradle`
- 支持显式指定 `--build-tool`
- 支持传递 `scip-java` 命令位置 `--scip-java-cmd`
- 支持 `--semanticdb-targetroot` 走手动兜底链路
- `index-java` 成功后会自动调用现有 ingest 管线

当前全局代码图正在从原来的函数中心模型演进到 Java 场景的结构图，目标节点层级为：

- `package`
- `class/interface`
- `method`

并逐步补齐以下核心关系：

- `belongs_to`
- `calls`
- `extends`
- `implements`
- `field_refs`

## HTTP 服务

```bash
python -m hybrid_platform.cli serve --db examples/demo.db --port 8080
```

查询示例：

```bash
curl -X POST http://127.0.0.1:8080/query \
  -H 'content-type: application/json' \
  -d '{"query":"parse options","mode":"hybrid","top_k":5,"include_code":true,"max_code_chars":1200}'
```

## 双层图流水线（CodeGraph + IntentGraph）

```bash
python -m hybrid_platform.cli build-code-graph --db examples/demo.db --repo demo/repo --commit abc123
python -m hybrid_platform.cli build-intent-fn --db examples/demo.db
python -m hybrid_platform.cli build-intent-module --db examples/demo.db --resolution 1.0
python -m hybrid_platform.cli build-intent-module --db examples/demo.db --resolutions 0.6,1.0,1.4
python -m hybrid_platform.cli apply-isolated-policy --db examples/demo.db
python -m hybrid_platform.cli repair-calls --db examples/demo.db --reclassify
python -m hybrid_platform.cli eval-graph --db examples/demo.db
```

可选安装真实 Leiden 依赖（未安装时会自动回退到 fallback 聚类）：

```bash
pip install igraph leidenalg
```

图查询示例：

```bash
python -m hybrid_platform.cli query-graph --db examples/demo.db --graph-mode code --seed-ids "fn:scip-cpp demo main()." --hops 2
python -m hybrid_platform.cli query-graph --db examples/demo.db --graph-mode explore --query "options parser"
```

`graph-mode explore` 现在会同时尝试：
- 模块级意图召回（`module_intents`）
- 函数级意图召回（`function_intents`）
- 语义召回（chunk embedding -> symbol 回映）

返回结果中会额外包含：
- `seed_nodes`
- `seed_communities`
- `explain`
