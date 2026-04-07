# P2 Structured Index & Graph Specification

## 1. 目标

构建支持 `def/ref/call/hierarchy` 的结构化索引层，面向低延迟查询。

## 2. 存储模型

- `documents`
- `symbols`
- `occurrences`
- `relations`
- 必要二级索引：`symbol_id`, `display_name`, `file_path`, `kind`, `repo`, `commit`

## 3. 图派生规则

- 基于 occurrence + relation 生成调用/继承/实现边
- 对不确定边附加 `confidence`
- 每条边保留 evidence（文件与位置）

## 4. 增量策略

- 版本分层：按 `repo+commit` 写入
- 局部重建：仅重建受影响文档与关联边
- 旧版本按 TTL 惰性清理

## 5. 验收

- `def_of/refs_of/callers_of/callees_of` 可用
- 核心查询延迟满足指标
