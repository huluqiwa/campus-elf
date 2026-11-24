/** C 端反馈 API（M4-UI2）：提交（幂等重放）+ 删除（匿名化）——消费 AI-05 既有端点。
 *
 * 走 `session.request` 基建（401 自动刷新重放——与 campus-chat 会话方法同源）；
 * 错误由 request 帮手抛出（含后端信封 detail 文案），UI 层 toast 展示。
 * 载荷契约（后端 campus_feedback.py）：rating ∈ helpful|unhelpful；
 * reason_tags 枚举 inaccurate/irrelevant/outdated/harmful/other；comment ≤500。
 */

import type { AuthSession } from "./auth";

export const FEEDBACK_RATINGS = ["helpful", "unhelpful"] as const;
export type FeedbackRating = (typeof FEEDBACK_RATINGS)[number];

export const FEEDBACK_TAGS = ["inaccurate", "irrelevant", "outdated", "harmful", "other"] as const;
export type FeedbackTag = (typeof FEEDBACK_TAGS)[number];

export interface FeedbackSubmitInput {
  conversationId: string;
  turnId: string;
  traceId: string;
  rating: FeedbackRating;
  reasonTags: FeedbackTag[];
  comment?: string;
}

export interface FeedbackSubmitResult {
  feedbackId: string;
  replayed: boolean;
}

export interface FeedbackApi {
  submitFeedback(input: FeedbackSubmitInput): Promise<FeedbackSubmitResult>;
  deleteFeedback(feedbackId: string): Promise<{ anonymized: boolean }>;
}

export function createFeedbackApi(session: AuthSession, options: { baseUrl: string }): FeedbackApi {
  void options; // baseUrl 由 session.request 统一管理；保留形参对称性
  return {
    async submitFeedback(input) {
      const payload = await session.request<{ feedback_id: string; replayed: boolean }>(
        "/api/v1/campus/feedback",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            conversation_id: input.conversationId,
            turn_id: input.turnId,
            trace_id: input.traceId,
            rating: input.rating,
            reason_tags: input.reasonTags,
            ...(input.comment ? { comment: input.comment } : {}),
          }),
        },
      );
      return { feedbackId: payload.feedback_id, replayed: payload.replayed };
    },

    async deleteFeedback(feedbackId) {
      const payload = await session.request<{ anonymized: boolean }>(
        `/api/v1/campus/feedback/${feedbackId}`,
        { method: "DELETE" },
      );
      return { anonymized: payload.anonymized };
    },
  };
}
