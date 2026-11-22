<script setup lang="ts">
import { computed } from "vue";
import { useRouter } from "vue-router";

import { useAuthStore } from "../stores/auth";

const router = useRouter();
const auth = useAuthStore();
const canChat = computed(() => auth.user?.permissions?.includes("campus.kb.qa") ?? false);

async function logout(): Promise<void> {
  await auth.logout();
  await router.replace({ name: "login" });
}
</script>

<template>
  <div class="campus-page">
    <van-nav-bar title="校园陪伴精灵" />
    <h1>C 端 H5 脚手架</h1>
    <van-cell-group inset>
      <van-cell title="当前用户" :value="auth.user?.userId ?? '-'" />
    </van-cell-group>
    <div class="actions">
      <van-button
        v-if="canChat"
        round
        block
        type="primary"
        data-testid="go-chat"
        @click="router.push({ name: 'chat' })"
      >
        开始 AI 问答
      </van-button>
      <van-button
        round
        block
        plain
        data-testid="go-conversations"
        @click="router.push({ name: 'conversations' })"
      >
        历史会话
      </van-button>
      <van-button
        round
        block
        plain
        data-testid="go-change-password"
        @click="router.push({ name: 'change-password' })"
      >
        修改密码
      </van-button>
      <van-button round block type="primary" class="logout" data-testid="logout" @click="logout">
        退出登录
      </van-button>
    </div>
  </div>
</template>

<style scoped>
.campus-page {
  padding-bottom: 32px;
}
h1 {
  text-align: center;
  font-size: 18px;
  margin: 16px 0;
}
.actions {
  padding: 16px;
}
.logout {
  margin-top: 12px;
}
</style>
