"""M1 验收（TD-110）全栈 E2E 种子数据。

前置：scripts/e2e-stack.sh up 已完成（依赖栈 healthy、迁移到 head、backend 容器就绪，
宿主端口见 E2E_BASE_URL，默认 18000）。

造数策略（能走真实链路的走真实链路）：
- 平台账号不开放注册 → 复用业务测试支撑 `seed_platform_user` 直写（管理连接），
  登录仍走真实 `/api/v1/auth/login`；
- 租户走**真实 HTTP 注册 → 平台审核 → 自动开通**链路（与 BS-M1-B 同一链路），
  超管账号与初始密码由开通流程产生，初始密码从 MailHog 真实邮件提取；
- 校方运营管理员/教师/学生复用 `seed_tenant_user`（真实角色链 user_roles→role_permissions）；
- M2 收尾（TD-192）专用：角色操作员（role.manage + mfa.reset）、绑定内置 school_admin 的
  权限目标、已绑定 TOTP 的 MFA 目标；TOTP secret 用 E2E_ENCRYPTION_KEK 加密入库，
  明文只写 git 忽略的账号文件；
- KB-01C 全栈专用：上传+查看（全校）与仅上传（本人）各一个，随机口令只写账号文件；
- 全部账号做登录烟测，失败即中止（fail fast，避免 E2E 拿着坏数据假失败）。

输出：deploy/.e2e-accounts.json（git 忽略）供 frontend/e2e-fullstack 用例读取。
"""

import asyncio
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from tests.support.auth import seed_platform_user, seed_tenant_user  # noqa: E402
from tests.support.db import make_test_engine, upgrade_test_database  # noqa: E402
from tests.support.dsn import assert_test_database_dsn  # noqa: E402
from tests.support.mailhog import (  # noqa: E402
    extract_initial_password,
    message_body,
    wait_for_message,
)

from app.security.encryption import (  # noqa: E402
    decode_kek,
    decrypt_secret,
    encrypt_secret,
    mfa_aad,
)
from app.security.totp import generate_totp_secret  # noqa: E402

API_PREFIX = "/api/v1"

# 测试域固定口令（≥10 字符、≥3 类字符）：只用于本地全栈 E2E，不用于任何真实环境，
# 因此不套用生产口令策略（历史账号沿用了含登录名子串的写法，新增账号不再这样写）。
# 账号文件 git 忽略；口令不入库、不打印、不入日志。
PLATFORM_ACCOUNT = "e2e-ops"
PLATFORM_PASSWORD = "E2E-Platform-Pass-1"
SCHOOL_NAME = "E2E 全栈验收中学"
SCHOOL_ADMIN_EMAIL = "admin@e2e-scenario.edu"
SCHOOL_OPS_LOGIN_ID = "e2e-ops-admin"
SCHOOL_OPS_PASSWORD = "E2E-School-Op-Pass-1"
TEACHER_LOGIN_ID = "e2e-teacher"
TEACHER_EMAIL = "teacher@e2e-scenario.edu"
TEACHER_PASSWORD = "E2E-Teacher-Pass-1"
STUDENT_LOGIN_ID = "e2e-student"
STUDENT_PASSWORD = "E2E-Student-Init-1"
# C 端聊天链（UI1/UI2）专用：不强制改密（链可重复跑）、独立口令（不与首登改密用例共用）。
CAMPUS_CHAT_LOGIN_ID = "e2e-campus-chat"
CAMPUS_CHAT_PASSWORD = "Campus-Elf-ChatD-2026x"
# 重置流程专用账号：忘记密码用例会改掉它的口令，避免与教师/运营账号的登录用例互踩。
RESETTER_LOGIN_ID = "e2e-resetter"
RESETTER_EMAIL = "resetter@e2e-scenario.edu"
RESETTER_PASSWORD = "E2E-Resetter-Pass-1"
# 会话设备管理专用账号：独占使用（设备限额 tenant=3，并行用例不得共用账号登录）。
DEVICES_LOGIN_ID = "e2e-devices"
DEVICES_PASSWORD = "E2E-Devices-Pass-1"
# M2 收尾（TD-192）专用：角色操作员不绑定内置 school_admin（否则改它会自锁），
# 权限目标绑内置 school_admin（撤权/恢复出厂的热更新都要作用在一个真实角色上）；
# MFA 目标已绑定 TOTP，供「管理员重置 → 旧 access 立即 401」链路使用。
ROLE_ADMIN_LOGIN_ID = "e2e-role-admin"
ROLE_ADMIN_PASSWORD = "Campus-Elf-RoleA-2026x"
ROLE_TARGET_LOGIN_ID = "e2e-role-target"
ROLE_TARGET_PASSWORD = "Campus-Elf-RoleB-2026x"
MFA_TARGET_LOGIN_ID = "e2e-mfa-target"
MFA_TARGET_PASSWORD = "Campus-Elf-MfaC-2026x"
# KB-01C 全栈专用：与 TD-192 的角色/账号零交叉（那些用例会撤权/改角色）。
# 口令随机生成、只写账号文件：新增固定串容易触发 gitleaks 的 generic-api-key 误报。
KB_ADMIN_LOGIN_ID = "e2e-kb-admin"
KB_ADMIN_PASSWORD = f"Kb-{secrets.token_urlsafe(18)}"
KB_UPLOADER_LOGIN_ID = "e2e-kb-uploader"
KB_UPLOADER_PASSWORD = f"Kb-{secrets.token_urlsafe(18)}"
# KB-05B（TD-292）全栈专用：审批人与发布人分账号（审批职责分离的真实边界断言：
# uploader 无审批中心入口、publisher 无审批入口/审批 403）。口令同 KB-01C 随机生成。
KB_APPROVER_LOGIN_ID = "e2e-kb-approver"
KB_APPROVER_PASSWORD = f"Kb-{secrets.token_urlsafe(18)}"
KB_PUBLISHER_LOGIN_ID = "e2e-kb-publisher"
KB_PUBLISHER_PASSWORD = f"Kb-{secrets.token_urlsafe(18)}"
# KB-10B 全栈专用：块编辑提交人（chunk.edit）与派生确认人（chunk.confirm + school_super_admin
# 内置角色——derive 双重门禁的真实 user_roles 链路）。口令同 KB-01C 随机生成。
KB_CHUNK_EDITOR_LOGIN_ID = "e2e-kb-chunk-editor"
KB_CHUNK_EDITOR_PASSWORD = f"Kb-{secrets.token_urlsafe(18)}"
KB_CHUNK_CONFIRMER_LOGIN_ID = "e2e-kb-chunk-confirmer"
KB_CHUNK_CONFIRMER_PASSWORD = f"Kb-{secrets.token_urlsafe(18)}"

PDF_BYTES = b"%PDF-1.4\n% e2e seed qualification\n%%EOF\n"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"[e2e-seed] 缺少环境变量 {name}（先运行 scripts/e2e-stack.sh up）")
    return value


def _tighten_accounts_file(path: Path) -> None:
    """账号文件只留当前用户可读写。

    Windows 上 Python 的 chmod 只改只读位，真正生效的是 ACL，故用 icacls 去掉继承并只授当前用户；
    POSIX 上 os.open(0o600) 已生效，这里再补一次 chmod 兜底（失败不静默：提示但继续）。
    """
    if os.name == "nt":
        user = os.environ.get("USERNAME")
        if not user:
            print("[e2e-seed] 警告：无法确定当前用户，账号文件 ACL 未收紧", file=sys.stderr)
            return
        try:
            result = subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError as error:
            print(f"[e2e-seed] 警告：无法执行 icacls 收紧账号文件 ACL：{error}", file=sys.stderr)
            return
        if result.returncode != 0:
            print(
                f"[e2e-seed] 警告：收紧账号文件 ACL 失败（icacls 返回 {result.returncode}）",
                file=sys.stderr,
            )
        return
    try:
        path.chmod(0o600)
    except OSError as error:
        print(f"[e2e-seed] 警告：账号文件 chmod 0600 失败：{error}", file=sys.stderr)


async def _clear_mailhog(api_base: str) -> None:
    """清空 MailHog 历史邮件：库可重置而 MailHog 是长驻容器，避免取到上一次运行的旧邮件。"""

    async with httpx.AsyncClient(timeout=10) as client:
        await client.delete(f"{api_base}/api/v1/messages")


async def _bind_builtin_role(
    engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    role_code: str,
) -> None:
    """挂到开通流程种下的内置角色（真实 user_roles 链路，不另建自定义角色）。"""

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        role_id = await session.scalar(
            text("select id from roles where tenant_id = :tenant_id and code = :code"),
            {"tenant_id": tenant_id, "code": role_code},
        )
        if role_id is None:
            raise SystemExit(f"[e2e-seed] 内置角色 {role_code} 不存在（开通流程未种角色）")
        await session.execute(
            text(
                "insert into user_roles (tenant_id, user_id, role_id) "
                "values (:tenant_id, :user_id, :role_id)"
            ),
            {"tenant_id": tenant_id, "user_id": user_id, "role_id": role_id},
        )


async def _enable_tenant_mfa(
    engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    *,
    secret: str,
    kek: bytes,
) -> None:
    """写入加密后的 TOTP secret；水位留空，E2E 首次登录可直接用当前窗口的码。"""

    ciphertext = encrypt_secret(secret, kek=kek, aad=mfa_aad("tenant", user_id))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "update users set mfa_enabled = true, mfa_secret_enc = :enc, "
                "mfa_last_used_step = null "
                "where tenant_id = :tenant_id and id = :user_id"
            ),
            {"enc": ciphertext, "tenant_id": tenant_id, "user_id": user_id},
        )


async def _assert_mfa_roundtrip(
    engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    *,
    secret: str,
    kek: bytes,
) -> None:
    """读回库里的密文并解密比对：绑定写坏了就在 seed 阶段中止，不留给 E2E 假失败。"""

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        stored = await session.scalar(
            text("select mfa_secret_enc from users where tenant_id = :tenant_id and id = :user_id"),
            {"tenant_id": tenant_id, "user_id": user_id},
        )
    if not stored:
        raise SystemExit(f"[e2e-seed] MFA 目标 {user_id} 的密文未写入")
    decrypted = decrypt_secret(str(stored), kek=kek, aad=mfa_aad("tenant", user_id))
    if decrypted != secret:
        raise SystemExit(f"[e2e-seed] MFA 目标 {user_id} 的密文解密结果不一致")


async def main() -> None:
    # 护栏：TEST_DATABASE_URL 必须指向 *_test 库（防止误伤开发/业务库）。
    assert_test_database_dsn(_require_env("TEST_DATABASE_URL"))
    mailhog_api = _require_env("TEST_MAILHOG_API")
    kek = decode_kek(_require_env("E2E_ENCRYPTION_KEK"))
    base_url = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:18000")

    engine = make_test_engine("admin")
    try:
        await seed_platform_user(
            engine,
            account=PLATFORM_ACCOUNT,
            password=PLATFORM_PASSWORD,
            permissions=(
                "platform.tenant.review",
                "platform.tenant.view",
                "platform.audit.view",
            ),
        )
    finally:
        await engine.dispose()

    await _clear_mailhog(mailhog_api)

    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        # 2) 平台登录（真实认证层）
        response = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"realm": "platform", "account": PLATFORM_ACCOUNT, "password": PLATFORM_PASSWORD},
        )
        if response.status_code != 200:
            raise SystemExit(
                f"[e2e-seed] 平台登录失败: {response.status_code} {response.text[:200]}"
            )
        platform_headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

        # 3) 真实注册 → 审核 → 开通（BS-M1-B 同链路）
        response = await client.post(
            f"{API_PREFIX}/platform/tenants/register",
            data={
                "school_name": SCHOOL_NAME,
                "address": "验收路 110 号",
                "leadership": "E2E 校长",
                "email": SCHOOL_ADMIN_EMAIL,
            },
            files={"qualification": ("qualification.pdf", PDF_BYTES, "application/pdf")},
        )
        if response.status_code != 201:
            raise SystemExit(f"[e2e-seed] 注册失败: {response.status_code} {response.text[:300]}")
        request_id = response.json()["request_id"]

        response = await client.post(
            f"{API_PREFIX}/platform/tenant-requests/{request_id}/review",
            json={"result": "approved", "reason": "E2E 验收开通"},
            headers=platform_headers,
        )
        if response.status_code != 200:
            raise SystemExit(f"[e2e-seed] 审核失败: {response.status_code} {response.text[:300]}")
        review = response.json()
        school_code = review["school_code"]
        assert review["status"] == "approved", review
        assert school_code.startswith("SCH-"), review

        # 4) 从 MailHog 真实邮件提取超管初始密码（首登强制改密用例的输入）
        message = wait_for_message(mailhog_api, to=SCHOOL_ADMIN_EMAIL, timeout=15)
        admin_initial_password = extract_initial_password(message_body(message))

    # 5) 校方运营管理员 / 教师 / 学生（真实角色链；运营管理员看全校、教师仅本人）
    engine = make_test_engine("admin")
    try:
        await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=SCHOOL_OPS_LOGIN_ID,
            password=SCHOOL_OPS_PASSWORD,
            user_type="school_admin",
            # TD-183 链路：建班/学年流转要 org.manage（同时决定「学年流转」菜单可见），
            # 批量导入要 personnel.import；两者都是校方运营岗的真实职责范围。
            permissions=(
                "school.personnel.view",
                "school.org.manage",
                "school.personnel.import",
            ),
            scope_types=("school",),
        )
        await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=TEACHER_LOGIN_ID,
            password=TEACHER_PASSWORD,
            user_type="teacher",
            email=TEACHER_EMAIL,
            permissions=("school.personnel.view",),
            scope_types=("self",),
        )
        # KB-01C：上传+查看（全校范围），验证上传后轮询与抽屉详情。
        await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=KB_ADMIN_LOGIN_ID,
            password=KB_ADMIN_PASSWORD,
            user_type="school_admin",
            permissions=("school.kb.upload", "school.kb.view"),
            scope_types=("school",),
        )
        # 默认教师形态：只有上传、没有查看（TD-278），验证真实 403 与「无查看权限」静态说明。
        await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=KB_UPLOADER_LOGIN_ID,
            password=KB_UPLOADER_PASSWORD,
            user_type="teacher",
            permissions=("school.kb.upload",),
            scope_types=("self",),
        )
        # KB-05B（TD-292）：审批人（审批中心 + 通过/驳回）；发布人（构建/测试/发布/删除）。
        # 职责分离的真实边界：publisher 不给审批码——UI 无审批中心入口，直连接口 403。
        await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=KB_APPROVER_LOGIN_ID,
            password=KB_APPROVER_PASSWORD,
            user_type="school_admin",
            permissions=("school.approval.todo", "school.kb.approval", "school.kb.view"),
            scope_types=("school",),
        )
        await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=KB_PUBLISHER_LOGIN_ID,
            password=KB_PUBLISHER_PASSWORD,
            user_type="school_admin",
            permissions=(
                "school.kb.view",
                "school.kb.build",
                "school.kb.test",
                "school.kb.publish",
                "school.kb.delete",
                # KB-06B：分块预览（全栈链③断言列表/统计与表格保真；构建方角色本就属 KB √ 列）
                "school.kb.view_chunks",
                # KB-09：人群配置/确认（全栈链②③构建前配置+确认 ACL——门禁 fail-closed）
                "school.kb.acl.manage",
            ),
            scope_types=("school",),
        )
        # KB-10B：块编辑提交人 / 派生确认人（码分授；确认人挂 school_super_admin 内置角色）
        _chunk_editor_tenant_id, _chunk_editor_user_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=KB_CHUNK_EDITOR_LOGIN_ID,
            password=KB_CHUNK_EDITOR_PASSWORD,
            user_type="school_admin",
            permissions=(
                "school.kb.view",
                "school.kb.view_chunks",
                "school.kb.chunk.edit",
            ),
            scope_types=("school",),
        )
        chunk_confirmer_tenant_id, chunk_confirmer_user_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=KB_CHUNK_CONFIRMER_LOGIN_ID,
            password=KB_CHUNK_CONFIRMER_PASSWORD,
            user_type="school_admin",
            permissions=(
                "school.kb.view",
                "school.kb.view_chunks",
                "school.kb.chunk.confirm",
            ),
            scope_types=("school",),
        )
        await _bind_builtin_role(
            engine,
            chunk_confirmer_tenant_id,
            chunk_confirmer_user_id,
            "school_super_admin",
        )
        student_tenant_id, student_user_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=STUDENT_LOGIN_ID,
            password=STUDENT_PASSWORD,
            user_type="student",
            must_change_password=True,
        )
        # C 端聊天链（UI1/UI2）需要 campus.kb.qa 等 campus 域码：挂内置 student 角色
        # （§3.3 全量 campus 码），与教师/运营岗同走真实 user_roles 链路。
        await _bind_builtin_role(engine, student_tenant_id, student_user_id, "student")
        # 聊天链专用学生：不强制改密（链可重复跑），同样挂内置 student 角色。
        chat_tenant_id, chat_user_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=CAMPUS_CHAT_LOGIN_ID,
            password=CAMPUS_CHAT_PASSWORD,
            user_type="student",
        )
        await _bind_builtin_role(engine, chat_tenant_id, chat_user_id, "student")
        await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=RESETTER_LOGIN_ID,
            password=RESETTER_PASSWORD,
            user_type="school_admin",
            email=RESETTER_EMAIL,
        )
        await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=DEVICES_LOGIN_ID,
            password=DEVICES_PASSWORD,
            user_type="school_admin",
        )
        await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=ROLE_ADMIN_LOGIN_ID,
            password=ROLE_ADMIN_PASSWORD,
            user_type="school_admin",
            permissions=(
                "school.role.manage",
                "school.personnel.mfa.reset",
                "school.personnel.view",
            ),
            scope_types=("school",),
        )
        role_target_tenant_id, role_target_user_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=ROLE_TARGET_LOGIN_ID,
            password=ROLE_TARGET_PASSWORD,
            user_type="teacher",
            scope_types=("school",),
        )
        await _bind_builtin_role(engine, role_target_tenant_id, role_target_user_id, "school_admin")
        mfa_tenant_id, mfa_user_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=MFA_TARGET_LOGIN_ID,
            password=MFA_TARGET_PASSWORD,
            user_type="teacher",
        )
        mfa_secret = generate_totp_secret()
        await _enable_tenant_mfa(engine, mfa_tenant_id, mfa_user_id, secret=mfa_secret, kek=kek)
        await _assert_mfa_roundtrip(engine, mfa_tenant_id, mfa_user_id, secret=mfa_secret, kek=kek)
    finally:
        await engine.dispose()

    # 6) 登录烟测（fail fast）
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        for payload in (
            {"realm": "platform", "account": PLATFORM_ACCOUNT, "password": PLATFORM_PASSWORD},
            {
                "realm": "tenant",
                "school_code": school_code,
                "login_id": SCHOOL_OPS_LOGIN_ID,
                "password": SCHOOL_OPS_PASSWORD,
            },
            {
                "realm": "tenant",
                "school_code": school_code,
                "login_id": TEACHER_LOGIN_ID,
                "password": TEACHER_PASSWORD,
            },
            {
                "realm": "tenant",
                "school_code": school_code,
                "login_id": KB_ADMIN_LOGIN_ID,
                "password": KB_ADMIN_PASSWORD,
            },
            {
                "realm": "tenant",
                "school_code": school_code,
                "login_id": KB_UPLOADER_LOGIN_ID,
                "password": KB_UPLOADER_PASSWORD,
            },
            {
                "realm": "tenant",
                "school_code": school_code,
                "login_id": KB_APPROVER_LOGIN_ID,
                "password": KB_APPROVER_PASSWORD,
            },
            {
                "realm": "tenant",
                "school_code": school_code,
                "login_id": KB_PUBLISHER_LOGIN_ID,
                "password": KB_PUBLISHER_PASSWORD,
            },
            {
                "realm": "tenant",
                "school_code": school_code,
                "login_id": STUDENT_LOGIN_ID,
                "password": STUDENT_PASSWORD,
            },
            {
                "realm": "tenant",
                "school_code": school_code,
                "login_id": CAMPUS_CHAT_LOGIN_ID,
                "password": CAMPUS_CHAT_PASSWORD,
            },
            {
                "realm": "tenant",
                "school_code": school_code,
                "login_id": ROLE_ADMIN_LOGIN_ID,
                "password": ROLE_ADMIN_PASSWORD,
            },
            {
                "realm": "tenant",
                "school_code": school_code,
                "login_id": ROLE_TARGET_LOGIN_ID,
                "password": ROLE_TARGET_PASSWORD,
            },
        ):
            response = await client.post(f"{API_PREFIX}/auth/login", json=payload)
            if response.status_code != 200:
                raise SystemExit(
                    f"[e2e-seed] 登录烟测失败 {payload.get('account') or payload.get('login_id')}: "
                    f"{response.status_code} {response.text[:200]}"
                )

        # MFA 目标：密码登录必须停在挑战（证明认证层读到了刚写入的绑定）。
        response = await client.post(
            f"{API_PREFIX}/auth/login",
            json={
                "realm": "tenant",
                "school_code": school_code,
                "login_id": MFA_TARGET_LOGIN_ID,
                "password": MFA_TARGET_PASSWORD,
            },
        )
        if response.status_code != 200 or response.json().get("status") != "mfa_required":
            raise SystemExit(
                f"[e2e-seed] MFA 目标登录未进入挑战: {response.status_code} {response.text[:200]}"
            )

    # 7) 写账号文件（git 忽略）
    accounts = {
        "e2e_base_url": base_url,
        "mailhog_api": mailhog_api,
        "school_code": school_code,
        "school_name": SCHOOL_NAME,
        "platform": {"account": PLATFORM_ACCOUNT, "password": PLATFORM_PASSWORD},
        "school_admin_forced": {
            "school_code": school_code,
            "login_id": SCHOOL_ADMIN_EMAIL,
            "initial_password": admin_initial_password,
        },
        "school_ops": {
            "school_code": school_code,
            "login_id": SCHOOL_OPS_LOGIN_ID,
            "password": SCHOOL_OPS_PASSWORD,
        },
        "teacher": {
            "school_code": school_code,
            "login_id": TEACHER_LOGIN_ID,
            "email": TEACHER_EMAIL,
            "password": TEACHER_PASSWORD,
        },
        "student": {
            "school_code": school_code,
            "login_id": STUDENT_LOGIN_ID,
            "password": STUDENT_PASSWORD,
        },
        # C 端聊天链（UI1/UI2）：不强制改密，供链⑤反复运行。
        "campus_chat": {
            "school_code": school_code,
            "login_id": CAMPUS_CHAT_LOGIN_ID,
            "password": CAMPUS_CHAT_PASSWORD,
        },
        "resetter": {
            "school_code": school_code,
            "login_id": RESETTER_LOGIN_ID,
            "email": RESETTER_EMAIL,
            "password": RESETTER_PASSWORD,
        },
        "devices": {
            "school_code": school_code,
            "login_id": DEVICES_LOGIN_ID,
            "password": DEVICES_PASSWORD,
        },
        "kb_admin": {
            "school_code": school_code,
            "login_id": KB_ADMIN_LOGIN_ID,
            "password": KB_ADMIN_PASSWORD,
        },
        "kb_uploader": {
            "school_code": school_code,
            "login_id": KB_UPLOADER_LOGIN_ID,
            "password": KB_UPLOADER_PASSWORD,
        },
        "kb_approver": {
            "school_code": school_code,
            "login_id": KB_APPROVER_LOGIN_ID,
            "password": KB_APPROVER_PASSWORD,
        },
        "kb_publisher": {
            "school_code": school_code,
            "login_id": KB_PUBLISHER_LOGIN_ID,
            "password": KB_PUBLISHER_PASSWORD,
        },
        "kb_chunk_editor": {
            "school_code": school_code,
            "login_id": KB_CHUNK_EDITOR_LOGIN_ID,
            "password": KB_CHUNK_EDITOR_PASSWORD,
        },
        "kb_chunk_confirmer": {
            "school_code": school_code,
            "login_id": KB_CHUNK_CONFIRMER_LOGIN_ID,
            "password": KB_CHUNK_CONFIRMER_PASSWORD,
        },
        "role_admin": {
            "school_code": school_code,
            "login_id": ROLE_ADMIN_LOGIN_ID,
            "password": ROLE_ADMIN_PASSWORD,
        },
        "role_target": {
            "school_code": school_code,
            "login_id": ROLE_TARGET_LOGIN_ID,
            "password": ROLE_TARGET_PASSWORD,
        },
        "mfa_target": {
            "school_code": school_code,
            "login_id": MFA_TARGET_LOGIN_ID,
            "password": MFA_TARGET_PASSWORD,
            "totp_secret": mfa_secret,
        },
    }
    accounts_file = Path(
        os.environ.get("E2E_ACCOUNTS_FILE", BACKEND_ROOT.parent / "deploy" / ".e2e-accounts.json")
    )
    # 以 0600 创建再写：POSIX 上避免「按 umask 建文件 → 事后 chmod」之间可被读取的窗口；
    # Windows 的 0o600 不改 ACL，真正的收紧由写后的 icacls 完成。
    serialized = json.dumps(accounts, ensure_ascii=False, indent=2) + "\n"
    fd = os.open(accounts_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
    except OSError:
        os.close(fd)
        raise
    with handle:
        handle.write(serialized)
    _tighten_accounts_file(accounts_file)
    print(f"[e2e-seed] 完成：school_code={school_code}，账号文件={accounts_file}")


if __name__ == "__main__":
    # alembic 的 env.py 自带 asyncio.run：迁移必须在进入事件循环之前完成。
    upgrade_test_database()
    asyncio.run(main())
