# Apache Hadoop × scip-java 补丁

## 问题

`hadoop-yarn-applications-catalog-webapp` 的 `pom.xml` 使用：

- `maven-compiler-plugin` → `compilerArguments` → `endorseddirs`
- `maven-dependency-plugin` → 将 `javax:javaee-endorsed-api:7.0` 拷到 `target/endorsed`

在 **scip-java** 触发的编译中，javac 使用 **`--release`** 时与 **`-endorseddirs` 不兼容**，构建失败。

## 修复

对 **Hadoop 3.6.x** 的该模块：

1. 增加依赖：`javax:javaee-endorsed-api:7.0`（编译 classpath，替代 endorsed 目录）。
2. 移除 `endorsed.dir` 属性、`maven-compiler-plugin` 中的 `endorseddirs` 配置。
3. 移除仅用于填充 endorsed 目录的 `maven-dependency-plugin` 拷贝执行。

应用方式示例：

```bash
# 在 Hadoop 源码根目录
git apply /path/to/hybrid_platform/patches/apache-hadoop/hadoop-yarn-applications-catalog-webapp-pom.patch
```

或手动合并 `hadoop-yarn-applications-catalog-webapp-pom.patch` 中的变更。

## 验证

日志中应出现该模块 **compile / war 成功**，且无 `endorseddirs` / `--release` 冲突。全量 `scip-java index` 仍依赖磁盘空间与 Maven 依赖下载。
