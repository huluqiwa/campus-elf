# M0 CPU 嵌入/重排基准（M0-F）

> **性质**：数量级基线，**不是性能门禁**，不做调优。数字来自本机开发环境单次测量；
> 生产容量规划在 M6 用 k6 压测另行验证（A9/A10）。

## 1. 结论摘要

| 用例 | 批大小 | p50 (ms) | p95 (ms) | 吞吐 (items/s) |
|---|---|---|---|---|
| embed_batch1 | 1 | 407.1 | 549.9 | 2.4 |
| embed_batch8 | 8 | 2729.1 | 3174.5 | 2.89 |
| rerank_candidates1 | 1 | 1714.0 | 2181.9 | 0.56 |
| rerank_candidates8 | 8 | 11704.1 | 12698.0 | 0.68 |

每项 30 次测量、3 次预热；报告原文：`benchmarks/results/m0_embed_rerank.json`（git 忽略）。

**关键结论**

1. **嵌入吞吐约 2.4–2.9 条/秒**（CPU、float32、`--max-batch-tokens 2048`、容器 2 CPU）：批量 8 相对单条几乎没有吞吐收益（8 × 407ms ≈ 2729ms），说明瓶颈是单核前向计算，批处理只省了网络/调度开销。按此推算，1 万块的 KB 首次构建约需 1 小时——**M3 必须以异步 Worker + 进度回传的方式做构建**（本来也是设计如此），并考虑后续用 GPU 或 ONNX 加速。
2. **重排延迟是交互链路上的硬约束**：单候选约 1.7s，8 候选 p50 ≈ 11.7s。而 A10 要求快路径首 token P95 ≤ 3s、ReAct ≤ 5s —— **CPU 重排（8 候选）无法进入快路径预算**。这是 M0-F 提前暴露的关键风险，已登记 TD-056，M3/M4 需从以下方案中选择并验证：
   - 降低候选数（top-3/top-4，仍约 5–7s，可能仍不达标）；
   - 提高容器 CPU 配额（当前 `cpus: 2`，本机 16 vCPU；TEI CPU 推理可并行，预计近线性改善）；
   - 换更快重排模型（如 `bge-reranker-base`）或量化/ONNX；
   - 生产换 GPU 实例跑 TEI；
   - 重排仅用于 ReAct/难例路径，快路径只用 RRF 融合结果（需评估召回质量，入 Ragas 评估）。
3. 服务端接受的**最大客户端批大小为 8**——由 compose 显式配置的 `--max-client-batch-size 8` 限制（脚本探测时 8 通过、16/32 被 422 拒绝；本批语料约 80–160 字，远未触及 `--max-batch-tokens 2048`）。基准脚本会探测该上限并把超限用例记为跳过，而不是失败。

## 2. 环境

| 项 | 值 |
|---|---|
| 主机 | Windows 11 (10.0.22631)，Intel64 Family 6 Model 154（16 vCPU） |
| Python | 3.13.13 |
| TEI | `ghcr.io/huggingface/text-embeddings-inference:cpu-1.9.4`（digest 固定） |
| 模型 | embed `BAAI/bge-m3`（revision `5617a9f6…`）；rerank `BAAI/bge-reranker-v2-m3`（revision `953dc6f6…`） |
| 容器配额 | `mem_limit 2560m`、`cpus: 2`、`--dtype float32` |
| 语料 | 合成校园片段（80–160 字），每批取不同文本 |

## 3. 方法与复现

```bash
# 依赖：dev 栈的 rag profile 已启动
cd deploy && docker compose --profile rag up -d

# 运行（backend 的 uv 环境提供 httpx）
cd ../backend && uv run python ../benchmarks/embed_rerank_bench.py --markdown
```

- 预热 3 次（排除模型首次加载），随后每项测量 30 次，取 p50/p95 与 items/s。
- 自动探测服务端批上限（32/16/8/4/2/1），超限用例记录为 `skipped` 并附原因。
- 非 2xx 响应会连带响应体报错，避免把"输入非法"误判成"慢"。

## 4. 与本里程碑其他结论的关系

- `docs/deployment.md` §6.5：TEI 首次启动直连 HuggingFace 下载权重（约 2.2GB+），缓存在 `tei_models` 卷；离线预置见 `deploy/tei/prefetch.py`（TD-010）。
- 显存：TEI 为 CPU 推理，不占显存；MinerU 的 VLM 走 llama-cpp CPU、显存占用 <500MiB（M0-E）。
- 上云选型：本表的吞吐/延迟数字是"CPU 实例够不够用"的第一手依据；结论 2 表明**重排链路对 CPU 敏感**，若生产要在关键路径做重排，GPU 实例或更多 vCPU 需要纳入选型。
