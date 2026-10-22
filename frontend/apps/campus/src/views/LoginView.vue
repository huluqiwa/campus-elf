<script setup lang="ts">
/** C 端登录页（M1-C-UI + M1-D2）：学校编号 + 学号/工号 + 密码，绑定验证器后追加 6 位动态码。 */
import { toUserMessage } from "@campus-elf/api-client";
import { showToast } from "vant";
import { reactive, ref } from "vue";
import { useRouter } from "vue-router";

import { useAuthStore } from "../stores/auth";

const router = useRouter();
const auth = useAuthStore();
const submitting = ref(false);
const errorMessage = ref("");
/** 动态码步骤：密码已通过，等待 6 位码或恢复码。 */
const step = ref<"credentials" | "code">("credentials");
const code = ref("");
const form = reactive({ schoolCode: "", loginId: "", password: "" });

async function afterSuccess(): Promise<void> {
  if (auth.mustChangePassword) {
    await router.replace({ name: "change-password" });
    return;
  }
  await router.replace({ name: "home" });
}

async function submit(): Promise<void> {
  if (submitting.value) return;
  errorMessage.value = "";
  if (!form.schoolCode || !form.loginId || !form.password) {
    errorMessage.value = "请填写学校编号、学号/工号与密码";
    return;
  }
  submitting.value = true;
  try {
    const outcome = await auth.login({
      schoolCode: form.schoolCode,
      loginId: form.loginId,
      password: form.password,
    });
    if (outcome.status === "mfaRequired") {
      form.password = "";
      code.value = "";
      step.value = "code";
      return;
    }
    if (outcome.status === "mfaEnrollmentRequired") {
      errorMessage.value = "该账号需先由管理员完成验证器绑定";
      showToast(errorMessage.value);
      return;
    }
    await afterSuccess();
  } catch (error) {
    errorMessage.value = toUserMessage(error, "登录失败，请检查网络后重试");
    showToast(errorMessage.value);
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
    showToast(errorMessage.value);
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
</script>

<template>
  <div class="campus-page">
    <h1>校园陪伴精灵</h1>
    <van-form v-if="step === 'credentials'" @submit="submit">
      <van-cell-group inset>
        <van-field
          v-model="form.schoolCode"
          name="schoolCode"
          label="学校编号"
          placeholder="如 SCH-7K3M9Q2P"
          data-testid="school-code"
        />
        <van-field
          v-model="form.loginId"
          name="loginId"
          label="学号/工号"
          placeholder="学号或工号"
          data-testid="login-id"
        />
        <van-field
          v-model="form.password"
          type="password"
          name="password"
          label="密码"
          placeholder="密码"
          data-testid="password"
        />
      </van-cell-group>
      <p v-if="errorMessage" class="error" role="alert" data-testid="login-error">
        {{ errorMessage }}
      </p>
      <div class="actions">
        <van-button
          round
          block
          type="primary"
          native-type="submit"
          :loading="submitting"
          data-testid="login-submit"
        >
          登录
        </van-button>
        <router-link
          class="forgot"
          :to="{ name: 'forgot-password' }"
          data-testid="forgot-password-link"
        >
          忘记密码？
        </router-link>
      </div>
    </van-form>

    <van-form v-else @submit="submitCode">
      <van-cell-group inset>
        <van-field
          v-model="code"
          name="code"
          label="动态码"
          placeholder="123456"
          autocomplete="one-time-code"
          data-testid="mfa-code"
        />
      </van-cell-group>
      <p v-if="errorMessage" class="error" role="alert" data-testid="login-error">
        {{ errorMessage }}
      </p>
      <div class="actions">
        <van-button
          round
          block
          type="primary"
          native-type="submit"
          :loading="submitting"
          data-testid="mfa-submit"
        >
          验证并登录
        </van-button>
        <van-button
          round
          block
          plain
          type="primary"
          data-testid="mfa-back"
          @click="backToCredentials"
        >
          返回重新登录
        </van-button>
      </div>
    </van-form>
  </div>
</template>

<style scoped>
.campus-page {
  padding: 48px 0;
}
h1 {
  text-align: center;
  font-size: 20px;
  margin-bottom: 24px;
}
.actions {
  padding: 16px;
}
.error {
  color: #d43030;
  text-align: center;
  margin: 8px 0 0;
}
.forgot {
  display: block;
  margin-top: 12px;
  text-align: center;
  color: #1989fa;
  text-decoration: none;
  font-size: 14px;
}
</style>
