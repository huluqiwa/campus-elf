<script setup lang="ts">
/**
 * 登录页（M1-C-UI）：平台运营与校方共用一页，通过身份切换区分 realm。
 * 平台：账号 + 密码；校方/C 端管理账号：学校编号 + 学号/工号/邮箱 + 密码。
 */
import { toUserMessage } from "@campus-elf/api-client";
import { ElMessage } from "element-plus";
import { reactive, ref } from "vue";
import { useRouter } from "vue-router";

import { useAuthStore } from "../stores/auth";

const router = useRouter();
const auth = useAuthStore();

const mode = ref<"platform" | "tenant">("tenant");
const submitting = ref(false);
const errorMessage = ref("");

const form = reactive({
  schoolCode: "",
  loginId: "",
  account: "",
  password: "",
});

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
    const tokens =
      mode.value === "platform"
        ? await auth.loginPlatform({ account: form.account, password: form.password })
        : await auth.loginTenant({
            schoolCode: form.schoolCode,
            loginId: form.loginId,
            password: form.password,
          });
    if (tokens.mustChangePassword) {
      await router.replace({ name: "change-password" });
      return;
    }
    await router.replace({ name: "home" });
  } catch (error) {
    errorMessage.value = toUserMessage(error, "登录失败，请检查网络后重试");
    ElMessage.error(errorMessage.value);
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <main class="login-page">
    <h1>校园陪伴精灵 · 管理后台</h1>
    <el-card shadow="never" class="login-card">
      <el-radio-group v-model="mode" data-testid="login-mode">
        <el-radio-button value="tenant">校方</el-radio-button>
        <el-radio-button value="platform">平台运营</el-radio-button>
      </el-radio-group>

      <el-form :model="form" label-position="top" class="login-form" @submit.prevent="submit">
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
</style>
