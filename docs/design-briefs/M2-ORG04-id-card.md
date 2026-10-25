# M2-ORG04 — 身份证加密采集与受控查看（ORG-04）实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-25 |
| 状态 | 草稿（待用户确认 4 项决策——**高风险功能**） |
| 关联功能点 | M2 第 4 点：ORG-04 身份证可选采集（加密+查重+掩码+受控查看） |
| 验收引用 | docs/spec.md §3.5（AES-GCM 加密 + HMAC 查重；默认掩码前3后4；完整查看需权限+理由+审计）、docs/design.md §4.2（id_card_enc/hmac/mask 三列已预留）、docs/rbac-matrix.md 行 176（`school.personnel.sensitive.read` △） |

## 1. 目标

1. 教师/学生建档与编辑可**可选采集**身份证号；落库即 AES-256-GCM 加密（`id_card_enc`）+ HMAC-SHA256 指纹（`id_card_hmac`，查重）+ 掩码展示值（`id_card_mask`，前3后4）。
2. 所有既有列表/详情接口**只返回掩码**（不含明文/密文/HMAC）。
3. **完整查看**走专用端点：持 `school.personnel.sensitive.read` + 请求体必填理由 → 解密返回明文 + 审计。
4. **查重**：录入/导入时按 HMAC 查重（同租内同号即冲突），不泄露明文。

**不含**：HTTP 人员核查适配器（spec 说默认关闭，登记 TD 延后到 M3/M5 有真实需求时再实现）；MFA secret 加密改造（已有独立 KEK，不动）。

## 2. 候选方案

### 决策 D1：密钥来源（高风险核心）

- **方案 A（推荐）：独立 `ID_CARD_KEK` 环境变量**（base64 32 字节，格式同 ENCRYPTION_KEK）——密钥分离原则：MFA secret 与身份证号是不同安全域，泄露影响面不同；轮换互不影响。staging/production 必填校验（复用 ENCRYPTION_KEK 的校验模式），开发可缺省（缺省时身份证采集接口返回 501 + 明确提示）。
- 方案 B：复用 ENCRYPTION_KEK 派生子密钥（HKDF）——少一个 env，但 MFA secret 泄露 = 身份证也泄露（攻击面放大），且 ENCRYPTION_KEK 轮换会连带身份证不可解。
- 方案 C：PG pgcrypto 列级加密——密钥在 SQL 层（GUC/连接串），日志/慢查询可能泄漏；不符合「密钥不进 SQL 层」原则。

### 决策 D2：写入路径

- **方案 A（推荐）：扩展建档/编辑 + 导入可选列**——`POST /school/teachers|students` 与新增 `PUT /school/teachers|students/{id}`（ORG-01 只做了 GET/POST）增可选 `id_card_number` 字段（18 位中国居民身份证，含校验码验证）；导入 CSV/XLSX 增可选 `身份证号` 列。写入即加密三件套；明文**不落日志/审计/响应**。
- 方案 B：独立端点 POST /school/personnel/{id}/id-card——多一个端点但建档后两步操作。

方案 A 的 PUT 编辑接口是 ORG-01 按设计 §5.4 只做了 GET/POST 的**新增补充**（design §5.4 没有 PUT，但「编辑」验收口径需要——回写 design.md §5.4 增 PUT 行）。

### 决策 D3：完整查看 API 形状

- **方案 A（推荐）：`GET /school/personnel/{personnel_id}/id-card`，请求体（JSON）必填 `reason`（≥10 字符）**——GET 语义（读取不改状态）；reason 放请求体避免 URL/日志泄漏；响应 `{number}` 明文 + `Cache-Control: no-store`；审计 `sensitive.read` 含 reason/目标/操作者。权限 `school.personnel.sensitive.read`（realm=tenant）。
- 方案 B：POST /school/personnel/{id}/sensitive-view + token 一次性取回——多一步但可做「查看链接分发」；MVP 不需要。

### 决策 D4：HTTP 人员核查适配器

- **方案 A（推荐）：登记 TD 延后**——spec 说「默认关闭，超时降级，人工确认」；MVP 无真实核查服务可对接，先做纯本地采集。TD 目标：M5/M6（有真实第三方接口时）。
- 方案 B：现在就实现适配器骨架——增加不可测的集成面。

### 范围内子决策（随简报确认）

- **校验**：18 位格式 + GB 11643-1999 校验码算法（加权因子验证）；15 位旧号不支持（录入提示升位）。
- **掩码格式**：`前3 + *×10 + 后4`（如 `110***********1234`），18 位输入固定 18 位输出。
- **查重范围**：同租户内（tenant_id + hmac 唯一性校验），不跨租。
- **TD-069**：身份证写入走 ORM（含 updated_at onupdate），不新增非 ORM 路径。
- **审计动作**：`sensitive.id_card.read`（完整查看）/ `org.id_card.created|updated`（写入，metadata 只含 mask 不含明文/密文）。

## 3. 选择结论

- D1 选 A（独立 ID_CARD_KEK）、D2 选 A（扩展建档/编辑+导入）、D3 选 A（GET + reason 请求体）、D4 选 A（TD 延后）。
- 是否需要改 spec/design：design.md §5.4 增 PUT /school/teachers|students/{id} 与 GET /school/personnel/{id}/id-card 两行（回写）。

## 4. 实现顺序与 TDD 测试清单

1. 配置：`ID_CARD_KEK` 环境变量（base64 32 字节；staging/production 必填校验，开发缺省=功能 501）。
2. 加密服务 `app/security/id_card.py`：encrypt（AES-256-GCM nonce 随机）/decrypt/hmac/mask/validate（GB 11643 校验码）。
3. 写入路径：create_teacher/create_student 增可选 id_card_number；PUT /school/teachers|students/{id}（含 department_id + id_card_number）；导入增可选列。
4. 读取路径：列表/详情返回 mask；GET /school/personnel/{id}/id-card（sensitive.read + reason → 明文 + 审计）。
5. 查重：HMAC 唯一校验（同租内）。

红→绿清单：
- [ ] 单元：加密往返、HMAC 确定性、掩码格式、身份证校验码（合法/非法/15 位拒绝）、nonce 随机性（同号两次加密密文不同）
- [ ] 集成：写入→读取掩码→完整查看（权限+理由+审计）；无权限 403；有权限无理由 422；HMAC 查重 409；RLS 跨租隔离（HMAC 不跨租匹配）；停用人员不可查看
- [ ] 业务场景 BS-M2-04：校管录入带身份证的教师 → 列表只见掩码 → 无 sensitive.read 的校管查看明文 403 → 授予后查看（填理由）→ 审计含 reason → 同号第二人录入 409 → 导入含身份证列 → 掩码正确
- [ ] E2E：无新增（无 UI）

## 5. 测试策略

| 层级 | 内容 |
|---|---|
| 单元 | 加密/HMAC/掩码/校验码（≥15 例：含边界与拒绝路径） |
| 集成 | 真实 PG：写入→掩码→受控查看→审计→查重→RLS→停用 |
| 业务场景 | BS-M2-04 全链路 |
| E2E / 性能 / AI 评估 | 不涉及 |

## 6. 预判技术债

| TD | 条目 | 处置 |
|---|---|---|
| TD-NEW | HTTP 人员核查适配器（spec 说默认关闭） | 已延后 → M5/M6（有真实第三方时） |
| TD-NEW | 密钥轮换（ID_CARD_KEK 变更后存量密文解密） | 已延后 → M6（部署硬化时设计密钥版本号/轮换流程） |

## 7. 高风险确认

- 是否高风险：**是**——加密与敏感字段（AGENTS §3 红线 + spec ORG-04 验收）。
- 需要用户确认：
  1. D1 密钥独立（ID_CARD_KEK 新环境变量）；
  2. D3 完整查看 GET + 理由请求体；
  3. 校验码算法拒绝非法号（含 15 位旧号升位提示）；
  4. HTTP 核查适配器延后（TD）。
- 确认结论：待确认。

## 8. 审查对照

- 审查重点：密钥不落日志/审计/错误响应；nonce 随机（同号两次加密密文不同）；HMAC 密钥与加密密钥分离（HMAC 用独立派生或 KEK 不同域）；掩码不含明文残留；reason 不截断进审计；审计动作含操作者/目标/reason；RLS 跨租 HMAC 不可匹配；无权限/无理由的 403/422 有测试钉住。
- 允许偏差：错误码文本；掩码分隔符样式；用例数 ±3。
