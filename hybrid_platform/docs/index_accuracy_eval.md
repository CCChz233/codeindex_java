# Index Accuracy Eval 使用与迁移文档

本文说明如何使用 `eval-index-accuracy` 模块评测已经生成好的 index DB。它适合你现在的场景：**index DB 只在服务器文件系统上，Mac 负责准备评测集和查看报告**。

## 1. 模块定位

`eval-index-accuracy` 只做评测，不做建索引，不修改 index DB。

它读取：

- 一个已经构建完成的 SQLite index DB
- 一个评测集 JSONL/JSON 文件
- 一个配置文件，用于加载 embedding/vector 查询运行时

它输出：

- 总体指标：`success@k`、`recall@k`、`mrr`、`ndcg@10`
- 按 case 类型拆分的指标：`entity`、`retrieval`、`graph`
- 每条 case 的 expected、retrieved、是否命中、失败原因
- 当前 index 的 `source_mode`、capabilities、repo、commit 信息

代码入口：

- 模块：`hybrid_platform/index_accuracy_eval.py`
- CLI：`python -m hybrid_platform.cli eval-index-accuracy`

典型命令：

```bash
python -m hybrid_platform.cli --config /path/to/config.json eval-index-accuracy \
  --db /path/to/index.db \
  --repo owner/repo \
  --commit abc123 \
  --dataset /path/to/eval.jsonl \
  --top-k 10 \
  --mode hybrid \
  --output /path/to/report.json
```

## 2. 推荐迁移方式：在服务器跑评测

当前 CLI 直接读取本地文件路径。SQLite index DB 在服务器文件系统上时，推荐流程是：

1. 在 Mac 上准备评测集 `eval.jsonl`
2. 上传到服务器
3. SSH 到服务器
4. 在服务器使用 index DB 的真实路径跑评测
5. 把 `report.json` 下载回 Mac 查看

示例：

```bash
# Mac: 上传评测集
scp ./eval.jsonl user@server:/data/codeindex/evals/eval.jsonl

# Mac: 登录服务器
ssh user@server
```

服务器上执行：

```bash
cd /data/codeindex_java/hybrid_platform

./myenv/bin/python -m hybrid_platform.cli --config /data/codeindex/config/eval_config.json eval-index-accuracy \
  --db /data/codeindex/indices/owner_repo_abc123.db \
  --repo owner/repo \
  --commit abc123 \
  --dataset /data/codeindex/evals/eval.jsonl \
  --top-k 10 \
  --mode hybrid \
  --output /data/codeindex/evals/report.json
```

Mac 下载报告：

```bash
scp user@server:/data/codeindex/evals/report.json ./report.json
```

不建议把远程 SQLite/LanceDB 目录通过 SSHFS/NFS 挂到 Mac 再跑。网络文件系统会让 SQLite 和 LanceDB 变慢，也更容易出现锁和一致性问题。

## 3. 服务器环境检查

在服务器上先确认 CLI 能运行：

```bash
cd /data/codeindex_java/hybrid_platform
./myenv/bin/python -m hybrid_platform.cli eval-index-accuracy --help
```

确认 DB 存在：

```bash
ls -lh /data/codeindex/indices/owner_repo_abc123.db
```

确认 `repo` 和 `commit` 与 index DB 一致。可以用健康查询或直接跑评测后看报告里的 `index_info`。如果 repo/commit 写错，很多 expected files/symbols 会解析不到或命中率为 0。

## 4. 配置文件

评测时配置文件主要影响 `retrieval` case。`entity` 和 `graph` case 大多只读 SQLite 表，不需要调用 embedding 服务。

### 4.1 使用 HTTP embedding URL

如果 query embedding 通过 URL 获取，配置可以写成：

```json
{
  "embedding": {
    "version": "v1",
    "provider": "http",
    "model": "your-embedding-model",
    "dim": 512,
    "api_base": "http://embedding-host:port",
    "endpoint": "/embeddings",
    "api_key": "",
    "timeout_s": 30,
    "batch_size": 256,
    "max_workers": 8
  },
  "vector": {
    "backend": "sqlite",
    "write_mode": "sqlite_only",
    "lancedb": {}
  },
  "query": {
    "mode": "hybrid",
    "top_k": 10,
    "blend_strategy": "linear",
    "include_code": false,
    "max_code_chars": 1200
  }
}
```

注意：

- `embedding.version` 必须和建索引时写入 chunks/embeddings 的版本一致，常见是 `v1`。
- `embedding.dim` 必须和已写入向量维度一致。
- 服务器必须能访问 `api_base + endpoint`。
- 如果 embedding 服务需要鉴权，把 token 写到 `api_key` 或按你们的 HTTP embedding 实现约定配置。

### 4.2 SQLite 向量 vs LanceDB 向量

如果建索引时向量写入 SQLite 的 `embeddings` 表：

```json
{
  "vector": {
    "backend": "sqlite",
    "write_mode": "sqlite_only",
    "lancedb": {}
  }
}
```

如果建索引时向量写入 LanceDB：

```json
{
  "vector": {
    "backend": "lancedb",
    "write_mode": "lancedb_only",
    "lancedb": {
      "uri": "/data/codeindex/indices/owner_repo_abc123.db.lancedb",
      "table": "chunk_vectors",
      "metric": "cosine"
    }
  }
}
```

关键点：

- 只给 SQLite DB 不一定够；如果向量在 LanceDB，必须保证 `vector.lancedb.uri` 指向服务器上的真实 LanceDB 目录。
- 如果建索引时是 dual write，评测时可以用 `backend=sqlite` 或 `backend=lancedb`，但要确保对应数据确实存在。
- `retrieval` 的 `semantic` 和 `hybrid` 会用 query embedding + 已有 chunk 向量；缺向量会导致语义召回为空或结果很差。

## 5. 评测集格式

推荐用 JSONL，一行一个 case。也支持普通 JSON 数组，或 `{"samples": [...]}`。

每条 case 必须有：

- `id`：case 名称，建议唯一
- `kind`：`entity`、`retrieval`、`graph`
- `expected`：期望命中的对象

`expected` 支持三种字段，优先级固定：

1. `chunks` 或 `chunk_ids`
2. `symbols` 或 `symbol_ids`
3. `files` 或 `paths`

如果同一个 expected 里同时写了 `chunks`、`symbols`、`files`，只会使用优先级最高的一类。建议每条 case 只写一种 expected。

也兼容现有 Spring reviewed flat JSONL 格式：如果一行没有 `kind`，但包含顶层 `sample_id`、`query`、`gold_files` 或 `gold_symbols`，模块会自动把它转成 `retrieval` case。转换规则：

- `id` 使用 `sample_id`
- `kind` 固定为 `retrieval`
- `query` 使用顶层 `query`
- 如果有 `gold_files`，`expected` 使用 `{"files": [...]}`，按 `|` 分隔
- 只有没有 `gold_files` 时，才使用 `gold_symbols`

因此这个文件可以直接作为 `--dataset` 使用：

```bash
'/Users/chz/workspace/codeindex_java/JAVA test/spring_framework_eval_v1_reviewed.verify.jsonl'
```

### 5.1 entity case

用于评估符号索引是否准确。底层调用 `find_entity`。

示例：

```json
{"id":"entity-abstract-bytebuf","kind":"entity","entity_query":{"type":"class","name":"AbstractByteBuf","match":"exact","package_contains":"io.netty.buffer"},"expected":{"symbols":["semanticdb maven . . io/netty/buffer/AbstractByteBuf#"]}}
```

常用字段：

- `entity_query.type`：`class`、`interface`、`enum`、`type`、`method`、`field`、`constructor`、`any`
- `entity_query.name`：要查的名称
- `entity_query.match`：`exact` 或 `contains`
- `entity_query.package_contains`：可选，用于缩小包名或 symbol_id 范围

推荐 expected：

- 最推荐：`symbols`
- 也可以：`files`

不推荐用 `chunks`，因为 entity 查询返回的是 symbol，不是 chunk。

### 5.2 retrieval case

用于评估自然语言/关键词 query 能否召回正确代码。底层调用 `HybridRetrievalService.query`。

示例：

```json
{"id":"retrieval-buffer-capacity","kind":"retrieval","query":"where is byte buffer capacity expanded","mode":"hybrid","expected":{"files":["buffer/src/main/java/io/netty/buffer/AbstractByteBuf.java"]}}
```

可选字段：

- `mode`：`hybrid`、`semantic`，也可用 `structure` 做符号文本查询
- `blend_strategy`：`linear` 或 `rrf`，仅 hybrid 时有意义

推荐 expected：

- 最严格：`chunks`
- 较稳定：`symbols`
- 最容易维护：`files`

如果你刚开始做评测集，建议先用 `files`。等失败 case 稳定后，再逐步把关键 case 改成 `symbols` 或 `chunks`。

### 5.3 graph case

用于评估结构化关系查询是否准确。底层走结构化 DSL。

示例：

```json
{"id":"graph-main-callees","kind":"graph","op":"callees_of","symbol_id":"scip-cpp demo main().","expected":{"symbols":["scip-cpp demo add()."]}}
```

支持的 `op`：

- `def_of`
- `refs_of`
- `callers_of`
- `callees_of`

也支持 CLI 风格别名：

- `def-of`
- `refs-of`
- `callers-of`
- `callees-of`

推荐 expected：

- `def_of`：推荐 `files` 或 `symbols`
- `refs_of`：推荐 `files`
- `callers_of` / `callees_of`：推荐 `symbols`

如果 index 是 `source_mode=syntax`，`callers_of` 和 `callees_of` 通常不可用，报告会标记为 `unsupported_capability`，不会当成程序崩溃。

## 6. 一个完整 eval.jsonl 示例

```jsonl
{"id":"entity-add","kind":"entity","entity_query":{"type":"any","name":"add","match":"exact"},"expected":{"symbols":["scip-cpp demo add()."]}}
{"id":"retrieval-parse-options","kind":"retrieval","query":"parse_options","mode":"hybrid","expected":{"symbols":["scip-cpp demo parse_options()."]}}
{"id":"graph-main-callees","kind":"graph","op":"callees_of","symbol_id":"scip-cpp demo main().","expected":{"symbols":["scip-cpp demo add()."]}}
{"id":"graph-main-def","kind":"graph","op":"def_of","symbol_id":"scip-cpp demo main().","expected":{"files":["src/main.cc"]}}
```

## 7. 输出报告解读

报告顶层结构：

```json
{
  "summary": {},
  "by_kind": {},
  "cases": [],
  "index_info": {},
  "repo": "owner/repo",
  "commit": "abc123",
  "dataset": "/abs/path/eval.jsonl"
}
```

### 7.1 summary

常见字段：

- `samples`：总 case 数
- `kind_counts`：三类 case 的数量
- `top_k`：本次评测 top-k
- `mode`：默认 retrieval mode
- `success@k`：top-k 内是否至少命中一个 expected，按 case 求平均
- `recall@k`：top-k 内覆盖了多少 expected units，按 case 求平均
- `mrr`：第一个相关结果的倒数排名，按 case 求平均
- `ndcg@10`：前 10 个结果的排序质量
- `unsupported_count`：因为当前 index 能力不足而无法执行的 case 数
- `empty_expected_count`：expected 为空或无法解析的 case 数

`success@k` 更适合做“能不能找得到”的验收；`recall@k` 更适合看多个 expected 是否都被覆盖。

### 7.2 by_kind

按 `entity`、`retrieval`、`graph` 单独统计。建议先看这个字段定位问题：

- `entity` 低：符号表覆盖或 find_entity 查询条件有问题
- `retrieval` 低：chunk、embedding、向量后端或 query 语义有问题
- `graph` 低：relations/code graph/source_mode 能力有问题

### 7.3 cases

每条 case 会包含：

- `id`
- `kind`
- `expected`
- `expected_unit_kind`
- `expected_units`
- `retrieved`
- `metrics`
- `is_relevant`
- `failure_reason`

`retrieved` 中每个结果包含：

- `rank`
- `id`
- `type`
- `score`
- `payload`
- `is_relevant`
- `matched_expected_units`

这部分用于人工排查失败 case。

### 7.4 failure_reason

常见值：

- 空字符串：case 命中
- `no_results`：查询没有返回结果
- `no_relevant_hit`：有结果，但没有命中 expected
- `empty_expected`：expected 没写或解析后为空
- `unsupported_capability`：当前 index 的 source_mode 不支持该能力
- `case_error`：case 格式错误或运行时异常

## 8. source_mode 对评测的影响

报告里的 `index_info.source_mode` 很重要。

### 8.1 source_mode=scip

能力最完整，通常支持：

- `find_entity`
- `def`
- `ref`
- `call`
- `hierarchy`
- `keyword`
- `hybrid`
- `semantic`

适合完整评测 `entity/retrieval/graph`。

### 8.2 source_mode=syntax

通常来自 tree-sitter fallback。能力通常支持：

- `find_entity`
- `def`
- `hierarchy`
- `keyword`
- `hybrid`
- `semantic`

通常不支持：

- `refs_of`
- `callers_of`
- `callees_of`

因此 graph case 里如果测 calls，会看到 `unsupported_capability`。这不是评测模块坏了，而是该 index 本身没有 call capability。

### 8.3 source_mode=document

只有文档/chunk 层面的检索能力，通常不支持符号和图：

- 可以测 retrieval
- 不适合测 entity
- 不适合测 graph

## 9. 推荐工作流

### 9.1 第一次迁移验证

先准备 5 到 10 条小评测集，覆盖：

- 2 条 entity：类名/方法名
- 2 条 retrieval：自然语言描述 + gold file
- 1 条 graph：`def_of`
- 如果是 scip index，再加 1 条 `callees_of`

跑通命令：

```bash
./myenv/bin/python -m hybrid_platform.cli --config /data/codeindex/config/eval_config.json eval-index-accuracy \
  --db /data/codeindex/indices/owner_repo_abc123.db \
  --repo owner/repo \
  --commit abc123 \
  --dataset /data/codeindex/evals/smoke_eval.jsonl \
  --top-k 10 \
  --mode hybrid \
  --output /data/codeindex/evals/smoke_report.json
```

确认：

- `summary.samples` 等于评测集条数
- `index_info.source_mode` 符合预期
- `unsupported_count` 符合预期
- 每条 case 都有 `retrieved`

### 9.2 扩展正式评测集

建议分层维护：

- `smoke_eval.jsonl`：少量核心 case，迁移/部署后快速跑
- `regression_eval.jsonl`：稳定回归集，几十到几百条
- `debug_eval.jsonl`：临时排查失败用，不作为长期门禁

### 9.3 对比不同 index 或不同 embedding

同一份 dataset 可以跑多次：

```bash
# index A
./myenv/bin/python -m hybrid_platform.cli --config cfg_a.json eval-index-accuracy \
  --db index_a.db --repo owner/repo --commit abc123 \
  --dataset eval.jsonl --output report_a.json

# index B
./myenv/bin/python -m hybrid_platform.cli --config cfg_b.json eval-index-accuracy \
  --db index_b.db --repo owner/repo --commit abc123 \
  --dataset eval.jsonl --output report_b.json
```

然后对比：

- `summary`
- `by_kind`
- 失败 case 的 `failure_reason`
- 同一 case 的 `retrieved` 排名变化

## 10. 常见问题

### 10.1 `retrieval` 全部没有结果

优先检查：

- DB 里是否已经执行过 chunk
- DB 里是否已经执行过 embed
- `embedding.version` 是否和建索引时一致
- `vector.backend` 是否指向实际存在的向量存储
- 服务器能否访问 embedding URL

### 10.2 `entity` 全部失败

优先检查：

- `index_info.source_mode` 是否是 `document`
- `symbols` 表是否为空
- `entity_query.type/name/match/package_contains` 是否过窄
- expected 里的 symbol_id 是否来自同一个 index

### 10.3 `graph` 的 callers/callees 是 `unsupported_capability`

这通常是 index 由 tree-sitter fallback 构建，`source_mode=syntax`。syntax 模式一般没有 call capability。处理方式：

- 如果你要测 tree-sitter fallback，就不要把 callers/callees 作为硬失败
- 如果你要测完整调用关系，需要用 `source_mode=scip` 的 index

### 10.4 `expected.files` 明明对，但没有命中

检查路径写法：

- 推荐写仓库相对路径，例如 `src/main/java/com/acme/Foo.java`
- 不要写服务器绝对路径
- 模块会做后缀匹配，但最好保持和 `documents.relative_path` 一致

### 10.5 `expected.symbols` 不确定怎么拿

可以先用 CLI 查：

```bash
./myenv/bin/python -m hybrid_platform.cli --config /data/codeindex/config/eval_config.json find-entity \
  --db /data/codeindex/indices/owner_repo_abc123.db \
  --type class \
  --name Foo \
  --match exact
```

把返回的 `symbol_id` 放到评测集的 `expected.symbols`。

### 10.6 只想先测结构，不想依赖 embedding URL

评测集只写 `entity` 和 `graph` case 即可。不要写 `retrieval` case，或者 retrieval 只用 `mode=structure` 做符号文本查询。

## 11. 最小可执行清单

服务器上需要准备：

- `codeindex_java/hybrid_platform` 代码
- 可运行的 Python 环境，推荐 `./myenv/bin/python`
- 已构建好的 SQLite index DB
- 如果使用 LanceDB，准备好 `.lancedb` 目录并写入配置
- 可访问的 embedding URL，前提是评测集中有 semantic/hybrid retrieval case
- 评测集 JSONL
- eval 配置 JSON

最终命令模板：

```bash
cd /data/codeindex_java/hybrid_platform

./myenv/bin/python -m hybrid_platform.cli --config /data/codeindex/config/eval_config.json eval-index-accuracy \
  --db /data/codeindex/indices/<slug>.db \
  --repo '<owner/repo>' \
  --commit '<commit_sha>' \
  --dataset /data/codeindex/evals/eval.jsonl \
  --top-k 10 \
  --mode hybrid \
  --output /data/codeindex/evals/report.json
```

评测完成后查看：

```bash
python -m json.tool /data/codeindex/evals/report.json | less
```
