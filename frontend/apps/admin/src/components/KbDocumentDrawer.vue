<script setup lang="ts">
/**
 * 知识库文档详情抽屉（M3 KB-01B 简报 D5；KB-02B 增审批入口；KB-05B 增发布/回滚/下线/删除）：
 * 打开即读详情 + 第一页版本；版本列表按 `next_cursor` 追加；文档不可见（404）时提示并让列表刷新。
 *
 * KB-02B 入口（简报 D2-A）：按钮按「文档状态 + 权限码」渲染，**不做服务端状态预判**——
 * 同版本已有待审批单/活跃构建时按钮仍在，点了吃后端 409（服务端是唯一门禁）；
 * 提交/创建成功后在本抽屉会话内记忆并禁用按钮（重开抽屉会复位，届时 409 兜底）；
 * REJECTED 的驳回理由不直显（前端没有按 version 查审批单的端点，TD-291），给审批中心指引。
 *
 * KB-05B 发布动作（简报 M3-KB-05B D2-A/D5-A）：发布只对 TEST_PASSED、下线/删除/回滚只对
 * PUBLISHED；门禁纯函数在 kb-logic（服务端仍是唯一门禁）。活跃版本服真化（KB-08B/TD-305）：
 * activeVersionId 由详情回包 `active_version_id` 初始化（KB-08A 契约），会话内
 * publish/rollback/offline/delete 成功响应就地更新（写动作成功后仍重读详情与版本列表，
 * 不手改本地状态，187g-C 口径）；版本行按 isActiveVersion 挂「当前活跃」tag；刷新/重开
 * 不再丢失——竞态窗口（真值加载后另一端切换过）选到活跃版本仍吃 409 兜底。
 *
 * KB-06B 分块预览（简报 M3-KB-06B 卡 1A/2A/3A/4A）：版本表行「分块」入口 → 抽屉内子视图
 * （按所选版本定域，spec「切换旧版本看该版本分块」）；q 显式搜索（不做按键即搜）、
 * 章节下拉来自 stats.sections（精确匹配）、类型下拉；筛选变化即重置回第一页；
 * 详情子面板纯文本渲染 `text_markdown`（**无 v-html**——文档内容不可信，不渲染即无 XSS 面，
 * TD-307 登记 markdown/虚拟滚动升级路径）；父/子导航走详情端点引用，跨版本引用不渲染。
 *
 * KB-07B 检索测试（简报 M3-KB-07B 卡 1A/2A/3A）：分块子视图内常驻紧凑区（统计摘要与
 * 筛选行之间）——query/top_k/with_rerank 输入 → 结果表逐行展示 dense/sparse（分数 · 该路
 * 名次）、RRF、rerank 分数（applied 才有值）、最终引用标记与「查看分块」复用详情导航；
 * 状态并入 chunksGeneration 代际（切版本/关子视图作废在途响应）；前端只做预检
 * （码点 ≤300/1~20），服务端是唯一门禁；错误 reportError 驻留（与列表 404 口径同源）；
 * rerank 失败后端降级不报错（rerank_status 标注）；纯文本渲染无 v-html。
 */
import { ApiError, toUserMessage } from "@campus-elf/api-client";
import type {
  KbAudienceView,
  KbChunkDetailView,
  KbChunkEditResponse,
  KbChunkStatsView,
  KbChunkView,
  KbDocumentDetailView,
  KbRetrievalTestResult,
  KbVersionApprovalView,
  KbVersionView,
} from "@campus-elf/api-client";
import { ElMessage, ElMessageBox } from "element-plus";
import { computed, ref, watch } from "vue";

import { approvalsApi, kbApi, testApi } from "../api";
import { reportError } from "../error-toast";
import {
  KB_BUILD_CODE,
  KB_UPLOAD_CODE,
  REJECT_REASON_GUIDANCE,
  kbEntryConflictMessage,
} from "../approval-logic";
import {
  KB_AUDIENCE_TYPES,
  KB_PAGE_SIZE,
  aclBannerText,
  aclConfirmedLabel,
  aclManageGate,
  aclVisible as aclVisibleGate,
  audienceEditError,
  audienceTypeLabel,
  chunkConfirmGate,
  chunkEditActionMessage,
  chunkEditError,
  chunkEditGate,
  chunkEditMaxChars,
  CHUNK_EDIT_MIN_CAP,
  CHUNK_EDIT_REASON_MAX_CHARS,
  chunkFilterError,
  chunkTypeLabel,
  chunksGate,
  deleteGate,
  documentStatusMeta,
  embeddingStatusMeta,
  formatBytes,
  formatChunkStats,
  formatEditDiff,
  formatScore,
  isActiveVersion,
  kbPublishActionMessage,
  normalizeChunkQ,
  offlineGate,
  publishGate,
  rerankStatusLabel,
  retrievalQueryError,
  retrievalTopKError,
  rollbackTargets,
  routeScoreText,
  sectionOptions,
  tokenLabel,
  unfilteredGate,
  type KbAudienceEditRow,
  type KbRollbackTarget,
} from "../kb-logic";
import { codePointLength } from "../logic";
import { useAuthStore } from "../stores/auth";
import {
  caseSummaries,
  passRateLabel,
  testRunStatusMeta,
  testSetFileError,
  testSectionGate,
  type KbTestRunDetail,
} from "../test-logic";
import KbScanTag from "./KbScanTag.vue";

const props = defineProps<{
  modelValue: boolean;
  documentId: string | null;
}>();

const emit = defineEmits<{
  "update:modelValue": [visible: boolean];
  /** 文档已不可见：父页面刷新当前页。 */
  gone: [];
}>();

const auth = useAuthStore();
const permissions = computed(() => auth.user?.permissions ?? []);

const approvals = ref<KbVersionApprovalView[]>([]);
const testRun = ref<KbTestRunDetail | null>(null);
const testSetName = ref("");
const testFile = ref<File | null>(null);
const testFileError = ref<string | null>(null);
const uploadingTestSet = ref(false);
const startingTestRun = ref(false);

const detail = ref<KbDocumentDetailView | null>(null);
const loading = ref(false);
const versions = ref<KbVersionView[]>([]);
const versionsLoading = ref(false);
const nextCursor = ref<string | null>(null);
let goneNotified = false;
/** 每次打开/切换文档自增；迟到的响应与 404 一律作废，避免把 A 的数据/结论写进 B。 */
let loadGeneration = 0;

/** 本抽屉会话内已成功提交/已建任务的版本（重开抽屉复位；真正的状态以审批中心/后端为准）。 */
const submittedInSession = ref<Set<string>>(new Set());
const buildCreatedInSession = ref<Set<string>>(new Set());
const submitting = ref(false);
const building = ref(false);

// KB-05B：发布动作状态。activeVersionId 服真化（KB-08B）：详情回包初始化 + 会话内动作响应就地更新。
const publishing = ref(false);
const offlining = ref(false);
const deleting = ref(false);
const rollingBack = ref(false);
const activeVersionId = ref<string | null>(null);

// KB-06B：分块子视图状态（独立 chunksGeneration：子视图有自己的打开/关闭与筛选重置节奏）。
const chunksVisible = computed(() => chunksGate(permissions.value));

// KB-09C：人群配置子视图状态（版本行入口；与分块子视图互斥打开——同一时间只看一个子视图）。
// aclGeneration 与 chunksGeneration 同构（审查 M1）：迟到的 getAudiences/PUT/confirm 响应
// 不得把 A 版本的人群渲染/写进 B 版本（跨版本误写路径）。
const aclEntryVisible = computed(() => aclVisibleGate(permissions.value));
const aclManageVisible = computed(() => aclManageGate(permissions.value));
const aclView = ref<{ versionId: string; versionNo: number } | null>(null);
const aclData = ref<KbAudienceView | null>(null);
const aclLoading = ref(false);
const aclSaving = ref(false);
const aclConfirming = ref(false);
const aclDialogVisible = ref(false);
const aclRows = ref<KbAudienceEditRow[]>([]);
const aclEditError = ref<string | null>(null);
let aclGeneration = 0;
const chunksView = ref<{ versionId: string; versionNo: number } | null>(null);
const chunks = ref<KbChunkView[]>([]);
const chunksLoading = ref(false);
const chunksNextCursor = ref<string | null>(null);
const chunkFilters = ref<{ q: string; sectionPath: string; chunkType: "" | "parent" | "child" }>({
  q: "",
  sectionPath: "",
  chunkType: "",
});
const chunksFilterError = ref<string | null>(null);
const chunksParentFilter = ref<{ id: string; index: number } | null>(null);
const chunkDetail = ref<KbChunkDetailView | null>(null);
const chunkDetailLoading = ref(false);
const chunkStats = ref<KbChunkStatsView | null>(null);
let chunksGeneration = 0;

// KB-07B：检索测试状态（卡 3A——结果作废并入 chunksGeneration 代际，切版本/关子视图/筛选变更
// 都会作废在途响应；loading 存活期用独立「运行序号」——审查 M1：筛选变更 bump 代际但
// 不重置 loading，若 loading 也绑代际会让「运行」按钮永久转圈（Element Plus loading 吞 click）。
const retrievalQuery = ref("");
const retrievalTopK = ref<number>(10);
const retrievalWithRerank = ref(false);
// KB-09C：视角三控件（缺省=操作者视角；ignore_acl 仅 unfiltered 码持有人渲染）。
const retrievalAsUserId = ref("");
const retrievalAsAudiencesText = ref("");
const retrievalIgnoreAcl = ref(false);
const retrievalUnfilteredVisible = computed(() => unfilteredGate(permissions.value));
const retrievalTestLoading = ref(false);
const retrievalTestError = ref<string | null>(null);
const retrievalTestResult = ref<KbRetrievalTestResult | null>(null);
let retrievalRunSeq = 0;

// KB-10B：块编辑与派生（卡 E1-E6；chunk.edit 码 / chunk.confirm 码 + school_super_admin role 双判——
// 服务端仍是唯一门禁；409/422 族回填对话框不关闭，KB-09C 先例；E6 引导零后端改动）。
const chunkEditVisible = computed(() => chunkEditGate(permissions.value));
const chunkConfirmVisible = computed(() =>
  chunkConfirmGate(permissions.value, auth.user?.roles ?? []),
);
const editDialogVisible = ref(false);
const editTarget = ref<KbChunkView | null>(null);
const editContent = ref("");
const editReason = ref("");
const editCapChars = ref(CHUNK_EDIT_MIN_CAP);
const editDialogError = ref<string | null>(null);
const editSubmitting = ref(false);
const chunkEdits = ref<KbChunkEditResponse[]>([]);
const chunkEditsLoading = ref(false);
const openEditDiffIndex = ref<number | null>(null);
const derivingVersionId = ref<string | null>(null);

/** 实时预检（镜像 validate_edit_content 422 族；提交按钮禁用绑定）。 */
const editContentCheck = computed(() =>
  editDialogVisible.value ? chunkEditError(editContent.value, editCapChars.value) : null,
);
/** 理由预检（必填由提交按钮 trim 判断；长度上限镜像后端 max_length=500——复审 m6）。 */
const editReasonCheck = computed(() =>
  editDialogVisible.value && codePointLength(editReason.value.trim()) > CHUNK_EDIT_REASON_MAX_CHARS
    ? `编辑理由超出长度上限（${codePointLength(editReason.value.trim())} > ${CHUNK_EDIT_REASON_MAX_CHARS} 码点）`
    : null,
);

/** 卡 E6（TD-321）：版本已派生（提交记录含 derived_version_id）→ 对话框横幅引导，防静默搁浅。 */
const targetVersionDerived = computed(() =>
  chunkEdits.value.some((row) => row.derived_version_id !== null),
);
const targetDerivedVersionShort = computed(() => {
  const derived = chunkEdits.value.find((row) => row.derived_version_id !== null);
  const id = derived?.derived_version_id ?? null;
  return id === null ? null : id.slice(0, 8);
});

/** diff 摘要（卡 E3：当前块文本 vs 提交 new_content；纯文本 multiset 对比）。 */
const editDiff = computed(() => {
  const index = openEditDiffIndex.value;
  if (index === null) return null;
  const edit = chunkEdits.value[index];
  const chunk = chunks.value.find((row) => row.id === edit?.chunk_id);
  if (edit === undefined || chunk === undefined) return null;
  return formatEditDiff(chunk.text, edit.new_content);
});

const testGate = computed(() =>
  detail.value
    ? testSectionGate(detail.value.status, permissions.value)
    : { visible: false, hint: null },
);
const latestApproval = computed(() => approvals.value[0] ?? null);

const canSubmitEntry = computed(() => {
  const version = detail.value?.current_version;
  return (
    permissions.value.includes(KB_UPLOAD_CODE) &&
    detail.value?.status === "PENDING_APPROVAL" &&
    version !== null &&
    version !== undefined &&
    !submittedInSession.value.has(version.id)
  );
});

const canBuildEntry = computed(() => {
  const version = detail.value?.current_version;
  return (
    permissions.value.includes(KB_BUILD_CODE) &&
    detail.value?.status === "APPROVED" &&
    version !== null &&
    version !== undefined &&
    !buildCreatedInSession.value.has(version.id)
  );
});

// KB-05B 发布动作门禁（kb-logic 纯函数；对 current_version 的自身状态判定，与后端守卫同源）。
const currentVersion = computed(() => detail.value?.current_version ?? null);
const canPublish = computed(() => {
  const version = currentVersion.value;
  return version !== null && publishGate(version.status, version.id, permissions.value);
});
const canOffline = computed(() => {
  const version = currentVersion.value;
  return version !== null && offlineGate(version.status, version.id, permissions.value);
});
const canDelete = computed(() => {
  const version = currentVersion.value;
  return version !== null && deleteGate(version.status, version.id, permissions.value);
});
// 回滚与下线同门禁（PUBLISHED + school.kb.publish）；目标候选见 rollbackTargetList。
const canRollback = computed(() => canOffline.value);
const rollbackTargetList = computed(() => rollbackTargets(versions.value, activeVersionId.value));
const publishRowVisible = computed(
  () => canPublish.value || canOffline.value || canRollback.value || canDelete.value,
);

async function submitApproval(): Promise<void> {
  const version = detail.value?.current_version;
  if (!version) return;
  submitting.value = true;
  try {
    await approvalsApi.submitKbVersion(version.id);
    submittedInSession.value = new Set([...submittedInSession.value, version.id]);
    ElMessage.success("已提交审批，可在「审批中心 → 已办」查看进度或撤销");
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      // 服务端 message 优先（KB_NOT_SUBMITTABLE 覆盖多种不可提交状态，固定译文会在
      // 「并发被驳回后提交」这类窗口误导）。kbEntryConflictMessage 是可读化兜底：
      // detail.message 缺失时 ApiError.message 会是「请求失败（409）」，实际先于映射到达用户
      // （复审 Minor 4 的如实注记；后端与 mock 恒带 message，现网无行为差异）。
      ElMessage.warning(toUserMessage(error, kbEntryConflictMessage(error, "提交审批失败")));
      return;
    }
    reportError(error, "提交审批失败");
  } finally {
    submitting.value = false;
  }
}

async function createBuild(): Promise<void> {
  const version = detail.value?.current_version;
  if (!version) return;
  const confirmed = await ElMessageBox.confirm(
    "确认创建构建任务？同一版本同时只能有一个进行中的构建，创建后不可在本页取消。",
    "创建构建确认",
    { type: "warning" },
  ).catch(() => false);
  if (!confirmed) return;
  building.value = true;
  try {
    const job = await approvalsApi.createKbBuild(version.id);
    buildCreatedInSession.value = new Set([...buildCreatedInSession.value, version.id]);
    ElMessage.success(`构建任务已创建（${job.status}），解析与进度由 KB-03 提供`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      ElMessage.warning(toUserMessage(error, kbEntryConflictMessage(error, "创建构建失败")));
      return;
    }
    reportError(error, "创建构建失败");
  } finally {
    building.value = false;
  }
}

// ---- KB-05B：发布/回滚/下线/删除（服务端是唯一门禁；409 走 kbPublishActionMessage 兜底） ----

/** 动作成功/409 后重读服务端状态（409 常意味着并发状态已变，重读同样有价值）。 */
async function refreshAfterAction(): Promise<void> {
  const id = props.documentId;
  if (id === null) return;
  const generation = loadGeneration;
  await Promise.all([loadDetail(id, generation), loadVersions(id, generation)]);
}

function publishActionError(error: unknown, fallback: string): void {
  if (error instanceof ApiError && error.status === 409) {
    ElMessage.warning(kbPublishActionMessage(error, fallback));
    return;
  }
  reportError(error, fallback);
}

async function publishCurrent(): Promise<void> {
  const version = currentVersion.value;
  if (!version) return;
  const confirmed = await ElMessageBox.confirm(
    `发布 v${version.version_no}？发布后新会话将检索到该版本的内容（已在线的会话不受影响）。`,
    "发布确认",
    { type: "warning" },
  ).catch(() => false);
  if (!confirmed) return;
  publishing.value = true;
  try {
    const result = await kbApi.publishVersion(version.id);
    activeVersionId.value = result.version_id;
    if (!result.alias_synced) {
      ElMessage.warning(`v${result.version_no} 已发布，但 Qdrant alias 同步未完成，请稍后刷新确认`);
    } else if (result.replayed) {
      ElMessage.success(`v${result.version_no} 已是活跃发布（重放：已重建 alias）`);
    } else {
      ElMessage.success(`v${result.version_no} 已发布`);
    }
    await refreshAfterAction();
  } catch (error) {
    publishActionError(error, "发布失败");
    await refreshAfterAction();
  } finally {
    publishing.value = false;
  }
}

async function rollbackTo(target: KbRollbackTarget): Promise<void> {
  const confirmed = await ElMessageBox.confirm(
    `回滚到 v${target.version_no}（${target.filename}）？活跃发布将切换到该版本，原活跃版本保持已发布状态。`,
    "回滚确认",
    { type: "warning" },
  ).catch(() => false);
  if (!confirmed) return;
  rollingBack.value = true;
  try {
    const result = await kbApi.rollbackVersion(target.id);
    activeVersionId.value = result.version_id;
    ElMessage.success(`已回滚：v${result.version_no} 成为活跃发布`);
    await refreshAfterAction();
  } catch (error) {
    publishActionError(error, "回滚失败");
    await refreshAfterAction();
  } finally {
    rollingBack.value = false;
  }
}

async function offlineCurrent(): Promise<void> {
  const version = currentVersion.value;
  if (!version) return;
  const confirmed = await ElMessageBox.confirm(
    `下线 v${version.version_no}？下线后该版本不再对外提供检索；状态为终态，不能重新发布（重发需上传新版本走新链路）。`,
    "下线确认",
    { type: "warning" },
  ).catch(() => false);
  if (!confirmed) return;
  offlining.value = true;
  try {
    const result = await kbApi.offlineVersion(version.id);
    if (result.was_active) activeVersionId.value = null;
    ElMessage.success(
      result.was_active
        ? `v${version.version_no} 已下线，活跃发布已撤除`
        : `v${version.version_no} 已下线`,
    );
    await refreshAfterAction();
  } catch (error) {
    publishActionError(error, "下线失败");
    await refreshAfterAction();
  } finally {
    offlining.value = false;
  }
}

async function deleteCurrent(): Promise<void> {
  const version = currentVersion.value;
  if (!version) return;
  const confirmed = await ElMessageBox.confirm(
    `删除 v${version.version_no}？软删除为终态且不可逆；若它是当前活跃发布，新会话将检索不到该文档。向量数据保留（清理由后台任务负责）。`,
    "删除确认",
    { type: "warning", confirmButtonText: "删除" },
  ).catch(() => false);
  if (!confirmed) return;
  deleting.value = true;
  try {
    const result = await kbApi.deleteVersion(version.id);
    if (result.was_active) activeVersionId.value = null;
    ElMessage.success(
      result.was_active
        ? `v${version.version_no} 已删除，活跃发布已撤除`
        : `v${version.version_no} 已删除`,
    );
    await refreshAfterAction();
  } catch (error) {
    publishActionError(error, "删除失败");
    await refreshAfterAction();
  } finally {
    deleting.value = false;
  }
}

function notifyGone(): void {
  if (goneNotified) return;
  goneNotified = true;
  ElMessage.warning("文档已不可见，列表已刷新");
  emit("gone");
  emit("update:modelValue", false);
}

// ---- KB-06B：分块子视图（按所选版本定域；筛选变化即重置回第一页，卡 3A） ----

function resetChunksList(): void {
  chunks.value = [];
  chunksNextCursor.value = null;
  chunkDetail.value = null;
}

async function loadChunksPage(generation: number, cursor?: string): Promise<void> {
  const view = chunksView.value;
  if (!view) return;
  chunksLoading.value = true;
  try {
    const page = await kbApi.listChunks(view.versionId, {
      limit: KB_PAGE_SIZE,
      cursor,
      q: normalizeChunkQ(chunkFilters.value.q),
      section_path:
        chunkFilters.value.sectionPath === "" ? undefined : chunkFilters.value.sectionPath,
      chunk_type: chunkFilters.value.chunkType === "" ? undefined : chunkFilters.value.chunkType,
      parent_id: chunksParentFilter.value?.id,
    });
    if (generation !== chunksGeneration) return;
    chunks.value = cursor === undefined ? page.items : [...chunks.value, ...page.items];
    chunksNextCursor.value = page.next_cursor;
  } catch (error) {
    if (generation !== chunksGeneration) return;
    reportError(error, "分块列表加载失败");
  } finally {
    if (generation === chunksGeneration) chunksLoading.value = false;
  }
}

async function loadChunkStats(generation: number): Promise<void> {
  const view = chunksView.value;
  if (!view) return;
  try {
    const stats = await kbApi.getChunkStats(view.versionId);
    if (generation !== chunksGeneration) return;
    chunkStats.value = stats;
  } catch (error) {
    if (generation !== chunksGeneration) return;
    if (error instanceof ApiError && error.status === 404) {
      notifyGone();
      return;
    }
    reportError(error, "分块统计加载失败");
  }
}

/** 从版本表行进入子视图：重置筛选与列表（spec：按所选版本定域，不混入其他版本）。 */
function openChunks(row: KbVersionView): void {
  aclView.value = null; // KB-09C：子视图互斥
  chunksView.value = { versionId: row.id, versionNo: row.version_no };
  chunkFilters.value = { q: "", sectionPath: "", chunkType: "" };
  chunksFilterError.value = null;
  chunksParentFilter.value = null;
  chunkStats.value = null;
  resetChunksList();
  // KB-07B：检索测试区随子视图重置（输入/结果/错误一并复位，卡 3A）
  retrievalQuery.value = "";
  retrievalTopK.value = 10;
  retrievalWithRerank.value = false;
  retrievalAsUserId.value = "";
  retrievalAsAudiencesText.value = "";
  retrievalIgnoreAcl.value = false;
  retrievalTestLoading.value = false;
  retrievalTestError.value = null;
  retrievalTestResult.value = null;
  chunksGeneration += 1;
  const generation = chunksGeneration;
  void loadChunksPage(generation);
  void loadChunkStats(generation);
  void loadChunkEdits(generation);
}

function closeChunks(): void {
  chunksView.value = null;
  chunksGeneration += 1;
}

// ---- KB-10B：块编辑提交记录 / 编辑对话框 / 派生确认（服务端仍是唯一门禁） ----

function chunkIndexById(chunkId: string): number | null {
  return chunks.value.find((row) => row.id === chunkId)?.chunk_index ?? null;
}

async function loadChunkEdits(generation: number): Promise<void> {
  if (chunksView.value === null) return;
  chunkEditsLoading.value = true;
  try {
    const items = await kbApi.listChunkEdits(chunksView.value.versionId);
    if (generation !== chunksGeneration) return;
    chunkEdits.value = items;
  } catch (error) {
    if (generation !== chunksGeneration) return;
    reportError(error, "编辑提交记录加载失败");
  } finally {
    if (generation === chunksGeneration) chunkEditsLoading.value = false;
  }
}

function toggleEditDiff(index: number): void {
  openEditDiffIndex.value = openEditDiffIndex.value === index ? null : index;
}

/** 打开编辑对话框（仅子块；上限按 TD-320 动态口径显示）。 */
function openChunkEdit(chunk: KbChunkView): void {
  if (chunk.chunk_type !== "child") return;
  editTarget.value = chunk;
  editContent.value = chunk.text;
  editReason.value = "";
  editCapChars.value = chunkEditMaxChars(chunk.char_count);
  editDialogError.value = null;
  editDialogVisible.value = true;
}

function isKbError(error: unknown, code: string): boolean {
  return (
    error instanceof ApiError &&
    error.detail !== null &&
    typeof error.detail === "object" &&
    (error.detail as Record<string, unknown>).code === code
  );
}

async function submitChunkEdit(): Promise<void> {
  const target = editTarget.value;
  if (target === null || editSubmitting.value) return;
  editSubmitting.value = true;
  editDialogError.value = null;
  try {
    await kbApi.submitChunkEdit(target.id, {
      new_content: editContent.value,
      reason: editReason.value.trim(),
      base_hash: target.chunk_hash,
    });
    editDialogVisible.value = false;
    ElMessage.success("编辑已提交，待派生确认");
    if (chunksView.value !== null) {
      await loadChunkEdits(chunksGeneration);
    }
  } catch (error) {
    // 409 CHUNK_EDIT_STATE：当前后端 submit 无任何路径返回该码（submit 不查派生态、
    // 并发派生冲突走版本号唯一约束在 derive 端点）——此分支当前不可达，仅防御未来
    // 行为变更；真实的已派生引导在对话框横幅（targetVersionDerived）与派生入口 409。
    if (isKbError(error, "CHUNK_EDIT_STATE")) {
      editDialogVisible.value = false;
      const derived = chunkEdits.value.find((row) => row.derived_version_id !== null);
      const derivedId = derived?.derived_version_id ?? null;
      ElMessage.warning(
        derivedId !== null
          ? `该版本已派生（新版本 ${derivedId.slice(0, 8)}…）——请到版本列表打开派生版本继续编辑`
          : "该版本已派生——请到版本列表打开派生版本继续编辑",
      );
      return;
    }
    // 409 CHUNK_CONFLICT / 422 族：对话框内警示不关闭（KB-09C 先例；服务端 message 优先）
    editDialogError.value = chunkEditActionMessage(error, "编辑提交失败");
  } finally {
    editSubmitting.value = false;
  }
}

async function deriveFromVersion(row: KbVersionView): Promise<void> {
  if (derivingVersionId.value !== null) return;
  derivingVersionId.value = row.id;
  try {
    const result = await kbApi.deriveVersion(row.id);
    ElMessage.success(`派生版本已创建并排队构建（纳入 ${result.edited_chunks} 处编辑）`);
    await refreshAfterAction();
  } catch (error) {
    ElMessage.warning(chunkEditActionMessage(error, "派生确认失败"));
  } finally {
    derivingVersionId.value = null;
  }
}

// ---- KB-09C：人群配置子视图（GET view / PUT·confirm acl.manage；服务端是唯一门禁） ----

function openAcl(row: KbVersionView): void {
  chunksView.value = null; // 子视图互斥
  chunksGeneration += 1;
  aclGeneration += 1;
  aclView.value = { versionId: row.id, versionNo: row.version_no };
  void loadAcl();
}

function closeAcl(): void {
  aclGeneration += 1;
  aclView.value = null;
  aclData.value = null;
  aclDialogVisible.value = false;
}

async function loadAcl(): Promise<void> {
  const view = aclView.value;
  if (!view) return;
  const generation = aclGeneration;
  aclLoading.value = true;
  try {
    const fresh = await kbApi.getAudiences(view.versionId);
    // 迟响应丢弃（审查 M1）：closeAcl/openB 后 vA 的响应不得覆盖 B 的数据
    if (generation !== aclGeneration || aclView.value?.versionId !== view.versionId) return;
    aclData.value = fresh;
  } catch (error) {
    if (generation !== aclGeneration || aclView.value?.versionId !== view.versionId) return;
    reportError(error, "人群配置加载失败");
  } finally {
    if (generation === aclGeneration) aclLoading.value = false;
  }
}

function openAclDialog(): void {
  const view = aclView.value;
  const data = aclData.value;
  // 归属校验（审查 M1）：aclData 必须仍是当前子视图版本的数据（迟响应不播种编辑行集）
  if (!view || !data || data.version_id !== view.versionId) return;
  aclRows.value = data.audiences.map((item) => ({
    audience_type: item.audience_type,
    audience_id: item.audience_id,
  }));
  aclEditError.value = null;
  aclDialogVisible.value = true;
}

function addAclRow(): void {
  aclRows.value.push({ audience_type: "department", audience_id: "" });
}

function removeAclRow(index: number): void {
  aclRows.value.splice(index, 1);
  onAclRowsChange();
}

function onAclRowTypeChange(index: number, audienceType: string): void {
  const row = aclRows.value[index];
  if (row === undefined) return;
  row.audience_type = audienceType;
  if (audienceType === "all") row.audience_id = null; // all 不带 id（后端 AUDIENCE_ID_FORBIDDEN）
  onAclRowsChange();
}

function onAclRowIdChange(index: number, value: string): void {
  const row = aclRows.value[index];
  if (row === undefined) return;
  row.audience_id = value;
  onAclRowsChange();
}

/** 行集变化即跑预检（kb-logic 镜像后端 422 族；服务端仍是唯一门禁）。 */
function onAclRowsChange(): void {
  aclEditError.value = audienceEditError(aclRows.value);
}

async function saveAcl(): Promise<void> {
  const view = aclView.value;
  if (!view || !aclManageVisible.value) return;
  const precheck = audienceEditError(aclRows.value);
  if (precheck !== null) {
    aclEditError.value = precheck;
    return;
  }
  const generation = aclGeneration;
  aclSaving.value = true;
  try {
    const result = await kbApi.replaceAudiences(view.versionId, {
      // m4：提交前 trim id，与后端落库规范化对齐（避免对话框与库短时不一致）
      audiences: aclRows.value.map((row) => ({
        audience_type: row.audience_type,
        audience_id: row.audience_id === null ? null : row.audience_id.trim(),
      })),
    });
    if (generation !== aclGeneration || aclView.value?.versionId !== view.versionId) return;
    aclData.value = result;
    aclDialogVisible.value = false;
    ElMessage.warning("人群已保存并置为未确认：确认后才能构建/发布");
  } catch (error) {
    if (generation !== aclGeneration || aclView.value?.versionId !== view.versionId) return;
    if (error instanceof ApiError && error.status === 422) {
      aclEditError.value = toUserMessage(error, "人群配置被拒绝");
    } else {
      reportError(error, "人群保存失败");
    }
  } finally {
    aclSaving.value = false; // 无条件复位（审查复审 m5）：loading 是全局 UI 灯，数据写入已由守卫拦
  }
}

async function confirmAcl(): Promise<void> {
  const view = aclView.value;
  if (!view || !aclManageVisible.value) return;
  const generation = aclGeneration;
  aclConfirming.value = true;
  try {
    const result = await kbApi.confirmAudiences(view.versionId);
    if (generation !== aclGeneration || aclView.value?.versionId !== view.versionId) return;
    aclData.value = result;
    ElMessage.success("人群 ACL 已确认");
  } catch (error) {
    if (generation !== aclGeneration || aclView.value?.versionId !== view.versionId) return;
    if (error instanceof ApiError && error.status === 409) {
      ElMessage.warning(toUserMessage(error, "人群确认被拒绝"));
    } else {
      reportError(error, "人群确认失败");
    }
  } finally {
    aclConfirming.value = false; // 无条件复位（同上）
  }
}

/** 筛选应用：任一变化（含「只看子块」/清除）都回第一页——后端筛选在游标推进之前，旧游标跨筛选失效。 */
function loadMoreChunks(): void {
  if (chunksNextCursor.value === null) return;
  void loadChunksPage(chunksGeneration, chunksNextCursor.value);
}

function applyChunkFilters(): void {
  const rawQ = chunkFilters.value.q;
  const error = chunkFilterError(rawQ === "" ? undefined : rawQ);
  chunksFilterError.value = error;
  if (error) return;
  chunksParentFilter.value = null;
  resetChunksList();
  chunksGeneration += 1;
  void loadChunksPage(chunksGeneration);
}

/** 详情面板「只看该块的子块」：parent_id 直达筛选（后端 parent_id 参数）。 */
function filterByParent(parentId: string, parentIndex: number): void {
  chunksFilterError.value = null;
  chunksParentFilter.value = { id: parentId, index: parentIndex };
  resetChunksList();
  chunksGeneration += 1;
  void loadChunksPage(chunksGeneration);
}

async function openChunkDetailById(chunkId: string): Promise<void> {
  // 代际守卫（审查 m-3）：详情在途时筛选/关子视图会使 chunksGeneration 自增，
  // 迟到响应一律作废（防旧块覆盖新详情、防复位后重现、防「其他版本」噪音 toast）。
  const generation = chunksGeneration;
  chunkDetailLoading.value = true;
  try {
    const fresh = await kbApi.getChunk(chunkId);
    if (generation !== chunksGeneration || chunksView.value === null) return;
    // 详情端点按块自身版本定域；把其他版本的块渲染进当前版本子视图是伪造导航
    if (fresh.document_version_id !== chunksView.value.versionId) {
      ElMessage.warning("该分块属于其他版本");
      return;
    }
    chunkDetail.value = fresh;
  } catch (error) {
    if (generation !== chunksGeneration || chunksView.value === null) return;
    if (error instanceof ApiError && error.status === 404) {
      ElMessage.warning("分块不存在（可能已被删除）");
      return;
    }
    reportError(error, "分块详情加载失败");
  } finally {
    if (generation === chunksGeneration) chunkDetailLoading.value = false;
  }
}

/** KB-07B：运行检索测试（卡 3A——前端预检 → 服务端为唯一门禁；错误 reportError 驻留）。 */
async function runRetrievalTest(): Promise<void> {
  const view = chunksView.value;
  if (!view) return;
  // 预检错误走内联文案（与 chunksFilterError 同形），服务端错误走 reportError
  const queryError = retrievalQueryError(retrievalQuery.value);
  if (queryError !== null) {
    retrievalTestError.value = queryError;
    return;
  }
  const topKError = retrievalTopKError(retrievalTopK.value);
  if (topKError !== null) {
    retrievalTestError.value = topKError;
    return;
  }
  retrievalTestError.value = null;
  // loading 存活期跟运行序号（审查 M1）；结果作废跟 chunksGeneration（卡 3A）。
  // 结果渲染也带运行序号门（复审 m-a）：回车并发 + 中途改词时，后到的旧响应不得覆盖新结果
  const runSeq = ++retrievalRunSeq;
  const generation = chunksGeneration;
  retrievalTestLoading.value = true;
  try {
    const result = await kbApi.retrievalTest(view.versionId, {
      query: retrievalQuery.value,
      top_k: retrievalTopK.value,
      with_rerank: retrievalWithRerank.value,
      // KB-09C 视角三参数（缺省不进请求体=操作者视角；as_user 与 as_audiences 二选一，
      // UI 层以 as_audiences 优先——两者同时填时后端 422 PERSPECTIVE_CONFLICT 兜底）
      ...(retrievalAsAudiencesText.value.trim() !== ""
        ? {
            as_audiences: retrievalAsAudiencesText.value
              .split(/[,\n]/)
              .map((token) => token.trim())
              .filter((token) => token !== ""),
          }
        : retrievalAsUserId.value.trim() !== ""
          ? { as_user_id: retrievalAsUserId.value.trim() }
          : {}),
      ...(retrievalUnfilteredVisible.value && retrievalIgnoreAcl.value ? { ignore_acl: true } : {}),
    });
    if (runSeq !== retrievalRunSeq) return;
    if (generation !== chunksGeneration || chunksView.value === null) return;
    retrievalTestResult.value = result;
  } catch (error) {
    if (runSeq !== retrievalRunSeq) return;
    if (generation !== chunksGeneration || chunksView.value === null) return;
    retrievalTestResult.value = null;
    reportError(error, "检索测试运行失败");
  } finally {
    if (runSeq === retrievalRunSeq) retrievalTestLoading.value = false;
  }
}

async function loadDetail(id: string, generation: number): Promise<void> {
  loading.value = true;
  try {
    const fresh = await kbApi.getDocument(id);
    if (generation !== loadGeneration) return;
    detail.value = fresh;
    // KB-08B 服真化：活跃指针以服务端真值初始化（动作响应的就地更新在前，重读随后校准）
    activeVersionId.value = fresh.active_version_id;
  } catch (error) {
    if (generation !== loadGeneration) return;
    if (error instanceof ApiError && error.status === 404) {
      notifyGone();
      return;
    }
    reportError(error, "文档详情加载失败");
  } finally {
    if (generation === loadGeneration) loading.value = false;
  }
}

async function loadVersions(id: string, generation: number, cursor?: string): Promise<void> {
  versionsLoading.value = true;
  try {
    const page = await kbApi.listVersions(id, { limit: KB_PAGE_SIZE, cursor });
    if (generation !== loadGeneration) return;
    versions.value = cursor === undefined ? page.items : [...versions.value, ...page.items];
    nextCursor.value = page.next_cursor;
  } catch (error) {
    if (generation !== loadGeneration) return;
    if (error instanceof ApiError && error.status === 404) {
      notifyGone();
      return;
    }
    reportError(error, "版本列表加载失败");
  } finally {
    if (generation === loadGeneration) versionsLoading.value = false;
  }
}

function loadMoreVersions(): void {
  const id = props.documentId;
  if (id === null || nextCursor.value === null) return;
  void loadVersions(id, loadGeneration, nextCursor.value);
}

watch([() => props.modelValue, () => props.documentId], ([open, id]) => {
  loadGeneration += 1;
  const generation = loadGeneration;
  detail.value = null;
  versions.value = [];
  nextCursor.value = null;
  loading.value = false;
  versionsLoading.value = false;
  submittedInSession.value = new Set();
  buildCreatedInSession.value = new Set();
  activeVersionId.value = null;
  testRun.value = null;
  testSetName.value = "";
  testFile.value = null;
  testFileError.value = null;
  // KB-06B：子视图随抽屉打开/切换文档复位
  chunksView.value = null;
  chunks.value = [];
  chunksNextCursor.value = null;
  chunkDetail.value = null;
  chunkStats.value = null;
  chunksFilterError.value = null;
  chunksParentFilter.value = null;
  chunkFilters.value = { q: "", sectionPath: "", chunkType: "" };
  chunksGeneration += 1;
  // KB-09C：人群子视图随抽屉打开/切换文档复位（与分块子视图同口径——否则重开抽屉
  // 仍停在人群子视图，文档主体（构建/发布按钮）不可见）
  aclView.value = null;
  aclData.value = null;
  aclDialogVisible.value = false;
  aclGeneration += 1;
  if (!open || id === null) return;
  // TD-291：加载审批单（最近一单直显状态与驳回理由）
  void loadApprovals(id, generation);
  loadLatestTestRun();
  // 只在打开时重置：关闭抽屉不能被当成新一轮，否则并发的两个 404 会二次提示
  goneNotified = false;
  void loadDetail(id, generation);
  void loadVersions(id, generation);
});

async function loadApprovals(docId: string, generation: number): Promise<void> {
  try {
    const items = await testApi.listVersionApprovals(docId);
    if (generation !== loadGeneration) return;
    approvals.value = items;
  } catch {
    if (generation === loadGeneration) approvals.value = [];
  }
}

function loadLatestTestRun(): void {
  // KB-04A 没有按 version 查最新 run 的端点——抽屉暂不预载（TD-296 前端进度随后续）
}

function onTestFileChange(event: Event): void {
  const input = event.target as HTMLInputElement;
  testFile.value = input.files?.[0] ?? null;
  testFileError.value = testFile.value ? testSetFileError(testFile.value) : null;
}

async function uploadAndRun(): Promise<void> {
  const version = detail.value?.current_version;
  if (!version || !testFile.value || testFileError.value) return;
  const name = testSetName.value.trim() || testFile.value.name.replace(/\.[^.]+$/, "");
  uploadingTestSet.value = true;
  try {
    const testSet = await testApi.uploadTestSet(version.id, {
      name,
      file: testFile.value,
    });
    uploadingTestSet.value = false;
    startingTestRun.value = true;
    const run = await testApi.startTestRun(version.id, testSet.test_set_id);
    testRun.value = {
      ...run,
      metrics_json: null,
      started_at: new Date().toISOString(),
      finished_at: null,
    };
    ElMessage.success(`测试集已上传（${testSet.case_count} 条），测试运行已发起`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      ElMessage.warning(toUserMessage(error, "该版本已有进行中的测试运行"));
    } else {
      reportError(error, "测试集上传或测试发起失败");
    }
  } finally {
    uploadingTestSet.value = false;
    startingTestRun.value = false;
  }
}

async function pollTestRun(): Promise<void> {
  const current = testRun.value;
  if (!current) return;
  try {
    const fresh = await testApi.getTestRun(current.run_id);
    testRun.value = fresh;
  } catch {
    // 轮询失败不中断
  }
}

function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", { hour12: false });
}

/** KB-09C：检索测试视角 token 回显（空集=解析失败/空集 fail-closed——零召回可归因）。 */
function resolvedTokensText(tokens: string[]): string {
  if (tokens.length === 0) return "（空集 fail-closed）";
  return tokens.map((token) => tokenLabel(token)).join("、");
}

function currentScan() {
  return detail.value?.current_version?.scan ?? null;
}
</script>

<template>
  <el-drawer
    :model-value="modelValue"
    size="560px"
    data-testid="kb-document-drawer"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <template #header>
      <div class="drawer-head">
        <span class="title">{{ detail?.title ?? "文档详情" }}</span>
        <el-tag v-if="detail" :type="documentStatusMeta(detail.status).tone" size="small">
          {{ documentStatusMeta(detail.status).label }}
        </el-tag>
      </div>
    </template>

    <div v-loading="loading" class="drawer-body">
      <!-- KB-06B 分块子视图（卡 1A：版本行入口；纯文本渲染无 v-html，TD-307 登记升级路径）。 -->
      <template v-if="detail && chunksView">
        <section data-testid="kb-chunks-view">
          <div class="chunks-head">
            <el-button link data-testid="kb-chunks-back" @click="closeChunks">← 返回文档</el-button>
            <span class="chunks-title">
              v{{ chunksView.versionNo }} 分块预览
              <el-tag
                v-if="chunksParentFilter"
                size="small"
                closable
                data-testid="kb-chunks-parent-filter"
                @close="applyChunkFilters"
              >
                只看 #{{ chunksParentFilter.index }} 的子块
              </el-tag>
            </span>
          </div>
          <p v-if="chunkStats" class="muted" data-testid="kb-chunks-stats">
            {{ formatChunkStats(chunkStats) }}
          </p>

          <!-- KB-10B（卡 E3）：编辑提交记录区（含 diff 摘要展开；派生入口在版本行）。 -->
          <div
            v-if="chunkEdits.length > 0"
            v-loading="chunkEditsLoading"
            class="chunk-edits"
            data-testid="kb-chunk-edits"
          >
            <span class="retrieval-title">编辑提交记录</span>
            <ul data-testid="kb-chunk-edits-list">
              <li v-for="(edit, index) in chunkEdits" :key="`${edit.chunk_id}-${index}`">
                <el-tag size="small" :type="edit.status === 'submitted' ? 'warning' : 'success'">
                  {{ edit.status === "submitted" ? "待派生" : "已纳入派生" }}
                </el-tag>
                <span>块 #{{ chunkIndexById(edit.chunk_id) ?? "?" }}</span>
                <span class="muted">{{ edit.reason }}</span>
                <el-button
                  link
                  size="small"
                  type="primary"
                  data-testid="kb-chunk-edit-diff"
                  @click="toggleEditDiff(index)"
                >
                  {{ openEditDiffIndex === index ? "收起改动" : "查看改动" }}
                </el-button>
              </li>
            </ul>
            <div v-if="editDiff !== null" data-testid="kb-chunk-edit-diff-view">
              <p v-for="(line, ri) in editDiff.removed" :key="`r-${ri}`" class="diff-removed">
                - {{ line }}
              </p>
              <p v-for="(line, ai) in editDiff.added" :key="`a-${ai}`" class="diff-added">
                + {{ line }}
              </p>
            </div>
          </div>

          <!-- KB-07B 检索测试区（卡 1A/2A：常驻紧凑，结果表仅运行后渲染；纯文本无 v-html；
               分数与名次来自后端逐路旁路查询——「为什么召回」逐路可解释）。 -->
          <div class="retrieval-test" data-testid="kb-retrieval">
            <div class="retrieval-inputs">
              <span class="retrieval-title">检索测试</span>
              <el-input
                v-model="retrievalQuery"
                size="small"
                placeholder="检索词（≤300 字符）"
                clearable
                class="retrieval-q"
                data-testid="kb-retrieval-q"
                @keyup.enter="runRetrievalTest"
              />
              <el-input-number
                v-model="retrievalTopK"
                size="small"
                :min="1"
                :max="20"
                controls-position="right"
                class="retrieval-topk"
                data-testid="kb-retrieval-topk"
              />
              <el-switch
                v-model="retrievalWithRerank"
                size="small"
                data-testid="kb-retrieval-rerank"
              />
              <span class="muted rerank-hint">重排较慢（分钟级）</span>
              <!-- KB-09C 视角三控件（缺省=操作者视角；as_user 与 as_audiences 二选一） -->
              <el-input
                v-model="retrievalAsUserId"
                size="small"
                placeholder="以用户视角（用户 id，可空）"
                clearable
                class="retrieval-as"
                data-testid="kb-retrieval-as-user"
              />
              <el-input
                v-model="retrievalAsAudiencesText"
                size="small"
                placeholder="模拟人群 token（逗号分隔，可空）"
                clearable
                class="retrieval-as"
                data-testid="kb-retrieval-as-audiences"
              />
              <template v-if="retrievalUnfilteredVisible">
                <el-switch
                  v-model="retrievalIgnoreAcl"
                  size="small"
                  data-testid="kb-retrieval-ignore-acl"
                />
                <span class="muted rerank-hint">忽略 ACL</span>
              </template>
              <el-button
                size="small"
                type="primary"
                :loading="retrievalTestLoading"
                data-testid="kb-retrieval-run"
                @click="runRetrievalTest"
              >
                运行
              </el-button>
            </div>
            <p v-if="retrievalTestError" class="test-error" data-testid="kb-retrieval-error">
              {{ retrievalTestError }}
            </p>
            <p v-else-if="!retrievalTestResult" class="muted" data-testid="kb-retrieval-empty">
              输入检索词后运行，查看逐路分数与最终引用。
            </p>
            <template v-if="retrievalTestResult">
              <p class="muted" data-testid="kb-retrieval-meta">
                {{ rerankStatusLabel(retrievalTestResult.rerank_status) }} ·
                {{ retrievalTestResult.pool_size }} 行 · {{ retrievalTestResult.latency_ms }}ms
                <template v-if="retrievalTestResult.acl_filter_applied">
                  · 视角 token：
                  <span data-testid="kb-retrieval-resolved-tokens">
                    {{ resolvedTokensText(retrievalTestResult.resolved_audience_tokens) }}
                  </span>
                </template>
                <template v-else>
                  · <span data-testid="kb-retrieval-unfiltered">未过滤视图（ignore_acl）</span>
                </template>
              </p>
              <el-table
                :data="retrievalTestResult.results"
                size="small"
                data-testid="kb-retrieval-table"
              >
                <el-table-column prop="final_rank" label="#" width="52" />
                <el-table-column label="最终引用" width="88">
                  <template #default="{ row }">
                    <el-tag v-if="row.is_final_reference" size="small" type="success">
                      最终引用
                    </el-tag>
                    <span v-else class="muted">—</span>
                  </template>
                </el-table-column>
                <el-table-column label="块" width="72">
                  <template #default="{ row }">
                    <span class="retrieval-chunk-index">#{{ row.chunk_index }}</span>
                    <el-tag size="small" :type="row.chunk_type === 'parent' ? 'warning' : 'info'">
                      {{ chunkTypeLabel(row.chunk_type) }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="章节" min-width="120">
                  <template #default="{ row }">
                    <span class="chunk-section">{{ row.section_path ?? "—" }}</span>
                  </template>
                </el-table-column>
                <!-- 内容预览（后端已截 200 码点）：纯文本，不渲染 markdown（无 v-html）。 -->
                <el-table-column label="内容" min-width="160">
                  <template #default="{ row }">
                    <span class="chunk-preview">{{ row.text_preview }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="dense" width="116">
                  <template #default="{ row }">{{ routeScoreText(row.dense) }}</template>
                </el-table-column>
                <el-table-column label="sparse" width="116">
                  <template #default="{ row }">{{ routeScoreText(row.sparse) }}</template>
                </el-table-column>
                <el-table-column label="RRF" width="84">
                  <template #default="{ row }">{{ formatScore(row.rrf_score) }}</template>
                </el-table-column>
                <el-table-column label="rerank" width="84">
                  <template #default="{ row }">{{ formatScore(row.rerank_score) }}</template>
                </el-table-column>
                <!-- KB-09C：该块 payload ACL token ∩ 视角 token（空=非 ACL 命中，如 ignore_acl 视图）。 -->
                <el-table-column label="命中 token" min-width="120">
                  <template #default="{ row }">
                    <el-tag
                      v-for="token in row.acl_matched_tokens"
                      :key="token"
                      size="small"
                      type="info"
                      class="acl-token-tag"
                    >
                      {{ tokenLabel(token) }}
                    </el-tag>
                    <span v-if="row.acl_matched_tokens.length === 0" class="muted">—</span>
                  </template>
                </el-table-column>
                <el-table-column label="" width="76">
                  <template #default="{ row }">
                    <el-button
                      link
                      size="small"
                      type="primary"
                      data-testid="kb-retrieval-open-chunk"
                      @click="openChunkDetailById(row.chunk_id)"
                    >
                      查看分块
                    </el-button>
                  </template>
                </el-table-column>
              </el-table>
            </template>
          </div>

          <div class="chunks-filters">
            <el-input
              v-model="chunkFilters.q"
              size="small"
              placeholder="搜索块内容（≤200 字符）"
              clearable
              class="chunks-q-input"
              data-testid="kb-chunks-q"
              @keyup.enter="applyChunkFilters"
              @clear="applyChunkFilters"
            />
            <el-select
              v-model="chunkFilters.sectionPath"
              size="small"
              clearable
              placeholder="章节"
              class="chunks-select"
              data-testid="kb-chunks-section"
              @change="applyChunkFilters"
            >
              <el-option
                v-for="opt in sectionOptions(chunkStats?.sections ?? [])"
                :key="opt.value"
                :label="opt.label"
                :value="opt.value"
              />
            </el-select>
            <el-select
              v-model="chunkFilters.chunkType"
              size="small"
              clearable
              placeholder="类型"
              class="chunks-select chunks-select-type"
              data-testid="kb-chunks-type"
              @change="applyChunkFilters"
            >
              <el-option label="父块" value="parent" />
              <el-option label="子块" value="child" />
            </el-select>
            <el-button
              size="small"
              type="primary"
              data-testid="kb-chunks-search"
              @click="applyChunkFilters"
            >
              搜索
            </el-button>
          </div>
          <p v-if="chunksFilterError" class="test-error" data-testid="kb-chunks-filter-error">
            {{ chunksFilterError }}
          </p>
          <el-table
            v-loading="chunksLoading"
            :data="chunks"
            size="small"
            data-testid="kb-chunks-table"
          >
            <el-table-column prop="chunk_index" label="#" width="52" />
            <el-table-column label="类型" width="80">
              <template #default="{ row }">
                <el-tag size="small" :type="row.chunk_type === 'parent' ? 'warning' : 'info'">
                  {{ chunkTypeLabel(row.chunk_type) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="章节" min-width="150">
              <template #default="{ row }">
                <span class="chunk-section">{{ row.section_path ?? "—" }}</span>
              </template>
            </el-table-column>
            <!-- 内容预览（单行截断，DOM 含全文）：不渲染 markdown（无 v-html），只作行辨识。 -->
            <el-table-column label="内容" min-width="180">
              <template #default="{ row }">
                <span class="chunk-preview">{{ row.text }}</span>
              </template>
            </el-table-column>
            <el-table-column label="字符" width="64">
              <template #default="{ row }">{{ row.char_count }}</template>
            </el-table-column>
            <el-table-column label="嵌入" width="88">
              <template #default="{ row }">
                <el-tag size="small" :type="embeddingStatusMeta(row.embedding_status).tone">
                  {{ embeddingStatusMeta(row.embedding_status).label }}
                </el-tag>
              </template>
            </el-table-column>
            <!-- KB-10B（卡 E4）：人工修订 tag（spec :328 分块详情透出）。 -->
            <el-table-column label="人工修订" width="88">
              <template #default="{ row }">
                <el-tag
                  v-if="row.is_human_edited"
                  size="small"
                  type="danger"
                  data-testid="kb-chunk-human-tag"
                >
                  人工修订
                </el-tag>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column v-if="chunkEditVisible" label="" width="56">
              <template #default="{ row }">
                <el-button
                  v-if="row.chunk_type === 'child'"
                  link
                  size="small"
                  type="primary"
                  data-testid="kb-chunk-edit-open"
                  @click="openChunkEdit(row as KbChunkView)"
                >
                  编辑
                </el-button>
              </template>
            </el-table-column>
            <el-table-column label="" width="56">
              <template #default="{ row }">
                <el-button
                  link
                  size="small"
                  type="primary"
                  data-testid="kb-chunk-detail-open"
                  @click="openChunkDetailById(row.id)"
                >
                  详情
                </el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-button
            v-if="chunksNextCursor !== null"
            class="load-more"
            size="small"
            :loading="chunksLoading"
            data-testid="kb-chunks-more"
            @click="loadMoreChunks"
            >加载更多</el-button
          >
          <p
            v-else-if="chunks.length === 0 && !chunksLoading"
            class="muted"
            data-testid="kb-chunks-empty"
          >
            没有匹配的分块。
          </p>

          <!-- 详情子面板（卡 2A）：text_markdown 纯文本；父/子导航走详情端点引用。 -->
          <div
            v-if="chunkDetail"
            v-loading="chunkDetailLoading"
            class="chunk-detail"
            data-testid="kb-chunk-detail"
          >
            <div class="chunk-detail-head">
              <el-button
                link
                size="small"
                data-testid="kb-chunk-detail-close"
                @click="chunkDetail = null"
              >
                ← 返回列表
              </el-button>
              <span>
                #{{ chunkDetail.chunk_index }} · {{ chunkTypeLabel(chunkDetail.chunk_type) }}
              </span>
              <span class="muted">{{ chunkDetail.section_path ?? "" }}</span>
              <el-tag
                v-if="chunkDetail.is_human_edited"
                size="small"
                type="danger"
                data-testid="kb-chunk-detail-human-tag"
              >
                人工修订
              </el-tag>
              <el-button
                v-if="chunkEditVisible && chunkDetail.chunk_type === 'child'"
                link
                size="small"
                type="primary"
                data-testid="kb-chunk-detail-edit"
                @click="openChunkEdit(chunkDetail)"
              >
                编辑
              </el-button>
            </div>
            <p v-if="chunkDetail.parent" class="chunk-nav">
              父块：
              <el-button
                link
                size="small"
                type="primary"
                data-testid="kb-chunk-parent-link"
                @click="openChunkDetailById(chunkDetail.parent.id)"
              >
                #{{ chunkDetail.parent.chunk_index }}
              </el-button>
            </p>
            <p v-if="chunkDetail.children.length > 0" class="chunk-nav">
              子块：
              <el-button
                v-for="child in chunkDetail.children"
                :key="child.id"
                link
                size="small"
                type="primary"
                data-testid="kb-chunk-child-link"
                @click="openChunkDetailById(child.id)"
              >
                #{{ child.chunk_index }}
              </el-button>
              <el-button
                link
                size="small"
                type="primary"
                data-testid="kb-chunk-children-filter"
                @click="filterByParent(chunkDetail.id, chunkDetail.chunk_index)"
              >
                只看该块的子块
              </el-button>
            </p>
            <pre class="chunk-text" data-testid="kb-chunk-text">{{
              chunkDetail.text_markdown
            }}</pre>
          </div>
        </section>
      </template>
      <!-- KB-09C 人群配置子视图（卡 C1-A：版本行入口；确认态=可构建/发布不变量的 UX 提示）。 -->
      <template v-else-if="detail && aclView">
        <section v-loading="aclLoading" data-testid="kb-acl-view">
          <div class="chunks-head">
            <el-button link data-testid="kb-acl-back" @click="closeAcl">← 返回文档</el-button>
            <span class="chunks-title">
              v{{ aclView.versionNo }} 查阅人群
              <el-tag
                :type="(aclData?.acl_confirmed ?? false) ? 'success' : 'warning'"
                size="small"
                data-testid="kb-acl-confirmed-tag"
              >
                {{ aclConfirmedLabel(aclData?.acl_confirmed ?? false) }}
              </el-tag>
            </span>
          </div>
          <el-alert
            v-if="aclBannerText(aclData?.acl_confirmed ?? false)"
            :title="aclBannerText(aclData?.acl_confirmed ?? false) ?? ''"
            type="warning"
            show-icon
            :closable="false"
            class="acl-banner"
            data-testid="kb-acl-banner"
          />
          <el-table :data="aclData?.audiences ?? []" size="small" data-testid="kb-acl-table">
            <el-table-column label="类型" width="110">
              <template #default="{ row }">{{ audienceTypeLabel(row.audience_type) }}</template>
            </el-table-column>
            <el-table-column label="对象" min-width="180">
              <template #default="{ row }">
                <span class="mono">{{ row.audience_id ?? "—" }}</span>
              </template>
            </el-table-column>
          </el-table>
          <p v-if="(aclData?.audiences.length ?? 0) === 0" class="muted" data-testid="kb-acl-empty">
            未配置人群：构建/发布将被 409 ACL_REQUIRED 拒绝。
          </p>
          <div v-if="aclManageVisible" class="acl-actions">
            <el-button size="small" data-testid="kb-acl-edit" @click="openAclDialog">
              编辑人群
            </el-button>
            <el-button
              size="small"
              type="primary"
              :disabled="aclData === null || aclData.acl_confirmed"
              :loading="aclConfirming"
              data-testid="kb-acl-confirm"
              @click="confirmAcl"
            >
              确认人群
            </el-button>
          </div>
        </section>
      </template>
      <template v-else-if="detail">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="文档 ID">
            <span class="mono">{{ detail.id }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="标签">
            <template v-if="detail.tags.length > 0">
              <el-tag v-for="tag in detail.tags" :key="tag" size="small" class="tag">{{
                tag
              }}</el-tag>
            </template>
            <span v-else>—</span>
          </el-descriptions-item>
          <el-descriptions-item label="描述">{{ detail.description ?? "—" }}</el-descriptions-item>
          <el-descriptions-item label="创建时间">
            {{ formatDateTime(detail.created_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="更新时间">
            {{ formatDateTime(detail.updated_at) }}
          </el-descriptions-item>
        </el-descriptions>

        <h3>当前版本</h3>
        <template v-if="detail.current_version">
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="版本">
              v{{ detail.current_version.version_no }}
            </el-descriptions-item>
            <el-descriptions-item label="文件名">
              {{ detail.current_version.filename }}
            </el-descriptions-item>
            <el-descriptions-item label="大小">
              {{ formatBytes(detail.current_version.size_bytes) }}
            </el-descriptions-item>
            <el-descriptions-item label="扫描">
              <KbScanTag :scan="currentScan()" />
              <div v-if="detail.current_version.scan.error_code" class="extra">
                错误码：{{ detail.current_version.scan.error_code }}
              </div>
              <div v-if="detail.current_version.scan.signature" class="extra">
                签名：{{ detail.current_version.scan.signature }}
              </div>
              <div class="extra">
                尝试次数：{{ detail.current_version.scan.attempts }} · 扫描时间：{{
                  detail.current_version.scan.scanned_at
                    ? formatDateTime(detail.current_version.scan.scanned_at)
                    : "—"
                }}
              </div>
            </el-descriptions-item>
          </el-descriptions>
        </template>
        <p v-else class="muted">暂无版本。</p>

        <div
          v-if="detail.status === 'REJECTED'"
          class="reject-guidance"
          data-testid="kb-reject-guidance"
        >
          <el-alert
            v-if="latestApproval && latestApproval.reject_reason"
            type="error"
            :closable="false"
            show-icon
            :title="latestApproval.reject_reason"
            data-testid="kb-reject-reason-display"
          />
          <el-alert
            v-else
            type="info"
            :closable="false"
            show-icon
            :title="REJECT_REASON_GUIDANCE"
          />
        </div>

        <div
          v-if="detail.status === 'PENDING_APPROVAL' || detail.status === 'APPROVED'"
          class="entry-row"
        >
          <el-button
            v-if="canSubmitEntry"
            type="primary"
            :loading="submitting"
            data-testid="kb-submit-approval"
            @click="submitApproval"
          >
            提交审批
          </el-button>
          <el-button
            v-if="
              detail.status === 'PENDING_APPROVAL' &&
              !canSubmitEntry &&
              detail.current_version !== null &&
              permissions.includes(KB_UPLOAD_CODE)
            "
            disabled
            data-testid="kb-submit-approval-done"
          >
            已提交审批
          </el-button>
          <el-button
            v-if="canBuildEntry"
            type="primary"
            plain
            :loading="building"
            data-testid="kb-create-build"
            @click="createBuild"
          >
            创建构建
          </el-button>
          <el-button
            v-if="
              detail.status === 'APPROVED' &&
              !canBuildEntry &&
              detail.current_version !== null &&
              permissions.includes(KB_BUILD_CODE)
            "
            disabled
            data-testid="kb-create-build-done"
          >
            已创建构建任务
          </el-button>
        </div>

        <!-- KB-05B：发布动作行（TEST_PASSED 发布；PUBLISHED 下线/回滚/删除）。 -->
        <div v-if="publishRowVisible" class="entry-row" data-testid="kb-publish-row">
          <el-button
            v-if="canPublish"
            type="success"
            :loading="publishing"
            data-testid="kb-publish"
            @click="publishCurrent"
          >
            发布
          </el-button>
          <el-dropdown
            v-if="canRollback"
            class="rollback-dropdown"
            trigger="click"
            data-testid="kb-rollback"
            @command="rollbackTo"
          >
            <el-button :loading="rollingBack" data-testid="kb-rollback-trigger">回滚</el-button>
            <template #dropdown>
              <el-dropdown-menu data-testid="kb-rollback-menu">
                <el-dropdown-item v-if="rollbackTargetList.length === 0" disabled>
                  无可回滚目标（需其他已发布版本）
                </el-dropdown-item>
                <el-dropdown-item
                  v-for="target in rollbackTargetList"
                  :key="target.id"
                  :command="target"
                  data-testid="kb-rollback-target"
                >
                  v{{ target.version_no }} · {{ target.filename }}
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <el-button
            v-if="canOffline"
            :loading="offlining"
            data-testid="kb-offline"
            @click="offlineCurrent"
          >
            下线
          </el-button>
          <el-button
            v-if="canDelete"
            type="danger"
            plain
            :loading="deleting"
            data-testid="kb-delete"
            @click="deleteCurrent"
          >
            删除
          </el-button>
        </div>

        <section v-if="testGate.visible" class="test-section" data-testid="kb-test-section">
          <h3>测试</h3>
          <p v-if="testGate.hint" class="muted" data-testid="kb-test-hint">{{ testGate.hint }}</p>
          <template v-if="testGate.visible && !testGate.hint">
            <p v-if="testRun" class="muted" data-testid="kb-test-run-status">
              {{ testRunStatusMeta(testRun.status).label }}
              <template v-if="testRun.finished_at">
                · 通过率 {{ passRateLabel(testRun.metrics_json) }} ·
                {{ caseSummaries(testRun.metrics_json).length }} 条
              </template>
            </p>
            <div v-if="!testRun" class="test-upload-row">
              <el-input
                v-model="testSetName"
                placeholder="测试集名称（可选）"
                size="small"
                class="test-name-input"
                data-testid="kb-test-set-name"
              />
              <input
                type="file"
                accept=".csv,.xlsx,.json"
                data-testid="kb-test-set-file"
                @change="onTestFileChange"
              />
            </div>
            <p v-if="testFileError" class="test-error" data-testid="kb-test-file-error">
              {{ testFileError }}
            </p>
            <el-button
              v-if="!testRun"
              type="primary"
              size="small"
              :disabled="!testFile || !!testFileError"
              :loading="uploadingTestSet || startingTestRun"
              data-testid="kb-test-start"
              @click="uploadAndRun"
            >
              上传并运行测试
            </el-button>
            <el-button
              v-if="testRun && testRun.status === 'TESTING'"
              size="small"
              data-testid="kb-test-poll"
              @click="pollTestRun"
            >
              刷新结果
            </el-button>
          </template>
        </section>

        <h3>版本列表</h3>
        <el-table :data="versions" size="small" data-testid="kb-version-table">
          <el-table-column prop="version_no" label="版本" width="70" />
          <el-table-column label="状态" width="160">
            <template #default="{ row }">
              <el-tag :type="documentStatusMeta(row.status).tone" size="small">
                {{ documentStatusMeta(row.status).label }}
              </el-tag>
              <!-- KB-08B 服真化：当前活跃发布标识（判据 isActiveVersion，null 不打标） -->
              <el-tag
                v-if="isActiveVersion(row.id as string, activeVersionId)"
                type="success"
                size="small"
                data-testid="kb-active-tag"
              >
                当前活跃
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="filename" label="文件名" min-width="150" />
          <el-table-column label="大小" width="90">
            <template #default="{ row }">{{ formatBytes(row.size_bytes) }}</template>
          </el-table-column>
          <el-table-column label="扫描" min-width="180">
            <template #default="{ row }"><KbScanTag :scan="row.scan" /></template>
          </el-table-column>
          <el-table-column label="创建时间" min-width="160">
            <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
          </el-table-column>
          <!-- KB-06B：分块入口列（卡 1A）；无 view_chunks 的主体整列不出现。 -->
          <el-table-column v-if="chunksVisible" label="分块" width="76">
            <template #default="{ row }">
              <el-button
                link
                size="small"
                type="primary"
                data-testid="kb-chunks-open"
                @click="openChunks(row as KbVersionView)"
              >
                查看
              </el-button>
            </template>
          </el-table-column>
          <!-- KB-09C：人群配置入口列（卡 C1-A；无 school.kb.view 的主体整列不出现）。 -->
          <el-table-column v-if="aclEntryVisible" label="人群" width="76">
            <template #default="{ row }">
              <el-button
                link
                size="small"
                type="primary"
                data-testid="kb-acl-open"
                @click="openAcl(row as KbVersionView)"
              >
                查看
              </el-button>
            </template>
          </el-table-column>
          <!-- KB-10B（卡 E3）：派生确认入口（chunk.confirm 码 + 校方超管 role 双判；服务端唯一门禁）。 -->
          <el-table-column v-if="chunkConfirmVisible" label="派生" width="76">
            <template #default="{ row }">
              <el-button
                link
                size="small"
                type="primary"
                data-testid="kb-derive-open"
                :loading="derivingVersionId === (row as KbVersionView).id"
                @click="deriveFromVersion(row as KbVersionView)"
              >
                派生
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-button
          v-if="nextCursor !== null"
          class="load-more"
          size="small"
          :loading="versionsLoading"
          data-testid="kb-versions-more"
          @click="loadMoreVersions"
          >加载更多</el-button
        >
        <p v-else-if="versions.length === 0 && !versionsLoading" class="muted">暂无版本记录。</p>
      </template>

      <!-- KB-09C 人群编辑对话框（卡 C2-A：条目逐行编辑 + kb-logic 镜像后端 422 族预检）。 -->
      <el-dialog
        v-model="aclDialogVisible"
        title="编辑查阅人群（全量替换）"
        width="520px"
        data-testid="kb-acl-dialog"
      >
        <p class="muted acl-dialog-hint">
          保存为全量替换：不在列表中的既有条目将被移除，且确认态重置为未确认（需重新确认后才能构建/发布）。
        </p>
        <el-table :data="aclRows" size="small" data-testid="kb-acl-edit-table">
          <el-table-column label="类型" min-width="260">
            <template #default="{ $index }">
              <el-radio-group
                :model-value="aclRows[$index]?.audience_type"
                size="small"
                data-testid="kb-acl-row-type"
                @update:model-value="onAclRowTypeChange($index, $event as string)"
              >
                <el-radio v-for="t in KB_AUDIENCE_TYPES" :key="t" :value="t" class="acl-type-radio">
                  {{ audienceTypeLabel(t) }}
                </el-radio>
              </el-radio-group>
            </template>
          </el-table-column>
          <el-table-column label="对象 id / 年级值" min-width="200">
            <template #default="{ $index }">
              <el-input
                :model-value="aclRows[$index]?.audience_id ?? ''"
                size="small"
                :disabled="aclRows[$index]?.audience_type === 'all'"
                :placeholder="aclRows[$index]?.audience_type === 'all' ? '全体（无需 id）' : ''"
                data-testid="kb-acl-row-id"
                @update:model-value="onAclRowIdChange($index, $event)"
              />
            </template>
          </el-table-column>
          <el-table-column label="" width="60">
            <template #default="{ $index }">
              <el-button
                link
                size="small"
                type="danger"
                data-testid="kb-acl-row-remove"
                @click="removeAclRow($index)"
              >
                移除
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-button size="small" class="acl-add-row" data-testid="kb-acl-row-add" @click="addAclRow">
          添加条目
        </el-button>
        <p v-if="aclEditError" class="test-error" data-testid="kb-acl-edit-error">
          {{ aclEditError }}
        </p>
        <template #footer>
          <el-button data-testid="kb-acl-dialog-cancel" @click="aclDialogVisible = false">
            取消
          </el-button>
          <el-button
            type="primary"
            :loading="aclSaving"
            :disabled="aclEditError !== null"
            data-testid="kb-acl-save"
            @click="saveAcl"
          >
            保存（置为未确认）
          </el-button>
        </template>
      </el-dialog>

      <!-- KB-10B 编辑对话框（卡 E1）：textarea + 理由 + kb-logic 镜像 422 族预检 + 409 回填不关闭。 -->
      <el-dialog
        v-model="editDialogVisible"
        title="编辑子块文本"
        width="640"
        data-testid="kb-chunk-edit-dialog"
      >
        <div v-if="editTarget !== null">
          <p class="muted">
            块 #{{ editTarget.chunk_index }} · {{ chunkTypeLabel(editTarget.chunk_type) }} · 上限
            {{ editCapChars }} 码点（TD-320：max(500, 原块码点)）
          </p>
          <p
            v-if="targetVersionDerived"
            class="edit-error"
            data-testid="kb-chunk-edit-derived-warn"
          >
            该版本已派生{{
              targetDerivedVersionShort === null ? "" : `（新版本 ${targetDerivedVersionShort}…）`
            }}——本次提交不会纳入现有派生，建议到派生版本继续编辑
          </p>
          <el-input
            v-model="editContent"
            type="textarea"
            :rows="10"
            data-testid="kb-chunk-edit-content"
          />
          <el-input
            v-model="editReason"
            placeholder="编辑理由（必填）"
            class="edit-reason"
            data-testid="kb-chunk-edit-reason"
          />
          <p
            v-if="editContentCheck !== null"
            class="edit-error"
            data-testid="kb-chunk-edit-precheck"
          >
            {{ editContentCheck }}
          </p>
          <p
            v-if="editReasonCheck !== null"
            class="edit-error"
            data-testid="kb-chunk-edit-reason-precheck"
          >
            {{ editReasonCheck }}
          </p>
          <p v-if="editDialogError !== null" class="edit-error" data-testid="kb-chunk-edit-error">
            {{ editDialogError }}
          </p>
        </div>
        <template #footer>
          <el-button data-testid="kb-chunk-edit-cancel" @click="editDialogVisible = false">
            取消
          </el-button>
          <el-button
            type="primary"
            :disabled="
              editContentCheck !== null || editReasonCheck !== null || editReason.trim() === ''
            "
            :loading="editSubmitting"
            data-testid="kb-chunk-edit-submit"
            @click="submitChunkEdit"
          >
            提交编辑
          </el-button>
        </template>
      </el-dialog>
    </div>
  </el-drawer>
</template>

<style scoped>
.drawer-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.title {
  font-weight: 600;
}
.mono {
  font-family: monospace;
  font-size: 12px;
}
.tag {
  margin-right: 6px;
}
.extra {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.muted {
  color: var(--el-text-color-secondary);
}
.reject-guidance {
  margin-bottom: 4px;
}
.entry-row {
  display: flex;
  gap: 8px;
}
/* el-dropdown 不是 flex 子项原生尺寸，self 对齐让回滚按钮与同行按钮等高排列 */
.rollback-dropdown {
  align-self: center;
}
.load-more {
  margin-top: 8px;
}
.test-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}
.test-section h3 {
  margin: 0;
  font-size: 14px;
}
.test-upload-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.test-name-input {
  width: 180px;
}
.test-error {
  color: var(--el-color-danger);
  font-size: 12px;
  margin: 0;
}
.reject-guidance {
  margin-bottom: 4px;
}
/* KB-06B 分块子视图 */
.chunks-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.chunks-title {
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.chunks-filters {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 8px 0;
}
.chunks-q-input {
  flex: 1;
  min-width: 120px;
}
.chunks-select {
  width: 130px;
}
.chunks-select-type {
  width: 96px;
}
/* KB-07B 检索测试区 */
.retrieval-test {
  margin: 8px 0;
  padding: 8px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
}
.retrieval-inputs {
  display: flex;
  align-items: center;
  gap: 8px;
  /* 窄抽屉下防溢出：溢出会挤压 el-input-number 使其失去可操作性（KB-07B 审查修实测） */
  flex-wrap: wrap;
}
.retrieval-title {
  font-weight: 600;
  white-space: nowrap;
}
.retrieval-q {
  flex: 1;
  min-width: 120px;
}
.retrieval-topk {
  width: 104px;
}
.rerank-hint {
  white-space: nowrap;
}
/* KB-09C：视角控件与人群子视图 */
.retrieval-as {
  flex: 1;
  min-width: 140px;
}
.acl-banner {
  margin-bottom: 8px;
}
.acl-actions {
  margin-top: 8px;
  display: flex;
  gap: 8px;
}
.acl-token-tag {
  margin-right: 4px;
}
.acl-dialog-hint {
  margin-top: 0;
}
.acl-add-row {
  margin-top: 8px;
}
.acl-type-radio {
  margin-right: 8px;
}
.retrieval-chunk-index {
  margin-right: 4px;
}
.chunk-section {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: bottom;
}
.chunk-preview {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: bottom;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.chunk-detail {
  margin-top: 10px;
  padding: 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}
.chunk-detail-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
}
.chunk-nav {
  margin: 4px 0;
  font-size: 12px;
}
.chunk-text {
  margin: 8px 0 0;
  padding: 10px;
  background: var(--el-fill-color-light);
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 320px;
  overflow: auto;
}
.chunk-edits {
  margin-top: 8px;
  padding: 8px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}
.chunk-edits ul {
  margin: 6px 0 0;
  padding: 0;
  list-style: none;
}
.chunk-edits li {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  padding: 2px 0;
}
.diff-removed {
  margin: 2px 0;
  color: var(--el-color-danger);
  background: var(--el-fill-color-light);
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
}
.diff-added {
  margin: 2px 0;
  color: var(--el-color-success);
  background: var(--el-fill-color-light);
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
}
.edit-error {
  margin: 6px 0 0;
  color: var(--el-color-danger);
  font-size: 12px;
}
</style>
