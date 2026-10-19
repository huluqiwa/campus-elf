import { createPinia } from "pinia";
import { createApp } from "vue";

import App from "./App.vue";
import { router } from "./router";

// Element Plus 组件与样式由 unplugin-vue-components 按需注入（见 vite.config.ts）；
// 脚手架阶段不再全量注册，中文语言包在接入 ConfigProvider 时按需引入。
createApp(App).use(createPinia()).use(router).mount("#app");
