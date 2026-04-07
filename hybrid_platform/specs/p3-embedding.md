# P3 Embedding Pipeline Specification

## 1. 目标

建立 chunk -> embedding -> vector index 的可版本化管线。

## 2. Chunk 策略

- 优先符号边界（函数/方法/类）
- 超长块切分并带 overlap
- 每个 chunk 绑定 `primary_symbol_ids`

## 3. 向量化策略

- embedding model/version 强制显式
- 批量计算 + 失败重试
- 幂等写入（同版本不重复生成）

## 4. ANN 与召回

- 默认 brute-force cosine（小规模）
- 可切换 HNSW/IVF（大规模）
- 支持 repo/namespace 分片

## 5. 验收

- 向量生成成功率 > 99.9%
- Recall@K 达标
