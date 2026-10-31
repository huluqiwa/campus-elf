"""共享分页信封（M3 起新接口使用；既有模块暂不迁移，TD-241）。

顺序口径与 `TenantScopedRepository.list_cursor` 一致：`created_at DESC, id DESC`。
"""

from pydantic import BaseModel


class PageResponse[ItemT](BaseModel):
    """游标分页响应：本页条目 + 下一页游标（None 表示到底）。"""

    items: list[ItemT]
    next_cursor: str | None = None
