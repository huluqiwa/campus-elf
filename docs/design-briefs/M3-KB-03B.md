# M3-KB-03B — 构建进度可观测与版本审批查询（KB-03 后端 B 点）实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-11-07 |
| 状态 | 已完成（2026-11-07，全按推荐项执行；实现中自查 + 合并后 main CI 验证；交付事实见 §9） |
| 关联功能点 | M3 第 7 点：KB-03B（KB-03 的收尾点；KB-03A 管线已合并 `87f9b83`）。**本点后端收尾后，前端进度 UI 随 KB-03B2 下一简报**（A/B 切分既定节奏） |
| 验收引用 | `docs/design.md` §5.6（:618-640）/§5.5（:460-462）、`docs/spec.md` §3.6 KB-03（:260-272）、`docs/state-machines.md` §4、`docs/tech-debt.md` TD-291（已采纳 → KB-03）/review-log Minor 3（KB-03B 处置承诺） |
| 不做的事 | 前端进度 UI 与轮询兜底的消费侧（KB-03B2 下一简报；本点交付 SSE 端点与快照已足够前端开发）；发布/alias（KB-05）；测试沙箱（KB-04）；审计侧新增 |

## 0. 现状核对（读代码得到的事实）

**已有基座（KB-03A 交付）**

- 管线阶段推进点已收敛在 `app/kb/build_task.py::_stage_progress`（独立短事务写 stage/progress/heartbeat/tier/fallback_reason）与 `_finish`（READY/FAILED 收口 + 审计）——**事件写入只需在这两处扩展**；
- `kb_build_events` 表已在 design §4.3 规划（`id, job_id, seq, event_type, payload_json, created_at`）但**未建**（KB-03A 迁移刻意不建——B 点职责）；
- SSE 基建已有：`app/core/sse.py`（`format_sse`/`format_heartbeat`/`SSE_HEADERS`，含 `X-Accel-Buffering: no`）+ `app/api/campus_chat.py` 的 StreamingResponse 先例；
- 快照端点 `GET /school/kb/build-jobs/{job_id}` 已交付（TD-288）；前端轮询兜底就是每 3s 打它（§5.6 规划）。
- KB-02B 遗留：文档抽屉 REJECTED 状态只有「到审批中心 → 已办查看」静态指引（TD-291 已采纳 → 本点兑现）。

**审查 Minor 3（review-log 承诺本点处置）**

- `kb_chunks` 父子 id 在 CHUNKING 阶段即分配（`chunk_ids = [uuid4()…]`），但 `IndexPoint.parent_id` 构造时传 `None`（父映射在 `_finish` 才回填）→ Qdrant payload `parent_id` 恒 None；
- 父块**也被嵌入并写入索引**（dense_texts 覆盖全部 raw_chunks）——design §7.4 的 `chunk_type` payload 存在但检索语义未定；
- `ensure_collection` 用首个向量长度建集合，dense 维度未与配置校验（TEI 配错模型会静默建成 768 维）。

**TD-291 契约**

- `approval_requests.document_version_id`（KB-02 已建，部分唯一索引兜底一版本一活跃单）；按 version 正向查询 + `created_at DESC` 取最近一单即可让抽屉直显审批状态与驳回理由。

## 1. 目标

1. **事件持久化**：`kb_build_events` 表（RLS）；worker 在阶段推进/收口时写 `build.stage`/`build.progress`/`build.done`/`build.error` 事件（seq 按 job 递增）；管线失败路径也留事件。
2. **SSE 端点**：`GET /school/kb/build-jobs/{job_id}/events`（`school.kb.view` + 数据范围；fetch + Bearer，不用原生 EventSource——design §5.6 明示）——连入即回放 `Last-Event-ID`/`?since=<seq>` 之后的事件 → 跟随新事件 → `build.done`/`build.error` 后关闭；**15s 心跳**；job 终态后连入只回放历史即关闭（不挂空流）。
3. **TD-291 兑现**：`GET /school/kb/document-versions/{version_id}/approvals`（`school.kb.view` + 数据范围）→ 该版本的审批单列表（`created_at DESC`）；前端（KB-03B2）取最近一单渲染状态与驳回理由，替换静态指引。
4. **review-log Minor 3 处置**：Qdrant payload `parent_id` 回填（CHUNKING 时确定父子映射）；**父块不入索引**（只索引子块——检索按子块召回、父块用于上下文展开，节省一半嵌入/存储；VALIDATING 对账改「点数 == 子块数」）；`KB_EMBED_DIM` 配置校验（默认 1024，建集合时与实际维度比对，漂移即 FAILED）。

**验收（本点，全部断言业务结果）**

- 事件：构建全阶段后 `kb_build_events` 有序 seq（stage×6 + progress + done），失败路径有 `build.error`；`?since=N` 只回 N 之后的事件；终态后连入回放历史并关闭。
- SSE：`text/event-stream` + SSE_HEADERS；跨租 404；无 view 403；心跳帧在空闲期出现；done 后流自然结束（集成测试读流断言帧序）。
- TD-291：REJECTED 版本查审批单 → 含驳回理由/审批人/时间；无审批单返回空表；跨租 404。
- Minor 3：Qdrant payload `parent_id` 非空且指向父块；点数 == 子块数；`KB_EMBED_DIM` 漂移 → FAILED `EMBED_DIM_MISMATCH`。
- 静态门禁 + CI 全绿；真实引擎业务场景复跑（探测 skip 同 TD-295）。

## 2. 候选方案

### D1 事件写入位置

- **方案 A（推荐）：worker 写入**——`_stage_progress` 与 `_finish` 在既有短事务内追加 `kb_build_events` 行（同事务原子，无额外连接）；事件负载 = 快照字段子集（不重复存全量快照，SSE 回放时按 job 现值补齐）。
  - 优点：与状态推进同事务（事件与状态一致性由 DB 保证）；零额外基础设施；seq 用 `INSERT … seq = 子查询 max+1`（同 job 内递增，并发安全靠行锁/唯一 `(job_id, seq)`）。
  - 缺点：事件行数 = 阶段数（个位级）+ progress 批次（嵌入批次数，几十级）——量级完全可控。
- 方案 B：Redis pub/sub + 表兜底。缺点：订阅者不在（SSE 端点晚启）丢事件，仍需表回放——两套机制比一套表更复杂。不选。

### D2 SSE 流的关闭语义

- **方案 A（推荐）：回放历史 → 若 job 已终态直接结束流；否则跟随新事件（每 15s 心跳兜底），直到写终态事件后关闭**。客户端重连用 `Last-Event-ID`/`?since` 续传。
  - 依据：§5.6「断线重连不丢最终状态」；终态后挂空流浪费连接。
- 方案 B：无限流 + 客户端自行断开。缺点：泄漏连接。不选。

### D3 父块是否入索引（Minor 3 定案）

- **方案 A（推荐）：只索引子块**；payload `parent_id` 指向父块 chunk_id；VALIDATING 对账「点数 == 子块数」。
  - 依据：§7.5 检索按子块召回、按 `parent_id` 去重取父块上下文——父块本身不参与召回，入索引纯浪费嵌入与存储；`chunk_type` payload 保留（检索侧可过滤）。
  - 对 KB-03A 的影响：`_finish` 的对账语义与集成断言同步改（点数 = 子块数）；chunk_id 分配不变。
- 方案 B：父子都入。缺点：嵌入/存储 ×2，检索时还得过滤父块。不选。

### D4 dense 维度校验

- **方案 A（推荐）**：`KB_EMBED_DIM` 配置（默认 1024 = BGE-M3）；`_run_pipeline` EMBEDDING 完成后校验实际维度 == 配置，漂移 → FAILED `EMBED_DIM_MISMATCH`（TEI 配错模型在构建期就暴露，而非 KB-07 检索期）。
- 方案 B：建集合时才校验。缺点：错误集合已创建，重试又删旧建新——漂移循环。不选。

### D5 TD-291 端点形态

- **方案 A（推荐）：`GET /school/kb/document-versions/{version_id}/approvals`**（`school.kb.view` + 数据范围，`created_at DESC`，limit 20）→ `ApprovalRequestResponse` 复用 KB-02B 的响应模型；前端抽屉取 `items[0]`。
  - 优点：列表形态天然容纳「撤销再提交」的多行历史；复用既有响应模型零新契约。
- 方案 B：单对象端点（只回最近一单）。缺点：历史不可见，撤销/重提交后前端无法展示过程。不选。

## 3. 选择结论

- **D1 A（worker 同事务写事件）／D2 A（终态即关流）／D3 A（只索引子块）／D4 A（KB_EMBED_DIM 构建期校验）／D5 A（列表端点复用响应模型）**。用户 2026-11-07 问题卡片确认全按推荐项执行。

## 4. 实现顺序与 TDD 测试清单

1. **迁移（head `e8c2a6d0f4b9` → 新）**：`kb_build_events`（job_id 复合 FK CASCADE、`(job_id, seq)` 唯一、RLS）+ downgrade。
2. **worker 事件写入（先红）**：`_stage_progress`/`_finish`/失败路径追加事件行（build.stage × 6 / build.progress / build.done / build.error）；seq 递增单测。
3. **SSE 端点（先红）**：`GET /build-jobs/{job_id}/events`（回放/续传/跟随/心跳/终态关闭/404/403）。
4. **Minor 3（先红）**：IndexPoint 带 parent_id；只索引子块 + 对账语义改；KB_EMBED_DIM 校验。
5. **TD-291（先红）**：`GET /document-versions/{version_id}/approvals`（复用响应模型；数据范围；跨租 404）。
6. **业务场景复跑**：真实引擎链路 + 事件/SSE 断言。
7. **门禁与审查**：静态门禁；独立审查；文档（design §5.6/§7.4 落地注、test-plan BS-11 补证据、tech-debt TD-291 收口 + Minor 3 处置、review-log、简报；handoff/README/milestone 随合并后 docs PR）。

红→绿清单：

- [ ] 事件：全阶段 seq 连续（1..N）、事件类型与阶段对应；失败路径 build.error 在最后一 seq；`(job_id, seq)` 唯一约束兜底
- [ ] SSE：200 + text/event-stream + X-Accel-Buffering: no；`?since=N` 只回后续事件；Last-Event-ID 头等价；终态后回放完即关；心跳帧出现（测试用短心跳间隔注入）
- [ ] 权限：跨租 404；无 view 403（SSE 与 approvals 两端点同口径）
- [ ] Minor 3：payload parent_id 非空且等于父块 chunk_id；点数 == 子块数（父块不入索引）；KB_EMBED_DIM 漂移 → FAILED
- [ ] TD-291：REJECTED 版本返回理由/审批人；多行历史按 created_at DESC；跨租 404
- [ ] 既有用例不回归（KB-03A 的 7 条集成中点数对账断言改为子块数）

## 5. 测试策略

| 层级 | 内容 | 运行地 |
|---|---|---|
| 单元 | seq 递增、事件负载形状 | CI |
| 集成 | SSE 端点（httpx stream 读帧：回放/续传/关闭/心跳/权限）；TD-291；Minor 3（Qdrant payload + 对账语义 + dim 漂移）——fake ports 同 KB-03A | CI |
| 业务场景 | 真实引擎链路 + 事件回放/SSE 断言（探测 skip 同 TD-295） | 本地 |
| 静态门禁 | ruff / mypy / bandit / alembic check | 本地+CI |

## 6. 预判技术债

| TD 编号 | 条目 | 处置 |
|---|---|---|
| TD-291（既有） | 文档抽屉不直显审批状态与驳回理由 | **本点兑现端点**；前端直显随 KB-03B2（届时替换静态指引） |
| TD-296（新） | KB-03B2 前端进度 UI：抽屉构建进度条（SSE 优先 + 3s 轮询兜底）+ TD-291 消费 | 已采纳 → KB-03B2（下一简报） |
| TD-297（新） | SSE 心跳与事件写入的连接生命周期未做压测（万级并发观流）；Nginx 关缓冲的生产配置验证 | 已延后 → M6（与 TD-214 Prometheus、部署硬化同批） |

## 7. 高风险确认

- **属于高风险功能**：SSE 长连接（资源占用）与权限边界；但复用既有 `app/core/sse.py` 与 `school.kb.view` 判据，风险集中在「流的生命周期」（D2 已定案：终态即关）。
- **需要用户确认的点**（推荐项在前）：
  1. **D1 事件 worker 同事务写入**（不引入 Redis pub/sub）。
  2. **D2 终态即关流**（done/error 后服务端结束，不挂空流）。
  3. **D3 只索引子块**（父块不入索引，对账改子块数——省一半嵌入/存储，检索语义 §7.5 不变）。
  4. **D4 `KB_EMBED_DIM=1024` 构建期校验**。
- D5（列表端点复用响应模型）推荐直接按 A 执行，有异议请在确认时指出。
- **确认结论**：用户 2026-11-07 问题卡片确认全部按推荐项执行（① worker 同事务写事件；② 终态即关流；③ 只索引子块；④ KB_EMBED_DIM=1024 构建期校验；⑤ TD-291 列表端点复用响应模型）。

## 9. 交付事实（2026-11-07）

- **PR #45**（`feature/m3-kb03b-build-events-sse`，head `e804d23`）→ rebase 合并（合并提交见 git log），分支已删除。合并授权：用户在 PR CI 全绿后确认。
- **PR CI**：backend run `42386397474` **986 passed, 1 skipped（1043.44s / 17:23）**（skipped=真实引擎 CI skip，TD-295 口径；986 = KB-03A 的 979 + 本点 7 新增）；四包分支覆盖率 79.92/82.41/81.25/66.67% 全过；security run `42386398169` success（50s）。
- **本地门禁**：ruff/mypy（247 文件）/bandit×2/alembic check（roundtrip 实测）全绿；单测 32 + 集成 14（KB-03A 回归 7 + 本点 7）+ 业务场景 1 passed（37.64s 真实引擎复跑）+ 迁移回滚 12 + config 32。
- **实现中自查修掉的 6 个缺陷**：CHUNK_TYPE_CHILD 导入缺、dense_vectors 索引偏移（只嵌子块后 dense 按子块序）、child_positions 使用前定义、build_request 去 await（同步方法）、KB_EMBED_DIM 与 FakeEmbedder 4 维冲突（改 1024 维）、substring 断言假绿（"seq":1 会命中 "seq":10）。
- **TD**：TD-291 已兑现端点（前端直显随 KB-03B2）；TD-296（前端进度 UI → KB-03B2）、TD-297（SSE 压测 → M6）已登记。
