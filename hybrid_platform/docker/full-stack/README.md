# Full-Stack Containers

这套目录定义了本仓库的**标准容器路径**：统一使用 **Linux/amd64** 镜像覆盖索引与在线服务。  
Mac 仅作为 Docker 宿主机，通过 `docker buildx` / `docker run --platform=linux/amd64` 复用同一套镜像；**不单独承诺 arm64 原生一致性**。

## 镜像职责

- `indexer` target：clone / `scip-java` / ingest / build-code-graph / chunk / embed / metadata
- `runtime` target：`serve` REST 或 `mcp-streamable`

远程 embedding 仍作为外部依赖，继续通过外部挂载的 JSON 配置文件传入。

## 构建镜像

在仓库根目录执行：

```bash
cd /Users/chz/workspace/codeindex_java/hybrid_platform

docker buildx build \
  --platform linux/amd64 \
  --target indexer \
  --build-arg PYTHON_VERSION=3.11 \
  --build-arg JDK_VERSION=21 \
  --build-arg SCIP_JAVA_VERSION=0.12.3 \
  -f docker/full-stack/Dockerfile \
  -t hybrid-full-stack-indexer:local \
  --load \
  .

docker buildx build \
  --platform linux/amd64 \
  --target runtime \
  --build-arg PYTHON_VERSION=3.11 \
  -f docker/full-stack/Dockerfile \
  -t hybrid-full-stack-runtime:local \
  --load \
  .
```

说明：

- 官方标准平台固定为 `linux/amd64`；不要省略 `--platform linux/amd64`
- 需要不同 JDK 时，调整 `JDK_VERSION=17|21|23`
- 某些 Kotlin 1.9 / Spring 6.2 项目若必须用旧版 `scip-java`，可覆盖 `SCIP_JAVA_VERSION`

## 标准卷布局

- `/data/hybrid_indices/<slug>.db`：SQLite 索引
- `/data/hybrid_indices/<slug>.db.lancedb`：LanceDB 向量目录
- `/data/index_metadata.json`：metadata 注册表
- `/workspace/repos/<slug>`：标准 clone 工作树

## 索引容器

### 标准复现模式：容器内 clone

```bash
docker run --rm --platform linux/amd64 \
  -v /abs/path/to/data:/data \
  -v /abs/path/to/workspace:/workspace \
  -v /abs/path/to/config.json:/config/config.json:ro \
  hybrid-full-stack-indexer:local \
  --repo-name owner/repo \
  --commit deadbeefdeadbeefdeadbeefdeadbeefdeadbeef \
  --git-url https://github.com/owner/repo.git \
  --config /config/config.json
```

附加的 Maven / Gradle / `scip-java` 参数写在单独的 `--` 之后：

```bash
docker run --rm --platform linux/amd64 \
  -v /abs/path/to/data:/data \
  -v /abs/path/to/workspace:/workspace \
  -v /abs/path/to/config.json:/config/config.json:ro \
  hybrid-full-stack-indexer:local \
  --repo-name owner/repo \
  --commit deadbeefdeadbeefdeadbeefdeadbeefdeadbeef \
  --git-url https://github.com/owner/repo.git \
  --config /config/config.json \
  -- \
  -DskipTests
```

### 开发模式：挂载已有源码树

```bash
docker run --rm --platform linux/amd64 \
  -v /abs/path/to/data:/data \
  -v /abs/path/to/local-repo:/mounted/repo \
  -v /abs/path/to/config.json:/config/config.json:ro \
  hybrid-full-stack-indexer:local \
  --repo-name owner/repo \
  --commit deadbeefdeadbeefdeadbeefdeadbeefdeadbeef \
  --repo-root /mounted/repo \
  --config /config/config.json
```

这是开发/调试路径，不是官方标准复现模式。

### 已有 `.scip`

```bash
docker run --rm --platform linux/amd64 \
  -v /abs/path/to/data:/data \
  -v /abs/path/to/workspace:/workspace \
  -v /abs/path/to/repo:/mounted/repo \
  -v /abs/path/to/index.scip:/mounted/index.scip:ro \
  -v /abs/path/to/config.json:/config/config.json:ro \
  hybrid-full-stack-indexer:local \
  --repo-name owner/repo \
  --commit deadbeefdeadbeefdeadbeefdeadbeefdeadbeef \
  --repo-root /mounted/repo \
  --prebuilt-scip /mounted/index.scip \
  --config /config/config.json
```

## 运行容器

每个 `runtime` 容器只服务一个 `.db`。REST 和 MCP 默认拆成两个容器，即便它们指向同一个库。

### REST

```bash
docker run --rm --platform linux/amd64 \
  -p 9301:9301 \
  -v /abs/path/to/data:/data \
  -v /abs/path/to/config.json:/config/config.json:ro \
  -e HYBRID_DB=/data/hybrid_indices/<slug>.db \
  -e HYBRID_CONFIG=/config/config.json \
  -e HYBRID_ADMIN_TOKEN=change-me \
  --entrypoint /opt/hybrid_platform/docker/full-stack/entrypoints/run_rest.sh \
  hybrid-full-stack-runtime:local \
  --host 0.0.0.0 \
  --port 9301
```

验证：

```bash
curl http://127.0.0.1:9301/health
```

### MCP

```bash
docker run --rm --platform linux/amd64 \
  -p 8765:8765 \
  -v /abs/path/to/data:/data \
  -v /abs/path/to/config.json:/config/config.json:ro \
  -e HYBRID_DB=/data/hybrid_indices/<slug>.db \
  -e HYBRID_CONFIG=/config/config.json \
  -e HYBRID_MCP_HOST=0.0.0.0 \
  -e HYBRID_MCP_PORT=8765 \
  -e HYBRID_MCP_PATH=/mcp \
  -e HYBRID_MCP_BEARER_TOKEN=change-me \
  --entrypoint /opt/hybrid_platform/docker/full-stack/entrypoints/run_mcp.sh \
  hybrid-full-stack-runtime:local
```

多库访问不在容器内聚合。请使用外部 Nginx/Caddy/Ingress 进行路径或子域分发。

## docker-compose

示例文件见 [docker-compose.yml](./docker-compose.yml)。

常见用法：

```bash
cd /Users/chz/workspace/codeindex_java/hybrid_platform/docker/full-stack

HYBRID_CONFIG_FILE=/abs/path/to/config.json \
INDEX_REPO_NAME=owner/repo \
INDEX_COMMIT=deadbeefdeadbeefdeadbeefdeadbeefdeadbeef \
INDEX_GIT_URL=https://github.com/owner/repo.git \
docker compose --profile indexer run --rm indexer
```

启动 REST：

```bash
cd /Users/chz/workspace/codeindex_java/hybrid_platform/docker/full-stack

HYBRID_CONFIG_FILE=/abs/path/to/config.json \
HYBRID_DB=/data/hybrid_indices/<slug>.db \
docker compose --profile rest up --build rest
```

启动 MCP：

```bash
cd /Users/chz/workspace/codeindex_java/hybrid_platform/docker/full-stack

HYBRID_CONFIG_FILE=/abs/path/to/config.json \
HYBRID_DB=/data/hybrid_indices/<slug>.db \
HYBRID_MCP_BEARER_TOKEN=change-me \
docker compose --profile mcp up --build mcp
```

## 远程 embedding

镜像不覆盖应用层 embedding 配置。继续在外部配置文件里传：

```json
{
  "embedding": {
    "provider": "http",
    "api_base": "http://your-vllm-host:8004/v1",
    "endpoint": "/embeddings",
    "model": "bge-m3",
    "dim": 1024
  }
}
```

如果在线服务容器需要访问远程 embedding，请确保容器网络可达该地址。
