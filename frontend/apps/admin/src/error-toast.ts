import { ApiError, toUserMessage } from "@campus-elf/api-client";
import { ElMessage } from "element-plus";

/**
 * 失败提示（M2-UI3 TD-187g-C）：写动作与读取失败时说人话。
 *
 * **403 不在这里弹**：`main.ts:23` 的全局回调已对每个 403 用服务端原因提示过一次，
 * 这里再补一条，屏幕上会叠出两行同一句话（E2E 的 strict-mode 断言就是这么撞出来的）。
 * 其余状态码全局回调不管，仍由调用点给兜底文案。
 */
export function reportError(error: unknown, fallback: string): void {
  if (error instanceof ApiError && error.status === 403) return;
  ElMessage.error(toUserMessage(error, fallback));
}
