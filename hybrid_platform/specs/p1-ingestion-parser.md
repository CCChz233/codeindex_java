# P1 Ingestion & Parser Specification

## 1. 目标

建立可重入、可重试、可流式的 `.scip` 解析链路，保证超大规模仓库稳定 ingest。

## 2. 输入输出

- 输入：
  - `.scip` 二进制（可选）
  - `*.scip.ndjson`（默认）
  - `repo`, `commit`, `index_version`
- 输出：
  - 规范化实体流（Document/Symbol/Occurrence/Relation）
  - 失败记录与统计指标

## 3. 解析策略

- 流式读取，分批写入（batch size 可配置）
- 文件级错误隔离
- 幂等处理：同一 `repo+commit+path` 重跑可覆盖

## 4. 质量门禁

- 空文档比例阈值
- symbol 总量异常阈值
- relation 缺失比阈值

## 5. 验收

- 解析成功率 >= 99.9%
- 同 commit 重跑结果稳定
- 支持分钟级增量 ingest
