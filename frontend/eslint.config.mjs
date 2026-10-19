// ESLint 扁平配置（ESLint 10）：JS/TS/Vue 统一规则；与 Prettier 分工（格式化交给 Prettier）。
import js from "@eslint/js";
import prettier from "eslint-config-prettier";
import pluginVue from "eslint-plugin-vue";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    ignores: [
      "**/dist/**",
      "**/node_modules/**",
      "playwright-report/**",
      "test-results/**",
      "**/.vite/**",
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs["flat/recommended"],
  {
    files: ["**/*.vue"],
    languageOptions: {
      parserOptions: {
        parser: tseslint.parser,
      },
    },
  },
  {
    files: ["**/*.{js,mjs,cjs,ts,vue}"],
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
    },
    rules: {
      // 单文件组件命名（App.vue/HomeView.vue 等已符合多词规则；脚手架页面允许单词名）
      "vue/multi-word-component-names": "off",
      // 脚手架阶段允许显式 any 出现在适配层（后续接入 OpenAPI 生成类型后收紧）
      "@typescript-eslint/no-explicit-any": "warn",
    },
  },
  prettier,
);
