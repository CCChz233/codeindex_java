# Server Agent Runbook

这份手册面向**已经登录到索引服务器上的 agent / 运维脚本**。目标不是解释架构，而是让服务器侧能够稳定完成：

1. 拉取代码
2. 准备 Python / JDK / `scip-java`
3. 按 `JAVA test/*.jsonl` manifest 拉源码
4. 编译并建立索引
5. 校验索引产物

本文默认服务器路径为：

- 仓库根目录：`/data1/qadong/codeindex_java`
- hybrid 根目录：`/data1/qadong/codeindex_java/hybrid_platform`

官方标准平台仍然是 **Linux/amd64**。

## 1. 代码同步

服务器上第一次拉代码：

```bash
mkdir -p /data1/qadong
cd /data1/qadong

git clone git@github.com:CCChz233/codeindex_java.git
cd /data1/qadong/codeindex_java

git checkout <branch>
```

后续更新：

```bash
cd /data1/qadong/codeindex_java
git fetch origin
git checkout <branch>
git pull --ff-only
```

如果服务器还没有 GitHub SSH 权限，先生成并注册公钥：

```bash
ssh-keygen -t ed25519 -C "root@$(hostname)"
cat ~/.ssh/id_ed25519.pub
ssh -T git@github.com
```

## 2. Python 环境

推荐使用单独 conda 环境，不要依赖 `base`。

```bash
conda create -n codeindex-java python=3.11 -y
conda activate codeindex-java

cd /data1/qadong/codeindex_java/hybrid_platform
export HYBRID_PYTHON="$(which python)"

python -m pip install -U pip setuptools wheel
python -m pip install -e .
python -m pip install -r requirements.txt
```

最小校验：

```bash
conda activate codeindex-java
cd /data1/qadong/codeindex_java/hybrid_platform
export HYBRID_PYTHON="$(which python)"

python -c "import lancedb, pyarrow, tree_sitter, tree_sitter_java, mcp, uvicorn; print('python deps ok')"
python -m hybrid_platform.cli --help
```

## 3. JDK

这个 manifest 集合当前只涉及 `11 / 17 / 21` 三种 JDK。服务器上建议都装好。

Debian / Ubuntu 示例：

```bash
apt-get update
apt-get install -y openjdk-11-jdk openjdk-17-jdk openjdk-21-jdk
```

建议固定三个环境变量：

```bash
export JAVA_HOME_11=/usr/lib/jvm/java-11-openjdk-amd64
export JAVA_HOME_17=/usr/lib/jvm/java-17-openjdk-amd64
export JAVA_HOME_21=/usr/lib/jvm/java-21-openjdk-amd64
```

校验：

```bash
ls /usr/lib/jvm
test -x "$JAVA_HOME_11/bin/java"
test -x "$JAVA_HOME_17/bin/java"
test -x "$JAVA_HOME_21/bin/java"
```

## 4. scip-java

### 4.1 安装 Coursier

```bash
curl -fLo /usr/local/bin/cs https://github.com/coursier/launchers/raw/master/cs-x86_64-pc-linux
chmod +x /usr/local/bin/cs
cs --help >/dev/null
```

### 4.2 安装 scip-java 二进制

把二进制统一放到 `/data1/qadong/bin`：

```bash
mkdir -p /data1/qadong/bin /data1/qadong/tmp

cs bootstrap --standalone \
  -o /data1/qadong/bin/scip-java-0.12.3 \
  com.sourcegraph:scip-java_2.13:0.12.3 \
  --main com.sourcegraph.scip_java.ScipJava

cs bootstrap --standalone \
  -o /data1/qadong/bin/scip-java-0.11.2 \
  com.sourcegraph:scip-java_2.13:0.11.2 \
  --main com.sourcegraph.scip_java.ScipJava

cs bootstrap --standalone \
  -o /data1/qadong/bin/scip-java-0.10.4 \
  com.sourcegraph:scip-java_2.13:0.10.4 \
  --main com.sourcegraph.scip_java.ScipJava

chmod +x /data1/qadong/bin/scip-java-0.12.3 /data1/qadong/bin/scip-java-0.11.2 /data1/qadong/bin/scip-java-0.10.4
```

最小校验：

```bash
/data1/qadong/bin/scip-java-0.12.3 --help >/dev/null
/data1/qadong/bin/scip-java-0.11.2 --help >/dev/null
/data1/qadong/bin/scip-java-0.10.4 --help >/dev/null
```

### 4.3 版本选择规则

- 默认优先：`0.12.3`
- Maven 老项目 / 兼容性保守场景：`0.11.2`
- Spring Framework 6.2.x / Kotlin 1.9.x：使用仓库内包装脚本 [run-scip-java-spring62.sh](../scripts/run-scip-java-spring62.sh)，它会指向 `/data1/qadong/bin/scip-java-0.10.4`

## 5. 配置文件

不要直接把服务器临时配置提交到 Git。建议所有服务器侧配置写到：

- `/data1/qadong/codeindex_java/hybrid_platform/var/`

### 5.1 通用 vLLM 配置

创建：

```bash
cat >/data1/qadong/codeindex_java/hybrid_platform/var/server_vllm_generic_config.json <<'JSON'
{
  "java_index": {
    "scip_java_cmd": "/data1/qadong/bin/scip-java-0.12.3"
  },
  "embedding": {
    "version": "v3_vllm_bge_m3",
    "provider": "http",
    "model": "bge-m3",
    "dim": 1024,
    "api_base": "http://118.196.65.175:8004/v1",
    "endpoint": "/embeddings",
    "timeout_s": 300,
    "batch_size": 32,
    "max_workers": 2
  }
}
JSON
```

### 5.2 Spring / Kotlin 兼容配置

如果某些目标必须走 `0.10.4` 包装脚本，创建：

```bash
cat >/data1/qadong/codeindex_java/hybrid_platform/var/server_vllm_spring62_config.json <<'JSON'
{
  "java_index": {
    "scip_java_cmd": "/data1/qadong/codeindex_java/hybrid_platform/scripts/run-scip-java-spring62.sh"
  },
  "embedding": {
    "version": "v3_vllm_bge_m3",
    "provider": "http",
    "model": "bge-m3",
    "dim": 1024,
    "api_base": "http://118.196.65.175:8004/v1",
    "endpoint": "/embeddings",
    "timeout_s": 300,
    "batch_size": 32,
    "max_workers": 2
  }
}
JSON
```

### 5.3 离线 deterministic 配置

如果只是先打通建索引链路，不想依赖远程 embedding：

```bash
cp /data1/qadong/codeindex_java/hybrid_platform/config/java_eval_deterministic_config.json \
  /data1/qadong/codeindex_java/hybrid_platform/var/server_deterministic_config.json
```

然后把 `java_index.scip_java_cmd` 改成 `/data1/qadong/bin/scip-java-0.12.3` 或兼容包装脚本。

## 6. Manifest 目标路由与 overrides

统一入口是：

- [java_eval_index_prep.sh](../scripts/java_eval_index_prep.sh)
- [java_eval_prep.py](../hybrid_platform/java_eval_prep.py)

manifest 路径示例：

- `/data1/qadong/codeindex_java/JAVA test/test_java_agent_manifest_size_ge_100000.jsonl`

### 6.1 先生成 JDK 路由 overrides

```bash
conda activate codeindex-java
cd /data1/qadong/codeindex_java/hybrid_platform
export HYBRID_PYTHON="$(which python)"

export MANIFEST="/data1/qadong/codeindex_java/JAVA test/test_java_agent_manifest_size_ge_100000.jsonl"
export OVERRIDES="/data1/qadong/codeindex_java/hybrid_platform/var/java_eval_overrides.json"

python - <<'PY'
import json, os
from pathlib import Path

manifest = Path(os.environ["MANIFEST"])
out = Path(os.environ["OVERRIDES"])
java_homes = {
    "11": os.environ["JAVA_HOME_11"],
    "17": os.environ["JAVA_HOME_17"],
    "21": os.environ["JAVA_HOME_21"],
}

targets = {}
with manifest.open(encoding="utf-8") as f:
    for line in f:
        if not line.strip():
            continue
        d = json.loads(line)
        ver = str(((d.get("environment") or {}).get("docker_specs") or {}).get("java_version", "")).strip()
        key = f'{d["repo"]}@{str(d["base_sha"]).lower()}'
        targets[key] = {
            "java_home": java_homes[ver]
        }

doc = {"defaults": {}, "targets": targets}
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(out)
PY
```

### 6.2 如有特殊仓库，再补充 override

`overrides.json` 支持这些字段：

- `config_path`
- `build_tool`
- `java_home`
- `recurse_submodules`
- `clone_shallow`
- `build_args`
- `build_env`

例如某个 Spring/Kotlin 仓库强制使用旧版 `scip-java`：

```json
{
  "targets": {
    "spring-projects/spring-framework@<sha>": {
      "config_path": "/data1/qadong/codeindex_java/hybrid_platform/var/server_vllm_spring62_config.json",
      "java_home": "/usr/lib/jvm/java-17-openjdk-amd64"
    }
  }
}
```

## 7. 建索引

### 7.1 导出 targets / routes

```bash
conda activate codeindex-java
cd /data1/qadong/codeindex_java/hybrid_platform
export HYBRID_PYTHON="$(which python)"

./scripts/java_eval_index_prep.sh derive \
  --manifest "/data1/qadong/codeindex_java/JAVA test/test_java_agent_manifest_size_ge_100000.jsonl" \
  --config "/data1/qadong/codeindex_java/hybrid_platform/var/server_vllm_generic_config.json" \
  --overrides "/data1/qadong/codeindex_java/hybrid_platform/var/java_eval_overrides.json"
```

### 7.2 先跑一个 sample

```bash
./scripts/java_eval_index_prep.sh build \
  --manifest "/data1/qadong/codeindex_java/JAVA test/test_java_agent_manifest_size_ge_100000.jsonl" \
  --config "/data1/qadong/codeindex_java/hybrid_platform/var/server_vllm_generic_config.json" \
  --overrides "/data1/qadong/codeindex_java/hybrid_platform/var/java_eval_overrides.json" \
  --sample-id "ls1intum__Artemis-11249" \
  --stop-on-error
```

### 7.3 单个跑通后再批量

```bash
./scripts/java_eval_index_prep.sh build \
  --manifest "/data1/qadong/codeindex_java/JAVA test/test_java_agent_manifest_size_ge_100000.jsonl" \
  --config "/data1/qadong/codeindex_java/hybrid_platform/var/server_vllm_generic_config.json" \
  --overrides "/data1/qadong/codeindex_java/hybrid_platform/var/java_eval_overrides.json" \
  --stop-on-error
```

### 7.4 只生成命令、不实际执行

```bash
./scripts/java_eval_index_prep.sh build \
  --manifest "/data1/qadong/codeindex_java/JAVA test/test_java_agent_manifest_size_ge_100000.jsonl" \
  --config "/data1/qadong/codeindex_java/hybrid_platform/var/server_vllm_generic_config.json" \
  --overrides "/data1/qadong/codeindex_java/hybrid_platform/var/java_eval_overrides.json" \
  --dry-run
```

## 8. 校验索引

```bash
./scripts/java_eval_index_prep.sh validate \
  --manifest "/data1/qadong/codeindex_java/JAVA test/test_java_agent_manifest_size_ge_100000.jsonl" \
  --config "/data1/qadong/codeindex_java/hybrid_platform/var/server_vllm_generic_config.json" \
  --overrides "/data1/qadong/codeindex_java/hybrid_platform/var/java_eval_overrides.json" \
  --require-worktree
```

校验会检查：

- `.db` 文件存在
- `documents / symbols / occurrences / chunks / embeddings` 非空
- `find_entity` smoke 可跑

## 9. 默认产物路径

- worktrees：`/data1/qadong/java_eval/worktrees`
- SQLite DB：`/data1/qadong/codeindex_java/hybrid_platform/var/hybrid_indices`
- 路由和报告：`/data1/qadong/codeindex_java/hybrid_platform/var/java_eval/manifests`
- 构建日志：`/data1/qadong/codeindex_java/hybrid_platform/var/java_eval/logs`
- metadata：`/data1/qadong/codeindex_java/hybrid_platform/var/index_metadata.json`

## 10. 给服务器 agent 的执行顺序

服务器 agent 不要自己发明流程，严格按这个顺序：

1. `git fetch && git checkout && git pull --ff-only`
2. `conda activate codeindex-java`
3. `export HYBRID_PYTHON="$(which python)"`
4. 校验 `python deps ok`
5. 校验 `JAVA_HOME_11 / 17 / 21`
6. 校验 `scip-java-0.12.3 / 0.11.2 / 0.10.4`
7. `derive`
8. 先 `build --sample-id <one-case> --stop-on-error`
9. 通过后再全量 `build`
10. `validate --require-worktree`

不要跳过第 8 步直接全量。先拿一个样本证明链路通，再放大。

## 11. 常见失败点

- `JAVA_HOME is invalid`
  - `overrides.json` 里的 `java_home` 路径错了。
- `scip-java 执行失败`
  - JDK 与仓库不匹配，或该仓库需要不同 `scip-java` 版本。
- `ingest/chunk/embed` 失败
  - 先看 `var/java_eval/logs/<slug>.log`
- 远程 embedding 超时
  - 先改用 deterministic 配置打通链路，再切回 vLLM。
- 某些 Spring/Kotlin 仓库在 `0.12.3` 下失败
  - 改 target override 的 `config_path`，切到 `server_vllm_spring62_config.json`

## 12. 相关文档

- [java_eval_index_prep.md](./java_eval_index_prep.md)
- [java_index_repo_setup.md](./java_index_repo_setup.md)
- [HANDOVER_JAVA_INDEX_AND_MCP.md](./HANDOVER_JAVA_INDEX_AND_MCP.md)
- [docker/full-stack/README.md](../docker/full-stack/README.md)
