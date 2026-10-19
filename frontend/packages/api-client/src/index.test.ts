import { describe, expect, it, vi } from "vitest";

import { createApiClient } from "./index";

/** 构造可控的 fetch 替身。 */
function fetchStub(response: {
  ok?: boolean;
  status?: number;
  json?: () => Promise<unknown>;
  rejectWith?: Error;
}): { fetchImpl: typeof fetch; calls: string[] } {
  const calls: string[] = [];
  const fetchImpl = (async (input: RequestInfo | URL) => {
    calls.push(String(input));
    if (response.rejectWith) {
      throw response.rejectWith;
    }
    return {
      ok: response.ok ?? true,
      status: response.status ?? 200,
      json: response.json ?? (async () => ({})),
    } as Response;
  }) as typeof fetch;
  return { fetchImpl, calls };
}

describe("createApiClient", () => {
  it("拼接健康检查路径并归一化尾斜杠", async () => {
    const { fetchImpl, calls } = fetchStub({
      json: async () => ({
        status: "ok",
        service: "CampusCompanionElf",
        version: "0.1.0",
        env: "test",
      }),
    });

    const client = createApiClient({ baseUrl: "http://api.example.com///", fetchImpl });
    const result = await client.getHealth();

    expect(calls).toEqual(["http://api.example.com/health/live"]);
    expect(result.service).toBe("CampusCompanionElf");
  });

  it("空 baseUrl 时使用同源相对路径", async () => {
    const { fetchImpl, calls } = fetchStub({ json: async () => ({ status: "ok" }) });

    await createApiClient({ baseUrl: "", fetchImpl }).getHealth();

    expect(calls).toEqual(["/health/live"]);
  });

  it("非 2xx 响应抛出带状态码的错误", async () => {
    const { fetchImpl } = fetchStub({ ok: false, status: 503 });

    await expect(createApiClient({ baseUrl: "", fetchImpl }).getHealth()).rejects.toThrow(
      "health request failed: 503",
    );
  });

  it("超时触发 AbortSignal（不挂起调用方）", async () => {
    vi.useFakeTimers();
    try {
      let abortedSignal: AbortSignal | undefined;
      const fetchImpl = ((_: RequestInfo | URL, init?: RequestInit) =>
        new Promise((_, reject) => {
          abortedSignal = init?.signal ?? undefined;
          abortedSignal?.addEventListener("abort", () => reject(new Error("aborted")));
        })) as typeof fetch;

      const promise = createApiClient({ baseUrl: "", fetchImpl, timeoutMs: 50 }).getHealth();
      const assertion = expect(promise).rejects.toThrow("aborted");
      await vi.advanceTimersByTimeAsync(60);

      await assertion;
      expect(abortedSignal?.aborted).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("透传网络错误，不吞异常", async () => {
    const { fetchImpl } = fetchStub({ rejectWith: new TypeError("network down") });

    await expect(createApiClient({ baseUrl: "", fetchImpl }).getHealth()).rejects.toThrow(
      "network down",
    );
  });
});
