<template>
  <div class="conversation-strip">
    <div class="strip-header">
      <span class="strip-label">最近会话</span>
      <span v-if="conversations.length" class="strip-count">{{ conversations.length }}</span>
    </div>
    <div class="session-list">
      <div
        v-for="conversation in conversations"
        :key="conversation.id"
        class="session-item"
        :class="{ active: conversation.id === currentId }"
      >
        <button type="button" class="session-button" @click="$emit('select', conversation.id)">
          <span class="session-title">{{ conversation.title || '未命名会话' }}</span>
          <span v-if="conversation.status === 'archived'" class="session-status">已归档</span>
          <span class="session-time">{{ formatTime(conversation.last_message_at || conversation.updated_at) }}</span>
        </button>
        <a-dropdown trigger="click" position="br">
          <a-button size="mini" type="text" class="session-actions" aria-label="会话管理">
            <template #icon><icon-more /></template>
          </a-button>
          <template #content>
            <a-doption @click="$emit('rename', conversation)">
              <template #icon><icon-edit /></template>重命名
            </a-doption>
            <a-doption
              v-if="conversation.status === 'active'"
              @click="$emit('archive', conversation.id)"
            >
              归档
            </a-doption>
            <a-doption v-else @click="$emit('restore', conversation.id)">恢复</a-doption>
            <a-doption style="color: rgb(var(--danger-6))" @click="$emit('delete', conversation.id)">
              <template #icon><icon-delete /></template>删除
            </a-doption>
          </template>
        </a-dropdown>
      </div>
      <span v-if="!conversations.length" class="empty-session">尚无其他会话</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { IconDelete, IconEdit, IconMore } from '@arco-design/web-vue/es/icon'
import type { AssistantConversation } from '@/api/assistant'

defineProps<{
  conversations: AssistantConversation[]
  currentId: number | null
}>()

defineEmits<{
  (event: 'select', id: number): void
  (event: 'rename', conversation: AssistantConversation): void
  (event: 'archive', id: number): void
  (event: 'restore', id: number): void
  (event: 'delete', id: number): void
}>()

function formatTime(value: string | null) {
  if (!value) return '--'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '--'
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}
</script>

<style scoped>
.conversation-strip {
  padding: 8px 12px 10px;
  background-color: #fff;
  border-bottom: 1px solid var(--color-border-2);
}

.strip-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}

.strip-label {
  font-size: 12px;
  font-weight: 500;
  color: var(--color-text-2);
}

.strip-count {
  font-size: 11px;
  color: var(--color-text-3);
  background-color: var(--color-fill-2);
  padding: 0 6px;
  border-radius: 999px;
}

.session-list {
  display: flex;
  gap: 6px;
  overflow-x: auto;
  scrollbar-width: thin;
}

.session-item {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  min-width: 168px;
  max-width: 228px;
  overflow: hidden;
  background-color: var(--color-fill-2);
  border: 1px solid transparent;
  border-radius: 5px;
}

.session-item.active {
  background-color: rgb(var(--arcoblue-1));
  border-color: rgb(var(--arcoblue-3));
}

.session-button {
  display: inline-flex;
  flex: 1;
  align-items: center;
  gap: 6px;
  min-width: 0;
  padding: 6px 10px;
  color: var(--color-text-2);
  text-align: left;
  background: transparent;
  border: 0;
  cursor: pointer;
  transition: background-color 0.18s ease, border-color 0.18s ease, color 0.18s ease;
}

.session-item:hover .session-button,
.session-item.active .session-button {
  color: rgb(var(--arcoblue-6));
}

.session-title {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-time {
  flex: 0 0 auto;
  color: var(--color-text-3);
  font-size: 11px;
}

.session-status {
  flex: 0 0 auto;
  padding: 1px 4px;
  color: var(--color-text-3);
  font-size: 10px;
  background: var(--color-fill-3);
  border-radius: 3px;
}

.session-actions {
  flex: 0 0 auto;
  margin-right: 2px;
  color: var(--color-text-3);
}

.empty-session {
  padding: 6px 0;
  color: var(--color-text-3);
  font-size: 12px;
}
</style>
