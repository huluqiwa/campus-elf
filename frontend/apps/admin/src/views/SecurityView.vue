<script setup lang="ts">
/**
 * 安全设置页（M1-D2）：查看/开启/关闭两步验证。
 *
 * 开启与关闭都会轮换全部登录会话（后端语义），所以本页在操作成功后需要重新拉取用户状态。
 * 平台强制策略下不允许关闭（后端 403，前端同时禁用入口避免出现无效操作）。
 */
import { toUserMessage } from "@campus-elf/api-client";
import { ElMessage } from "element-plus";
import { computed, nextTick, reactive, ref } from "vue";
import { useRouter } from "vue-router";

import MfaEnrollPanel from "../components/MfaEnrollPanel.vue";
import { useAuthStore } from "../stores/auth";

const auth = useAuthStore();
const router = useRouter();

const panel = ref<InstanceType<typeof MfaEnrollPanel> | null>(null);
const showPanel = ref(false);
const errorMessage = ref("");
const submitting = ref(false);
const enabled = computed(() => auth.user?.mfaEnabled === true);
const required = computed(() => auth.user?.mfaRequired === true);

const enableForm = reactive({ password: "" });
const disableForm = reactive({ password: "", code: "" });

async function startEnroll(): Promise<void> {
  errorMessage.value = "";
  if (!enableForm.password) {
    errorMessage.value = "请输入当前密码";
    return;
  }
  showPanel.value = true;
  await nextTick(); // 面板刚挂载，ref 才有值
  await panel.value?.start();
}

async function onActivated(): Promise<void> {
  // 密码只在绑定过程中留在内存，激活成功即清掉
  enableForm.password = "";
  ElMessage.success("两步验证已启用，其他设备需要重新登录");
}

async function submitDisable(): Promise<void> {
  if (!disableForm.password || !disableForm.code.trim()) {
    errorMessage.value = "请输入当前密码与验证器动态码（或恢复码）";
    return;
  }
  errorMessage.value = "";
  submitting.value = true;
  try {
    await auth.disableMfa(disableForm.password, disableForm.code.trim());
    disableForm.password = "";
    disableForm.code = "";
    ElMessage.success("两步验证已关闭，其他设备需要重新登录");
  } catch (error) {
    errorMessage.value = toUserMessage(error, "关闭失败，请检查密码与动态码");
    ElMessage.error(errorMessage.value);
  } finally {
    submitting.value = false;
  }
}

async function goHome(): Promise<void> {
  await router.replace({ name: "home" });
}
</script>

<template>
  <main class="security-page">
    <h1>安全设置</h1>
    <el-card shadow="never" class="security-card">
      <p data-testid="mfa-status" class="status">
        两步验证：<strong>{{ enabled ? "已启用" : "未启用" }}</strong>
        <span v-if="required">（平台策略要求必须启用）</span>
      </p>
      <p v-if="errorMessage" class="error" role="alert" data-testid="security-error">
        {{ errorMessage }}
      </p>

      <template v-if="!enabled && !showPanel">
        <el-form label-position="top" @submit.prevent="startEnroll">
          <el-form-item label="当前密码">
            <el-input
              v-model="enableForm.password"
              type="password"
              show-password
              placeholder="重新输入密码以确认身份"
              data-testid="enable-password"
            />
          </el-form-item>
        </el-form>
        <el-button type="primary" data-testid="enable-start" @click="startEnroll">
          开始绑定验证器
        </el-button>
      </template>

      <!-- 绑定面板在激活后仍保留，否则一次性恢复码会随分支切换消失 -->
      <template v-if="showPanel">
        <MfaEnrollPanel
          ref="panel"
          mode="self"
          :current-password="enableForm.password"
          @activated="onActivated"
        />
        <el-button
          v-if="enabled"
          type="primary"
          data-testid="enable-finish"
          @click="showPanel = false"
        >
          我已保存恢复码
        </el-button>
      </template>

      <template v-if="enabled && !showPanel">
        <p class="hint">
          恢复码只能在启用时看到一次；用完后需要重新绑定。关闭两步验证会让所有设备退出登录。
        </p>
        <template v-if="!required">
          <el-form label-position="top" @submit.prevent="submitDisable">
            <el-form-item label="当前密码">
              <el-input
                v-model="disableForm.password"
                type="password"
                show-password
                placeholder="密码"
                data-testid="disable-password"
              />
            </el-form-item>
            <el-form-item label="动态码 / 恢复码">
              <el-input
                v-model="disableForm.code"
                placeholder="123456"
                autocomplete="one-time-code"
                data-testid="disable-code"
              />
            </el-form-item>
          </el-form>
          <el-button
            type="danger"
            :loading="submitting"
            data-testid="disable-submit"
            @click="submitDisable"
          >
            关闭两步验证
          </el-button>
        </template>
        <p v-else class="hint" data-testid="disable-blocked">
          当前策略要求必须启用两步验证，无法关闭。
        </p>
      </template>

      <el-button text data-testid="security-back" @click="goHome">返回</el-button>
    </el-card>
  </main>
</template>

<style scoped>
.security-page {
  max-width: 520px;
  margin: 0 auto;
  padding: 48px 24px;
}
.security-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.hint {
  color: var(--el-text-color-secondary);
  font-size: 14px;
  margin: 0;
}
.status {
  margin: 0;
}
.error {
  color: var(--el-color-danger);
  margin: 0;
}
</style>
