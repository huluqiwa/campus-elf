"""KB-06A 分块预览读侧（只读）：列表/详情/统计 + LIKE 转义与游标纯函数。

数据访问纪律（简报 `docs/design-briefs/M3-KB-06.md` 卡 7，均为固定口径）：

- 版本解析走 scope 过滤仓储（`KbDocumentVersion`，`owner_attr="created_by"`）——
  不可见/跨租/不存在一律 404，与 KB-01A 文档读语义、KB-05 的 404 对齐口径一致；
- `kb_chunks` 无归属列，chunk 查询走 `TenantScopedRepository + ScopeBypass`
  （版本已按数据范围校验，分块按版本归属访问；KB-05 第 3 轮审查确认该机制
  不构成范围旁路——前提是版本/文档读取仍走 scope 过滤仓储）；
- 共享 `TenantScopedRepository.list_cursor` 硬编码 `(created_at DESC, id DESC)`
  次序，分块按 `(chunk_index ASC, id ASC)` 内联 keyset（**不动共享分页函数**），
  游标格式独立、解码失败沿用 `CursorError` → `INVALID_CURSOR` 400 语义；
- 端点只挂 `school.kb.view_chunks`（spec §KB-06 明文权限）。
"""

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.kb import KbChunk, KbDocumentVersion
from app.db.repository import CursorError, ScopeBypass, TenantScopedRepository
from app.db.session import db_session
from app.kb.service import KbError
from app.rbac.scopes import DataScopeContext

# ---------------------------------------------------------------------------
# 纯函数：ILIKE 转义 / 搜索词规范化 / 游标编解码
# ---------------------------------------------------------------------------


def escape_like(value: str) -> str:
    """转义 LIKE/ILIKE 通配符（`\\` 必须最先翻倍，否则用户输入的转义符泄漏）。"""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def normalize_q(q: str | None) -> str | None:
    """搜索词规范化：strip 后为空按「未提供」处理（词内空白是有效语义）。"""

    if q is None:
        return None
    stripped = q.strip()
    return stripped or None


def encode_chunk_cursor(chunk_index: int, chunk_id: UUID) -> str:
    """把分块游标编码为不透明字符串（base64url(JSON)），形状 `{"i": 索引, "d": 块id}`。"""

    payload = json.dumps({"i": chunk_index, "d": str(chunk_id)}).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def decode_chunk_cursor(cursor: str) -> tuple[int, UUID]:
    """解码分块游标为 `(chunk_index, chunk_id)`。

    异常:
        CursorError: 游标不是合法 base64/JSON、字段类型不对（bool 按 TD-231
            教训按类型拒）、索引为负或块 id 非法——由路由层映射 400，
            绝不能当成「第一页」静默吞掉（TD-243 口径）。
    """

    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        index = payload["i"]
        chunk_id = UUID(str(payload["d"]))
    except Exception as exc:
        raise CursorError(f"无法解码分块游标：{cursor[:32]}…") from exc
    if type(index) is not int or index < 0:
        # bool 是 int 子类，`True == 1` 会蒙混等值断言，必须按类型拒
        raise CursorError(f"分块游标索引非法：{cursor[:32]}…")
    return index, chunk_id


# ---------------------------------------------------------------------------
# 视图模型（读侧 dataclass；接口层再转 pydantic）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChunkView:
    """分块列表/详情行（KB-10A：is_human_edited 三列透出，spec :328）。"""

    id: UUID
    chunk_type: str
    chunk_index: int
    parent_id: UUID | None
    text: str
    text_markdown: str
    section_path: str | None
    page_start: int | None
    page_end: int | None
    token_count: int
    char_count: int
    embedding_status: str
    created_at: datetime
    is_human_edited: bool
    edited_by: UUID | None
    edited_at: datetime | None


@dataclass(frozen=True, slots=True)
class ChunkRef:
    """父/子引用（详情家族导航用）。"""

    id: UUID
    chunk_index: int


@dataclass(frozen=True, slots=True)
class ChunkDetailView:
    """分块详情：行字段 + 版本归属 + 父/子引用。"""

    chunk: ChunkView
    document_version_id: UUID
    parent: ChunkRef | None
    children: list[ChunkRef]


@dataclass(frozen=True, slots=True)
class SectionStat:
    """章节统计行（按 chunk_count 降序、路径升序稳定排列）。"""

    section_path: str
    chunk_count: int


@dataclass(frozen=True, slots=True)
class ChunkStats:
    """版本分块统计（per-version 固定口径，不随筛选参数变化）。"""

    total_chunks: int
    parent_count: int
    child_count: int
    embedding_status: dict[str, int]
    total_tokens: int
    total_chars: int
    section_count: int
    sections: list[SectionStat]


@dataclass(frozen=True, slots=True)
class ChunkPage:
    """分块列表页（游标分页信封）。"""

    items: list[ChunkView]
    next_cursor: str | None


# ---------------------------------------------------------------------------
# 仓储与版本可见性
# ---------------------------------------------------------------------------

_CHUNK_BYPASS = ScopeBypass(reason="分块无归属列：版本已按数据范围校验，分块按版本归属访问")


def _version_repository(
    session: AsyncSession, tenant_id: UUID, scope: DataScopeContext
) -> TenantScopedRepository[KbDocumentVersion]:
    return TenantScopedRepository(
        session, KbDocumentVersion, tenant_id, scope=scope, owner_attr="created_by"
    )


def _chunk_repository(tenant_id: UUID, session: AsyncSession) -> TenantScopedRepository[KbChunk]:
    return TenantScopedRepository(session, KbChunk, tenant_id, scope=_CHUNK_BYPASS)


async def _require_visible_version(
    session: AsyncSession, tenant_id: UUID, scope: DataScopeContext, version_id: UUID
) -> KbDocumentVersion:
    """版本必须对当前主体可见（scope 过滤）；不可见/跨租/不存在统一 404。"""

    version = await _version_repository(session, tenant_id, scope).get(version_id)
    if version is None:
        raise KbError("VERSION_NOT_FOUND", "文档版本不存在", 404)
    return version


def _chunk_view(chunk: KbChunk) -> ChunkView:
    return ChunkView(
        id=chunk.id,
        chunk_type=chunk.chunk_type,
        chunk_index=chunk.chunk_index,
        parent_id=chunk.parent_id,
        text=chunk.text,
        text_markdown=chunk.text_markdown,
        section_path=chunk.section_path,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        token_count=chunk.token_count,
        char_count=chunk.char_count,
        embedding_status=chunk.embedding_status,
        created_at=chunk.created_at,
        is_human_edited=chunk.is_human_edited,
        edited_by=chunk.edited_by,
        edited_at=chunk.edited_at,
    )


# ---------------------------------------------------------------------------
# 列表（keyset 分页 + 四筛选）
# ---------------------------------------------------------------------------


async def list_chunks(
    *,
    tenant_id: UUID,
    scope: DataScopeContext,
    version_id: UUID,
    limit: int = 20,
    cursor: str | None = None,
    q: str | None = None,
    section_path: str | None = None,
    chunk_type: str | None = None,
    parent_id: UUID | None = None,
) -> ChunkPage:
    """按版本列出分块（`chunk_index ASC, id ASC` 游标分页）。

    筛选全部在游标推进**之前**（与共享 `list_cursor` 同语义：先过滤后分页，
    否则游标跳过被过滤的行）；`q` 走 ILIKE contains（父/子块都参与匹配），
    `section_path` 精确匹配（简报卡 4）。

    异常:
        KbError: 版本不可见（`VERSION_NOT_FOUND`，404）或游标非法
            （`INVALID_CURSOR`，400）。
    """

    page_size = max(1, limit)
    async with db_session(tenant_id=tenant_id) as session:
        version = await _require_visible_version(session, tenant_id, scope, version_id)
        repository = _chunk_repository(tenant_id, session)
        stmt = repository.base_query().where(KbChunk.document_version_id == version.id)
        term = normalize_q(q)
        if term is not None:
            stmt = stmt.where(KbChunk.text.ilike(f"%{escape_like(term)}%"))
        if section_path is not None:
            stmt = stmt.where(KbChunk.section_path == section_path)
        if chunk_type is not None:
            stmt = stmt.where(KbChunk.chunk_type == chunk_type)
        if parent_id is not None:
            stmt = stmt.where(KbChunk.parent_id == parent_id)
        stmt = stmt.order_by(KbChunk.chunk_index.asc(), KbChunk.id.asc())
        marker: tuple[int, UUID] | None = None
        if cursor is not None:
            try:
                marker = decode_chunk_cursor(cursor)
            except CursorError as exc:
                raise KbError("INVALID_CURSOR", "分页游标无效", 400) from exc
        if marker is not None:
            marker_index, marker_id = marker
            stmt = stmt.where(
                tuple_(KbChunk.chunk_index, KbChunk.id) > tuple_(marker_index, marker_id)
            )
        rows = list((await session.execute(stmt.limit(page_size))).scalars().all())
        next_cursor = (
            encode_chunk_cursor(rows[-1].chunk_index, rows[-1].id)
            if len(rows) == page_size
            else None
        )
    return ChunkPage(items=[_chunk_view(row) for row in rows], next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# 详情（父/子引用）
# ---------------------------------------------------------------------------


async def get_chunk(
    *, tenant_id: UUID, scope: DataScopeContext, chunk_id: UUID
) -> ChunkDetailView:
    """按 id 取分块详情（含父/子引用）。

    404 链（卡 7-4）：chunk 租户谓词 get 不到 → 404；其版本经 scope 过滤
    仓储复核不可见 → 404。两态同码同文，不泄露存在性。

    异常:
        KbError: `CHUNK_NOT_FOUND`，404。
    """

    async with db_session(tenant_id=tenant_id) as session:
        repository = _chunk_repository(tenant_id, session)
        chunk = await repository.get(chunk_id)
        if chunk is None:
            raise KbError("CHUNK_NOT_FOUND", "分块不存在", 404)
        version = await _version_repository(session, tenant_id, scope).get(
            chunk.document_version_id
        )
        if version is None:
            raise KbError("CHUNK_NOT_FOUND", "分块不存在", 404)

        children_rows = list(
            (
                await session.execute(
                    repository.base_query()
                    .where(
                        KbChunk.document_version_id == chunk.document_version_id,
                        KbChunk.parent_id == chunk.id,
                    )
                    .order_by(KbChunk.chunk_index.asc(), KbChunk.id.asc())
                )
            )
            .scalars()
            .all()
        )
        parent_ref: ChunkRef | None = None
        if chunk.parent_id is not None:
            parent_row = await repository.get(chunk.parent_id)
            # FK 只约束同租不约束同版本；跨版本（理论脏数据）按「无父」呈现，不伪造引用
            if parent_row is not None and parent_row.document_version_id == chunk.document_version_id:
                parent_ref = ChunkRef(id=parent_row.id, chunk_index=parent_row.chunk_index)
    return ChunkDetailView(
        chunk=_chunk_view(chunk),
        document_version_id=chunk.document_version_id,
        parent=parent_ref,
        children=[
            ChunkRef(id=row.id, chunk_index=row.chunk_index) for row in children_rows
        ],
    )


# ---------------------------------------------------------------------------
# 统计（单条分组聚合 + Python 汇总）
# ---------------------------------------------------------------------------

_EMBEDDING_STATUSES = ("pending", "ready", "failed")


async def chunk_stats(
    *, tenant_id: UUID, scope: DataScopeContext, version_id: UUID
) -> ChunkStats:
    """版本分块统计（per-version 固定口径；统计不接筛选参数，简报 §3 显式化）。

    异常:
        KbError: 版本不可见（`VERSION_NOT_FOUND`，404）。
    """

    async with db_session(tenant_id=tenant_id) as session:
        version = await _require_visible_version(session, tenant_id, scope, version_id)
        repository = _chunk_repository(tenant_id, session)
        subquery = (
            repository.base_query()
            .where(KbChunk.document_version_id == version.id)
            .subquery()
        )
        rows = list(
            (
                await session.execute(
                    select(
                        subquery.c.chunk_type,
                        subquery.c.embedding_status,
                        subquery.c.section_path,
                        func.count().label("n"),
                        func.coalesce(func.sum(subquery.c.token_count), 0).label("tokens"),
                        func.coalesce(func.sum(subquery.c.char_count), 0).label("chars"),
                    ).group_by(
                        subquery.c.chunk_type,
                        subquery.c.embedding_status,
                        subquery.c.section_path,
                    )
                )
            )
            .mappings()
            .all()
        )

    total = 0
    parent_count = 0
    child_count = 0
    total_tokens = 0
    total_chars = 0
    embedding: dict[str, int] = dict.fromkeys(_EMBEDDING_STATUSES, 0)
    section_counts: dict[str, int] = {}
    for row in rows:
        count = int(row["n"])
        total += count
        total_tokens += int(row["tokens"])
        total_chars += int(row["chars"])
        if row["chunk_type"] == "parent":
            parent_count += count
        else:
            child_count += count
        status = str(row["embedding_status"])
        if status in embedding:
            embedding[status] += count
        if row["section_path"] is not None:
            key = str(row["section_path"])
            section_counts[key] = section_counts.get(key, 0) + count
    sections = [
        SectionStat(section_path=path, chunk_count=count)
        for path, count in sorted(section_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return ChunkStats(
        total_chunks=total,
        parent_count=parent_count,
        child_count=child_count,
        embedding_status=embedding,
        total_tokens=total_tokens,
        total_chars=total_chars,
        section_count=len(sections),
        sections=sections,
    )
