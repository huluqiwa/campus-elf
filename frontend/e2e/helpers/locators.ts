import type { Locator, Page } from "@playwright/test";

/**
 * 表单字段定位器：兼容 Element Plus / Vant 两种属性落点。
 * - Element Plus 把 fallthrough attrs 绑到内部原生 `<input>`；
 * - Vant Field 把 attrs 留在外层容器，内部才是 `<input>`。
 * 因此用 "容器内的 input" 或 "input 自身" 两种选择器并联。
 */
export function field(page: Page, testId: string): Locator {
  return page
    .locator(
      `[data-testid="${testId}"] input, [data-testid="${testId}"] textarea, input[data-testid="${testId}"], textarea[data-testid="${testId}"]`,
    )
    .first();
}
