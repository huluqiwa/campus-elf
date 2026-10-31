<script setup lang="ts">
/** 扫描状态标签（M3 KB-01B）：开发旁路引擎强制警告色，未知档位显示原码。 */
import type { KbScanStateView } from "@campus-elf/api-client";

import { scanEngineLabel, scanStateMeta, scanTone } from "../kb-logic";

defineProps<{ scan: KbScanStateView | null }>();
</script>

<template>
  <span v-if="scan === null" class="empty">—</span>
  <span v-else class="scan" data-testid="kb-scan-tag">
    <el-tag :type="scanTone(scan)" size="small">{{ scanStateMeta(scan.state).label }}</el-tag>
    <span class="engine">{{ scanEngineLabel(scan.engine) }}</span>
  </span>
</template>

<style scoped>
.scan {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.engine {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.empty {
  color: var(--el-text-color-secondary);
}
</style>
