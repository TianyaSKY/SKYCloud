<template>
  <MainLayout active-menu="inbox" title="收件箱">
    <div class="inbox-content">
      <div class="action-bar">
        <a-button type="text" @click="handleMarkAllRead">
          <template #icon>
            <icon-check/>
          </template>
          全部已读
        </a-button>
      </div>
      <a-list :bordered="false" :loading="loading">
        <template #empty>
          <a-empty description="暂无消息"/>
        </template>
        <a-list-item v-for="item in messages" :key="item.id" class="message-item">
          <a-list-item-meta
              :title="item.title"
          >
            <template #description>
              <div>
                {{ item.content ? (item.content.length > 50 ? item.content.slice(0, 50) + '...' : item.content) : '' }}
                <a-link @click="showDetail(item)">查看详情</a-link>
              </div>
            </template>
            <template #avatar>
              <a-badge
                  :count="!item.is_read ? 1 : 0"
                  :dot="!item.is_read"
                  :show-zero="false"
              >
                <a-avatar :style="{ backgroundColor: item.is_read ? '#f2f3f5' : '#e8f3ff' }">
                  <icon-notification :style="{ color: item.is_read ? '#86909c' : '#165dff' }"/>
                </a-avatar>
              </a-badge>
            </template>
          </a-list-item-meta>
          <template #actions>
            <span class="time">{{ formatDate(item.created_at) }}</span>
            <a-button v-if="!item.is_read" size="small" type="text" @click="handleMarkRead(item.id)">
              标记已读
            </a-button>
            <a-popconfirm content="确定删除这条消息吗？" @ok="handleDelete(item.id)">
              <a-button size="small" status="danger" type="text">
                删除
              </a-button>
            </a-popconfirm>
          </template>
        </a-list-item>
      </a-list>
      <div v-if="total > 0" class="pagination-container">
        <a-pagination
            :current="currentPage"
            :page-size="pageSize"
            :total="total"
            show-total
            @change="handlePageChange"
        />
      </div>
    </div>

    <a-modal v-model:visible="modalVisible" :footer="false" :title="currentMessage?.title">
      <div class="modal-content">{{ currentMessage?.content }}</div>
    </a-modal>
  </MainLayout>
</template>

<script lang="ts" setup>
import {onMounted, ref} from 'vue'
import {useRouter} from 'vue-router'
import {Message} from '@arco-design/web-vue'
import {IconCheck, IconNotification} from '@arco-design/web-vue/es/icon'
import MainLayout from '../components/MainLayout.vue'
import {deleteMessage, getInboxMessages, markAllAsRead, markAsRead} from '../api/inbox'

const router = useRouter()
const loading = ref(false)
const messages = ref<any[]>([])
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(10)

const modalVisible = ref(false)
const currentMessage = ref<any>({})

const showDetail = (item: any) => {
  currentMessage.value = item
  modalVisible.value = true
  if (!item.is_read) {
    handleMarkRead(item.id)
  }
}

const fetchMessages = async () => {
  loading.value = true
  try {
    const res: any = await getInboxMessages(currentPage.value, pageSize.value)
    const data = res.data || res
    messages.value = data.items || []
    total.value = data.total || 0
  } catch (error) {
    console.error('Fetch messages error:', error)
  } finally {
    loading.value = false
  }
}

const handleMarkRead = async (id: number) => {
  try {
    await markAsRead(id)
    await fetchMessages()
  } catch (error) {
  }
}

const handleMarkAllRead = async () => {
  try {
    await markAllAsRead()
    Message.success('已全部标记为已读')
    await fetchMessages()
  } catch (error) {
  }
}

const handleDelete = async (id: number) => {
  try {
    await deleteMessage(id)
    Message.success('消息已删除')
    await fetchMessages()
  } catch (error) {
  }
}

const handlePageChange = (page: number) => {
  currentPage.value = page
  fetchMessages()
}

const formatDate = (dateStr: string) => {
  if (!dateStr) return ''
  const date = new Date(dateStr)
  if (isNaN(date.getTime())) return ''
  return date.toLocaleString()
}

onMounted(() => {
  fetchMessages()
})
</script>

<style scoped>
.inbox-content {
  display: flex;
  flex-direction: column;
}

.action-bar {
  display: flex;
  justify-content: flex-end;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--color-fill-3);
}

.message-item {
  padding: 16px 0;
}

.time {
  color: var(--color-text-3);
  font-size: 12px;
  margin-right: 16px;
}

.pagination-container {
  margin-top: 20px;
  display: flex;
  justify-content: flex-end;
}

.modal-content {
  white-space: pre-wrap;
  line-height: 1.5;
  max-height: 60vh;
  overflow-y: auto;
  padding: 10px;
}
</style>