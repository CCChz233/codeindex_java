# Java 测评索引准备

本文面向 `JAVA test/*.jsonl` 一类 Java benchmark manifest，目标是把：

- `sample_id -> repo + base_sha`

转换成可执行的本地准备物：

- 源码工作树 `worktree`
- 索引库 `.db`
- 向量库 `.db.lancedb`
- 样本路由映射 `sample_id -> worktree/db/mcp`
- 批量构建与校验报告

## 1. 入口

统一入口脚本：

```bash
cd /data1/qadong/codeindex_java/hybrid_platform
./scripts/java_eval_index_prep.sh --help
```

底层 Python 模块：

```bash
./myenv/bin/python -m hybrid_platform.java_eval_prep --help
```

支持三个子命令：

- `derive`：从 manifest 提取唯一 `repo + base_sha` 目标，并输出 targets/routes
- `build`：复用现有 `repo_commit_to_index.sh` 批量 clone + 建索引
- `validate`：校验 DB 表计数、`find_entity` smoke、可选工作树存在性

## 2. 默认目录布局

为了区分“大文件”和“工具状态文件”，默认布局如下：

| 项 | 默认路径 |
|----|----------|
| worktrees 根目录 | `/data1/qadong/java_eval/worktrees`（若 `/data1/qadong` 不存在，则回退 `/data/qadong/java_eval/worktrees`，再回退仓库内 `hybrid_platform/var/java_eval/worktrees`） |
| targets / routes / reports | `hybrid_platform/var/java_eval/manifests/` |
| batch 构建日志 | `hybrid_platform/var/java_eval/logs/` |
| SQLite 索引输出 | `hybrid_platform/var/hybrid_indices/` |
| 多索引 metadata | `hybrid_platform/var/index_metadata.json`（可用 `--metadata-file` 覆盖） |
| 临时目录 | 优先 `/data1/qadong/tmp`，并自动注入 `TMPDIR`、`TMP`、`TEMP`、`SQLITE_TMPDIR`，避免根分区 `/tmp` 打满 |

目标粒度固定为：

- 一个 `repo + base_sha`
- 一份工作树
- 一份 `.db`
- 一个 `/mcp/<slug>`

不把同仓库不同提交混入同一个索引。

## 3. 推荐工作流

```mermaid
flowchart LR
manifest[ManifestJsonl] --> derive[DeriveTargetsAndRoutes]
derive --> pilot[PilotBuilds]
pilot --> validate[ValidateReadyIndexes]
validate --> batch[BatchBuildRemainingTargets]
batch --> routing[UseRoutesForEvalRunner]
```

### 3.1 先导出 targets / routes

```bash
./scripts/java_eval_index_prep.sh derive \
  --manifest "/data1/qadong/codeindex_java/JAVA test/test_java_agent_manifest_size_ge_100000.jsonl"
```

输出默认写到：

- `hybrid_platform/var/java_eval/manifests/test_java_agent_manifest_size_ge_100000.targets.json`
- `hybrid_platform/var/java_eval/manifests/test_java_agent_manifest_size_ge_100000.routes.json`

其中：

- `targets.json`：唯一 `repo + base_sha` 目标、路径、索引复用状态、构建参数
- `routes.json`：每条样本 `sample_id` 映射到对应 `worktree/db/mcp`

### 3.2 用离线 deterministic 配置做 pilot

为了先跑通链路，可使用：

- `config/java_eval_deterministic_config.json`

它保留当前项目的 Java 索引与 chunk 配置，但把 embedding provider 改为本地 `deterministic`，避免依赖线上 API。

示例：

```bash
./scripts/java_eval_index_prep.sh build \
  --manifest "/data1/qadong/codeindex_java/JAVA test/test_java_agent_manifest_size_ge_100000.jsonl" \
  --config ./config/java_eval_deterministic_config.json \
  --sample-id netty__netty-15575 \
  --sample-id jetty__jetty.project-13302
```

默认行为：

- 复用 `scripts/repo_commit_to_index.sh`
- 已经 `ready` 的索引默认跳过
- 每个目标一个日志文件：`hybrid_platform/var/java_eval/logs/<slug>.log`
- 构建报告写到：`hybrid_platform/var/java_eval/manifests/<manifest_stem>.build_report.json`
- 自动把可用本地 Maven `bin` 注入 `PATH`（用于无全局 `mvn`、但已有 Maven Wrapper 下载缓存的机器）

### 3.3 校验索引可用性

```bash
./scripts/java_eval_index_prep.sh validate \
  --manifest "/data1/qadong/codeindex_java/JAVA test/test_java_agent_manifest_size_ge_100000.jsonl" \
  --sample-id netty__netty-15575 \
  --require-worktree
```

校验项：

- `.db` 文件存在
- `documents/symbols/occurrences/chunks/embeddings` 非空
- 自动抽一个 `display_name` 跑一次 `find_entity` smoke
- 可选要求源码工作树存在（修复型 benchmark 推荐开启）

## 4. 选择 DB 还是 MCP

若测评框架直接使用本地路径：

- 读取 `routes.json`
- 按 `sample_id` 取 `worktree_path` 与 `db_path`

若测评框架通过 MCP：

- 先确保相应目标已写入 metadata 且状态为 `ready`
- 再使用 `mcp_path` 或启动后的 `/mcp/<slug>`

关键规则保持不变：

- 一次连接只绑定一个索引
- 外层 runner 按 `sample_id` 选择目标
- 不让模型在工具参数里选择仓库

## 5. 覆盖参数

如需为某些仓库沉淀额外参数，可给 `derive/build/validate` 传：

```bash
--overrides /path/to/overrides.json
```

格式：

```json
{
  "defaults": {
    "config_path": "/abs/path/to/config.json",
    "clone_shallow": true,
    "build_args": ["-DskipTests"],
    "build_env": {
      "MAVEN_OPTS": "-Xmx2g"
    }
  },
  "targets": {
    "org_repo_<sha>": {
      "build_tool": "gradle",
      "recurse_submodules": true,
      "build_env": {
        "MAVEN_OPTS": "-Drevapi.skip=true"
      },
      "pilot": true,
      "notes": "gradle pilot"
    }
  }
}
```

支持的 target key：

- `slug`
- `repo@base_sha`
- `repo|base_sha`

常见覆盖字段：

- `build_tool`
- `java_home`
- `recurse_submodules`
- `clone_shallow`
- `build_args`
- `build_env`
- `pilot`
- `notes`

## 6. 相关文件

- `scripts/java_eval_index_prep.sh`
- `hybrid_platform/java_eval_prep.py`
- `config/java_eval_deterministic_config.json`
- `scripts/repo_commit_to_index.sh`
- `scripts/index_build_repo_commit.sh`
- `var/index_metadata.json`
