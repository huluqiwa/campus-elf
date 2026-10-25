import type { Page } from "@playwright/test";

/**
 * 校方业务接口 mock（M2-UI1）：与 auth-mock 同风格的路径路由 + 请求体捕获。
 * 默认返回最小可用数据集；各用例可覆盖任意端点。
 */

export interface ClassFixture {
  id: string;
  entry_year: number;
  grade: string;
  name: string;
  status: "active" | "archived";
  head_teacher_id: string | null;
  counselor_id: string | null;
}

export interface TeacherFixture {
  id: string;
  user_id: string;
  login_id: string;
  employee_no: string;
  name: string;
  age: number | null;
  gender: string | null;
  position: string | null;
  department_id: string | null;
  initial_password: string | null;
}

export interface StudentFixture {
  id: string;
  user_id: string;
  login_id: string;
  student_no: string;
  name: string;
  gender: string | null;
  class_id: string | null;
  initial_password: string | null;
}

export const FIXTURE_TEACHER: TeacherFixture = {
  id: "30000000-0000-0000-0000-000000000001",
  user_id: "30000000-0000-0000-0000-000000000011",
  login_id: "E-9001",
  employee_no: "E-9001",
  name: "王老师",
  age: null,
  gender: "female",
  position: null,
  department_id: null,
  initial_password: null,
};

export const FIXTURE_STUDENT: StudentFixture = {
  id: "30000000-0000-0000-0000-000000000002",
  user_id: "30000000-0000-0000-0000-000000000012",
  login_id: "S-9001",
  student_no: "S-9001",
  name: "小明",
  gender: "male",
  class_id: null,
  initial_password: null,
};

export const FIXTURE_CLASS_ACTIVE: ClassFixture = {
  id: "30000000-0000-0000-0000-000000000003",
  entry_year: 2025,
  grade: "高一",
  name: "1班",
  status: "active",
  head_teacher_id: null,
  counselor_id: null,
};

export const FIXTURE_CLASS_ARCHIVED: ClassFixture = {
  ...FIXTURE_CLASS_ACTIVE,
  id: "30000000-0000-0000-0000-000000000004",
  entry_year: 2023,
  grade: "高三",
  name: "1班",
  status: "archived",
};

/** 记录到的 POST/PUT 请求体（按路径前缀分组），供断言。 */
export interface OrgMockCaptures {
  createTeacher: Record<string, unknown>[];
  createStudent: Record<string, unknown>[];
  transfer: Record<string, unknown>[];
  createClass: Record<string, unknown>[];
  updateClass: Record<string, unknown>[];
  rollover: Record<string, unknown>[];
}

export interface OrgMockOptions {
  teachers?: TeacherFixture[];
  students?: StudentFixture[];
  classes?: ClassFixture[];
  /** 覆盖任意端点：glob 路径 → 响应。优先于默认处理器。 */
  overrides?: Record<string, { status: number; body?: unknown }>;
}

export interface OrgMock {
  captures: OrgMockCaptures;
}

export async function mockOrgApi(page: Page, options: OrgMockOptions = {}): Promise<OrgMock> {
  const teachers = options.teachers ?? [FIXTURE_TEACHER];
  const students = options.students ?? [FIXTURE_STUDENT];
  const classes = options.classes ?? [FIXTURE_CLASS_ACTIVE];
  const captures: OrgMockCaptures = {
    createTeacher: [],
    createStudent: [],
    transfer: [],
    createClass: [],
    updateClass: [],
    rollover: [],
  };

  const json = (status: number, body: unknown) => ({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });

  await page.route("**/api/v1/school/teachers**", (route) => {
    const method = route.request().method();
    if (method === "POST") {
      captures.createTeacher.push(route.request().postDataJSON() as Record<string, unknown>);
      return route.fulfill(json(201, { ...FIXTURE_TEACHER, initial_password: "Init-Pass-123" }));
    }
    return route.fulfill(json(200, { items: teachers, next_cursor: null }));
  });

  await page.route("**/api/v1/school/students**", (route) => {
    const method = route.request().method();
    if (method === "POST" && route.request().url().includes("/transfer")) {
      captures.transfer.push(route.request().postDataJSON() as Record<string, unknown>);
      const classId = (captures.transfer.at(-1)?.class_id as string | null) ?? null;
      return route.fulfill(json(200, { ...FIXTURE_STUDENT, class_id: classId }));
    }
    if (method === "POST") {
      captures.createStudent.push(route.request().postDataJSON() as Record<string, unknown>);
      return route.fulfill(json(201, { ...FIXTURE_STUDENT, initial_password: "Init-Pass-123" }));
    }
    return route.fulfill(json(200, { items: students, next_cursor: null }));
  });

  await page.route("**/api/v1/school/classes**", (route) => {
    const method = route.request().method();
    if (method === "POST") {
      captures.createClass.push(route.request().postDataJSON() as Record<string, unknown>);
      return route.fulfill(json(201, { ...FIXTURE_CLASS_ACTIVE }));
    }
    if (method === "PUT") {
      captures.updateClass.push(route.request().postDataJSON() as Record<string, unknown>);
      return route.fulfill(json(200, { ...FIXTURE_CLASS_ACTIVE }));
    }
    if (method === "DELETE") {
      // Minor 6：显式处理删除，避免误落入列表分支返回假 200
      return route.fulfill(json(409, { detail: "班级仍有学生，先转班或移出后再删除" }));
    }
    const url = new URL(route.request().url());
    const includeArchived = url.searchParams.get("include_archived") === "true";
    return route.fulfill(
      json(200, {
        items: includeArchived ? classes : classes.filter((c) => c.status === "active"),
        next_cursor: null,
      }),
    );
  });

  await page.route("**/api/v1/school/classes/rollover", (route) => {
    captures.rollover.push(route.request().postDataJSON() as Record<string, unknown>);
    const body = captures.rollover.at(-1) as { dry_run?: boolean };
    if (body.dry_run === false) {
      return route.fulfill(
        json(200, {
          dry_run: false,
          advanced: [],
          archived: [],
          advanced_count: 1,
          archived_count: 1,
          rollover_id: "r-1",
        }),
      );
    }
    return route.fulfill(
      json(200, {
        dry_run: true,
        advanced: [
          { id: "x", entry_year: 2025, name: "1班", from_grade: "高一", to_grade: "高二" },
        ],
        archived: [
          { id: "y", entry_year: 2023, name: "1班", from_grade: "高三", to_grade: "高三" },
        ],
        advanced_count: 1,
        archived_count: 1,
        rollover_id: null,
      }),
    );
  });


  await page.route("**/api/v1/school/courses**", (route) =>
    route.fulfill(
      json(200, {
        items: [{ id: "c-1", code: "MATH", name: "数学" }],
        next_cursor: null,
      }),
    ),
  );

  const assignmentsState: Array<{
    id: string;
    teacher_id: string;
    class_id: string;
    course_id: string;
    term: string;
  }> = [];

  await page.route("**/api/v1/school/teacher-assignments**", (route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON() as {
        teacher_id: string;
        class_id: string;
        course_id: string;
        term: string;
      };
      const created = { id: `a-${assignmentsState.length + 1}`, ...body };
      assignmentsState.push(created);
      return route.fulfill(json(201, created));
    }
    return route.fulfill(json(200, { items: assignmentsState, next_cursor: null }));
  });

  const departmentsState: Array<{
    id: string;
    parent_id: string | null;
    name: string;
    code: string | null;
    path: string;
    status: string;
  }> = [{ id: "d-1", parent_id: null, name: "校办", code: null, path: "/d-1", status: "active" }];

  await page.route("**/api/v1/school/departments**", (route) => {
    const method = route.request().method();
    if (method === "DELETE") {
      return route.fulfill(json(409, { detail: "有子部门或仍有教师归属时不可删" }));
    }
    if (method === "POST") {
      const body = route.request().postDataJSON() as { name: string };
      departmentsState.push({
        id: `d-${departmentsState.length + 1}`,
        parent_id: null,
        name: body.name,
        code: null,
        path: `/d-${departmentsState.length + 1}`,
        status: "active",
      });
      return route.fulfill(json(201, departmentsState.at(-1)));
    }
    return route.fulfill(json(200, departmentsState));
  });

  await page.route("**/api/v1/school/personnel**", (route) =>
    route.fulfill(
      json(200, [
        {
          id: FIXTURE_TEACHER.user_id,
          user_type: "teacher",
          display_name: FIXTURE_TEACHER.name,
          status: "active",
          last_login_at: null,
          email_masked: null,
          id_card_mask: "110***********7758",
        },
      ]),
    ),
  );

  const pendingJob = {
    id: "j-1",
    type: "teachers",
    conflict: "error",
    status: "running",
    total_rows: 0,
    success_rows: 0,
    failed_rows: 0,
    skipped_rows: 0,
    retried_by: null,
    created_at: "2026-10-26T00:00:00Z",
    started_at: null,
    finished_at: null,
    message: null,
  };
  await page.route("**/api/v1/school/imports/j-1/retry", (route) =>
    route.fulfill(json(201, { source_job_id: "j-1", job_id: "j-2" })),
  );
  await page.route("**/api/v1/school/imports/j-1/errors**", (route) =>
    route.fulfill(json(200, { items: [], next_cursor: null })),
  );
  // 任意 job id 的进度查询（j-1 被 spec 覆盖项优先；j-2 等走此默认）
  await page.route("**/api/v1/school/imports/*", (route) =>
    route.fulfill(json(200, pendingJob)),
  );
  await page.route("**/api/v1/school/imports", (route) => {
    if (route.request().method() === "POST") {
      return route.fulfill(json(201, { ...pendingJob, status: "pending" }));
    }
    return route.fulfill(json(404, { detail: "not found" }));
  });

  // 覆盖项最后注册（Playwright 后注册的 route 先匹配）
  for (const [glob, response] of Object.entries(options.overrides ?? {})) {
    await page.route(glob, (route) => route.fulfill(json(response.status, response.body ?? {})));
  }

  return { captures };
}
