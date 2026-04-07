# P5 Evaluation & Observability Specification

## 1. 目标

形成可复现离线评测 + 在线观测闭环，驱动检索质量持续优化。

## 2. 离线评测

- 数据格式：`query -> relevant_symbol_ids/chunk_ids`
- 指标：Recall@K、MRR、NDCG@10
- 支持按查询类型分桶统计

## 3. 在线观测

- ingestion: 吞吐、失败率、延迟
- retrieval: QPS、P95、命中率、降级率
- quality proxy: 点击反馈/人工标注对齐率

## 4. 治理

- 敏感信息扫描与脱敏
- 索引生命周期管理
- 审计日志与变更记录

## 5. 验收

- 评测脚本可重复执行
- 指标可视化输入完备
