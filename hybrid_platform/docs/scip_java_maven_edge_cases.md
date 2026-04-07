# scip-java + Maven：常见边界情况与处理

`scip-java index` 会通过 **fork 的 javac**（通常带 **`--release`**）跑 Maven 编译。部分老 POM 仍使用 **JDK 8 时代的 `-endorseddirs`**，与 **javac 的 `--release` 互斥**，报错形如：

```text
error: option -endorseddirs cannot be used together with --release
```

## 1. `-endorseddirs` 与 `--release`（Apache Hadoop 等）

**原因**：`maven-compiler-plugin` 的 `compilerArguments` / `compilerArgs` 里配置了 `<endorseddirs>…</endorseddirs>`；endorsed 机制在 **JDK 9+ 已弱化，JDK 11+ 与模块化搭配时不再适用**；与 `--release` 同用时 **javac 直接拒绝**。

**推荐修复（源码侧，可自动化）**：

1. 删除或条件化 `endorseddirs`（例如仅 `jdk8` profile 启用）。
2. 将原 `maven-dependency-plugin` 拷贝到 `target/endorsed` 的 **同一份 JAR**（常见为 `javax:javaee-endorsed-api`）改为 **普通 `<dependency>`**，让其在 **编译 classpath** 上即可，无需 endorsed 目录。
3. 删除仅服务于 endorsed 目录的 `dependency-plugin` 拷贝任务（若不再需要）。

**已在环境中验证的模块**：Apache Hadoop 3.6.x 的  
`hadoop-yarn-applications-catalog-webapp`（见本仓库 `patches/apache-hadoop/` 下的补丁说明）。

**自动化建议**：

- 对「只读」上游仓库：在 CI 中 **`git apply`** 补丁，或维护 **fork 分支**。
- 在索引流水线里对 **已知项目** 做 **补丁白名单**，失败时回退为「记录日志 + 跳过该模块」需业务评估（会丢符号）。

## 2. 磁盘空间

全量 Hadoop/ES 等多模块 Maven 构建会大量下载依赖；若出现：

```text
Could not transfer artifact ... No space left on device
```

与 scip-java 无关，需 **扩容磁盘**、**清理 `~/.m2/repository`**，或将 **`MAVEN_OPTS` / `TMPDIR`** 指到大容量盘。

## 3. 其他思路（次选）

- **scip-java** 的 `--scip-ignored-javac-option-prefixes` 仅适用于 **scip-java.json / Bazel** 路径（见官方文档），**对 Maven 默认注入的 fork javac 不一定可剥掉 `endorseddirs`**，因此 **仍以改 POM 为主**。
- 若仅需 SemanticDB 而非完整 `mvn verify`，可考虑 **自建 `semanticdb-targetroot` + `index-semanticdb`**（见 `JavaIndexer` 的 `semanticdb_targetroot`），绕过部分 Maven 生命周期，但集成成本更高。

## 4. 参考

- [scip-java 手册：Maven 与跨仓库](https://sourcegraph.github.io/scip-java/docs/manual-configuration.html)
- `hybrid_platform/java_indexer.py`：`JavaIndexRequest.build_args` 可传 Maven 尾部参数（如 `-DskipTests`）。
