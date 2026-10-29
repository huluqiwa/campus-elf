import { expect, test } from "@playwright/test";

import { mockAuth } from "./helpers/auth-mock";
import { mockRbacApi } from "./helpers/rbac-mock";

// M2-UI3 TD-187g-B 角色页（接口 mock）：新建自定义角色、删除自定义角色、
// 角色级数据范围编辑、以及「恢复出厂」按钮改跟服务端下发的 resettable 走。

const ROLE_MANAGER = ["school.role.manage"];
const ROLE_MANAGER_AND_DELETE = ["school.role.manage", "school.role.delete"];

const CUSTOM_ROLE = {
  id: "40000000-0000-0000-0000-0000000000aa",
  code: "library_keeper",
  name: "图书馆管理员",
  is_system: false,
  editable: true,
  lock_reason: null,
  version: 2,
  defaults_applied: false,
  user_count: 1,
  permission_codes: ["school.kb.view"],
  bindable: true,
  resettable: false,
};

const SECOND_ROLE = {
  ...CUSTOM_ROLE,
  id: "40000000-0000-0000-0000-0000000000cc",
  code: "campus_operator",
  name: "校区运营",
};

/** 含不可转授码（`school.role.manage`）的可编辑角色：非超管复制它必被后端 422。 */
const ROLE_WITH_MANAGE_CODE = {
  ...CUSTOM_ROLE,
  id: "40000000-0000-0000-0000-0000000000dd",
  code: "audit_lead",
  name: "审计组长",
  permission_codes: ["school.kb.view", "school.role.manage"],
};

test("新建角色：编码/名称本地校验拦在前端，提交体带初始权限清单", async ({ page }) => {
  const rbac = await mockRbacApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: ROLE_MANAGER } });

  await page.goto("/settings/roles");
  await page.getByTestId("role-create-open").click();
  await expect(page.getByTestId("role-create-dialog")).toBeVisible();

  // 编码格式不对：本地先拦，不发请求
  await page.getByTestId("role-create-code").fill("Campus-Operator");
  await page.getByTestId("role-create-name").fill("校区运营");
  await expect(page.getByTestId("role-create-code-error")).toContainText("小写字母");
  await expect(page.getByTestId("role-create-submit")).toBeDisabled();

  // 名称全空白：与后端同一判据（TD-198），拦在前端
  await page.getByTestId("role-create-code").fill("campus_operator");
  await page.getByTestId("role-create-name").fill("   ");
  await expect(page.getByTestId("role-create-name-error")).toContainText("全空白");

  await page.getByTestId("role-create-name").fill("校区运营");
  await page.getByTestId("role-create-perm-school.kb.view").click();
  await page.getByTestId("role-create-reason").fill("新设校区运营岗，需要查看知识库");
  await page.getByTestId("role-create-submit").click();

  await expect(page.getByText("已创建角色「校区运营」")).toBeVisible();
  expect(rbac.captures.createRole).toHaveLength(1);
  expect(rbac.captures.createRole[0]).toMatchObject({
    code: "campus_operator",
    name: "校区运营",
    permission_codes: ["school.kb.view"],
    reason: "新设校区运营岗，需要查看知识库",
  });
  // 新角色落进列表并被选中（自建角色没有出厂定义 → 恢复出厂按钮不亮）
  await expect(page.getByTestId("role-campus_operator")).toBeVisible();
  await expect(page.getByTestId("role-reset")).toHaveCount(0);
  await expect(page.getByTestId("role-reset-unavailable")).toContainText("出厂定义");
});

test("新建角色：同码冲突给出可读提示（不是英文报错）", async ({ page }) => {
  await mockRbacApi(page, { roles: [CUSTOM_ROLE] });
  await mockAuth(page, { authenticated: true, user: { permissions: ROLE_MANAGER } });

  await page.goto("/settings/roles");
  await page.getByTestId("role-create-open").click();
  await page.getByTestId("role-create-code").fill("library_keeper");
  await page.getByTestId("role-create-name").fill("图书馆管理员");
  await page.getByTestId("role-create-submit").click();

  await expect(page.getByText("同码角色已存在")).toBeVisible();
});

test("新建角色：初始权限二选一——复制模式只提交 copy_from_role_id", async ({ page }) => {
  const rbac = await mockRbacApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: ROLE_MANAGER } });

  await page.goto("/settings/roles");
  await page.getByTestId("role-create-open").click();
  await page.getByTestId("role-create-code").fill("campus_operator");
  await page.getByTestId("role-create-name").fill("校区运营");
  await page.getByTestId("role-create-source-copy").click();
  await page.getByTestId("role-create-copy-source").click();
  await page.locator(".el-select-dropdown__item").first().click();
  await page.getByTestId("role-create-submit").click();

  await expect(page.getByText("已创建角色「校区运营」")).toBeVisible();
  expect(rbac.captures.createRole).toHaveLength(1);
  const payload = rbac.captures.createRole[0] as Record<string, unknown>;
  expect(payload.copy_from_role_id).toBeTruthy();
  // 两份来源同时给会被后端判成「来源二义」拒掉（ROLE_INITIAL_SOURCE_AMBIGUOUS）
  expect(payload.permission_codes).toBeUndefined();
});

test("新建角色：复制源只列自己授得出去的角色（含不可转授码的不给选）", async ({ page }) => {
  await mockRbacApi(page, { roles: [CUSTOM_ROLE, ROLE_WITH_MANAGE_CODE] });
  await mockAuth(page, {
    authenticated: true,
    user: { permissions: ROLE_MANAGER, roles: ["school_admin"] },
  });

  await page.goto("/settings/roles");
  await page.getByTestId("role-create-open").click();
  await page.getByTestId("role-create-source-copy").click();
  await page.getByTestId("role-create-copy-source").click();

  // 「审计组长」含 school.role.manage，非超管复制它必被后端 `_assert_delegable` 422 → 不给选
  await expect(page.locator(".el-select-dropdown__item")).toHaveCount(1);
  await expect(page.locator(".el-select-dropdown__item").first()).toContainText("图书馆管理员");
});

test("删除角色：没有 school.role.delete 时只给只读影响面；持删除码才能逐字确认执行", async ({
  page,
}) => {
  const rbac = await mockRbacApi(page, { roles: [CUSTOM_ROLE] });
  await mockAuth(page, { authenticated: true, user: { permissions: ROLE_MANAGER } });

  await page.goto("/settings/roles");
  // 只持配置权：入口仍在（藏掉入口就等于把「能预览影响面」也藏了），但只能看影响面
  await expect(page.getByTestId("role-delete-open")).toHaveText("查看删除影响面");
  await page.getByTestId("role-delete-open").click();
  await expect(page.getByTestId("role-delete-readonly")).toContainText("school.role.delete");
  await expect(page.getByTestId("role-delete-preview")).toContainText("影响用户：1 人");
  await expect(page.getByTestId("role-delete-submit")).toHaveCount(0);
  await expect(page.getByTestId("role-delete-code")).toHaveCount(0);
  expect(rbac.captures.deleteRole).toHaveLength(0);

  // 换上删除码：入口变可执行，对话框给逐字确认框
  await mockAuth(page, { authenticated: true, user: { permissions: ROLE_MANAGER_AND_DELETE } });
  await page.reload();
  await expect(page.getByTestId("role-delete-open")).toHaveText("删除角色");
  await page.getByTestId("role-delete-open").click();

  await expect(page.getByTestId("role-delete-preview")).toContainText("影响用户：1 人");
  await expect(page.getByTestId("role-delete-preview")).toContainText("library_keeper");

  // 确认码与理由任一不合格都不提交（确认码要逐字相同：后端与前端都按 trim 后比，
  // 所以"多打一个空格"不算写错——这里用一个真的不一样的编码来测拦截）
  await page.getByTestId("role-delete-code").fill("library-keeper");
  await page.getByTestId("role-delete-reason").fill("岗位撤销，角色不再使用");
  await page.getByTestId("role-delete-submit").click();
  await expect(page.getByText("请输入完整角色编码以确认")).toBeVisible();
  expect(rbac.captures.deleteRole).toHaveLength(0);

  await page.getByTestId("role-delete-code").fill("library_keeper");
  await page.getByTestId("role-delete-reason").fill("短");
  await page.getByTestId("role-delete-submit").click();
  await expect(page.getByText("删除理由至少 10 个字")).toBeVisible();
  expect(rbac.captures.deleteRole).toHaveLength(0);

  await page.getByTestId("role-delete-reason").fill("岗位撤销，角色不再使用");
  await page.getByTestId("role-delete-submit").click();
  await expect(page.getByText("已删除角色「图书馆管理员」")).toBeVisible();
  // TD-199：影响面指纹与版本原样回传
  expect(rbac.captures.deleteRole[0]).toMatchObject({
    confirm_role_code: "library_keeper",
    reason: "岗位撤销，角色不再使用",
    version: 2,
    fingerprint: "fp-40000000-0000-0000-0000-0000000000aa-1",
  });
  await expect(page.getByTestId("role-library_keeper")).toHaveCount(0);
});

test("删除影响面已变化：后端回带最新预览，界面就地刷新让人重新确认", async ({ page }) => {
  const rbac = await mockRbacApi(page, {
    roles: [CUSTOM_ROLE],
    deleteRejectOnce: {
      status: 409,
      body: {
        detail: {
          code: "ROLE_DELETE_PREVIEW_STALE",
          message: "删除影响面已变化（持有人集合与预览不一致），请刷新后重新确认",
          latest_preview: {
            role_code: "library_keeper",
            name: "图书馆管理员",
            version: 3,
            affected_users: 4,
            permission_codes: ["school.kb.view"],
            fingerprint: "fp-new",
          },
        },
      },
    },
  });
  await mockAuth(page, { authenticated: true, user: { permissions: ROLE_MANAGER_AND_DELETE } });

  await page.goto("/settings/roles");
  await page.getByTestId("role-delete-open").click();
  await page.getByTestId("role-delete-code").fill("library_keeper");
  await page.getByTestId("role-delete-reason").fill("岗位撤销，角色不再使用");
  await page.getByTestId("role-delete-submit").click();

  await expect(page.getByText("删除影响面已变化")).toBeVisible();
  // 最新影响面就地替换：人数与版本都变了
  await expect(page.getByTestId("role-delete-preview")).toContainText("影响用户：4 人");
  await expect(page.getByTestId("role-delete-preview")).toContainText("目标版本：3");
  expect(rbac.captures.deleteRole).toHaveLength(1);
});

test("删除时角色被他人改动（版本冲突）：界面重取影响面，重试可成功", async ({ page }) => {
  const rbac = await mockRbacApi(page, {
    roles: [CUSTOM_ROLE],
    deleteVersionConflictOnce: true,
  });
  await mockAuth(page, { authenticated: true, user: { permissions: ROLE_MANAGER_AND_DELETE } });

  await page.goto("/settings/roles");
  await page.getByTestId("role-delete-open").click();
  await expect(page.getByTestId("role-delete-preview")).toContainText("目标版本：2");
  await page.getByTestId("role-delete-code").fill("library_keeper");
  await page.getByTestId("role-delete-reason").fill("岗位撤销，角色不再使用");
  await page.getByTestId("role-delete-submit").click();

  // 版本冲突：不能停在旧预览上（那份 version 已失效，重试必然再 409）
  await expect(page.getByText("已刷新影响面")).toBeVisible();
  await expect(page.getByTestId("role-delete-preview")).toContainText("目标版本：3");
  expect(rbac.captures.deletePreview.length).toBe(2);
  expect(rbac.captures.deleteRole).toHaveLength(1);

  // 重试带上新版本 → 真的删掉
  await page.getByTestId("role-delete-submit").click();
  await expect(page.getByText("已删除角色「图书馆管理员」")).toBeVisible();
  expect(rbac.captures.deleteRole).toHaveLength(2);
  expect(rbac.captures.deleteRole[1]).toMatchObject({ version: 3 });
});

test("数据范围：列出当前范围、可改两类、只读行写清为什么不可改", async ({ page }) => {
  const rbac = await mockRbacApi(page, {
    roles: [CUSTOM_ROLE],
    scopes: {
      library_keeper: [
        { scope_type: "school", scope_id: null, editable: true },
        { scope_type: "class", scope_id: "c-1", editable: false },
      ],
    },
  });
  await mockAuth(page, { authenticated: true, user: { permissions: ROLE_MANAGER } });

  await page.goto("/settings/roles");
  await expect(page.getByTestId("role-scope-current")).toContainText("全校");
  await expect(page.getByTestId("role-scope-readonly")).toContainText("指定班级");

  await page.getByTestId("role-scope-edit").click();
  await expect(page.getByTestId("role-scope-school").locator("input")).toBeChecked();
  // 只读类型没有勾选框——界面不给出"写了也会被拒"的选项
  await expect(page.getByTestId("role-scope-class")).toHaveCount(0);
  await expect(page.getByTestId("role-scope-dialog")).toContainText("保留（不可改）");
  // 只读行不参与范围读写（后端窄删除只清可编辑那两类）
  expect(rbac.captures.scopeList).toHaveLength(1);
});

test("数据范围：切换角色时重读基线（当前范围跟着角色走）", async ({ page }) => {
  await mockRbacApi(page, {
    roles: [CUSTOM_ROLE, SECOND_ROLE],
    scopes: {
      library_keeper: [{ scope_type: "school", scope_id: null, editable: true }],
      campus_operator: [{ scope_type: "self", scope_id: null, editable: true }],
    },
  });
  await mockAuth(page, { authenticated: true, user: { permissions: ROLE_MANAGER } });

  await page.goto("/settings/roles");
  await expect(page.getByTestId("role-scope-current")).toContainText("全校");
  await page.getByTestId("role-campus_operator").click();
  await expect(page.getByTestId("role-scope-current")).toContainText("本人");
});

test("数据范围：预览影响面后提交，PUT 带范围清单与版本", async ({ page }) => {
  const rbac = await mockRbacApi(page, {
    roles: [CUSTOM_ROLE],
    scopes: {
      library_keeper: [{ scope_type: "school", scope_id: null, editable: true }],
    },
  });
  await mockAuth(page, { authenticated: true, user: { permissions: ROLE_MANAGER } });

  await page.goto("/settings/roles");
  await page.getByTestId("role-scope-edit").click();
  await page.getByTestId("role-scope-school").click();
  await page.getByTestId("role-scope-preview-submit").click();

  await expect(page.getByTestId("role-scope-preview")).toContainText("移除范围：school");
  await expect(page.getByTestId("role-scope-empties")).toBeVisible();

  await page.getByTestId("role-scope-reason").fill("该岗位只看自己的资料，收窄数据范围");
  await page.getByTestId("role-scope-submit").click();
  await expect(page.getByText("已更新数据范围")).toBeVisible();

  expect(rbac.captures.scopeSave).toHaveLength(1);
  expect(rbac.captures.scopeSave[0]).toMatchObject({
    scope_types: [],
    version: 2,
    reason: "该岗位只看自己的资料，收窄数据范围",
  });
});

test("数据范围：预览之后又改勾选 → 影响面作废，必须重新预览才能保存", async ({ page }) => {
  await mockRbacApi(page, {
    roles: [CUSTOM_ROLE],
    scopes: {
      library_keeper: [{ scope_type: "school", scope_id: null, editable: true }],
    },
  });
  await mockAuth(page, { authenticated: true, user: { permissions: ROLE_MANAGER } });

  await page.goto("/settings/roles");
  await page.getByTestId("role-scope-edit").click();
  await page.getByTestId("role-scope-preview-submit").click();
  await expect(page.getByTestId("role-scope-preview")).toBeVisible();
  await expect(page.getByTestId("role-scope-submit")).toBeEnabled();

  // 提交的是新清单、界面显示的却是旧影响面——那不叫"确认过"
  await page.getByTestId("role-scope-self").click();
  await expect(page.getByTestId("role-scope-preview")).toHaveCount(0);
  await expect(page.getByTestId("role-scope-need-preview")).toBeVisible();
  await expect(page.getByTestId("role-scope-submit")).toBeDisabled();
});

test("数据范围：还没预览过提示「请先预览」，勾选变了才提示「勾选已改动」", async ({ page }) => {
  await mockRbacApi(page, {
    roles: [CUSTOM_ROLE],
    scopes: {
      library_keeper: [{ scope_type: "school", scope_id: null, editable: true }],
    },
  });
  await mockAuth(page, { authenticated: true, user: { permissions: ROLE_MANAGER } });

  await page.goto("/settings/roles");
  await page.getByTestId("role-scope-edit").click();
  // 刚打开时勾选与基线一致：不能写成"勾选已改动"（评审 m1 的第一处反了）
  await expect(page.getByTestId("role-scope-need-preview")).toContainText("请先预览影响面");

  // 全取消：勾选确实变了（移除了 school），提示必须还在
  // （旧写法按"有没有勾选"判断，这里提示会消失——第二处反了）
  await page.getByTestId("role-scope-school").click();
  await expect(page.getByTestId("role-scope-need-preview")).toContainText("勾选已改动");
});

test("数据范围：权限区有未保存改动时不许提交范围（TD-202 共享版本列）", async ({ page }) => {
  const rbac = await mockRbacApi(page, {
    roles: [CUSTOM_ROLE],
    scopes: {
      library_keeper: [{ scope_type: "school", scope_id: null, editable: true }],
    },
  });
  await mockAuth(page, { authenticated: true, user: { permissions: ROLE_MANAGER } });

  await page.goto("/settings/roles");
  // 权限区先留一处未保存改动
  await page.getByTestId("perm-school.kb.upload").click();
  await expect(page.getByTestId("role-save")).toBeEnabled();

  await page.getByTestId("role-scope-edit").click();
  await expect(page.getByTestId("role-scope-blocked")).toContainText("权限区有未保存改动");
  await expect(page.getByTestId("role-scope-preview-submit")).toBeDisabled();
  expect(rbac.captures.scopeSave).toHaveLength(0);
});

test("数据范围：服务端判定扩大本人可见范围时必须勾确认才能提交", async ({ page }) => {
  const rbac = await mockRbacApi(page, {
    roles: [CUSTOM_ROLE],
    scopes: {
      library_keeper: [{ scope_type: "self", scope_id: null, editable: true }],
    },
    // 操作者就在被改的角色里，且自己当前只有 self → 结果集里的 school 是净增
    actorRoleCodes: ["library_keeper"],
    actorScopeTypes: ["self"],
  });
  await mockAuth(page, {
    authenticated: true,
    user: { permissions: ROLE_MANAGER, roles: ["library_keeper"] },
  });

  await page.goto("/settings/roles");
  await page.getByTestId("role-scope-edit").click();
  // 该角色现在只有 self 一行（服务端不下发 school 行），但"全校"这一类仍要能勾：
  // 后端 PUT 收任意可编辑类型的子集，只按已有行渲染勾选框会让人加不上新的一类。
  await expect(page.getByTestId("role-scope-school")).toBeVisible();
  await page.getByTestId("role-scope-school").click();
  await page.getByTestId("role-scope-preview-submit").click();

  await expect(page.getByTestId("role-scope-self-warn")).toContainText("school");
  await expect(page.getByTestId("role-scope-ack")).toBeVisible();
  // 没勾确认不让提交
  await expect(page.getByTestId("role-scope-submit")).toBeDisabled();

  await page.getByTestId("role-scope-ack").click();
  await page.getByTestId("role-scope-reason").fill("该岗位需要看全校排课信息，扩大数据范围");
  await page.getByTestId("role-scope-submit").click();

  await expect(page.getByText("已更新数据范围")).toBeVisible();
  expect(rbac.captures.scopeSave[0]).toMatchObject({
    scope_types: ["school", "self"],
    self_grant_ack: true,
  });
});

test("数据范围：校方超管免打断（只提示仍写审计），保存不必勾确认", async ({ page }) => {
  const rbac = await mockRbacApi(page, {
    roles: [CUSTOM_ROLE],
    scopes: {
      library_keeper: [{ scope_type: "self", scope_id: null, editable: true }],
    },
    // 超管本人也在该角色里 → 结果集触达本人，但后端对超管免 ack（只写审计）
    actorRoleCodes: ["school_super_admin", "library_keeper"],
    actorScopeTypes: ["self"],
  });
  await mockAuth(page, {
    authenticated: true,
    user: { permissions: ROLE_MANAGER, roles: ["school_super_admin", "library_keeper"] },
  });

  await page.goto("/settings/roles");
  await page.getByTestId("role-scope-edit").click();
  await page.getByTestId("role-scope-school").click();
  await page.getByTestId("role-scope-preview-submit").click();

  await expect(page.getByTestId("role-scope-self-warn")).toContainText("school");
  // 超管：只提示留痕，不逼勾选（界面与后端对同一动作不能有两套预期）
  await expect(page.getByTestId("role-scope-ack")).toHaveCount(0);
  await expect(page.getByTestId("role-scope-self-grant-notice")).toBeVisible();

  await page.getByTestId("role-scope-reason").fill("该岗位需要看全校排课信息，扩大数据范围");
  await page.getByTestId("role-scope-submit").click();
  await expect(page.getByText("已更新数据范围")).toBeVisible();
  expect(rbac.captures.scopeSave[0]).toMatchObject({ self_grant_ack: false });
});

test("数据范围：本地快照落后于库（服务端判自扩权）→ 逼出确认条且草稿不丢", async ({ page }) => {
  const rbac = await mockRbacApi(page, {
    roles: [CUSTOM_ROLE],
    scopes: {
      library_keeper: [{ scope_type: "self", scope_id: null, editable: true }],
    },
    // 操作者不在该角色里 → 预览看不出自扩权（模拟本地快照落后于库）
    actorRoleCodes: [],
    scopeSaveRejectOnce: {
      status: 422,
      body: {
        detail: {
          code: "SELF_GRANT_CONFIRMATION_REQUIRED",
          message:
            "本次修改会扩大自己可见的数据范围：school；请显式确认（self_grant_ack）并填写理由",
        },
      },
    },
  });
  await mockAuth(page, {
    authenticated: true,
    user: { permissions: ROLE_MANAGER, roles: ["library_keeper"] },
  });

  await page.goto("/settings/roles");
  await page.getByTestId("role-scope-edit").click();
  await page.getByTestId("role-scope-school").click();
  await page.getByTestId("role-scope-preview-submit").click();
  await expect(page.getByTestId("role-scope-preview")).toBeVisible();
  // 预览没报自扩权 → 界面没逼勾选
  await expect(page.getByTestId("role-scope-ack")).toHaveCount(0);

  await page.getByTestId("role-scope-reason").fill("该岗位需要看全校排课信息，扩大数据范围");
  await page.getByTestId("role-scope-submit").click();

  // 服务端拒了：确认条被逼出来，已勾的范围与已填的理由都还在（刷新页面会把它们清掉）
  await expect(page.getByTestId("role-scope-self-grant-forced")).toBeVisible();
  await expect(page.getByTestId("role-scope-ack")).toBeVisible();
  await expect(page.getByTestId("role-scope-school").locator("input")).toBeChecked();
  await expect(page.getByTestId("role-scope-reason")).toHaveValue(
    "该岗位需要看全校排课信息，扩大数据范围",
  );

  await page.getByTestId("role-scope-ack").click();
  await page.getByTestId("role-scope-submit").click();
  await expect(page.getByText("已更新数据范围")).toBeVisible();
  expect(rbac.captures.scopeSave).toHaveLength(2);
  expect(rbac.captures.scopeSave[1]).toMatchObject({ self_grant_ack: true });
});

test("数据范围：保存撞版本冲突 → 界面自愈（载入最新版本并提示重做）", async ({ page }) => {
  await mockRbacApi(page, {
    roles: [CUSTOM_ROLE],
    scopes: {
      library_keeper: [{ scope_type: "school", scope_id: null, editable: true }],
    },
    scopeSaveResponse: {
      status: 409,
      body: {
        detail: {
          code: "ROLE_VERSION_CONFLICT",
          message: "角色已被他人修改，请刷新后重试",
          latest: { ...CUSTOM_ROLE, version: 9 },
        },
      },
    },
  });
  await mockAuth(page, { authenticated: true, user: { permissions: ROLE_MANAGER } });

  await page.goto("/settings/roles");
  await page.getByTestId("role-scope-edit").click();
  await page.getByTestId("role-scope-self").click();
  await page.getByTestId("role-scope-preview-submit").click();
  await page.getByTestId("role-scope-reason").fill("该岗位只看自己的资料，收窄数据范围");
  await page.getByTestId("role-scope-submit").click();

  // 不静默覆盖：载入后端回带的最新版本并让人重做
  await expect(page.getByText("已载入最新版本")).toBeVisible();
  await expect(page.getByTestId("role-scope-dialog")).toBeHidden();
  await expect(page.getByTestId("role-scope-current")).toContainText("全校");
});

test("恢复出厂按钮跟服务端下发的 resettable 走：自建角色没有出厂定义时不亮并给原因", async ({
  page,
}) => {
  // 自定义角色没有出厂定义 → 服务端 resettable=false：按钮必须不亮。
  // 判据跟 resettable 走，不跟 is_system 走（TD-200）——内置且有出厂定义的角色按钮是亮的。
  const rbac = await mockRbacApi(page, {
    roles: [
      {
        ...CUSTOM_ROLE,
        id: "40000000-0000-0000-0000-0000000000bb",
        code: "custom_no_defaults",
        name: "自建但无出厂定义",
        is_system: false,
        resettable: false,
        lock_reason: null,
      },
    ],
  });
  await mockAuth(page, { authenticated: true, user: { permissions: ROLE_MANAGER } });

  await page.goto("/settings/roles");
  await expect(page.getByTestId("role-reset")).toHaveCount(0);
  await expect(page.getByTestId("role-reset-unavailable")).toContainText("出厂定义");
  expect(rbac.captures.resetPreview).toHaveLength(0);
});

test("锁定内置角色：保存区整块不渲染，不可改的原因由只读提示承担", async ({ page }) => {
  // 服务端真实形状：内置且不可编辑 → editable=false、resettable=false、lock_reason 有值。
  // （`is_system=true` + `editable=true` 在服务端不存在：内置角色的 editable 由
  //  EDITABLE_ROLE_CODES 决定，其余一律锁定。）
  await mockRbacApi(page, {
    roles: [
      {
        ...CUSTOM_ROLE,
        id: "40000000-0000-0000-0000-0000000000ee",
        code: "student",
        name: "学生",
        is_system: true,
        editable: false,
        lock_reason: "内置角色，权限与数据范围由系统固定",
        resettable: false,
      },
    ],
  });
  await mockAuth(page, { authenticated: true, user: { permissions: ROLE_MANAGER } });

  await page.goto("/settings/roles");
  await expect(page.getByTestId("role-lock-reason")).toContainText("系统固定");
  // 锁定角色的保存区整块不渲染：不会出现"恢复出厂设置"按钮，也没有重复的不可用原因
  await expect(page.getByTestId("role-reset")).toHaveCount(0);
  await expect(page.getByTestId("role-reset-unavailable")).toHaveCount(0);
});
