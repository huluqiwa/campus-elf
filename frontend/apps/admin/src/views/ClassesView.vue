<script setup lang="ts">
/** 班级与课程（M2-UI1）：建班（入学年份+当前年级）、指派班主任/辅导员、排课；归档届显式可见。 */
import { toUserMessage } from "@campus-elf/api-client";
import type { AssignmentView, ClassView, CourseView, TeacherView } from "@campus-elf/api-client";
import { ElMessage, ElMessageBox } from "element-plus";
import { onMounted, ref } from "vue";

import { orgApi } from "../api";

const activeTab = ref<"classes" | "courses" | "assignments">("classes");

const classes = ref<ClassView[]>([]);
const includeArchived = ref(false);
const classesLoading = ref(false);
const teachers = ref<TeacherView[]>([]);
const courses = ref<CourseView[]>([]);
const assignments = ref<AssignmentView[]>([]);

const classDialogVisible = ref(false);
const classEditingId = ref<string | null>(null);
const classForm = ref({
  entry_year: new Date().getFullYear(),
  grade: "",
  name: "",
  head_teacher_id: null as string | null,
  counselor_id: null as string | null,
});
const classSaving = ref(false);

const courseDialogVisible = ref(false);
const courseForm = ref({ code: "", name: "" });
const courseSaving = ref(false);

const assignmentDialogVisible = ref(false);
const assignmentForm = ref({ teacher_id: "", class_id: "", course_id: "", term: "" });
const assignmentSaving = ref(false);

const teacherNameOf = (teacherId: string | null): string => {
  if (teacherId === null) return "—";
  const found = teachers.value.find((t) => t.id === teacherId);
  return found === undefined ? "—" : found.name;
};

const courseNameOf = (courseId: string): string =>
  courses.value.find((c) => c.id === courseId)?.name ?? "—";

async function loadAll(): Promise<void> {
  classesLoading.value = true;
  try {
    const [classPage, teacherPage, coursePage, assignmentPage] = await Promise.all([
      orgApi.listClasses({ limit: 100, includeArchived: includeArchived.value }),
      orgApi.listTeachers({ limit: 200 }),
      orgApi.listCourses({ limit: 100 }),
      orgApi.listAssignments({ limit: 200 }),
    ]);
    classes.value = classPage.items;
    teachers.value = teacherPage.items;
    courses.value = coursePage.items;
    assignments.value = assignmentPage.items;
  } catch (error) {
    ElMessage.error(toUserMessage(error, "班级数据加载失败"));
  } finally {
    classesLoading.value = false;
  }
}

function openCreateClass(): void {
  classEditingId.value = null;
  classForm.value = {
    entry_year: new Date().getFullYear(),
    grade: "",
    name: "",
    head_teacher_id: null,
    counselor_id: null,
  };
  classDialogVisible.value = true;
}

function openEditClass(row: ClassView): void {
  classEditingId.value = row.id;
  classForm.value = {
    entry_year: row.entry_year,
    grade: row.grade,
    name: row.name,
    head_teacher_id: row.head_teacher_id,
    counselor_id: row.counselor_id,
  };
  classDialogVisible.value = true;
}

async function saveClass(): Promise<void> {
  const form = classForm.value;
  if (!form.grade || !form.name) {
    ElMessage.warning("当前年级与班名必填");
    return;
  }
  classSaving.value = true;
  try {
    if (classEditingId.value === null) {
      await orgApi.createClass(form);
      ElMessage.success("建班成功");
    } else {
      await orgApi.updateClass(classEditingId.value, {
        grade: form.grade,
        name: form.name,
        head_teacher_id: form.head_teacher_id,
        counselor_id: form.counselor_id,
      });
      ElMessage.success("已保存");
    }
    classDialogVisible.value = false;
    await loadAll();
  } catch (error) {
    ElMessage.error(toUserMessage(error, "保存失败（同届班名唯一）"));
  } finally {
    classSaving.value = false;
  }
}

async function removeClass(row: ClassView): Promise<void> {
  const confirmed = await ElMessageBox.confirm(
    `确认删除班级「${row.grade} ${row.name}」？`,
    "删除确认",
    { type: "warning" },
  ).catch(() => false);
  if (!confirmed) return;
  try {
    await orgApi.deleteClass(row.id);
    ElMessage.success("已删除");
    await loadAll();
  } catch (error) {
    ElMessage.error(toUserMessage(error, "删除失败"));
  }
}

async function saveCourse(): Promise<void> {
  if (!courseForm.value.code || !courseForm.value.name) {
    ElMessage.warning("课程编号与名称必填");
    return;
  }
  courseSaving.value = true;
  try {
    await orgApi.createCourse(courseForm.value);
    courseDialogVisible.value = false;
    courseForm.value = { code: "", name: "" };
    await loadAll();
  } catch (error) {
    ElMessage.error(toUserMessage(error, "建课失败"));
  } finally {
    courseSaving.value = false;
  }
}

async function saveAssignment(): Promise<void> {
  const form = assignmentForm.value;
  if (!form.teacher_id || !form.class_id || !form.course_id || !form.term) {
    ElMessage.warning("教师/班级/课程/学期均必填");
    return;
  }
  assignmentSaving.value = true;
  try {
    await orgApi.createAssignment(form);
    assignmentDialogVisible.value = false;
    await loadAll();
  } catch (error) {
    ElMessage.error(toUserMessage(error, "排课失败"));
  } finally {
    assignmentSaving.value = false;
  }
}

onMounted(() => {
  void loadAll();
});
</script>

<template>
  <el-tabs v-model="activeTab">
    <el-tab-pane label="班级" name="classes">
      <div class="toolbar">
        <el-button type="primary" data-testid="class-create" @click="openCreateClass"
          >新建班级</el-button
        >
        <el-switch v-model="includeArchived" active-text="显示归档届" @change="loadAll" />
      </div>
      <el-table v-loading="classesLoading" :data="classes" data-testid="class-table">
        <el-table-column prop="entry_year" label="入学年份" width="100" />
        <el-table-column prop="grade" label="当前年级" width="110" />
        <el-table-column prop="name" label="班名" width="120" />
        <el-table-column prop="status" label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.status === 'active' ? 'success' : 'info'">
              {{ row.status === "active" ? "在用" : "已归档" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="班主任" width="110">
          <template #default="{ row }">{{ teacherNameOf(row.head_teacher_id) }}</template>
        </el-table-column>
        <el-table-column label="辅导员" width="110">
          <template #default="{ row }">{{ teacherNameOf(row.counselor_id) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="140">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEditClass(row as ClassView)">编辑</el-button>
            <el-button
              v-if="row.status === 'active'"
              link
              type="danger"
              @click="removeClass(row as ClassView)"
              >删除</el-button
            >
          </template>
        </el-table-column>
      </el-table>
    </el-tab-pane>

    <el-tab-pane label="课程" name="courses">
      <div class="toolbar">
        <el-button type="primary" @click="courseDialogVisible = true">新建课程</el-button>
      </div>
      <el-table :data="courses" data-testid="course-table">
        <el-table-column prop="code" label="编号" width="140" />
        <el-table-column prop="name" label="名称" min-width="160" />
      </el-table>
    </el-tab-pane>

    <el-tab-pane label="任教关系" name="assignments">
      <div class="toolbar">
        <el-button type="primary" @click="assignmentDialogVisible = true">新排课</el-button>
      </div>
      <el-table :data="assignments" data-testid="assignment-table">
        <el-table-column label="教师" width="120">
          <template #default="{ row }">{{ teacherNameOf(row.teacher_id) }}</template>
        </el-table-column>
        <el-table-column label="班级" min-width="140">
          <template #default="{ row }">
            {{ classes.find((c) => c.id === row.class_id)?.grade }}
            {{ classes.find((c) => c.id === row.class_id)?.name }}
          </template>
        </el-table-column>
        <el-table-column label="课程" width="140">
          <template #default="{ row }">{{ courseNameOf(row.course_id) }}</template>
        </el-table-column>
        <el-table-column prop="term" label="学期" width="110" />
      </el-table>
    </el-tab-pane>
  </el-tabs>

  <!-- 建班/编辑 -->
  <el-dialog
    v-model="classDialogVisible"
    :title="classEditingId === null ? '新建班级' : '编辑班级'"
    width="440px"
  >
    <el-form label-width="90px">
      <el-form-item label="入学年份" required>
        <el-input-number
          v-model="classForm.entry_year"
          :min="1970"
          :max="2100"
          :disabled="classEditingId !== null"
        />
        <span v-if="classEditingId !== null" class="hint">届身份，建档后不可改</span>
      </el-form-item>
      <el-form-item label="当前年级" required>
        <el-input
          v-model="classForm.grade"
          data-testid="class-grade"
          maxlength="16"
          placeholder="如：高一 / 2024级 / 大一"
        />
      </el-form-item>
      <el-form-item label="班名" required>
        <el-input
          v-model="classForm.name"
          data-testid="class-name"
          maxlength="64"
          placeholder="同届内唯一"
        />
      </el-form-item>
      <el-form-item label="班主任">
        <el-select v-model="classForm.head_teacher_id" clearable placeholder="可选">
          <el-option
            v-for="t in teachers"
            :key="t.id"
            :value="t.id"
            :label="`${t.name}（${t.employee_no}）`"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="辅导员">
        <el-select v-model="classForm.counselor_id" clearable placeholder="可选">
          <el-option
            v-for="t in teachers"
            :key="t.id"
            :value="t.id"
            :label="`${t.name}（${t.employee_no}）`"
          />
        </el-select>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="classDialogVisible = false">取消</el-button>
      <el-button type="primary" data-testid="class-submit" :loading="classSaving" @click="saveClass"
        >保存</el-button
      >
    </template>
  </el-dialog>

  <!-- 建课 -->
  <el-dialog v-model="courseDialogVisible" title="新建课程" width="420px">
    <el-form label-width="90px">
      <el-form-item label="编号" required>
        <el-input v-model="courseForm.code" maxlength="32" />
      </el-form-item>
      <el-form-item label="名称" required>
        <el-input v-model="courseForm.name" maxlength="64" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="courseDialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="courseSaving" @click="saveCourse">创建</el-button>
    </template>
  </el-dialog>

  <!-- 排课 -->
  <el-dialog v-model="assignmentDialogVisible" title="新排课" width="440px">
    <el-form label-width="90px">
      <el-form-item label="教师" required>
        <el-select v-model="assignmentForm.teacher_id">
          <el-option
            v-for="t in teachers"
            :key="t.id"
            :value="t.id"
            :label="`${t.name}（${t.employee_no}）`"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="班级" required>
        <el-select v-model="assignmentForm.class_id">
          <el-option
            v-for="c in classes.filter((c) => c.status === 'active')"
            :key="c.id"
            :value="c.id"
            :label="`${c.grade} ${c.name}`"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="课程" required>
        <el-select v-model="assignmentForm.course_id">
          <el-option v-for="c in courses" :key="c.id" :value="c.id" :label="c.name" />
        </el-select>
      </el-form-item>
      <el-form-item label="学期" required>
        <el-input v-model="assignmentForm.term" maxlength="16" placeholder="如：2026春" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="assignmentDialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="assignmentSaving" @click="saveAssignment"
        >确认排课</el-button
      >
    </template>
  </el-dialog>
</template>

<style scoped>
.toolbar {
  margin-bottom: 12px;
  display: flex;
  gap: 16px;
  align-items: center;
}
.hint {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
