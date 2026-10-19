import { createPinia } from "pinia";
import { Cell, CellGroup } from "vant";
import "vant/lib/index.css";
import { createApp } from "vue";

import App from "./App.vue";
import { router } from "./router";

createApp(App).use(createPinia()).use(router).use(Cell).use(CellGroup).mount("#app");
