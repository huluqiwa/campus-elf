/** 组织与人员域 API（M2-UI1）：类型与后端 `app/api/school_org.py` 响应模型一一对齐。
 *
 * 设计：域工厂只依赖 `AuthSession["request"]`（401 自动刷新重放/403 分流都在
 * 会话层统一处理）；本模块不做错误翻译——HTTP 409/422 的 detail 由调用方经
 * `toUserMessage` 展示，机器可读码在异常 message 里透传。
 */

/** 后端 cursor 分页信封（TD-070）。 */
export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export interface ClassView {
  id: string;
  entry_year: number;
  grade: string;
  name: string;
  status: "active" | "archived";
  head_teacher_id: string | null;
  counselor_id: string | null;
}

export interface ClassCreateInput {
  entry_year: number;
  grade: string;
  name: string;
  head_teacher_id?: string | null;
  counselor_id?: string | null;
}

export interface ClassUpdateInput {
  grade?: string;
  name?: string;
  head_teacher_id?: string | null;
  counselor_id?: string | null;
}

export interface RolloverAdvance {
  from_grade: string;
  to_grade: string;
  entry_year?: number | null;
}

export interface GradeChange {
  id: string;
  entry_year: number;
  name: string;
  from_grade: string;
  to_grade: string;
}

export interface RolloverInput {
  advances: RolloverAdvance[];
  archive_grades: string[];
  idempotency_key: string;
  dry_run: boolean;
}

export interface RolloverResult {
  dry_run: boolean;
  advanced: GradeChange[];
  archived: GradeChange[];
  advanced_count: number;
  archived_count: number;
  rollover_id: string | null;
}

export interface CourseView {
  id: string;
  code: string;
  name: string;
}

export interface AssignmentView {
  id: string;
  teacher_id: string;
  class_id: string;
  course_id: string;
  term: string;
}

export interface DepartmentView {
  id: string;
  parent_id: string | null;
  name: string;
  code: string | null;
  path: string;
  status: "active" | "disabled";
}

export interface DepartmentCreateInput {
  name: string;
  code?: string | null;
  parent_id?: string | null;
}

export interface DepartmentUpdateInput {
  name?: string;
  code?: string | null;
  status?: "active" | "disabled";
  parent_id?: string | null;
}

export interface TeacherView {
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
  email_masked?: string | null;
  id_card_mask?: string | null;
}

export interface TeacherCreateInput {
  login_id: string;
  employee_no: string;
  name: string;
  age?: number | null;
  gender?: string | null;
  position?: string | null;
  email?: string | null;
  department_id?: string | null;
  id_card_number?: string | null;
}

export interface StudentView {
  id: string;
  user_id: string;
  login_id: string;
  student_no: string;
  name: string;
  gender: string | null;
  class_id: string | null;
  initial_password: string | null;
  email_masked?: string | null;
  id_card_mask?: string | null;
}

export interface StudentCreateInput {
  login_id: string;
  student_no: string;
  name: string;
  gender?: string | null;
  class_id?: string | null;
  email?: string | null;
  id_card_number?: string | null;
}

/** 人员档案（/school/personnel，offset 分页；只含掩码，不含 login_id）。 */
export interface PersonnelView {
  id: string;
  user_type: string;
  display_name: string | null;
  status: string;
  last_login_at: string | null;
  email_masked: string | null;
  id_card_mask: string | null;
}

export interface IdCardFull {
  number: string;
  mask: string;
}

/** 查询参数：跳过 undefined/null，保持 URL 干净（利于 mock 断言与缓存）。 */
export function buildQuery(params: Record<string, string | number | boolean | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) continue;
    search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

function jsonInit(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

export interface OrgApi {
  listClasses(query?: {
    limit?: number;
    cursor?: string;
    entryYear?: number;
    includeArchived?: boolean;
  }): Promise<Page<ClassView>>;
  createClass(input: ClassCreateInput): Promise<ClassView>;
  updateClass(id: string, input: ClassUpdateInput): Promise<ClassView>;
  deleteClass(id: string): Promise<void>;
  rollover(input: RolloverInput): Promise<RolloverResult>;

  listCourses(query?: { limit?: number; cursor?: string }): Promise<Page<CourseView>>;
  createCourse(input: { code: string; name: string }): Promise<CourseView>;

  listAssignments(query?: {
    limit?: number;
    cursor?: string;
    teacherId?: string;
  }): Promise<Page<AssignmentView>>;
  createAssignment(input: {
    teacher_id: string;
    class_id: string;
    course_id: string;
    term: string;
  }): Promise<AssignmentView>;

  listDepartments(): Promise<DepartmentView[]>;
  createDepartment(input: DepartmentCreateInput): Promise<DepartmentView>;
  updateDepartment(id: string, input: DepartmentUpdateInput): Promise<DepartmentView>;
  deleteDepartment(id: string): Promise<void>;

  listTeachers(query?: { limit?: number; cursor?: string }): Promise<Page<TeacherView>>;
  createTeacher(input: TeacherCreateInput): Promise<TeacherView>;

  listStudents(query?: { limit?: number; cursor?: string }): Promise<Page<StudentView>>;
  createStudent(input: StudentCreateInput): Promise<StudentView>;
  transferStudent(id: string, classId: string | null): Promise<StudentView>;

  /** 受控查看（POST：理由在请求体，不进 URL；浏览器 fetch 禁止 GET body）。 */
  readIdCard(personnelId: string, reason: string): Promise<IdCardFull>;

  listPersonnel(query?: {
    limit?: number;
    offset?: number;
    userType?: string;
  }): Promise<PersonnelView[]>;
}

export function createOrgApi(session: { request: AuthSessionRequest }): OrgApi {
  const request = session.request;

  return {
    async listClasses(query = {}) {
      const qs = buildQuery({
        limit: query.limit,
        cursor: query.cursor,
        entry_year: query.entryYear,
        include_archived: query.includeArchived,
      });
      return request<Page<ClassView>>(`/api/v1/school/classes${qs}`);
    },
    async createClass(input) {
      return request<ClassView>("/api/v1/school/classes", jsonInit("POST", input));
    },
    async updateClass(id, input) {
      return request<ClassView>(`/api/v1/school/classes/${id}`, jsonInit("PUT", input));
    },
    async deleteClass(id) {
      await request<void>(`/api/v1/school/classes/${id}`, { method: "DELETE" });
    },
    async rollover(input) {
      return request<RolloverResult>("/api/v1/school/classes/rollover", jsonInit("POST", input));
    },

    async listCourses(query = {}) {
      const qs = buildQuery({ limit: query.limit, cursor: query.cursor });
      return request<Page<CourseView>>(`/api/v1/school/courses${qs}`);
    },
    async createCourse(input) {
      return request<CourseView>("/api/v1/school/courses", jsonInit("POST", input));
    },

    async listAssignments(query = {}) {
      const qs = buildQuery({ limit: query.limit, cursor: query.cursor, teacher_id: query.teacherId });
      return request<Page<AssignmentView>>(`/api/v1/school/teacher-assignments${qs}`);
    },
    async createAssignment(input) {
      return request<AssignmentView>("/api/v1/school/teacher-assignments", jsonInit("POST", input));
    },

    async listDepartments() {
      return request<DepartmentView[]>("/api/v1/school/departments");
    },
    async createDepartment(input) {
      return request<DepartmentView>("/api/v1/school/departments", jsonInit("POST", input));
    },
    async updateDepartment(id, input) {
      return request<DepartmentView>(`/api/v1/school/departments/${id}`, jsonInit("PUT", input));
    },
    async deleteDepartment(id) {
      await request<void>(`/api/v1/school/departments/${id}`, { method: "DELETE" });
    },

    async listTeachers(query = {}) {
      const qs = buildQuery({ limit: query.limit, cursor: query.cursor });
      return request<Page<TeacherView>>(`/api/v1/school/teachers${qs}`);
    },
    async createTeacher(input) {
      return request<TeacherView>("/api/v1/school/teachers", jsonInit("POST", input));
    },

    async listStudents(query = {}) {
      const qs = buildQuery({ limit: query.limit, cursor: query.cursor });
      return request<Page<StudentView>>(`/api/v1/school/students${qs}`);
    },
    async createStudent(input) {
      return request<StudentView>("/api/v1/school/students", jsonInit("POST", input));
    },
    async transferStudent(id, classId) {
      return request<StudentView>(
        `/api/v1/school/students/${id}/transfer`,
        jsonInit("POST", { class_id: classId }),
      );
    },

    async readIdCard(personnelId, reason) {
      return request<IdCardFull>(
        `/api/v1/school/personnel/${personnelId}/id-card`,
        jsonInit("POST", { reason }),
      );
    },

    async listPersonnel(query = {}) {
      const qs = buildQuery({ limit: query.limit, offset: query.offset, user_type: query.userType });
      return request<PersonnelView[]>(`/api/v1/school/personnel${qs}`);
    },
  };
}

/** 会话请求函数的最小结构签名（避免对 AuthSession 的硬依赖，便于测试替身）。 */
export type AuthSessionRequest = <T>(path: string, init?: RequestInit) => Promise<T>;
