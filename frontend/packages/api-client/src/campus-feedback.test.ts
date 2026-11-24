/** campus-feedback 单元测试：载荷映射（snake_case）/幂等结果/错误传播。 */

import { describe, expect, it, vi } from "vitest";

import { createFeedbackApi, FEEDBACK_RATINGS, FEEDBACK_TAGS } from "./campus-feedback";

function makeApi() {
  const request = vi.fn(async <T>(path: string, init?: RequestInit): Promise<T> => {
    if (path.endsWith("/feedback") && init?.method === "POST") {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      // 校验载荷契约（snake_case + 无多余字段）
      expect(Object.keys(body).sort()).toEqual([
        "comment",
        "conversation_id",
        "rating",
        "reason_tags",
        "trace_id",
        "turn_id",
      ]);
      return { feedback_id: "fb-1", replayed: false } as T;
    }
    return { anonymized: true } as T;
  });
  const session = { request, getAccessToken: vi.fn(() => "tok"), refresh: vi.fn() };
  const api = createFeedbackApi(session as unknown as Parameters<typeof createFeedbackApi>[0], {
    baseUrl: "",
  });
  return { api, request };
}

describe("campusFeedback", () => {
  it("submitFeedback：载荷 snake_case 映射 + 结果 camelCase", async () => {
    const { api, request } = makeApi();
    const result = await api.submitFeedback({
      conversationId: "c1",
      turnId: "t1",
      traceId: "r1",
      rating: "unhelpful",
      reasonTags: ["inaccurate"],
      comment: "引用对不上",
    });
    expect(result).toEqual({ feedbackId: "fb-1", replayed: false });
    const body = JSON.parse(String(request.mock.calls[0][1]?.body));
    expect(body).toMatchObject({
      conversation_id: "c1",
      turn_id: "t1",
      trace_id: "r1",
      rating: "unhelpful",
      reason_tags: ["inaccurate"],
      comment: "引用对不上",
    });
    expect(request.mock.calls[0][0]).toBe("/api/v1/campus/feedback");
  });

  it("submitFeedback：幂等重放透传 replayed", async () => {
    const { api, request } = makeApi();
    request.mockResolvedValueOnce({ feedback_id: "fb-1", replayed: true });
    const result = await api.submitFeedback({
      conversationId: "c1",
      turnId: "t1",
      traceId: "r1",
      rating: "helpful",
      reasonTags: [],
    });
    expect(result.replayed).toBe(true);
  });

  it("deleteFeedback：DELETE + 结果透传", async () => {
    const { api, request } = makeApi();
    const result = await api.deleteFeedback("fb-1");
    expect(result.anonymized).toBe(true);
    expect(request.mock.calls[0][0]).toBe("/api/v1/campus/feedback/fb-1");
    expect((request.mock.calls[0][1] as RequestInit).method).toBe("DELETE");
  });

  it("枚举契约：rating/tag 取值与后端 REASON_TAGS、RATING_VALUES 一致", () => {
    // 后端改枚举前端不会编译失败，只会在运行时 400。这里按后端契约固定取值：
    // backend/app/feedback/service.py 的 RATING_VALUES / REASON_TAGS。
    expect([...FEEDBACK_RATINGS]).toEqual(["helpful", "unhelpful"]);
    expect([...FEEDBACK_TAGS]).toEqual([
      "inaccurate",
      "irrelevant",
      "outdated",
      "harmful",
      "other",
    ]);
  });
});
