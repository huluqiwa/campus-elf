import { readFileSync } from "node:fs";

/**
 * M1 验收（TD-110）全栈 E2E 支撑：种子账号与真实 API 调用。
 *
 * 账号文件由 `backend/scripts/e2e_seed.py` 生成（scripts/e2e-stack.sh up 编排），
 * 路径经 E2E_ACCOUNTS_FILE 注入；口令只存在于该 git 忽略文件与后端哈希列。
 */

export interface E2EAccounts {
  e2e_base_url: string;
  mailhog_api: string;
  school_code: string;
  school_name: string;
  platform: { account: string; password: string };
  school_admin_forced: {
    school_code: string;
    login_id: string;
    initial_password: string;
  };
  school_ops: { school_code: string; login_id: string; password: string };
  teacher: {
    school_code: string;
    login_id: string;
    email: string;
    password: string;
  };
  student: { school_code: string; login_id: string; password: string };
  resetter: {
    school_code: string;
    login_id: string;
    email: string;
    password: string;
  };
  devices: { school_code: string; login_id: string; password: string };
}

export function loadAccounts(): E2EAccounts {
  const file = process.env.E2E_ACCOUNTS_FILE;
  if (!file) {
    throw new Error(
      "E2E_ACCOUNTS_FILE 未设置：先运行 scripts/e2e-stack.sh up，再按其输出导出环境变量",
    );
  }
  return JSON.parse(readFileSync(file, "utf-8")) as E2EAccounts;
}

/** Node 侧真实登录（独立 UA = 独立设备，供会话设备管理用例区分两台设备）。 */
export const NODE_USER_AGENT = "campus-elf-e2e-node/1.0";

export async function apiLogin(
  accounts: E2EAccounts,
  payload: Record<string, unknown>,
): Promise<string> {
  const response = await fetch(`${accounts.e2e_base_url}/api/v1/auth/login`, {
    method: "POST",
    headers: { "content-type": "application/json", "user-agent": NODE_USER_AGENT },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(`真实登录失败 HTTP ${response.status}: ${await response.text()}`);
  }
  const body = (await response.json()) as { access_token?: string };
  if (!body.access_token) {
    throw new Error("登录响应缺少 access_token（可能进入 MFA 挑战，种子账号未启用 MFA）");
  }
  return body.access_token;
}

export async function apiGet(
  accounts: E2EAccounts,
  path: string,
  token: string,
): Promise<{ status: number; json: unknown }> {
  const response = await fetch(`${accounts.e2e_base_url}${path}`, {
    headers: { authorization: `Bearer ${token}`, "user-agent": NODE_USER_AGENT },
  });
  return { status: response.status, json: await response.json().catch(() => null) };
}

export async function apiPost(
  accounts: E2EAccounts,
  path: string,
  token: string,
  body?: Record<string, unknown>,
): Promise<{ status: number; json: unknown }> {
  const response = await fetch(`${accounts.e2e_base_url}${path}`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${token}`,
      "user-agent": NODE_USER_AGENT,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return { status: response.status, json: await response.json().catch(() => null) };
}

/** 解码 MailHog 邮件正文（Raw.Data 完整 MIME；与 tests/support/mailhog.py 的 message_body 对齐）。 */
function decodeMailBody(message: Record<string, unknown>): string {
  const raw = (message.Raw as { Data?: string } | undefined)?.Data;
  if (!raw) return String((message.Content as { Body?: string } | undefined)?.Body ?? "");
  const parts = raw.split(/\r?\n\r?\n/);
  const header = parts[0] ?? "";
  const body = parts.slice(1).join("\r\n\r\n");
  const encoding = /content-transfer-encoding:\s*(\S+)/i.exec(header)?.[1]?.toLowerCase() ?? "7bit";
  if (encoding === "base64") {
    try {
      return Buffer.from(body.replace(/\s+/g, ""), "base64").toString("utf-8");
    } catch {
      return body;
    }
  }
  if (encoding === "quoted-printable") {
    return body
      .replace(/=\r?\n/g, "")
      .replace(/=([0-9A-Fa-f]{2})/g, (_, hex: string) =>
        String.fromCharCode(Number.parseInt(hex, 16)),
      );
  }
  return body;
}

/** 从 MailHog 取某收件人最新一封邮件的正文（/api/v2/search，倒序第一条）。 */
export async function fetchLatestMailBody(
  accounts: E2EAccounts,
  to: string,
  timeoutMs = 15_000,
): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  let lastError: unknown = null;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(
        `${accounts.mailhog_api}/api/v2/search?kind=to&query=${encodeURIComponent(to)}`,
      );
      if (!response.ok) throw new Error(`MailHog HTTP ${response.status}`);
      const data = (await response.json()) as { items?: Array<Record<string, unknown>> };
      const newest = data.items?.[0];
      if (newest) return decodeMailBody(newest);
      lastError = new Error("暂无匹配邮件");
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`等待 MailHog 邮件超时（to=${to}）：${String(lastError)}`);
}

/** 从重置邮件正文提取令牌（链接 fragment #token=…；与 tests/support/mailhog.py 的 extract_reset_token 对齐）。 */
export function extractResetToken(body: string): string {
  const url = body.match(/https?:\/\/\S+/)?.[0];
  if (!url) throw new Error("重置邮件正文缺少重置链接");
  const fragment = url.split("#")[1] ?? "";
  const token = new URLSearchParams(fragment).get("token");
  if (!token) throw new Error(`重置链接缺少 token fragment：${url}`);
  return token;
}
