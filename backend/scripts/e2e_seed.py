"""M1 验收（TD-110）全栈 E2E 种子数据。

前置：scripts/e2e-stack.sh up 已完成（依赖栈 healthy、迁移到 head、backend 容器 8000 就绪）。

造数策略（能走真实链路的走真实链路）：
- 平台账号不开放注册 → 复用业务测试支撑 `seed_platform_user` 直写（管理连接），
  登录仍走真实 `/api/v1/auth/login`；
- 租户走**真实 HTTP 注册 → 平台审核 → 自动开通**链路（与 BS-M1-B 同一链路），
  超管账号与初始密码由开通流程产生，初始密码从 MailHog 真实邮件提取；
- 校方运营管理员/教师/学生复用 `seed_tenant_user`（真实角色链 user_roles→role_permissions）；
- 全部账号做登录烟测，失败即中止（fail fast，避免 E2E 拿着坏数据假失败）。

输出：deploy/.e2e-accounts.json（git 忽略）供 frontend/e2e-fullstack 用例读取。
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

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

API_PREFIX = "/api/v1"

# 测试域固定口令（≥10 字符、≥3 类字符、不含登录名）；不入库、不入 git，仅进 git 忽略的账号文件。
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
# 重置流程专用账号：忘记密码用例会改掉它的口令，避免与教师/运营账号的登录用例互踩。
RESETTER_LOGIN_ID = "e2e-resetter"
RESETTER_EMAIL = "resetter@e2e-scenario.edu"
RESETTER_PASSWORD = "E2E-Resetter-Pass-1"
# 会话设备管理专用账号：独占使用（设备限额 tenant=3，并行用例不得共用账号登录）。
DEVICES_LOGIN_ID = "e2e-devices"
DEVICES_PASSWORD = "E2E-Devices-Pass-1"

PDF_BYTES = b"%PDF-1.4\n% e2e seed qualification\n%%EOF\n"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"[e2e-seed] 缺少环境变量 {name}（先运行 scripts/e2e-stack.sh up）")
    return value


async def _clear_mailhog(api_base: str) -> None:
    """清空 MailHog 历史邮件：库可重置而 MailHog 是长驻容器，避免取到上一次运行的旧邮件。"""

    async with httpx.AsyncClient(timeout=10) as client:
        await client.delete(f"{api_base}/api/v1/messages")


async def main() -> None:
    # 护栏：TEST_DATABASE_URL 必须指向 *_test 库（防止误伤开发/业务库）。
    assert_test_database_dsn(_require_env("TEST_DATABASE_URL"))
    mailhog_api = _require_env("TEST_MAILHOG_API")
    base_url = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:8000")

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
            raise SystemExit(f"[e2e-seed] 平台登录失败: {response.status_code} {response.text[:200]}")
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
            permissions=("school.personnel.view",),
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
        await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=STUDENT_LOGIN_ID,
            password=STUDENT_PASSWORD,
            user_type="student",
            must_change_password=True,
        )
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
                "login_id": STUDENT_LOGIN_ID,
                "password": STUDENT_PASSWORD,
            },
        ):
            response = await client.post(f"{API_PREFIX}/auth/login", json=payload)
            if response.status_code != 200:
                raise SystemExit(
                    f"[e2e-seed] 登录烟测失败 {payload.get('account') or payload.get('login_id')}: "
                    f"{response.status_code} {response.text[:200]}"
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
    }
    accounts_file = Path(
        os.environ.get("E2E_ACCOUNTS_FILE", BACKEND_ROOT.parent / "deploy" / ".e2e-accounts.json")
    )
    accounts_file.write_text(
        json.dumps(accounts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[e2e-seed] 完成：school_code={school_code}，账号文件={accounts_file}")


if __name__ == "__main__":
    # alembic 的 env.py 自带 asyncio.run：迁移必须在进入事件循环之前完成。
    upgrade_test_database()
    asyncio.run(main())
