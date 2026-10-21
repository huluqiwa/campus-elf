import { ElMessage } from "element-plus";
import { createPinia } from "pinia";
import { createApp } from "vue";

import App from "./App.vue";
import { router } from "./router";
import { authOptions } from "./session";
import { useAuthStore } from "./stores/auth";

// Element Plus 组件与样式由 unplugin-vue-components 按需注入（见 vite.config.ts）；
// 脚手架阶段不再全量注册，中文语言包在接入 ConfigProvider 时按需引入。
const app = createApp(App).use(createPinia()).use(router);

// 会话回调在路由就绪后绑定（避免 store ↔ router 循环依赖）
authOptions.onUnauthorized = (reason: string) => {
  useAuthStore().clear();
  ElMessage.error(reason);
  void router.replace({ name: "login" });
};
authOptions.onPasswordChangeRequired = () => {
  void router.replace({ name: "change-password" });
};
authOptions.onForbidden = (message: string) => {
  ElMessage.warning(message);
};

app.mount("#app");
