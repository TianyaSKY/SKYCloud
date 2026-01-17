<template>
  <MainLayout active-menu="share" title="我的分享">
    <a-card title="分享管理">
      <template #extra>
        <a-button @click="fetchShares">
          <template #icon>
            <icon-refresh/>
          </template>
          刷新
        </a-button>
      </template>
      <a-table :data="shareList" :loading="loading">
        <template #columns>
          <a-table-column data-index="file_name" title="文件名">
            <template #cell="{ record }">
              <a-space>
                <icon-file :style="{ color: '#165dff', fontSize: '18px' }"/>
                <span>{{ record.file_name || '未知文件' }}</span>
              </a-space>
            </template>
          </a-table-column>
          <a-table-column title="分享链接">
            <template #cell="{ record }">
              <a-typography-paragraph :style="{ marginBottom: 0 }" copyable>
                {{ getShareUrl(record.token) }}
              </a-typography-paragraph>
            </template>
          </a-table-column>
          <a-table-column data-index="expires_at" title="过期时间">
            <template #cell="{ record }">
              {{ formatDate(record.expires_at) }}
            </template>
          </a-table-column>
          <a-table-column data-index="created_at" title="创建时间">
            <template #cell="{ record }">
              {{ formatDate(record.created_at) }}
            </template>
          </a-table-column>
          <a-table-column title="操作">
            <template #cell="{ record }">
              <a-popconfirm content="确定取消分享吗？" @ok="handleCancelShare(record)">
                <a-button size="small" status="danger" type="text">
                  取消分享
                </a-button>
              </a-popconfirm>
            </template>
          </a-table-column>
        </template>
      </a-table>
    </a-card>
  </MainLayout>
</template>

<script lang="ts" setup>
import {onMounted, ref} from 'vue'
import {Message} from '@arco-design/web-vue'
import {IconFile, IconRefresh} from '@arco-design/web-vue/es/icon'
import MainLayout from '../components/MainLayout.vue'
import {cancelShare, getMyShares} from '../api/share'

const loading = ref(false)
const shareList = ref<any[]>([])

const fetchShares = async () => {
  loading.value = true
  try {
    const res: any = await getMyShares()
    shareList.value = res.data || res
  } catch (error) {
    console.error('Fetch shares error:', error)
  } finally {
    loading.value = false
  }
}

const handleCancelShare = async (record: any) => {
  try {
    await cancelShare(record.id)
    Message.success('已取消分享')
    await fetchShares()
  } catch (error) {
  }
}

const getShareUrl = (token: string) => {
  return `${window.location.origin}/s/${token}`
}

const formatDate = (dateStr: string) => {
  if (!dateStr) return '永久有效'
  const date = new Date(dateStr)
  return date.toLocaleString()
}

onMounted(() => {
  fetchShares()
})
</script>
