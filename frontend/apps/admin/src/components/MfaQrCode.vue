<script setup lang="ts">
/**
 * 本地渲染绑定二维码（M1-D2）。
 *
 * secret 只在浏览器内转成 data URL，绝不发给任何第三方二维码服务或写进日志。
 */
import { toDataURL } from "qrcode";
import { ref, watch } from "vue";

const props = defineProps<{ value: string; label?: string }>();

const dataUrl = ref("");

watch(
  () => props.value,
  async (value) => {
    dataUrl.value = value ? await toDataURL(value, { errorCorrectionLevel: "M" }) : "";
  },
  { immediate: true },
);
</script>

<template>
  <div v-if="dataUrl" class="qr" data-testid="mfa-qr">
    <img :src="dataUrl" :alt="props.label ?? '验证器绑定二维码'" />
  </div>
</template>

<style scoped>
.qr {
  display: flex;
  justify-content: center;
  margin: 8px 0;
}
.qr img {
  width: 180px;
  height: 180px;
}
</style>
