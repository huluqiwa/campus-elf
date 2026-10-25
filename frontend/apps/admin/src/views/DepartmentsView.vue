<script setup lang="ts">
/** 部门树（M2-UI1）：扁平列表构树；建子部门/停用/启用/删除（409 原因透出）。 */
import { toUserMessage } from "@campus-elf/api-client";
import type { DepartmentView } from "@campus-elf/api-client";
import { ElMessage, ElMessageBox } from "element-plus";
import { computed, onMounted, ref } from "vue";

import { orgApi } from "../api";

interface TreeNode {
  id: string;
  label: string;
  status: string;
  raw: DepartmentView;
  children: TreeNode[];
}

const departments = ref<DepartmentView[]>([]);
const loading = ref(false);

const dialogVisible = ref(false);
const parentTarget = ref<DepartmentView | null>(null);
const form = ref({ name: "", code: "" });
const saving = ref(false);

const tree = computed<TreeNode[]>(() => {
  const byParent = new Map<string | null, DepartmentView[]>();
  for (const item of departments.value) {
    const list = byParent.get(item.parent_id) ?? [];
    list.push(item);
    byParent.set(item.parent_id, list);
  }
  function build(parentId: string | null): TreeNode[] {
    return (byParent.get(parentId) ?? []).map((item) => ({
      id: item.id,
      label: `${item.name}（${item.status === "active" ? "在用" : "已停用"}）`,
      status: item.status,
      raw: item,
      children: build(item.id),
    }));
  }
  return build(null);
});

async function load(): Promise<void> {
  loading.value = true;
  try {
    departments.value = await orgApi.listDepartments();
  } catch (error) {
    ElMessage.error(toUserMessage(error, "部门树加载失败"));
  } finally {
    loading.value = false;
  }
}

function openCreate(parent: DepartmentView | null): void {
  parentTarget.value = parent;
  form.value = { name: "", code: "" };
  dialogVisible.value = true;
}

async function save(): Promise<void> {
  if (!form.value.name) {
    ElMessage.warning("部门名称必填");
    return;
  }
  saving.value = true;
  try {
    await orgApi.createDepartment({
      name: form.value.name,
      code: form.value.code || null,
      parent_id: parentTarget.value?.id ?? null,
    });
    dialogVisible.value = false;
    await load();
  } catch (error) {
    ElMessage.error(toUserMessage(error, "创建失败"));
  } finally {
    saving.value = false;
  }
}

async function toggleStatus(row: DepartmentView): Promise<void> {
  try {
    await orgApi.updateDepartment(row.id, {
      status: row.status === "active" ? "disabled" : "active",
    });
    await load();
  } catch (error) {
    ElMessage.error(toUserMessage(error, "操作失败"));
  }
}

async function remove(row: DepartmentView): Promise<void> {
  const confirmed = await ElMessageBox.confirm(`确认删除部门「${row.name}」？`, "删除确认", {
    type: "warning",
  }).catch(() => false);
  if (!confirmed) return;
  try {
    await orgApi.deleteDepartment(row.id);
    ElMessage.success("已删除");
    await load();
  } catch (error) {
    ElMessage.error(toUserMessage(error, "删除失败（有子部门或仍有教师归属时不可删）"));
  }
}

onMounted(() => {
  void load();
});
</script>

<template>
  <div class="toolbar">
    <el-button type="primary" data-testid="dept-create" @click="openCreate(null)"
      >新建根部门</el-button
    >
  </div>
  <el-tree
    v-loading="loading"
    :data="tree"
    node-key="id"
    default-expand-all
    :expand-on-click-node="false"
    data-testid="department-tree"
  >
    <template #default="{ data }">
      <div class="node">
        <span>{{ data.label }}</span>
        <span class="actions">
          <el-button link type="primary" @click.stop="openCreate(data.raw as DepartmentView)"
            >+ 子部门</el-button
          >
          <el-button
            link
            :type="data.status === 'active' ? 'warning' : 'success'"
            @click.stop="toggleStatus(data.raw as DepartmentView)"
          >
            {{ data.status === "active" ? "停用" : "启用" }}
          </el-button>
          <el-button link type="danger" @click.stop="remove(data.raw as DepartmentView)"
            >删除</el-button
          >
        </span>
      </div>
    </template>
  </el-tree>

  <el-dialog
    v-model="dialogVisible"
    :title="parentTarget === null ? '新建根部门' : `在「${parentTarget.name}」下新建子部门`"
    width="420px"
  >
    <el-form label-width="90px">
      <el-form-item label="名称" required>
        <el-input v-model="form.name" data-testid="dept-name" maxlength="64" />
      </el-form-item>
      <el-form-item label="编号">
        <el-input v-model="form.code" maxlength="32" placeholder="可选" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" data-testid="dept-submit" :loading="saving" @click="save"
        >创建</el-button
      >
    </template>
  </el-dialog>
</template>

<style scoped>
.toolbar {
  margin-bottom: 12px;
}
.node {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
}
.actions {
  display: flex;
  gap: 4px;
}
</style>
