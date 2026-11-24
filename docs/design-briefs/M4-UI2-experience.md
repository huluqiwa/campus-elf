# M4-UI2 设计简报 — C 端体验收口（引用抽屉 + 反馈入口 + 思考折叠 + 全栈链⑤ + 3D 待机 CSS）

> 日期：2026-11-22。决策卡五张已经用户拍板（「按你推荐的来」，见 §2）。
> 前置：UI1 已合并（`8e062ad`）；AI-05 反馈 API、citation 数据、reasoning_summary 均已就绪。

---

## 1. 目标与验收

### 1.1 目标

C 端聊天体验收口（milestone §7.2 尾项）：引用抽屉（点击可查看文档/页码/章节——spec
AI-04 验收「点击可查看」）、反馈入口（消费 AI-05）、思考折叠面板、全栈 E2E 链⑤
（真实后端全链）、3D 待机 CSS 方案。

**本点范围**：

- api-client：`createFeedbackApi`（submitFeedback/deleteFeedback）+ 聊天 api 扩展
- ChatView：引用抽屉（van-popup 底部抽屉）、反馈入口（👍/👎 → 表单 → 提交 →
  已反馈标记）、思考折叠（van-collapse「思考过程 (n)」默认收起）、3D 待机 CSS 精灵
- 全栈 E2E：`e2e-fullstack/campus-fullstack-chat.spec.ts` 链⑤（真后端 + .env 真
  LLM key + 真 TEI/Qdrant + KB 发布种子；**本地套件**，CI 不跑）
- **零迁移、零后端改动**（全部消费既有已审 API）

**本点明确不做**：正文 [n] 内联链接化（保留纯文本——AI-04 降级口径）；撤回反馈入口
（DELETE API 保留，TD）；反馈撤回 UI；真 3D 模型（TD-P2）。

### 1.2 验收编号

| 编号 | 验收内容 |
|---|---|
| M4-UI2-A1 | 引用抽屉：final 态有 citations 的气泡显示「引用来源 (n)」→ 底部抽屉列出 [index] 文档 · 章节 · 页码；无引用不显示 |
| M4-UI2-A2 | 反馈入口：👍/👎 → 表单（tags 多选 + comment ≤500）→ 提交成功 toast + 气泡标「已反馈」；幂等重放（重复点击已反馈气泡不再提交） |
| M4-UI2-A3 | 思考折叠：reasoning_summary 步骤 ≥1 时显示「思考过程 (n)」van-collapse 默认收起；展开逐条展示 |
| M4-UI2-A4 | 3D 待机 CSS：聊天页空闲态精灵动画（呼吸/眨眼纯 CSS）；有消息后隐藏 |
| M4-UI2-A5 | 全栈链⑤：真后端登录 → 提问 → 流式正文（逐 chunk 到达）→ 引用抽屉 → 提交反馈（库里 ai_feedback 行 + consent_at）→ 停止弧（abort → 已停止）→ 清空 → 历史为空；**复现命令入简报** |
| M4-UI2-A6 | api-client 反馈方法单测（载荷/幂等参数/错误信封映射） |

### 1.3 依赖与现状

- AI-05 端点已审：POST `/campus/feedback`（201/200-replayed）+ DELETE；tags 枚举
  inaccurate/irrelevant/outdated/harmful/other；comment ≤500
- citation/step 数据在 chat store 已有；UI1 气泡结构含 steps 行内条（本点升级为折叠）
- e2e-fullstack 基建：双 preview（4173/4174）+ e2e-stack.sh 真后端栈 + campus
  登录弧先例（`campus-fullstack.spec.ts`）

---

## 2. 决策卡拍板记录（2026-11-22，用户「按你推荐的来」）

| 卡 | 结论 |
|---|---|
| 卡 1 范围 | **A：单点全做**（引用抽屉/反馈/折叠/全栈链⑤/3D CSS） |
| 卡 2 引用抽屉 | **A：气泡尾「引用来源 (n)」→ 底部抽屉**；正文 [n] 保留纯文本；chunk_id 不进 UI |
| 卡 3 反馈交互 | **A：👍/👎 → 表单 → 提交 → 已反馈标记**；不做撤回入口（TD） |
| 卡 4 全栈链⑤ | **A：真后端 + .env 真 LLM + 真 TEI/Qdrant；本地套件 CI 不跑**；含停止弧 |
| 卡 5 3D 待机 | **A：CSS/SVG 呼吸/眨眼 + Lottie 接口位** |

---

## 3. 子决策 D1–D5

| # | 决策 | 结论 |
|---|---|---|
| D1 | 引用渲染粒度 | 气泡尾统一入口（不拆正文做内联 [n] 链接）——正文文本由后端已发 delta 决定，前端不二次解析改写 |
| D2 | 反馈状态存储 | store 记 `feedbackByTurn: Map<turnId, rating>`——气泡按 turn 关联（meta 的 turn_id 是反馈键）；页面刷新后状态丢失可接受（后端幂等兜底，不查历史反馈——GET 无按 turn 查询端点，登记 TD） |
| D3 | 折叠面板组件 | van-collapse 单面板（title=`思考过程 (n)`）；streaming 中步骤实时追加（面板收起仍计数） |
| D4 | 待机精灵 | 纯 CSS（`@keyframes` 呼吸 scale + 眨眼 opacity）；SVG 内联（无外部资源）；`prefers-reduced-motion` 尊重系统设置 |
| D5 | 全栈链⑤停止弧 | UI abort（停止按钮）→ 后端 USER_CANCELLED → UI「已停止」尾标；断言以 UI 终态为准（trace 断言归 pytest 级 BS 测试，已在 AI-01b 覆盖） |

---

## 4. 实现顺序（TDD 红→绿）

| 步 | 内容 | 测试 |
|---|---|---|
| 1 | api-client feedback 模块（submit/delete）+ 会话 store 反馈状态 | vitest（载荷/幂等参数/错误信封） |
| 2 | ChatView：引用抽屉 + 反馈入口 + 思考折叠 + 待机精灵 | mock E2E 扩弧（抽屉打开/反馈提交/折叠展开/精灵存在） |
| 3 | 全栈链⑤ spec + 复现命令文档化 | 本地真栈跑（A5） |
| 4 | 门禁（前端全套 + prettier）+ 独立审查 + PR | CI 四 job |

### 4.1 全栈链⑤复现命令（A5）

```bash
# 1) 真后端栈（重置测试库 + 迁移 + seed；seed 写 deploy/.e2e-accounts.json）
scripts/e2e-stack.sh up

# 2) 真 LLM 旁通（可选；不设则跑 LLM_NOT_CONFIGURED 兜底弧）。
#    口令只从 .env 读取并注入当前进程，不落库/不入日志/不写进编排文件。
set -a && . ./.env && set +a
export E2E_LLM_API_KEY="$LLM_API_KEY" E2E_LLM_BASE_URL="$LLM_BASE_URL" E2E_LLM_MODEL="$LLM_MODEL"
docker compose -p campus-elf-e2e --env-file deploy/.env.test \
  -f deploy/docker-compose.e2e.yml up -d backend

# 3) 运行（CI 不跑；需先 pnpm build 出 preview 产物）
cd frontend
E2E_ACCOUNTS_FILE="../deploy/.e2e-accounts.json" \
  pnpm exec playwright test campus-fullstack-chat \
  --config playwright.fullstack.config.ts --reporter=list --workers=1
```

链路覆盖：KB 发布前置（上传→审批→ACL→构建→测试集→`TEST_PASSED`→发布，全 API
驱动）→ 学生登录 → 提问 → 等 `data-status` 终态 → 引用抽屉 → 反馈（断言
`POST /campus/feedback` = **201 首次落库**）→ 清空（断言 `DELETE .../messages`
`cleared > 0`）→ 打开该会话消息区为空。

**可重复运行**：种子新增 `e2e-campus-chat`（不强制改密、挂内置 `student` 角色），
与首登强改密用例（`student`）隔离；连跑两轮均全绿。链⑤断言的「清空」按 spec
AI-06/07 语义是**软删消息、会话行保留**，因此结尾判断「该会话消息为空」而不是
「会话列表为空」。

## 5. 测试策略

- **单元**：feedback api 载荷/映射/错误；引用去重展示逻辑
- **mock E2E**：抽屉开合/反馈提交弧/折叠/待机精灵存在性
- **全栈链⑤**：真实 LLM 流式 + 反馈落库（查库由后端测试已覆盖，链⑤以 UI 终态断言）
- **门禁**：prettier（CI 门——UI1 教训）+ vue-tsc/eslint/vitest + 后端不动

## 6. 预判技术债

| 预判 TD | 内容 | 处置方向 |
|---|---|---|
| TD-P6 | 撤回反馈入口缺失（DELETE API 无 UI） | 看板/反馈管理批次 |
| TD-P7 | 反馈状态刷新后丢失（无按 turn 查询端点） | 反馈管理批次（B 端页面同期） |
| TD-P2 维持 | 真 3D 模型 | 后续评估 |

## 7. 文档回写清单

design §5.8 引用抽屉交互注记 / requirements AI-05 注记补反馈入口 / handoff §1 +
README（合并后 docs PR）。

## 8. 高风险确认

无新增数据写入面（消费既有已审 API）；全栈链⑤为本地套件。**简报经用户确认后编码。**
