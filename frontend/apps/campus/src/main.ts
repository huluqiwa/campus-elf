import { createPinia } from "pinia";
import {
  Button,
  Cell,
  CellGroup,
  Dialog,
  Field,
  Form,
  List,
  NavBar,
  Radio,
  RadioGroup,
  showToast,
} from "vant";
import "vant/lib/index.css";
import { createApp } from "vue";

import App from "./App.vue";
import { router } from "./router";
import { authOptions } from "./session";
import { useAuthStore } from "./stores/auth";

const app = createApp(App)
  .use(createPinia())
  .use(router)
  .use(Button)
  .use(Cell)
  .use(CellGroup)
  .use(Field)
  .use(Form)
  .use(NavBar)
  .use(Radio)
  .use(RadioGroup)
  .use(List)
  .use(Dialog);

// 会话回调在路由就绪后绑定（避免 store ↔ router 循环依赖）
authOptions.onUnauthorized = (reason: string) => {
  useAuthStore().clear();
  showToast(reason);
  void router.replace({ name: "login" });
};
authOptions.onPasswordChangeRequired = () => {
  void router.replace({ name: "change-password" });
};
authOptions.onForbidden = (message: string) => {
  showToast(message);
};

app.mount("#app");
