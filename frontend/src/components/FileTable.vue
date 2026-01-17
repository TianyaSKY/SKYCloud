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
      <a-table-column :width="280" title="操作">
        <template #cell="{ record }">
          <a-space>
            <a-button v-if="!record.is_folder" size="small" type="text" @click="$emit('download', record)">
              下载
            </a-button>
            <a-button v-if="!record.is_folder" size="small" type="text" @click="$emit('share', record)">
              分享
            </a-button>
            <a-button size="small" type="text" @click="$emit('rename', record)">
              重命名
            </a-button>
            <a-button size="small" type="text" @click="$emit('move', record)">
              移动
            </a-button>
            <a-button v-if="!record.is_folder && record.status === 'fail'" size="small" type="text"
                      @click="$emit('retry-embedding', record)">
              重试
            </a-button>
            <a-popconfirm content="确定删除吗？" @ok="$emit('delete', record)">
              <a-button size="small" status="danger" type="text">
                删除
              </a-button>
            </a-popconfirm>
          </a-space>
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
import {IconFile, IconFolder} from '@arco-design/web-vue/es/icon'

const props = defineProps<{
  data: any[]
  loading: boolean
  pagination: any
  selectedKeys?: any[]
}>()

const emit = defineEmits(['file-click', 'download', 'share', 'delete', 'page-change', 'page-size-change', 'batch-delete', 'update:selectedKeys', 'sorter-change', 'retry-embedding', 'rename', 'move'])

const internalSelectedKeys = ref([])

const selection = computed({
  get: () => props.selectedKeys || internalSelectedKeys.value,
  set: (val:any) => {
    internalSelectedKeys.value = val
    emit('update:selectedKeys', val)
  }
})

const rowSelection = reactive({
  type: 'checkbox',
  showCheckedAll: true,
  onlyCurrent: false
})

const handleSorterChange = (dataIndex: string, direction: string) => {
  emit('sorter-change', {dataIndex, direction})
}

const formatSize = (size: number) => {
  if (!size) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  while (size >= 1024 && i < units.length - 1) {
    size /= 1024
    i++
  }
  return `${size.toFixed(2)} ${units[i]}`
}

const formatDate = (dateStr: string) => {
  if (!dateStr) return '-'
  const date = new Date(dateStr)
  return date.toLocaleString()
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
