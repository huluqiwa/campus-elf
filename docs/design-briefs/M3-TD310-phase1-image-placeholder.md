# M3-TD310-phase1 — 图片占位符过滤（TD-310 阶段 1：空占位符不单独成块）实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-11-12 |
| 状态 | **已确认**（2026-11-12：决策卡 1-4 经用户拍板全按推荐；第 0 步真实栈实证后卡 1 识别口径修正〔相对路径 → data URI 为主 + 相对路径兼容〕经用户随简报确认；据此编码） |
| 关联 | TD-310 阶段 1（`docs/tech-debt.md` 台账条目；技术债落地小点，非 spec 编号功能点）；下一个 PR 编号 #67 起 |
| 验收引用 | TD-310 台账（阶段 1＝「空占位符过滤/保底（不单独成块）」）；`docs/design.md` §7.2（chunker 语义）；`docs/milestone.md` §6.2「MinerU 4.0.12 V1 适配器」 |
| 前置事实 | 全部契约点已在代码逐条核实（出处随文标注）；**第 0 步真实 MinerU 含图 PDF 冒烟已完成（2026-11-12，本简报 §0.2）**；无新迁移、无新权限码、无审计面变化 |
| 不做的事 | **阶段 2 全部**（MinerU 图片资产管道、VLM/LLM 图注、OCR、图注回填、引用展示图片——台账口径 M4 或 M3 边界评审拍板）；**外部 http(s) 图片链接与非空 alt 行**（用户内容，不滤）；**行内占位符**（不占整行的 `![](...)`，随段落保留，登记已知边界）；**parser 适配器契约变化**（output_formats/zip 通道不动）；**构建事件/metadata 过滤计数**（无消费者，§6 处置）；**纯图文档的兜底改造**（既有 `CHUNK_EMPTY` 承接，见 §3.3）；TD-311（XLSX/CSV 白名单）、TD-293（DOCX/PPTX）同批不做 |

## 0. 现状与第 0 步实证

### 0.1 代码现状（已核实）

- chunker 只有 paragraph/table/code 三类块（`backend/app/kb/chunker.py:76`）；图片引用行会走普通段落行路径（`chunker.py:155` paragraph.append），**独占一行时独立成段块**；纯空白行已有丢弃逻辑（`chunker.py:101` `if text.strip()`），但占位符非空白。
- MinerU 适配器只请求 `output_formats:["markdown"]`、只取 `content.markdown`（`backend/app/kb/parser/mineru.py:51,96`）——图片资产不保留，`grep image/img` 适配器零命中。
- 切块结果为空 → `build_task.py:545` 显式失败 `CHUNK_EMPTY`（既有漏斗承接，见 §3.3）。

### 0.2 第 0 步实证（2026-11-12，真实 MinerU 4.0.12 dev 容器，standard 档，GPU）

**夹具**：最小手工 PDF（1 页：2 段 ASCII 文本 + 1 张 200×150 DeviceRGB 纯色图块，91,039 字节；临时脚本已按「用后即清」删除）。

**实测结果**（`POST /v1/file_parse?wait_timeout=600&response_format=json`）：

- 任务/文件状态 `completed`；`content` 仅有 `markdown` 一个键（无独立图片通道）。
- **图片引用形态：`![](data:image/jpeg;base64,<payload>)`**——alt 恒空、整行独占（单行不换行）、前后空行分隔；DeviceRGB 原始位图被 MinerU 重编码为 JPEG。
- **量化**：整份 markdown 14,061 字节，其中 base64 占约 99%（200×150 小图即如此；真实照片图按比例放大，数百 KB 级）。
- 解析耗时 140.6s（standard 档真实解析）。
- `output_files` 显示 `zip` 通道存在（本次 null，未请求）——**阶段 2 拿图片字节的候选路径**（zip 或 data URI 解码），本点不动适配器。
- 图前/图后文本各自成段（空行分隔），标题 `## …` 正常成为 section——既有切块语义对真实输出的形态假设全部成立。

**实证对决策卡 1 的修正**：拍板时的口径按「MinerU 输出相对路径 `![](images/x.jpg)`」假设设计（台账工程前提即如此记载）；实测是 **data URI**。修正后的口径见 §2.1——识别条件从「相对路径」改为「data:image base64（实证形态）**或**相对路径（兼容形态，防 MinerU 版本行为差异）」，其余条件与边界不变。**这也意味着问题比台账记载更重**：现状不是「短占位符噪音块」，而是 base64 整段进入块文本（jieba 分词、BGE-M3 嵌入、Qdrant payload、分块预览全被垃圾占据），阶段 1 的收益因此更大。

## 1. 目标与验收

| 来源 | 本点覆盖 |
|---|---|
| TD-310 阶段 1「空占位符过滤/保底（不单独成块）」 | 整行空占位符在 `split_into_blocks` 丢弃（段界语义），占位符不再成为任何块；含图 PDF 的块预算与嵌入文本不再被 base64 占据 |
| 既有口径不变 | 表格/代码原子块不拆、标题切节、同节段落合并、重叠语义——全部零变化（占位符过滤只新增一个行级分支） |
| 纯图文档边界 | markdown 非空但块集为空 → 既有 `CHUNK_EMPTY` 显式失败（`build_task.py:545`），不需要新代码；用单测钉住 chunker 返回 `[]` |

**验收清单**：单元红→绿（占位符行现状会成块）→ 实现后全绿；集成（fake parser 携带占位符 markdown）构建 READY 后 `kb_chunks` 无占位符文本；业务场景（真实 MinerU 含图 PDF）READY 后分块无 base64/占位符、图前图后段落各自在块中；静态门禁全绿；既有 KB 构建链路回归零漂移。

## 2. 决策卡（2026-11-12 拍板记录 + 实证修正）

### 卡 1 · 占位符识别口径 —— 拍板 D1-A，实证后条件修正（待确认）

**修正后口径（D1-A'）**：同时满足以下三条件的行为「空占位符行」——

1. 整行仅由 markdown 图片语法构成（行首尾允许空白）：`![alt](url)`；
2. **alt 为空或纯空白**（`![]` / `![ ]`，MinerU 实证形态）；
3. url 为 **`data:image/<type>;base64,<payload>`（实证形态）或相对路径（兼容形态：ASCII 路径字符且带扩展名，如 `images/x.jpg`）**。

**不滤（fail-safe 方向：最坏留噪音块，绝不丢语义）**：alt 非空（用户 MD 的 alt 有语义；若 MinerU 某些场景把 caption 放进 alt——未实证——该行保留，安全方向）；url 为 http(s) 外链；`data:` 但非 base64 形态（如 `data:image/svg+xml;utf8,…`）；不占整行的行内占位符（随段落保留，已知边界）。相对路径条件要求 ASCII 字符 + 扩展名，含中文/无扩展名的 `![](…)` 文本不会被误判。

**作用面**：过滤器在 chunker 层统一生效（PDF/MD/TXT 同一个 `split_into_blocks`）——MD 上传的 dangling 相对路径图片同口径（单文件上传本就不带图片资产）。

落选：B「所有 `![…](…)` 都算占位符」（误删用户 alt 语义行/外链内容）；C「改用 content_list/structured_content 结构识别」（要动适配器请求契约，与「阶段 1 只动 chunker 侧」冲突；`output_files.structured_content` 通道实测存在，登记为阶段 2 备选）。

### 卡 2 · 过滤粒度 —— 已拍板 D2-A：占位符行按「空行（段界）」处理

丢弃占位符文本本身，并按空行语义处理段界（复用 `chunker.py:149-153` 既有空行分支）。理由：图片是独立版面元素，若整行跳过（D2-B）会把图前/图后文本合并进同一段落缓冲，跨图内容混段是隐性误伤；MinerU 实证输出占位符前后本有空行，D2-A/D2-B 在真实 PDF 输出上行为一致，差异只出现在紧凑排版的 MD 上——取语义正确的 D2-A。落选 D2-C「保底一行独立块」：与阶段 1 目标正面矛盾（每个图继续产出一个无语义短块）；「图片存在过」的信息保留是阶段 2 图注回填的职责，阶段 2 在 chunker **上游** markdown 层回填（届时占位符行已替换为图注文本，图注不是图片语法行、不会被本过滤器丢弃）——联动已在台账，不冲突。

### 卡 3 · 防误伤与防漂移 —— 已拍板（影响面已机械核验）

- **既有 repo 测试零漂移**（本会话实证）：全仓 app+tests 无任何 `![](…)` 图片语法夹具（grep 零命中）；`expected_chunk_ids` 在 repo tests 零命中（KB-04A 对账测试集是运行时 API 数据，TD-300 口径=不迁移）；唯一 chunker 单测 `test_kb_chunker_unit.py`（6 条）无图片夹具；BS-M3-03 合成 PDF 纯文本无图（`test_bs_m3_03_kb_build.py:97-110`）；全栈链②③上传全是 `.md` 无图片语法；集成断言全是动态口径（`>=2`/`>=6`/`total` 取自 DB），无固定块数。新增过滤对全部既有用例是 no-op。
- **行为变更面（即交付物）**：含图 PDF 的构建结果变化——块数减少、无占位符块。新增用例钉住新行为。
- **部署注意**：chunker 口径变更后**已建版本不回溯重建**（新构建才走新口径）；M3 无生产数据，开发/测试种子无含图测试集，无实际失配面。**TD-294 切块参数实验必须在本点合并后的 chunker 上做**。
- **防误伤细则**（单测钉住）：表格行内图片语法不滤（`_TABLE_LINE_RE` 先于图片分支，原子块口径不变）；代码围栏内不滤（`chunker.py:123-127` 围栏优先）；标题行不滤；行内占位符不滤（卡 1）；外链/非空 alt/非 base64 data URI 不滤。

### 卡 4 · 测试策略与 PR 拆分 —— 已拍板：单 PR、全栈链不动、集成+业务场景各 1 条

纯后端小点，无前端面，不拆 A/B（无 KB-08A/B 式契约）；生产代码预计 `chunker.py` 一个正则 + 一处分支（~15 行）。全栈链②③对 PDF 路径零覆盖、上传全为 `.md`——不动、不跑（纯后端 PR，frontend/evals 按路径过滤不触发，KB-08A 先例）。

## 3. 实现口径（`backend/app/kb/chunker.py`）

### 3.1 识别正则（草案，实现时按单测微调）

```python
# TD-310 阶段 1：整行空图片占位符。MinerU 4.0.12 实证（2026-11-12 真实栈 standard 档，
# 见简报 M3-TD310-phase1-image-placeholder.md §0.2）：![](data:image/jpeg;base64,<payload>)，
# alt 恒空、单行、前后空行分隔；相对路径形态（![](images/x.jpg)）保留兼容。
# http(s) 外链、非空 alt、非 base64 的 data URI 是用户内容，不属占位符（fail-safe 方向）。
_EMPTY_IMAGE_LINE_RE = re.compile(
    r"^\s*!\[\s*\]\(\s*"
    r"(?:data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+"      # MinerU 实证形态
    r"|[A-Za-z0-9_@%./+-]+\.[A-Za-z0-9]+"                        # 相对路径（ASCII + 扩展名）
    r")\s*\)\s*$"
)
```

base64 字符集不含 `)`，括号闭合判定安全；payload 不允许空白（MinerU 单行输出；若未来出现换行包裹会**漏滤**——漏滤方向安全，登记 §6）。

### 3.2 段界分支（落点：`chunker.py:148` `flush_table()` 之后、空行分支之前）

```python
flush_table()
if _EMPTY_IMAGE_LINE_RE.match(line):
    # TD-310 阶段 1：空图片占位符不进切块——按「空行（段界）」处理：占位符无检索
    # 语义，丢弃文本但保留前后段落边界，防止跨图内容合并成同一段。
    if paragraph and paragraph[-1] != "":
        paragraph.append("")
    continue
```

先于空行分支不冲突：占位符行与空行的段界追加逻辑相同；连续多个占位符不残留空标记（`paragraph[-1] != ""` 守卫既有）。

### 3.3 边界行为表

| 输入形态 | 行为 | 依据 |
|---|---|---|
| 整行 `![](data:image/jpeg;base64,…)`（MinerU） | 丢弃 + 段界 | 卡 1/卡 2 |
| 整行 `![](images/x.jpg)`（相对路径） | 丢弃 + 段界 | 卡 1 兼容形态 |
| 整行 `![校园地图](images/campus.png)` | 保留原样 | 卡 1（alt 非空） |
| 整行 `![](https://…)` / `data:image/svg+xml;utf8,…` | 保留原样 | 卡 1（外链/非 base64） |
| 表格行内含图片语法 | 进 table 块原样保留 | 原子块口径不变 |
| 代码围栏内含图片语法 | 进 code 块原样保留 | 围栏优先（`chunker.py:123-127`） |
| 段落行内占位符（不占整行） | 随段落保留 | 卡 1 已知边界 |
| 纯图文档（markdown 仅占位符） | 块集为空 → 构建失败 `CHUNK_EMPTY` | `build_task.py:545` 既有漏斗 |

## 4. 实现顺序（TDD 红→绿）

1. **单元先红**（`backend/tests/test_kb_chunker_unit.py`，现有 6 条 → +10）：
   - `test_mineru_data_uri_placeholder_line_is_dropped`：图前/占位符/图后（空行分隔）→ 无块含 `data:image`/`![`，图前图后文本各自在块中；
   - `test_relative_path_placeholder_line_is_dropped`：`![](images/x.jpg)` 同上；
   - `test_placeholder_line_acts_as_paragraph_break`：文本/占位符/文本**无空行**分隔 → 块文本以 `\n\n` 分段（段界语义，D2-A 的核心断言）；
   - `test_consecutive_placeholders_leave_no_empty_blocks`：连续多个占位符 → 无空块、块数正确；
   - `test_placeholder_only_document_yields_no_chunks`：`chunk_markdown` 返回 `[]`（纯图文档 CHUNK_EMPTY 前提）；
   - `test_nonempty_alt_image_line_is_kept`：`![校园地图](images/campus.png)` 保留为段落文本；
   - `test_external_url_image_line_is_kept`：`![](https://example.com/x.png)` 保留；
   - `test_non_base64_data_uri_is_kept`：`data:image/svg+xml;utf8,…` 保留；
   - `test_image_syntax_inside_table_and_code_fence_untouched`：表格行/围栏行内图片语法原样进原子块；
   - `test_inline_placeholder_in_paragraph_is_kept`：`见下图 ![](data:image/png;base64,AAAA) 所示` 随段落保留。
2. **实现**：§3.1 正则 + §3.2 分支（单点推进，`app/` 仅 chunker.py 一文件）。
3. **集成**（`backend/tests/integration/test_kb_build_worker.py` +1，fake parser 带 placeholder markdown——先红）：构建 READY 后 `kb_chunks` 文本零占位符（`data:image`/`![](` 零命中）、父块/子块计数与无占位符口径一致。
4. **业务场景**（`backend/tests/business/test_bs_m3_03b_image_placeholder.py` 新文件，真实 MinerU 本地 dev 栈、CI skip 沿 TD-295 先例）：含图 PDF（生成器函数内嵌测试文件，BS-M3-03 `SMOKE_PDF` 先例：手工 PDF + 200×150 图块）→ 上传→审批→构建 READY → 断言 `kb_chunks` 无 `data:image`/`![](`、图前/图后标记文本各自出现在某块中、job READY 与 Qdrant 点数>0；纯图 PDF（只含图）→ 构建失败 `CHUNK_EMPTY`。
5. **静态门禁**：ruff（app/tests/scripts/alembic）/ mypy --no-incremental / bandit×2 / `alembic check`（无新迁移零漂移）；受影响回归 = chunker 单测全量 + build/chunks 集成子集。

## 5. 测试策略

| 层 | 范围 | 红→绿 |
|---|---|---|
| 单元 | `test_kb_chunker_unit.py` +10（§4.1 清单） | 核心语义 5 条先红；防误伤 5 条为守卫（实现前即绿，钉住不回归） |
| 集成 | `test_kb_build_worker.py` +1（fake parser 携带占位符 markdown） | 先红（现状占位符进块） |
| 业务场景 | `test_bs_m3_03b_image_placeholder.py` 新 1 文件 2 例（含图→干净块；纯图→CHUNK_EMPTY） | 实现后绿（真实栈本地跑，CI skip） |
| 回归 | chunker 单测全量 6 条 + build/chunks 集成子集（fake ports 链路） | 零漂移背书 |
| 全栈 E2E | 不动不跑（上传全 .md、零 PDF 路径覆盖；纯后端 PR 路径过滤先例） | — |
| 性能 | 不做（正则对单行为线性扫描；base64 大行实测可匹配） | — |

**审查对照清单**：识别正则与实证形态一致（§0.2）；fail-safe 方向（漏滤不丢内容）；段界语义（D2-A）；表格/围栏/标题优先级不被分支破坏；行为变更面与部署注意登记；简报对照（未登记偏离按 Major）。

## 6. 预判技术债（实现时登记 `docs/tech-debt.md`）

| TD | 条目 | 处置 |
|---|---|---|
| TD-310 | 图片无专门处理路径（两阶段） | 阶段 1 随本点处置改「已完成（阶段 1）」；阶段 2 维持「M4 或 M3 边界评审拍板独立点」不变，台账补记第 0 步实证形态（data URI）与 `output_files.zip` 通道，供阶段 2 选型 |
| 无新增 | 占位符过滤无构建面可观测性（过滤了多少个/多少字节） | **已延后**：阶段 2 图片标注子阶段（M4 或边界评审）一并设计图注可观测性；当前无消费者（构建事件/快照 UI 均不展示），单加计数属无价值面 |
| 无新增 | base64 换行包裹 / caption-in-alt 两项未实证边界 | 不登记独立 TD：两者 fail-safe 方向（漏滤保内容），随阶段 2 实证；简报 §6 留痕即可 |

## 7. 高风险确认

本点**不属高风险类**（不碰认证/租户隔离/权限/状态机/删除/发布/加密/AI 护栏；无新迁移/新码/审计面变化；纯函数层小改动）。风险集中在：(a) 识别口径与真实 MinerU 形态的一致性——已由第 0 步实证钉住（§0.2）；(b) chunker 行为变更的回归面——已机械核验零夹具漂移（卡 3）。决策卡 1-4 已于 2026-11-12 经用户拍板（全按推荐）；卡 1 的实证修正（相对路径 → data URI 为主 + 相对路径兼容）随本简报请用户确认后编码。
