"""人群 ACL 测试种子（M3 KB-09A）——直落库配置版本人群（不经 HTTP）。

卡 4 口径（简报 `docs/design-briefs/M3-KB-09.md`）：既有构建/发布链路的种子统一
配 `("all", None)` 已确认——配合检索侧用户恒注入 `all` token（KB-09B），既有
检索/对账断言零漂移；只有 ACL 语义用例显式配具体人群。

直落库走管理连接（TEST_DATABASE_URL 超管，FORCE RLS 旁路同既有种子先例）。
"""

from uuid import UUID

from tests.support.kb_env import UPLOADER, execute, rows

DEFAULT_AUDIENCES: tuple[tuple[str, str | None], ...] = (("all", None),)


async def _user_id_by_login(tenant_id: UUID, login_id: str) -> str:
    result = await rows(
        "select id from users where login_id = :lid and tenant_id = :tid",
        lid=login_id,
        tid=str(tenant_id),
    )
    assert result, f"测试账号未种子化：{login_id}"
    return str(result[0]["id"])


async def configure_audiences(
    tenant_id: UUID,
    version_id: UUID,
    *,
    audiences: tuple[tuple[str, str | None], ...] = DEFAULT_AUDIENCES,
    confirmed: bool = True,
    by_login_id: str = UPLOADER,
) -> None:
    """全量重写版本人群并置确认态（默认 `("all", None)` 已确认）。

    confirmed=False 时只写行集并清确认列（模拟「PUT 后未确认」中间态）。
    """

    operator_id = await _user_id_by_login(tenant_id, by_login_id)
    await execute(
        "delete from kb_document_audiences where version_id = :v", v=str(version_id)
    )
    for audience_type, audience_id in audiences:
        await execute(
            """
            insert into kb_document_audiences (tenant_id, version_id, audience_type,
                                               audience_id, created_by)
            values (:t, :v, :at, :aid, :by)
            """,
            t=str(tenant_id),
            v=str(version_id),
            at=audience_type,
            aid=audience_id,
            by=operator_id,
        )
    if confirmed:
        await execute(
            "update kb_document_versions set acl_confirmed_by = :by, acl_confirmed_at = now() "
            "where id = :v",
            by=operator_id,
            v=str(version_id),
        )
    else:
        await execute(
            "update kb_document_versions set acl_confirmed_by = null, acl_confirmed_at = null "
            "where id = :v",
            v=str(version_id),
        )