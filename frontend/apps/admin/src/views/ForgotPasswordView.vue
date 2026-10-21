<script setup lang="ts">
/**
 * 找回密码（M1-D1）：校方/平台共用一页，与登录页的身份切换一致。
 * 无论账号是否存在，后端都返回 202，前端只展示统一提示，避免账号枚举。
 */
import { toUserMessage } from "@campus-elf/api-client";
import { reactive, ref } from "vue";

import { session } from "../session";

const mode = ref<"platform" | "tenant">("tenant");
const submitting = ref(false);
const submitted = ref(false);
const errorMessage = ref("");

const form = reactive({ schoolCode: "", loginId: "", account: "" });

async function submit(): Promise<void> {
  if (submitting.value) return;
  errorMessage.value = "";
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
    await session.requestPasswordReset(
      mode.value === "tenant"
        ? { realm: "tenant", schoolCode: form.schoolCode, loginId: form.loginId }
        : { realm: "platform", account: form.account },
    );
    submitted.value = true;
  } catch (error) {
    errorMessage.value = toUserMessage(error, "提交失败，请检查网络后重试");
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <main class="forgot-page">
    <h1>找回密码</h1>
    <el-card shadow="never" class="forgot-card">
      <template v-if="submitted">
        <p class="hint" data-testid="reset-request-success">
          如果账号存在且已绑定邮箱，重置邮件已发送，请查收（30 分钟内有效，仅可使用一次）。
        </p>
      </template>
      <template v-else>
        <el-radio-group v-model="mode" data-testid="reset-mode">
          <el-radio-button value="tenant">校方</el-radio-button>
          <el-radio-button value="platform">平台运营</el-radio-button>
        </el-radio-group>

        <el-form :model="form" label-position="top" class="forgot-form" @submit.prevent="submit">
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

          <p v-if="errorMessage" class="error" role="alert" data-testid="reset-request-error">
            {{ errorMessage }}
          </p>

          <el-button
            type="primary"
            class="submit"
            :loading="submitting"
            data-testid="reset-request-submit"
            @click="submit"
          >
            发送重置邮件
          </el-button>
        </el-form>
      </template>

      <router-link class="back" :to="{ name: 'login' }" data-testid="back-to-login">
        返回登录
      </router-link>
    </el-card>
  </main>
</template>

<style scoped>
.forgot-page {
  max-width: 420px;
  margin: 0 auto;
  padding: 64px 24px;
}
h1 {
  text-align: center;
  font-size: 20px;
  margin-bottom: 24px;
}
.forgot-card :deep(.el-card__body) {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.submit {
  width: 100%;
}
.hint {
  color: #3a7d44;
  margin: 0;
}
.error {
  color: var(--el-color-danger);
  margin: 0 0 8px;
}
.back {
  color: var(--el-color-primary);
  text-decoration: none;
  font-size: 14px;
}
</style>
