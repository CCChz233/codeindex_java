# P6 Leiden Upgrade & Repair Calls Specification

## 1. Scope

本规格新增两项能力：

- 使用真实 Leiden 社区算法（可选依赖，自动回退）
- `repair-calls` 回流补边能力（针对 `MissingEdge` 节点）

## 2. Spec L1: Leiden Upgrade

### 输入

- 加权候选边集合：`(node_a, node_b, weight)`
- 函数意图节点全集
- 参数：`resolution`, `alpha`, `beta`, `gamma`

### 执行策略

1. 尝试导入 `igraph` 与 `leidenalg`
2. 导入成功：
   - 构建无向加权图
   - 执行 Leiden（`RBConfigurationVertexPartition`）
3. 导入失败或运行异常：
   - 自动回退到 fallback 聚类
   - 保证流程不中断

### 输出

- 社区结果（社区成员 + cohesion）
- 元信息：
  - `algorithm_used` (`leiden` or `fallback`)
  - `fallback_reason`（当回退时）

### 验收

- 依赖存在时使用真实 Leiden
- 依赖缺失时无异常退出
- 两种路径都可生成社区结果

## 3. Spec R1: Repair Calls

### 输入

- `MissingEdge` 节点集合（来自 `code_nodes.isolated_type`）
- `function_intents` 中语义向量
- 参数：`top_k`, `sim_threshold`, `max_edges_per_node`

### 规则

- 针对每个 `MissingEdge` 节点：
  - 计算对候选函数的语义相似度
  - 叠加路径先验（同目录优先）
  - 选取 top-k，且分数 >= 阈值
  - 写入低置信度 calls 边，evidence 标记 `repair_missing_edge`

### 输出

- 新增 calls 边数量
- 触发后续度更新（fan_in/fan_out）
- 可选触发 `isolated_policy` 重跑

### 验收

- 生成边可追溯（source/evidence 完整）
- 不产生自环边
- `uncertain_ratio` 在同一数据集上相对下降（目标方向）

