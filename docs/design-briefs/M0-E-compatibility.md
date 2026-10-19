# M0-E — 依赖兼容性、Arq、MinerU 4.0.12、Langfuse 4.63.0 实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-20 |
| 状态 | 待确认（高风险项：MinerU 构建/显存） |
| 关联功能点 | M0-E |
| 验收引用 | docs/milestone.md M0、docs/design.md 第 1/3/7 节 |

## 1. 目标

- 验证计划的后端依赖能在 Python 3.13 下安装、导入、启动。
- 验证 Arq 在 Python 3.13 + Redis 下可用；不兼容时提出替代，不静默替换。
- 构建 MinerU 4.0.12 固定版本镜像，用 V1 HTTP API 解析 1 个 PDF，验证 6GB 显存下 Standard→Basic→Flash 自动降级。
- 验证 Langfuse 4.63.0 开发 optional profile 可用且默认关闭；不接生产观测主机。
- **不实现业务功能**，不接 KB 管线，不写入真实数据。

## 2. 候选方案

### 2.1 依赖验证

**方案 A：一次性加入全部依赖并 `uv sync`**
- 做法：把所有后端依赖写进 pyproject，直接同步。
- 优点：一次到位。
- 缺点/成本：某个包不兼容时整锁失败，定位困难；锁文件大幅抖动。
- 风险：高。
- 对 spec/design 影响：无。

**方案 B（推荐）：分组增量加入 + 导入冒烟测试**
- 做法：先写 `tests/test_dependencies.py` 断言导入；按 web/db、rag、agent、observability/eval 四组加入 pyproject，逐组 `uv sync`；每个失败包单独报告。
- 优点：失败隔离，兼容性结论清晰，锁文件变化可解释。
- 缺点/成本：需要多轮 sync（可接受）。
- 风险：低。
- 对 spec/design 影响：无；若包不兼容，写入 TD 并给出替代。

**方案 C：推迟到 M1 再验证**
- 拒绝：M1 的鉴权/DB 数据表会立刻依赖这些包，晚验证会把环境问题混进业务问题。

### 2.2 MinerU 部署

**方案 A（推荐）：官方 `docker/global/Dockerfile` 构建固定 tag 镜像**
- 做法：`git clone --depth 1 --branch <4.0.12 tag>` → `docker build -f docker/global/Dockerfile -t mineru:4.0.12` → `api-server --host 0.0.0.0 --port 8000`，compose `parser` profile 映射 8100。
- 优点：与官方部署最接近；V1 API 独立服务；GPU 单并发可控。
- 缺点/成本：镜像大、构建耗时；模型下载体积大；6GB 显存可能需要 Basic/Flash。
- 风险：构建网络/磁盘不足；GPU 驱动；Standard 不可用。
- 对 spec/design 影响：无，符合已确认的 MinerU 4.0.12 决策。

**方案 B：在 backend 进程内 pip 安装 `mineru[torch]`**
- 拒绝：模型与 CUDA 依赖污染业务进程；显存与业务服务争用；升级/隔离困难。

**方案 C：远程 MinerU API**
- 拒绝：校园文档数据出域；生产依赖外部服务；与自托管要求冲突。

### 2.3 Arq

**方案 A（推荐）：Arq 0.26+，M0-E 做 Redis ping + 任务执行冒烟**
- 若 Python 3.13 导入/运行通过则锁定；否则进入方案 B/C。

**方案 B：Taskiq + Redis**
- 优点：异步原生、维护活跃、支持 3.13。
- 缺点：生态和示例少于 Celery/Arq；需要改 worker 代码。

**方案 C：Celery + Redis**
- 优点：生态最大。
- 缺点：同步模型与 async 业务不贴；配置复杂。

> 若 Arq 不兼容，先报告 + 给出 B/C 对比，经确认后再替换，不静默切换。

### 2.4 Langfuse

**方案 A（推荐）：开发 optional profile，固定 4.63.0**
- 做法：`docker compose --profile observability up -d` 单独验证 Web/Worker/Postgres/ClickHouse/Redis/S3；默认关闭；应用侧 `LANGFUSE_ENABLED=false`。
- 优点：与生产独立主机方案一致；不占 16GB 常驻；验证版本/健康/组件可用。
- 缺点/成本：组件多、内存大；仅按需启动。
- 风险：16GB 同时启 core+rag+observability 可能 OOM；必须单独启动验证。
- 对 spec/design 影响：无。

**方案 B：Langfuse Cloud**
- 拒绝：校园数据出境/隐私审批未完成。

**方案 C：跳过验证**
- 拒绝：M6 生产部署前必须验证 4.63.0 的 OTel/API/组件。

## 3. 选择结论

- 依赖：方案 B（分组增量 + 导入冒烟）。
- MinerU：方案 A（官方 Dockerfile 固定 tag + V1 API + 六 G 显存降级验证）。
- Arq：方案 A 优先；不兼容时先报告再选 B/C。
- Langfuse：方案 A（开发 optional，默认关闭，独立启动验证）。
- 不改 spec/design 决策；只把验证结果与版本证据写入 design/deployment。

## 4. 实现顺序与 TDD 测试清单

1. **依赖契约测试**：`tests/test_dependencies.py` 先写导入断言（红），再分组加依赖（绿）。
   - [ ] web/db：sqlalchemy、asyncpg、alembic、redis、arq、qdrant-client
   - [ ] rag：minio/aioboto3、httpx、jsonschema、jinja2
   - [ ] agent：langgraph、langchain-core、openai
   - [ ] auth/安全：pyjwt、pwdlib、pyotp、cryptography
   - [ ] eval/观测：ragas、langfuse
2. **Arq 冒烟**：连 Redis 执行一个 echo 任务，断言结果与队列清理。
3. **MinerU**：
   - [ ] 构建 `mineru:4.0.12`（或官方 tag）
   - [ ] compose `parser` profile 启动
   - [ ] `/v1/health` 健康
   - [ ] 用一个合成 PDF 调 V1 API，记录实际 tier（Standard/Basic/Flash）、耗时、显存
4. **Langfuse**：
   - [ ] optional profile 单独启动，组件 healthy
   - [ ] 应用侧默认关闭时不影响后端启动/测试
5. 文档与台账：把版本/摘要/降级结果写回 design/deployment，TD-012 标完成，新问题登记。

## 5. 测试策略

| 层级 | 内容 |
|---|---|
| 单元 | 依赖导入断言、版本断言 |
| 集成 | Arq + Redis 任务；MinerU V1 API 解析；Langfuse 组件健康 |
| 业务场景 | 本单元不做业务链路 |
| E2E | 不涉及 |
| AI 评估 | 不涉及 |
| 性能 | 记录 MinerU 解析耗时/显存；不做压测 |

## 6. 预判技术债

| TD 编号 | 条目 | 处置 |
|---|---|---|
| TD-012 | MinerU 4.0.12 构建/端点/6GB 验证 | 本单元完成后标已完成 |
| TD-010 | TEI 模型离线预置 | 延后 M0-F |
| TD-018 | sink async | 延后 M1 |
| TD-007 | MinIO 最小镜像 | 延后 M6 |
| 新增 | 依赖不兼容项（如有） | 先报告再定 |

## 7. 高风险确认

- MinerU 构建/模型下载会消耗时间、磁盘；6GB 显存 Standard 可能不可用。
  - 已确认策略：Standard→Basic→Flash 自动降级；实际 tier 写入构建元数据；若默认档位需要调整，先报告再改。
- Arq 若不兼容 Python 3.13：不静默替换，先给 Taskiq/Celery 对比。
- Langfuse 验证只做开发 optional；不部署生产观测主机、不外送真实数据。

## 8. 审查对照

- 审查时重点验证：
  - 依赖版本与 Python 3.13 实际解析结果；
  - Arq 任务真实执行而非仅导入；
  - MinerU 使用 V1 API、固定版本、实际 tier 与显存证据；
  - Langfuse 默认关闭、按需启动、未发送真实数据；
  - 未验证项是否登记 TD，不伪装成已完成。
