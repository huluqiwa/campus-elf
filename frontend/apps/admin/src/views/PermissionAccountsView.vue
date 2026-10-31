<script setup lang="ts">
/**
 * 权限账号页（M2-UI3 TD-187g-C）：列出本校权限账号 → 改角色绑定 / 停用 / 启用 / 重置 MFA。
 *
 * - 后端 `GET /school/permission-accounts` **只有游标分页，没有搜索参数**，行里也不回显登录名
 *   （学号/工号属敏感标识符）→ 本页给两件事：当前页本地过滤 + 按登录名精确定位
 *   （后者要 `school.personnel.mfa.reset`，缺码时那个框不出现，列表主路径不受影响 —— TD-255）；
 * - 「还有没有下一页」只看 `next_cursor`：后端只在**填满一页**时才给游标，
 *   按 `items.length < limit` 猜会把「还有下一页」判成没有；
 * - 抽屉打开时重读一次绑定快照（`roles_version` 必须是新鲜的），写控件的门禁见 `accountGates`；
 * - 后端三个写动作都回 204，被撤销的会话条数只在审计里 → 提示只说「需重新登录」，不说条数；
 * - 加载失败不清空已显示的行（行与页码仍对得上），但必须挂「数据可能不是最新」横幅 + 重试，
 *   重试重发**失败的那一页**——静默保留旧数据会让运营以为某个账号还是旧状态（TD-260）。
 */
import { ApiError, toUserMessage } from "@campus-elf/api-client";
import type {
  BindableRoleView,
  PermissionAccountView,
  UserLookupView,
} from "@campus-elf/api-client";
import { ElMessage } from "element-plus";
import { computed, onMounted, ref } from "vue";

import {
  ACCOUNT_MANAGE_CODE,
  filterAccountsLocally,
  MFA_RESET_CODE,
  statusLabel,
  typeLabel,
} from "../account-logic";
import { rbacApi } from "../api";
import { reportError } from "../error-toast";
import AccountDetailDrawer from "../components/AccountDetailDrawer.vue";
import { useAuthStore } from "../stores/auth";

const PAGE_SIZE = 50;

const auth = useAuthStore();

const accounts = ref<PermissionAccountView[]>([]);
const nextCursor = ref<string | null>(null);
/** 游标栈：`cursorStack[i]` 是第 i 页的入参游标（第 0 页为 null），与人员页同一做法。 */
const cursorStack = ref<(string | null)[]>([null]);
const page = ref(0);
const loading = ref(false);
/** 上次成功加载的时刻（「非最新」横幅用）。 */
const lastLoadedAt = ref<Date | null>(null);
/** 最近一次失败的页码：重试要重发这一页，而不是当前显示的那一页。 */
const failedPage = ref<number | null>(null);

const keyword = ref("");
const catalog = ref<BindableRoleView[]>([]);

const drawerVisible = ref(false);
const activeRow = ref<PermissionAccountView | null>(null);
const lookupTarget = ref<UserLookupView | null>(null);
const lookupLoginId = ref("");
const lookupLoading = ref(false);

const permissions = computed(() => auth.user?.permissions ?? []);
const selfUserId = computed<string | null>(() => auth.user?.userId ?? null);
const canLookup = computed(() => permissions.value.includes(MFA_RESET_CODE));
const visibleRows = computed(() => filterAccountsLocally(accounts.value, keyword.value));

function clockOf(at: Date): string {
  return [at.getHours(), at.getMinutes(), at.getSeconds()]
    .map((part) => String(part).padStart(2, "0"))
    .join(":");
}

/** 失败后保留的旧行必须自报「不是最新」，否则运营会把翻页失败当成空结果。 */
const staleHint = computed<string | null>(() => {
  if (failedPage.value === null) return null;
  const at = lastLoadedAt.value;
  return at ? `数据可能不是最新（上次成功 ${clockOf(at)}）` : "数据可能不是最新（尚未成功加载过）";
});

/** 请求序号：只让最新一次加载写状态，避免先发后到的旧响应覆盖新结果／清掉失败横幅。 */
let loadSeq = 0;

async function loadPage(index: number): Promise<void> {
  const seq = ++loadSeq;
  loading.value = true;
  try {
    const cursor = cursorStack.value[index] ?? null;
    const data = await rbacApi.listPermissionAccounts({
      limit: PAGE_SIZE,
      cursor: cursor ?? undefined,
    });
    if (seq !== loadSeq) return;
    accounts.value = data.items;
    nextCursor.value = data.next_cursor;
    cursorStack.value = cursorStack.value.slice(0, index + 1);
    if (data.next_cursor) cursorStack.value[index + 1] = data.next_cursor;
    page.value = index;
    lastLoadedAt.value = new Date();
    failedPage.value = null;
  } catch (error) {
    if (seq !== loadSeq) return;
    failedPage.value = index;
    reportError(error, "权限账号列表加载失败");
  } finally {
    if (seq === loadSeq) loading.value = false;
  }
}

function retryFailedPage(): void {
  if (failedPage.value !== null) void loadPage(failedPage.value);
}

async function ensureCatalog(): Promise<void> {
  if (catalog.value.length > 0 || !permissions.value.includes(ACCOUNT_MANAGE_CODE)) return;
  try {
    catalog.value = await rbacApi.listBindableRoles();
  } catch (error) {
    reportError(error, "可授权角色清单加载失败");
  }
}

function openRow(row: PermissionAccountView): void {
  activeRow.value = row;
  lookupTarget.value = null;
  drawerVisible.value = true;
  void ensureCatalog();
}

async function lookupAndOpen(): Promise<void> {
  const loginId = lookupLoginId.value.trim();
  if (!loginId) {
    ElMessage.warning("请输入登录名（工号 / 学号）");
    return;
  }
  lookupLoading.value = true;
  try {
    const found = await rbacApi.lookupUser(loginId);
    // 命中行若在当前页里，用列表行（带服务端 `is_super_admin`）而不是定位结果（没有这个字段）
    const row = accounts.value.find((item) => item.id === found.id) ?? null;
    activeRow.value = row;
    lookupTarget.value = row ? null : found;
    drawerVisible.value = true;
    await ensureCatalog();
  } catch (error) {
    // 只有 404 才是"没这个登录名"；5xx 与断网说成"未找到"会把人支去反复改工号，
    // 而不是回头试一次或找运维
    if (error instanceof ApiError && error.status === 404) {
      ElMessage.error(toUserMessage(error, "未找到该登录名对应的账号"));
    } else {
      reportError(error, "定位失败，请稍后重试");
    }
  } finally {
    lookupLoading.value = false;
  }
}

/** 写动作成功后重读当前页：状态、角色码、MFA 标记都以服务端为准，不在本地手改。 */
function onChanged(): void {
  void loadPage(page.value);
}

function statusTagType(status: string): "success" | "warning" | "info" {
  if (status === "active") return "success";
  if (status === "locked") return "warning";
  return "info";
}

onMounted(() => {
  void loadPage(0);
  void ensureCatalog();
});
</script>

<template>
  <div v-loading="loading" class="page">
    <el-card>
      <template #header>
        <div class="card-head">
          <span>权限账号</span>
          <span class="hint">
            只列出教师与校方管理员；列表不显示工号 / 学号（后端不回显敏感标识符）。
          </span>
        </div>
      </template>

      <div class="toolbar">
        <el-input
          v-model="keyword"
          class="filter-input"
          clearable
          placeholder="在本页按姓名或角色码过滤"
          data-testid="account-filter"
        />
        <div v-if="canLookup" class="lookup">
          <el-input
            v-model="lookupLoginId"
            placeholder="按登录名（工号 / 学号）精确定位"
            data-testid="account-lookup-input"
            @keydown.enter="lookupAndOpen"
          />
          <el-button
            :loading="lookupLoading"
            data-testid="account-lookup-submit"
            @click="lookupAndOpen"
          >
            定位
          </el-button>
        </div>
        <el-button data-testid="account-reload" @click="loadPage(page)">刷新</el-button>
      </div>

      <el-alert
        v-if="staleHint"
        class="stale-alert"
        type="warning"
        :closable="false"
        show-icon
        data-testid="account-stale-alert"
      >
        <div class="stale-body">
          <span>{{ staleHint }}</span>
          <el-button size="small" data-testid="account-stale-retry" @click="retryFailedPage">
            重试
          </el-button>
        </div>
      </el-alert>

      <el-table :data="visibleRows" data-testid="account-table" empty-text="没有符合条件的权限账号">
        <el-table-column label="姓名" min-width="140">
          <template #default="{ row }">
            <span>{{ row.display_name ?? "（未填姓名）" }}</span>
            <el-tag
              v-if="row.is_super_admin"
              size="small"
              type="danger"
              data-testid="account-super-tag"
            >
              校方超管
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="120">
          <template #default="{ row }">{{ typeLabel(row.user_type) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="statusTagType(row.status)">{{
              statusLabel(row.status)
            }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="角色" min-width="200">
          <template #default="{ row }">
            <span v-if="row.role_codes.length" class="codes">{{ row.role_codes.join("、") }}</span>
            <span v-else class="codes">（无角色）</span>
          </template>
        </el-table-column>
        <el-table-column label="MFA" width="80">
          <template #default="{ row }">{{ row.mfa_enabled ? "已开启" : "未开启" }}</template>
        </el-table-column>
        <el-table-column label="操作" width="120">
          <template #default="{ row }">
            <el-button
              size="small"
              data-testid="account-manage"
              @click="openRow(row as PermissionAccountView)"
              >管理</el-button
            >
          </template>
        </el-table-column>
      </el-table>

      <div class="pager">
        <el-button data-testid="account-prev" :disabled="page === 0" @click="loadPage(page - 1)">
          上一页
        </el-button>
        <span class="page-info" data-testid="account-page-info">第 {{ page + 1 }} 页</span>
        <el-button data-testid="account-next" :disabled="!nextCursor" @click="loadPage(page + 1)">
          下一页
        </el-button>
      </div>
    </el-card>

    <AccountDetailDrawer
      v-model="drawerVisible"
      :row="activeRow"
      :lookup-target="lookupTarget"
      :catalog="catalog"
      :self-user-id="selfUserId"
      :permissions="permissions"
      @updated="onChanged"
    />
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.card-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
}

.card-head .hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 12px;
}

.toolbar .filter-input {
  width: 260px;
}

.lookup {
  display: flex;
  gap: 8px;
}

.stale-alert {
  margin-bottom: 12px;
}

.stale-body {
  display: flex;
  align-items: center;
  gap: 12px;
}

.codes {
  font-size: 12px;
}

.pager {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 12px;
}

.page-info {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
