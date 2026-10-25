/** 业务 API 单例（M2-UI1）：与 session 同生命周期，页面直接 import 使用。 */

import { createImportsApi, createOrgApi } from "@campus-elf/api-client";

import { session } from "./session";

export const orgApi = createOrgApi(session);
export const importsApi = createImportsApi(session);
