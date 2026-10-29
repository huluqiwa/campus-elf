"""访问令牌撤销与会话标记（登出/被踢即时失效）。

- **jti 撤销标记**：登出时把访问令牌 jti 写入 Redis，TTL = 令牌剩余有效期；
- **会话标记**：每个登录家族（`sid` = `family_id`）一条，TTL = 刷新令牌有效期；
  登录/刷新/改密在数据库事务内登记（随提交生效），被踢/下线/登出/改密后删除；
  鉴权时与 jti 撤销合并为一次 MGET。

Redis 故障降级（TD-102）：连接/读写超时短；会话存在性检查失败时**放行**，仍由
`token_version` 与账号状态兜底；标记删除失败由刷新令牌撤销兜底，最长 15 分钟后失效。
"""

import logging
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.config import get_settings

# 键带 realm 与项目前缀，租户/平台互不干扰；TTL 与令牌剩余有效期一致，自动过期无需清理
_KEY_TEMPLATE = "{prefix}:auth:revoked:{realm}:jti:{jti}"
_SESSION_KEY_TEMPLATE = "{prefix}:auth:session:{realm}:{sid}"
_RATE_LIMIT_KEY_TEMPLATE = "{prefix}:auth:ratelimit:{scope}:{identity}"
_MFA_CHALLENGE_KEY_TEMPLATE = "{prefix}:auth:mfa:challenge:{realm}:{jti}"
_MFA_PENDING_KEY_TEMPLATE = "{prefix}:auth:mfa:pending:{realm}:{user_id}"
_Realm = Literal["tenant", "platform"]
_client: aioredis.Redis | None = None
# Redis 异常不得拖垮已鉴权请求：连接/读写超时短且显式设置（TD-102）
_SOCKET_TIMEOUT_SECONDS = 2.0
logger = logging.getLogger(__name__)


class RedisUnavailableError(RuntimeError):
    """MFA 第二步所需的 Redis 状态不可读写。

    会话标记可以 fail-open（由 `token_version` 兜底），但挑战的一次性消费与尝试
    上限没有数据库兜底，因此 MFA 路径遇到 Redis 故障必须 fail-closed（简报 §3.3）。
    """


def _redis() -> aioredis.Redis:
    """返回进程内复用的 Redis 客户端（懒加载）。"""

    global _client
    if _client is None:
        _client = aioredis.from_url(  # type: ignore[no-untyped-call]
            get_settings().redis_url,
            decode_responses=True,
            socket_timeout=_SOCKET_TIMEOUT_SECONDS,
            socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
        )
    return _client


def _key(jti: str, realm: _Realm) -> str:
    """撤销标记的键名（含项目前缀与 realm）。"""

    return _KEY_TEMPLATE.format(prefix=get_settings().redis_prefix, realm=realm, jti=jti)


def _session_key(session_id: UUID | str, realm: _Realm) -> str:
    """会话标记的键名（sid = 刷新令牌家族 id）。"""

    return _SESSION_KEY_TEMPLATE.format(
        prefix=get_settings().redis_prefix, realm=realm, sid=session_id
    )


async def revoke(jti: str, expires_at: datetime | None = None, *, realm: _Realm = "tenant") -> bool:
    """把访问令牌 jti 加入撤销集合（best-effort）。

    Redis 故障时返回 False 并告警：调用方仍须完成数据库侧的家族撤销与审计，
    不能让 Redis 异常把登出变成 500（TD-102 降级语义）。

    参数:
        jti: 访问令牌唯一标识。
        expires_at: 令牌过期时间；缺省按配置的访问令牌 TTL 计算。

    返回值:
        是否写入成功。
    """

    settings = get_settings()
    if expires_at is None:
        ttl = settings.access_token_ttl_seconds
    else:
        ttl = int((expires_at - datetime.now(UTC)).total_seconds())
        ttl = max(ttl, 1)
    try:
        await _redis().set(_key(jti, realm), "1", ex=ttl)
    except RedisError as exc:
        logger.warning("访问令牌撤销写入失败（realm=%s）：%s", realm, exc)
        return False
    return True


async def register_session(
    session_id: UUID, *, realm: _Realm = "tenant", ttl_seconds: int | None = None
) -> None:
    """登记会话标记（登录/刷新/改密时在数据库事务内调用，随提交生效）。

    写入失败只告警：Redis 故障期间鉴权按 fail-open 降级；恢复后该会话需重新登录，
    不会放宽任何数据库侧撤销（刷新令牌仍以数据库为准）。
    """

    ttl = ttl_seconds or get_settings().refresh_token_ttl_seconds
    try:
        await _redis().set(_session_key(session_id, realm), "1", ex=max(int(ttl), 1))
    except RedisError as exc:
        logger.warning("会话标记写入失败（realm=%s）：%s", realm, exc)


async def delete_session(session_id: UUID, *, realm: _Realm = "tenant") -> None:
    """删除会话标记（被踢/下线/登出/改密）；失败只告警，由刷新令牌撤销兜底。"""

    try:
        await _redis().delete(_session_key(session_id, realm))
    except RedisError as exc:
        logger.warning("会话标记删除失败（realm=%s）：%s", realm, exc)


async def delete_sessions(session_ids: list[UUID], *, realm: _Realm = "tenant") -> None:
    """批量删除会话标记（单条失败不影响其余）。"""

    for session_id in session_ids:
        await delete_session(session_id, realm=realm)


async def session_exists(session_id: UUID, *, realm: _Realm = "tenant") -> bool:
    """会话标记是否存在（Redis 故障时按 fail-open 返回 True）。"""

    try:
        return bool(await _redis().exists(_session_key(session_id, realm)))
    except RedisError as exc:
        logger.warning("会话标记查询失败（realm=%s）：%s", realm, exc)
        return True


async def session_ttl(session_id: UUID, *, realm: _Realm = "tenant") -> int:
    """会话标记剩余 TTL（秒）；不存在/故障返回 0。"""

    try:
        ttl = await _redis().ttl(_session_key(session_id, realm))
    except RedisError as exc:
        logger.warning("会话标记 TTL 查询失败（realm=%s）：%s", realm, exc)
        return 0
    return max(int(ttl), 0)


async def filter_active(session_ids: list[UUID], *, realm: _Realm = "tenant") -> set[UUID]:
    """过滤仍存在会话标记的 sid（一次 MGET；Redis 故障时全部保留）。"""

    if not session_ids:
        return set()
    try:
        values = await _redis().mget([_session_key(sid, realm) for sid in session_ids])
    except RedisError as exc:
        logger.warning("会话标记批量查询失败（realm=%s）：%s", realm, exc)
        return set(session_ids)
    return {sid for sid, value in zip(session_ids, values, strict=False) if value is not None}


async def is_active(jti: str, session_id: UUID, *, realm: _Realm = "tenant") -> bool:
    """令牌是否仍有效（jti 未撤销且会话标记存在）。

    一次 MGET 同时检查两个键；Redis 故障时返回 True（fail-open），仍由
    `token_version`/账号状态与刷新令牌撤销兜底。
    """

    try:
        values = await _redis().mget(_key(jti, realm), _session_key(session_id, realm))
    except RedisError as exc:
        logger.warning("Redis 不可用，跳过会话存在性检查（realm=%s）：%s", realm, exc)
        return True
    return values[0] is None and values[1] is not None


def _mfa_challenge_key(jti: str, realm: _Realm) -> str:
    """MFA 挑战状态键（记录尝试次数与是否仍可用）。"""

    return _MFA_CHALLENGE_KEY_TEMPLATE.format(
        prefix=get_settings().redis_prefix, realm=realm, jti=jti
    )


async def register_mfa_challenge(jti: str, *, realm: _Realm, ttl_seconds: int) -> None:
    """登记一次登录挑战（密码校验通过后调用）。

    初始计数为 0：`begin_mfa_challenge_attempt` 先自增再判定，保证允许
    `max_attempts` 次真实尝试（而不是少一次）。

    Redis 故障抛 `RedisUnavailableError`（fail-closed）：挑战状态是尝试上限与
    单次消费的唯一载体，登记失败却仍签发挑战令牌会让后续 verify 只能给出
    误导性的"会话过期"，登录应直接返回 503（与 verify 侧口径一致）。
    """

    try:
        await _redis().set(_mfa_challenge_key(jti, realm), "0", ex=max(int(ttl_seconds), 1))
    except RedisError as exc:
        logger.warning("MFA 挑战登记失败（realm=%s）：%s", realm, exc)
        raise RedisUnavailableError("MFA 挑战状态不可用") from exc


async def begin_mfa_challenge_attempt(
    jti: str, *, realm: _Realm, ttl_seconds: int, max_attempts: int
) -> int | None:
    """开始一次 MFA 校验尝试，返回当前尝试序号；挑战不存在/已用/超限返回 None。

    Redis 故障抛 `RedisUnavailableError`（fail-closed）：挑战的单次消费与尝试上限
    只存在于 Redis，放行等于两者同时失效（简报 §3.3）。
    """

    key = _mfa_challenge_key(jti, realm)
    try:
        async with _redis().pipeline(transaction=True) as pipe:
            pipe.exists(key)
            pipe.incr(key)
            # 保持登录时设定的剩余 TTL；仅在键缺失时补一个窗口
            pipe.expire(key, max(int(ttl_seconds), 1), nx=True)
            exists, count, _ = await pipe.execute()
    except RedisError as exc:
        logger.warning("MFA 挑战状态读取失败（realm=%s），拒绝本次校验：%s", realm, exc)
        raise RedisUnavailableError("MFA 挑战状态不可用") from exc
    if not int(exists):
        return None
    if int(count) > max_attempts:
        return None
    return int(count)


async def consume_mfa_challenge(jti: str, *, realm: _Realm) -> bool:
    """原子消费挑战（GETDEL）；返回该挑战此前是否仍存在，Redis 故障抛 `RedisUnavailableError`。

    必须在签发会话的数据库写入之前调用：这样"校验成功但挑战未消费"的状态不存在，
    同一挑战不可能被二次兑换；若随后的数据库事务失败，用户只需重新登录。
    """

    key = _mfa_challenge_key(jti, realm)
    try:
        consumed = await _redis().getdel(key)
    except RedisError as exc:
        logger.warning("MFA 挑战消费失败（realm=%s）：%s", realm, exc)
        raise RedisUnavailableError("MFA 挑战状态不可用") from exc
    return consumed is not None


def _mfa_pending_key(realm: _Realm, user_id: UUID | str) -> str:
    """待激活 TOTP 密文的键名（按 realm + 用户，同一账号只保留最新一次绑定）。"""

    return _MFA_PENDING_KEY_TEMPLATE.format(
        prefix=get_settings().redis_prefix, realm=realm, user_id=user_id
    )


async def store_mfa_pending_secret(
    ciphertext: str, *, realm: _Realm, user_id: UUID | str, ttl_seconds: int
) -> bool:
    """保存待激活的加密 secret（覆盖旧值）；写入失败返回 False。

    与登录降级不同，绑定流程 fail-closed：写不进 Redis 就无法完成激活，
    应直接告知用户稍后重试，而不是展示一个永远无法激活的 secret。
    """

    try:
        await _redis().set(
            _mfa_pending_key(realm, user_id), ciphertext, ex=max(int(ttl_seconds), 1)
        )
    except RedisError as exc:
        logger.warning("MFA 待激活 secret 写入失败（realm=%s）：%s", realm, exc)
        return False
    return True


async def get_mfa_pending_secret(*, realm: _Realm, user_id: UUID | str) -> str | None:
    """读取待激活的加密 secret；不存在返回 None（绑定过期）。

    Redis 故障抛 `RedisUnavailableError`，由调用方返回 503：不能把"服务不可用"
    说成"绑定已过期"，否则用户会误以为需要重新发起绑定。
    """

    try:
        value: str | None = await _redis().get(_mfa_pending_key(realm, user_id))
        return value
    except RedisError as exc:
        logger.warning("MFA 待激活 secret 读取失败（realm=%s）：%s", realm, exc)
        raise RedisUnavailableError("MFA 待激活密钥不可读") from exc


async def delete_mfa_pending_secret(*, realm: _Realm, user_id: UUID | str) -> None:
    """激活成功或运维重置后删除待激活 secret；失败只告警，由 TTL 兜底。"""

    try:
        await _redis().delete(_mfa_pending_key(realm, user_id))
    except RedisError as exc:
        logger.warning("MFA 待激活 secret 清理失败（realm=%s）：%s", realm, exc)


async def allow_rate_limit(scope: str, identity: str, *, limit: int, window_seconds: int) -> bool:
    """固定窗口限流：窗口内计数未超上限返回 True。

    Redis 故障时放行（fail-open）：限流是防滥用的附加层，不能把登录/重置
    变成 Redis 的单点依赖（TD-102 同一降级语义）。
    """

    key = _RATE_LIMIT_KEY_TEMPLATE.format(
        prefix=get_settings().redis_prefix, scope=scope, identity=identity
    )
    try:
        async with _redis().pipeline(transaction=True) as pipe:
            pipe.incr(key)
            # 只在首次计数时设置 TTL，窗口从第一个请求开始
            pipe.expire(key, max(int(window_seconds), 1), nx=True)
            count, _ = await pipe.execute()
    except RedisError as exc:
        logger.warning("限流计数失败（scope=%s），本次放行：%s", scope, exc)
        return True
    return int(count) <= limit


async def clear_rate_limit(scope: str, identity: str) -> None:
    """删除限流计数（运维/测试解封用，Redis 故障只告警）。"""

    key = _RATE_LIMIT_KEY_TEMPLATE.format(
        prefix=get_settings().redis_prefix, scope=scope, identity=identity
    )
    try:
        await _redis().delete(key)
    except RedisError as exc:
        logger.warning("限流计数清理失败（scope=%s）：%s", scope, exc)


async def reset_client() -> None:
    """关闭并清空客户端（测试/进程退出用）。

    关闭可能撞上"客户端是在**上一个**事件循环里建的、那个循环已经关了"的情况：
    redis-py 关闭连接时会让 transport 在**原循环**上挂一个回调，循环已关就抛
    `RuntimeError: Event loop is closed`。那条连接本来就是死的（循环都关了），
    关不掉也无所谓，所以这里吞掉这一种错误；其它异常照常抛出，不掩盖真问题。
    """

    global _client
    if _client is not None:
        client, _client = _client, None
        try:
            await client.aclose()
        except RuntimeError as exc:
            if "Event loop is closed" not in str(exc):
                raise
