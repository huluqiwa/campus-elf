<script setup lang="ts">
/**
 * 人员管理（M2-UI1）：教师/学生列表 + 建档（可选身份证）+ 学生转班 + 受控查看。
 * 分页：cursor 接口用"上一页/下一页"控件（D4-A，不伪造页码）。
 */
import { toUserMessage } from "@campus-elf/api-client";
import type { ClassView, StudentView, TeacherView } from "@campus-elf/api-client";
import { ElMessage } from "element-plus";
import { computed, onMounted, ref } from "vue";

import { orgApi } from "../api";
import { useAuthStore } from "../stores/auth";
import IdCardViewDialog from "./IdCardViewDialog.vue";

const auth = useAuthStore();
/** TD-141：受控查看按钮仅对持有 sensitive.read 的角色渲染（真正的边界是后端 403）。 */
const canViewIdCard = computed(
  () => auth.user?.permissions.includes("school.personnel.sensitive.read") ?? false,
);

const activeTab = ref<"teachers" | "students">("teachers");

// ---- 教师列表（cursor 分页：游标栈实现上一页/下一页） ----
const teachers = ref<TeacherView[]>([]);
const teacherCursorStack = ref<(string | null)[]>([null]);
const teacherNextCursor = ref<string | null>(null);
const teachersLoading = ref(false);

// ---- 学生列表 ----
const students = ref<StudentView[]>([]);
const studentCursorStack = ref<(string | null)[]>([null]);
const studentNextCursor = ref<string | null>(null);
const studentsLoading = ref(false);

const idCardTargetId = ref<string | null>(null);
const idCardVisible = ref(false);

// ---- 教师建档 ----
const teacherDialogVisible = ref(false);
const teacherForm = ref({ employee_no: "", name: "", id_card_number: "" });
const teacherSaving = ref(false);
const initialPassword = ref<string | null>(null);

// ---- 学生建档 / 转班 ----
const studentDialogVisible = ref(false);
const studentForm = ref({
  student_no: "",
  name: "",
  class_id: null as string | null,
  id_card_number: "",
});
const studentSaving = ref(false);
const transferTarget = ref<StudentView | null>(null);
const transferDialogVisible = ref(false);
const transferClassId = ref<string | null>(null);
const transferSaving = ref(false);

const activeClasses = ref<ClassView[]>([]);

async function loadTeachers(page: number): Promise<void> {
  teachersLoading.value = true;
  try {
    const cursor = teacherCursorStack.value[page] ?? null;
    const pageData = await orgApi.listTeachers({ limit: 50, cursor: cursor ?? undefined });
    teachers.value = pageData.items;
    teacherNextCursor.value = pageData.next_cursor;
    teacherCursorStack.value = teacherCursorStack.value.slice(0, page + 1);
    if (pageData.next_cursor) teacherCursorStack.value[page + 1] = pageData.next_cursor;
  } catch (error) {
    ElMessage.error(toUserMessage(error, "教师列表加载失败"));
  } finally {
    teachersLoading.value = false;
  }
}

async function loadStudents(page: number): Promise<void> {
  studentsLoading.value = true;
  try {
    const cursor = studentCursorStack.value[page] ?? null;
    const pageData = await orgApi.listStudents({ limit: 50, cursor: cursor ?? undefined });
    students.value = pageData.items;
    studentNextCursor.value = pageData.next_cursor;
    studentCursorStack.value = studentCursorStack.value.slice(0, page + 1);
    if (pageData.next_cursor) studentCursorStack.value[page + 1] = pageData.next_cursor;
  } catch (error) {
    ElMessage.error(toUserMessage(error, "学生列表加载失败"));
  } finally {
    studentsLoading.value = false;
  }
}

async function loadActiveClasses(): Promise<void> {
  try {
    const pageData = await orgApi.listClasses({ limit: 100 });
    activeClasses.value = pageData.items;
  } catch {
    activeClasses.value = [];
  }
}

function teacherPage(): number {
  return teacherCursorStack.value.length - 1;
}

function studentPage(): number {
  return studentCursorStack.value.length - 1;
}

function openTeacherDialog(): void {
  teacherForm.value = { employee_no: "", name: "", id_card_number: "" };
  teacherDialogVisible.value = true;
}

async function saveTeacher(): Promise<void> {
  const form = teacherForm.value;
  if (!form.employee_no || !form.name) {
    ElMessage.warning("工号与姓名必填");
    return;
  }
  teacherSaving.value = true;
  try {
    const created = await orgApi.createTeacher({
      login_id: form.employee_no, // 登录名与工号同值（建档最常用口径）
      employee_no: form.employee_no,
      name: form.name,
      id_card_number: form.id_card_number || null,
    });
    teacherDialogVisible.value = false;
    initialPassword.value = created.initial_password;
    await loadTeachers(teacherPage());
  } catch (error) {
    ElMessage.error(toUserMessage(error, "建档失败"));
  } finally {
    teacherSaving.value = false;
  }
}

function openStudentDialog(): void {
  studentForm.value = { student_no: "", name: "", class_id: null, id_card_number: "" };
  studentDialogVisible.value = true;
}

async function saveStudent(): Promise<void> {
  const form = studentForm.value;
  if (!form.student_no || !form.name) {
    ElMessage.warning("学号与姓名必填");
    return;
  }
  studentSaving.value = true;
  try {
    await orgApi.createStudent({
      login_id: form.student_no,
      student_no: form.student_no,
      name: form.name,
      class_id: form.class_id,
      id_card_number: form.id_card_number || null,
    });
    studentDialogVisible.value = false;
    await loadStudents(studentPage());
  } catch (error) {
    ElMessage.error(toUserMessage(error, "建档失败"));
  } finally {
    studentSaving.value = false;
  }
}

function openTransfer(student: StudentView): void {
  transferTarget.value = student;
  transferClassId.value = student.class_id;
  transferDialogVisible.value = true;
  void loadActiveClasses();
}

async function saveTransfer(): Promise<void> {
  if (transferTarget.value === null) return;
  transferSaving.value = true;
  try {
    await orgApi.transferStudent(transferTarget.value.id, transferClassId.value);
    transferDialogVisible.value = false;
    ElMessage.success("转班成功");
    await loadStudents(studentPage());
  } catch (error) {
    ElMessage.error(toUserMessage(error, "转班失败"));
  } finally {
    transferSaving.value = false;
  }
}

function openIdCard(row: TeacherView | StudentView): void {
  idCardTargetId.value = row.id;
  idCardVisible.value = true;
}

function classNameOf(classId: string | null): string {
  if (classId === null) return "未分班";
  const found = activeClasses.value.find((c) => c.id === classId);
  return found === undefined ? "—" : `${found.grade} ${found.name}`;
}

onMounted(() => {
  void loadTeachers(0);
  void loadStudents(0);
  void loadActiveClasses();
});
</script>

<template>
  <el-tabs v-model="activeTab">
    <el-tab-pane label="教师" name="teachers">
      <div class="toolbar">
        <el-button type="primary" data-testid="teacher-create" @click="openTeacherDialog"
          >教师建档</el-button
        >
        <el-button :disabled="teacherPage() === 0" @click="loadTeachers(teacherPage() - 1)"
          >上一页</el-button
        >
        <el-button :disabled="teacherNextCursor === null" @click="loadTeachers(teacherPage() + 1)"
          >下一页</el-button
        >
      </div>
      <el-table v-loading="teachersLoading" :data="teachers" data-testid="teacher-table">
        <el-table-column prop="employee_no" label="工号" width="120" />
        <el-table-column prop="name" label="姓名" width="120" />
        <el-table-column prop="gender" label="性别" width="80" />
        <el-table-column prop="position" label="职位" width="120" />
        <el-table-column prop="email_masked" label="邮箱（掩码）" min-width="150" />
        <el-table-column prop="id_card_mask" label="身份证（掩码）" min-width="160" />
        <el-table-column label="操作" width="180">
          <template #default="{ row }">
            <el-button
              v-if="canViewIdCard"
              link
              type="primary"
              @click="openIdCard(row as TeacherView)"
              >查看身份证</el-button
            >
          </template>
        </el-table-column>
      </el-table>
    </el-tab-pane>

    <el-tab-pane label="学生" name="students">
      <div class="toolbar">
        <el-button type="primary" data-testid="student-create" @click="openStudentDialog"
          >学生建档</el-button
        >
        <el-button :disabled="studentPage() === 0" @click="loadStudents(studentPage() - 1)"
          >上一页</el-button
        >
        <el-button :disabled="studentNextCursor === null" @click="loadStudents(studentPage() + 1)"
          >下一页</el-button
        >
      </div>
      <el-table v-loading="studentsLoading" :data="students" data-testid="student-table">
        <el-table-column prop="student_no" label="学号" width="120" />
        <el-table-column prop="name" label="姓名" width="120" />
        <el-table-column prop="gender" label="性别" width="80" />
        <el-table-column label="班级" min-width="140">
          <template #default="{ row }">{{ classNameOf(row.class_id) }}</template>
        </el-table-column>
        <el-table-column prop="email_masked" label="邮箱（掩码）" min-width="150" />
        <el-table-column prop="id_card_mask" label="身份证（掩码）" min-width="160" />
        <el-table-column label="操作" width="200">
          <template #default="{ row }">
            <el-button
              v-if="canViewIdCard"
              link
              type="primary"
              @click="openIdCard(row as StudentView)"
              >查看身份证</el-button
            >
            <el-button link type="warning" @click="openTransfer(row as StudentView)"
              >转班</el-button
            >
          </template>
        </el-table-column>
      </el-table>
    </el-tab-pane>
  </el-tabs>

  <!-- 教师建档 -->
  <el-dialog v-model="teacherDialogVisible" title="教师建档" width="440px">
    <el-form label-width="90px">
      <el-form-item label="工号" required>
        <el-input
          v-model="teacherForm.employee_no"
          data-testid="teacher-employee-no"
          maxlength="32"
          placeholder="同时作为登录名"
        />
      </el-form-item>
      <el-form-item label="姓名" required>
        <el-input v-model="teacherForm.name" data-testid="teacher-name" maxlength="64" />
      </el-form-item>
      <el-form-item label="身份证号">
        <el-input
          v-model="teacherForm.id_card_number"
          maxlength="20"
          placeholder="可选；18 位，落库即加密"
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="teacherDialogVisible = false">取消</el-button>
      <el-button
        type="primary"
        data-testid="teacher-submit"
        :loading="teacherSaving"
        @click="saveTeacher"
        >创建</el-button
      >
    </template>
  </el-dialog>

  <!-- 学生建档 -->
  <el-dialog v-model="studentDialogVisible" title="学生建档" width="440px">
    <el-form label-width="90px">
      <el-form-item label="学号" required>
        <el-input
          v-model="studentForm.student_no"
          data-testid="student-no"
          maxlength="32"
          placeholder="同时作为登录名"
        />
      </el-form-item>
      <el-form-item label="姓名" required>
        <el-input v-model="studentForm.name" data-testid="student-name" maxlength="64" />
      </el-form-item>
      <el-form-item label="班级">
        <el-select v-model="studentForm.class_id" clearable placeholder="可选；不选=未分班">
          <el-option
            v-for="c in activeClasses"
            :key="c.id"
            :value="c.id"
            :label="`${c.grade} ${c.name}`"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="身份证号">
        <el-input
          v-model="studentForm.id_card_number"
          maxlength="20"
          placeholder="可选；18 位，落库即加密"
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="studentDialogVisible = false">取消</el-button>
      <el-button
        type="primary"
        data-testid="student-submit"
        :loading="studentSaving"
        @click="saveStudent"
        >创建</el-button
      >
    </template>
  </el-dialog>

  <!-- 转班 -->
  <el-dialog v-model="transferDialogVisible" title="学生转班（留级/跳级/分班）" width="420px">
    <el-form label-width="90px">
      <el-form-item label="学生">
        <span>{{ transferTarget?.name }}（{{ transferTarget?.student_no }}）</span>
      </el-form-item>
      <el-form-item label="转入班级" required>
        <el-select v-model="transferClassId" clearable placeholder="选择目标班级；清空=未分班">
          <el-option
            v-for="c in activeClasses"
            :key="c.id"
            :value="c.id"
            :label="`${c.grade} ${c.name}`"
          />
        </el-select>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="transferDialogVisible = false">取消</el-button>
      <el-button
        type="primary"
        data-testid="transfer-submit"
        :loading="transferSaving"
        @click="saveTransfer"
        >确认转班</el-button
      >
    </template>
  </el-dialog>

  <!-- 初始密码只显示一次（D4 口径） -->
  <el-dialog
    :model-value="initialPassword !== null"
    title="建档成功"
    width="420px"
    @closed="initialPassword = null"
  >
    <el-alert type="warning" :closable="false" title="初始密码只显示这一次，请立即转交本人" />
    <p class="initial-password" data-testid="initial-password">{{ initialPassword }}</p>
    <template #footer>
      <el-button type="primary" @click="initialPassword = null">我已保存</el-button>
    </template>
  </el-dialog>

  <!-- 受控查看（理由弹窗） -->
  <IdCardViewDialog v-model="idCardVisible" :personnel-id="idCardTargetId" />
</template>

<style scoped>
.toolbar {
  margin-bottom: 12px;
  display: flex;
  gap: 8px;
}
.initial-password {
  font-family: monospace;
  font-size: 18px;
  text-align: center;
  letter-spacing: 2px;
}
</style>
