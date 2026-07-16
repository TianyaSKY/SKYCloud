<template>
  <a-table
      v-model:selectedKeys="selection"
      :data="data"
      :loading="loading"
      :pagination="pagination"
      :row-selection="rowSelection"
      row-key="id"
      @page-change="$emit('page-change', $event)"
      @page-size-change="$emit('page-size-change', $event)"
      @sorter-change="handleSorterChange"
  >
    <template #columns>
      <a-table-column
          :sortable="{sortDirections: ['ascend', 'descend'], sorter: true}"
          data-index="name"
          title="文件名"
      >
        <template #cell="{ record }">
          <a-space>
            <icon-folder v-if="record.is_folder" :style="{ color: '#ffb400', fontSize: '18px' }"/>
            <icon-file v-else :style="{ color: '#165dff', fontSize: '18px' }"/>
            <a-link @click="$emit('file-click', record)">{{ record.name }}</a-link>
            <a-tooltip v-if="!record.is_folder && record.status && record.status !== 'success'"
                       :content="getStatusTip(record.status)">
              <a-badge :status="getStatusBadgeStatus(record.status)"/>
            </a-tooltip>
          </a-space>
        </template>
      </a-table-column>
      <a-table-column
          :sortable="{sortDirections: ['ascend', 'descend'], sorter: true}"
          :width="120"
          data-index="size"
          title="大小"
      >
        <template #cell="{ record }">
          {{ record.is_folder ? '-' : formatSize(record.size) }}
        </template>
      </a-table-column>
      <a-table-column
          :sortable="{sortDirections: ['ascend', 'descend'], sorter: true}"
          :width="200"
          data-index="updated_at"
          title="修改时间"
      >
        <template #cell="{ record }">
          {{ formatDate(record.updated_at) }}
        </template>
      </a-table-column>
      <a-table-column :width="160" title="操作" align="center">
        <template #cell="{ record }">
          <div class="row-actions">
            <a-tooltip content="下载" position="top" v-if="!record.is_folder">
              <a-button
                  size="small"
                  type="text"
                  aria-label="下载"
                  @click="$emit('download', record)"
                  class="action-btn"
              >
                <template #icon><icon-download /></template>
              </a-button>
            </a-tooltip>
            <a-tooltip content="分享" position="top" v-if="!record.is_folder">
              <a-button
                  size="small"
                  type="text"
                  aria-label="分享"
                  @click="$emit('share', record)"
                  class="action-btn"
              >
                <template #icon><icon-share-alt /></template>
              </a-button>
            </a-tooltip>

            <a-dropdown trigger="click" position="br">
              <a-button
                  size="small"
                  type="text"
                  aria-label="更多操作"
                  class="action-btn"
              >
                <template #icon><icon-more /></template>
              </a-button>
              <template #content>
                <a-doption @click="$emit('rename', record)">
                  <template #icon><icon-edit /></template>重命名
                </a-doption>
                <a-doption @click="$emit('move', record)">
                  <template #icon><icon-drag-arrow /></template>移动
                </a-doption>
                <a-doption v-if="!record.is_folder && record.status === 'fail'" @click="$emit('retry-embedding', record)">
                  <template #icon><icon-refresh /></template>重试
                </a-doption>
                <a-doption style="color: rgb(var(--danger-6))" @click="confirmDelete(record)">
                  <template #icon><icon-delete /></template>删除
                </a-doption>
              </template>
            </a-dropdown>
          </div>
        </template>
      </a-table-column>
    </template>
  </a-table>

  <div v-if="selection.length > 0" style="margin-top: 16px">
    <a-alert>
      <div style="display: flex; align-items: center; justify-content: space-between; width: 100%">
        <a-space>
          <span>已选择 <span style="font-weight: bold">{{ selection.length }}</span> 项</span>
          <a-button size="mini" type="text" @click="selection = []">取消选择</a-button>
        </a-space>
        <a-space>
          <a-popconfirm content="确定删除选中的文件吗？" @ok="$emit('batch-delete', selection)">
            <a-button size="mini" status="danger" type="primary">批量删除</a-button>
          </a-popconfirm>
        </a-space>
      </div>
    </a-alert>
  </div>
</template>

<script lang="ts" setup>
import {computed, reactive, ref} from 'vue'
import { Modal } from '@arco-design/web-vue'
import {
  IconFile, IconFolder, IconDownload, IconShareAlt,
  IconMore, IconEdit, IconDragArrow, IconRefresh, IconDelete
} from '@arco-design/web-vue/es/icon'
import type {FileItem} from '@/api/file'
import {formatDate} from '@/utils/format'

// 后端 FileItem 不含 status，但列表渲染依赖 process_status 推送的待索引状态；
// 这里本地扩展一个带 status 字段的展示行类型，承载表格所需的可选字段。
interface FileTableRow extends FileItem {
  status?: 'pending' | 'processing' | 'success' | 'fail' | string
  size?: number
}

interface PaginationConfig {
  current: number
  pageSize: number
  total: number
  showTotal?: boolean
  showPageSize?: boolean
  pageSizeOptions?: number[]
}

const props = defineProps<{
  data: FileTableRow[]
  loading: boolean
  pagination: PaginationConfig
  selectedKeys?: number[]
}>()

const emit = defineEmits(['file-click', 'download', 'share', 'delete', 'page-change', 'page-size-change', 'batch-delete', 'update:selectedKeys', 'sorter-change', 'retry-embedding', 'rename', 'move'])

const internalSelectedKeys = ref<number[]>([])

const selection = computed({
  get: () => props.selectedKeys || internalSelectedKeys.value,
  set: (val: number[]) => {
    internalSelectedKeys.value = val
    emit('update:selectedKeys', val)
  }
})

const rowSelection = reactive({
  type: 'checkbox',
  showCheckedAll: true,
  onlyCurrent: false
})

const confirmDelete = (record: FileTableRow) => {
  Modal.warning({
    title: '确认删除',
    content: '确定要删除这个文件吗？',
    hideCancel: false,
    onOk: () => {
      emit('delete', record)
    }
  })
}

const handleSorterChange = (dataIndex: string, direction: string) => {
  emit('sorter-change', {dataIndex, direction})
}

const formatSize = (size?: number) => {
  if (!size) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let n = size
  let i = 0
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i++
  }
  return `${n.toFixed(2)} ${units[i]}`
}

const getStatusBadgeStatus = (status: string) => {
  switch (status) {
    case 'pending':
      return 'warning'
    case 'processing':
      return 'processing'
    case 'fail':
      return 'danger'
    default:
      return 'normal'
  }
}

const getStatusTip = (status: string) => {
  switch (status) {
    case 'pending':
      return '等待AI索引'
    case 'processing':
      return '正在处理AI索引'
    case 'fail':
      return 'AI索引失败'
    default:
      return '未知状态'
  }
}
</script>

<style scoped>
.row-actions {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  opacity: 0;
  transition: opacity 0.2s;
}

:deep(.arco-table-tr:hover) .row-actions {
  opacity: 1;
}

.action-btn {
  color: var(--color-text-2);
}

.action-btn:hover {
  color: rgb(var(--primary-6));
  background-color: var(--color-fill-2);
}
</style>
