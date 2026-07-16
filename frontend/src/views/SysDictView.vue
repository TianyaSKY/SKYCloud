<template>
  <MainLayout active-menu="sys-dicts" title="系统字典">
    <a-card title="系统设置">
      <template #extra>
        <a-space>
          <a-button @click="fetchDicts">
            <template #icon>
              <icon-refresh/>
            </template>
            刷新
          </a-button>
          <a-button type="primary" @click="handleAdd">
            <template #icon>
              <icon-plus/>
            </template>
            新增配置
          </a-button>
        </a-space>
      </template>

      <a-table :data="dicts" :loading="loading">
        <template #columns>
          <a-table-column data-index="key" title="配置键">
            <template #cell="{ record }">
              <a-tooltip v-if="record.des" :content="record.des">
                <span style="cursor: help; border-bottom: 1px dashed var(--color-text-3);">{{ record.key }}</span>
              </a-tooltip>
              <span v-else>{{ record.key }}</span>
            </template>
          </a-table-column>
          <a-table-column data-index="value" title="配置值"/>
          <a-table-column title="状态">
            <template #cell="{ record }">
              <a-tag :color="record.enable ? 'green' : 'red'">
                {{ record.enable ? '启用' : '禁用' }}
              </a-tag>
            </template>
          </a-table-column>
          <a-table-column title="操作">
            <template #cell="{ record }">
              <a-button type="text" @click="handleEdit(record)">编辑</a-button>
              <a-popconfirm content="确定删除该配置吗？" @ok="handleDelete(record.id)">
                <a-button status="danger" type="text">删除</a-button>
              </a-popconfirm>
            </template>
          </a-table-column>
        </template>
      </a-table>
    </a-card>

    <a-modal v-model:visible="visible" :title="form.id ? '编辑配置' : '新增配置'" :confirm-loading="submitting" @cancel="handleCancel" @ok="handleOk">
      <a-form :model="form">
        <a-form-item :rules="[{required:true,message:'请输入配置键'}]" field="key" label="配置键">
          <a-input v-model="form.key" placeholder="例如: site_name"/>
        </a-form-item>
        <a-form-item :rules="[{required:true,message:'请输入配置值'}]" field="value" label="配置值">
          <a-input v-model="form.value"/>
        </a-form-item>
        <a-form-item field="des" label="描述">
          <a-textarea v-model="form.des" placeholder="请输入配置描述"/>
        </a-form-item>
        <a-form-item field="enable" label="状态">
          <a-switch v-model="form.enable"/>
        </a-form-item>
      </a-form>
    </a-modal>
  </MainLayout>
</template>

<script lang="ts" setup>
import {onMounted, reactive, ref} from 'vue'
import {IconPlus, IconRefresh} from '@arco-design/web-vue/es/icon'
import {Message} from '@arco-design/web-vue'
import type {SysDict} from '@/api/sys_dict';
import {createSysDict, deleteSysDict, getSysDicts, updateSysDict} from '@/api/sys_dict'
import {sysDictSchema} from '@/schemas/sys_dict'
import {logger} from '@/utils/logger'
import MainLayout from '../components/MainLayout.vue'

const dicts = ref<SysDict[]>([])
const loading = ref(false)
const submitting = ref(false)
const visible = ref(false)
const form = reactive<SysDict>({
  id: undefined,
  key: '',
  value: '',
  des: '',
  enable: true
})

const fetchDicts = async () => {
  loading.value = true
  try {
    dicts.value = await getSysDicts()
  } catch (error) {
    logger.error('获取系统字典失败', error)
  } finally {
    loading.value = false
  }
}

const handleAdd = () => {
  form.id = undefined
  form.key = ''
  form.value = ''
  form.des = ''
  form.enable = true
  visible.value = true
}

const handleEdit = (record: SysDict) => {
  Object.assign(form, record)
  visible.value = true
}

const handleDelete = async (id: number) => {
  try {
    await deleteSysDict(id)
    Message.success('删除成功')
    fetchDicts()
  } catch (error) {
    logger.error('删除系统字典失败', error)
  }
}

const handleOk = async () => {
  // 提交防重入：避免用户连点 OK 重复创建/更新
  if (submitting.value) return
  // Zod 校验：键值非空 + 长度限制
  const result = sysDictSchema.safeParse(form)
  if (!result.success) {
    Message.warning(result.error.issues[0]?.message ?? '请填写完整信息')
    return
  }
  submitting.value = true
  try {
    if (form.id) {
      await updateSysDict(form.id, form)
      Message.success('更新成功')
    } else {
      await createSysDict(form)
      Message.success('创建成功')
    }
    visible.value = false
    fetchDicts()
  } catch (error) {
    logger.error('保存系统字典失败', error)
  } finally {
    submitting.value = false
  }
}

const handleCancel = () => {
  visible.value = false
}

onMounted(() => {
  fetchDicts()
})
</script>
