<script setup lang="ts">
/**
 * 登录页（M1-C-UI + M1-D2）：平台运营与校方共用一页，通过身份切换区分 realm。
 * 平台：账号 + 密码；校方/C 端管理账号：学校编号 + 学号/工号/邮箱 + 密码。
 * 已绑定验证器时先输密码、再输 6 位动态码（第二步只认动态码或恢复码）。
 */
import { toUserMessage } from "@campus-elf/api-client";
import { ElMessage } from "element-plus";
import { computed, reactive, ref } from "vue";
import { useRouter } from "vue-router";

import { useAuthStore } from "../stores/auth";

const router = useRouter();
const auth = useAuthStore();

const mode = ref<"platform" | "tenant">("tenant");
const submitting = ref(false);
const errorMessage = ref("");
/** 动态码步骤：密码已通过，等待 6 位码或恢复码。 */
const step = ref<"credentials" | "code">("credentials");
const code = ref("");

const form = reactive({
  schoolCode: "",
  loginId: "",
  account: "",
  password: "",
});

async function afterSuccess(): Promise<void> {
  if (auth.mustEnrollMfa) {
    await router.replace({ name: "mfa-enroll" });
    return;
  }
  if (auth.tokens?.mustChangePassword) {
    await router.replace({ name: "change-password" });
    return;
  }
  await router.replace({ name: "home" });
}

async function submit(): Promise<void> {
  if (submitting.value) return; // 防重复提交（按钮 loading 只挡点击，不挡回车）
  errorMessage.value = "";
  if (!form.password) {
    errorMessage.value = "请输入密码";
    return;
  }
  if (mode.value === "tenant" && (!form.schoolCode || !form.loginId)) {
    errorMessage.value = "请输入学校编号与学号/工号";
    return;
  }
  if (mode.value === "platform" && !form.account) {
    errorMessage.value = "请输入平台账号";
    return;
  }

  submitting.value = true;
  try {
    const outcome =
      mode.value === "platform"
        ? await auth.loginPlatform({ account: form.account, password: form.password })
        : await auth.loginTenant({
            schoolCode: form.schoolCode,
            loginId: form.loginId,
            password: form.password,
          });
    if (outcome.status === "mfaRequired") {
      // 密码正确但还没完成第二步：清空密码，只保留动态码输入
      form.password = "";
      code.value = "";
      step.value = "code";
      return;
    }
    await afterSuccess();
  } catch (error) {
    errorMessage.value = toUserMessage(error, "登录失败，请检查网络后重试");
    ElMessage.error(errorMessage.value);
  } finally {
    submitting.value = false;
  }
}

async function submitCode(): Promise<void> {
  if (submitting.value) return;
  errorMessage.value = "";
  if (!code.value.trim()) {
    errorMessage.value = "请输入验证器上的 6 位动态码（或恢复码）";
    return;
  }
  submitting.value = true;
  try {
    await auth.verifyMfa(code.value.trim());
    step.value = "credentials";
    code.value = "";
    await afterSuccess();
  } catch (error) {
    errorMessage.value = toUserMessage(error, "动态码校验失败，请重试");
    ElMessage.error(errorMessage.value);
  } finally {
    submitting.value = false;
  }
}

function backToCredentials(): void {
  auth.clear();
  step.value = "credentials";
  code.value = "";
  errorMessage.value = "";
}

const accountLabel = computed(() => (mode.value === "platform" ? "平台账号" : "学号/工号"));
</script>

<template>
  <main class="login-page">
    <h1>校园陪伴精灵 · 管理后台</h1>
    <el-card shadow="never" class="login-card">
      <el-radio-group v-if="step === 'credentials'" v-model="mode" data-testid="login-mode">
        <el-radio-button value="tenant">校方</el-radio-button>
        <el-radio-button value="platform">平台运营</el-radio-button>
      </el-radio-group>

      <el-form
        v-if="step === 'credentials'"
        :model="form"
        label-position="top"
        class="login-form"
        @submit.prevent="submit"
      >
        <template v-if="mode === 'tenant'">
          <el-form-item label="学校编号">
            <el-input
              v-model="form.schoolCode"
              placeholder="如 SCH-7K3M9Q2P"
              data-testid="school-code"
            />
          </el-form-item>
          <el-form-item label="学号 / 工号 / 邮箱">
            <el-input
              v-model="form.loginId"
              placeholder="校方管理员为注册邮箱"
              data-testid="login-id"
            />
          </el-form-item>
        </template>
        <el-form-item v-else label="平台账号">
          <el-input v-model="form.account" placeholder="平台账号" data-testid="account" />
        </el-form-item>

        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            placeholder="密码"
            data-testid="password"
            @keyup.enter="submit"
          />
        </el-form-item>

        <p v-if="errorMessage" class="error" role="alert" data-testid="login-error">
          {{ errorMessage }}
        </p>

        <el-button
          type="primary"
          class="submit"
          :loading="submitting"
          data-testid="login-submit"
          @click="submit"
        >
          登录
        </el-button>

        <router-link
          class="forgot"
          :to="{ name: 'forgot-password' }"
          data-testid="forgot-password-link"
        >
          忘记密码？
        </router-link>
      </el-form>

      <el-form v-else label-position="top" class="login-form" @submit.prevent="submitCode">
        <p class="hint" data-testid="mfa-step-hint">
          请输入{{ accountLabel }}对应的验证器动态码（6 位数字），或一次性恢复码。
        </p>

        <el-form-item label="动态码">
          <el-input
            v-model="code"
            placeholder="123456"
            autocomplete="one-time-code"
            data-testid="mfa-code"
            @keyup.enter="submitCode"
          />
        </el-form-item>

        <p v-if="errorMessage" class="error" role="alert" data-testid="login-error">
          {{ errorMessage }}
        </p>

        <el-button
          type="primary"
          class="submit"
          :loading="submitting"
          data-testid="mfa-submit"
          @click="submitCode"
        >
          验证并登录
        </el-button>

        <el-button text class="submit" data-testid="mfa-back" @click="backToCredentials">
          返回重新登录
        </el-button>
      </el-form>
    </el-card>
  </main>
</template>

<style scoped>
.login-page {
  max-width: 420px;
  margin: 0 auto;
  padding: 64px 24px;
}
.login-card :deep(.el-card__body) {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.submit {
  width: 100%;
}
.error {
  color: var(--el-color-danger);
  margin: 0 0 8px;
}
.hint {
  color: var(--el-text-color-secondary);
  font-size: 14px;
  margin: 0 0 8px;
}
.forgot {
  display: block;
  margin-top: 12px;
  text-align: center;
  color: var(--el-color-primary);
  text-decoration: none;
  font-size: 14px;
}
</style>
