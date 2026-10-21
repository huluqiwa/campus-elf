<script setup lang="ts">
/**
 * C 端找回密码（M1-D1）：学校编号 + 学号/工号。
 * 无论账号是否存在都提示"已发送"，避免账号枚举。
 */
import { toUserMessage } from "@campus-elf/api-client";
import { reactive, ref } from "vue";

import { session } from "../session";

const submitting = ref(false);
const submitted = ref(false);
const errorMessage = ref("");
const form = reactive({ schoolCode: "", loginId: "" });

async function submit(): Promise<void> {
  if (submitting.value) return;
  errorMessage.value = "";
  if (!form.schoolCode || !form.loginId) {
    errorMessage.value = "请填写学校编号与学号/工号";
    return;
  }
  submitting.value = true;
  try {
    await session.requestPasswordReset({
      realm: "tenant",
      schoolCode: form.schoolCode,
      loginId: form.loginId,
    });
    submitted.value = true;
  } catch (error) {
    errorMessage.value = toUserMessage(error, "提交失败，请检查网络后重试");
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div class="campus-page">
    <h1>找回密码</h1>

    <p class="notice" data-testid="student-reset-notice">
      学生账号暂不支持自助找回（后续版本开放）；教师/员工可使用邮箱重置。
    </p>

    <p v-if="submitted" class="hint" data-testid="reset-request-success">
      如果该账号支持邮箱重置且已绑定邮箱，重置邮件已发送，请查收（30 分钟内有效，仅可使用一次）。
    </p>
    <van-form v-else @submit="submit">
      <van-cell-group inset>
        <van-field
          v-model="form.schoolCode"
          label="学校编号"
          placeholder="如 SCH-7K3M9Q2P"
          data-testid="school-code"
        />
        <van-field
          v-model="form.loginId"
          label="学号/工号"
          placeholder="学号或工号"
          data-testid="login-id"
        />
      </van-cell-group>
      <p v-if="errorMessage" class="error" role="alert" data-testid="reset-request-error">
        {{ errorMessage }}
      </p>
      <div class="actions">
        <van-button
          round
          block
          type="primary"
          native-type="submit"
          :loading="submitting"
          data-testid="reset-request-submit"
        >
          发送重置邮件
        </van-button>
      </div>
    </van-form>

    <div class="actions">
      <router-link class="back" :to="{ name: 'login' }" data-testid="back-to-login">
        返回登录
      </router-link>
    </div>
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
.hint {
  padding: 0 24px;
  color: #3a7d44;
  text-align: center;
}
.notice {
  padding: 0 24px;
  margin-bottom: 16px;
  color: #8a6d3b;
  font-size: 13px;
  line-height: 1.6;
  text-align: center;
}
.error {
  color: #d43030;
  text-align: center;
  margin: 8px 0 0;
}
.back {
  display: block;
  text-align: center;
  color: #1989fa;
  text-decoration: none;
  font-size: 14px;
}
</style>
