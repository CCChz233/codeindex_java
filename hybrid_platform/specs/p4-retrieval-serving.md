# P4 Retrieval & Serving Specification

## 1. 目标

实现结构化、关键词、向量三路召回与融合重排，输出 explainable 结果。

## 2. 多路召回

- Structured recall（symbol/graph）
- BM25/FTS recall
- Semantic vector recall

## 3. 融合策略

- baseline: 线性融合
- 可选: RRF 融合
- 输出 explain 字段：命中来源、子分数、最终分

## 4. 服务接口

- `query(mode, text, filters, top_k, blend_strategy)`
- mode: `structure|semantic|hybrid`
- 可降级：向量不可用时回退到 structure + keyword

## 5. 验收

- 混合检索质量高于单路
- API 稳定，错误码清晰
