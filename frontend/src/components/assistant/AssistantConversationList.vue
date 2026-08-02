<template>
  <div class="conversation-strip">
    <div class="strip-header">
      <span class="strip-label">最近会话</span>
      <span v-if="conversations.length" class="strip-count">{{ conversations.length }}</span>
    </div>
    <div class="session-list">
      <button
        v-for="conversation in conversations.slice(0, 6)"
        :key="conversation.id"
        type="button"
        class="session-button"
        :class="{ active: conversation.id === currentId }"
        @click="$emit('select', conversation.id)"
      >
        <span class="session-title">{{ conversation.title || '未命名会话' }}</span>
        <span class="session-time">{{ formatTime(conversation.last_message_at || conversation.updated_at) }}</span>
      </button>
      <span v-if="!conversations.length" class="empty-session">尚无其他会话</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { AssistantConversation } from '@/api/assistant'

defineProps<{
  conversations: AssistantConversation[]
  currentId: number | null
}>()

defineEmits<{ (event: 'select', id: number): void }>()

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

.session-button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 140px;
  max-width: 200px;
  padding: 6px 10px;
  color: var(--color-text-2);
  text-align: left;
  background-color: var(--color-fill-2);
  border: 1px solid transparent;
  border-radius: 5px;
  cursor: pointer;
  transition: background-color 0.18s ease, border-color 0.18s ease, color 0.18s ease;
}

.session-button:hover {
  color: rgb(var(--arcoblue-6));
  background-color: rgb(var(--arcoblue-1));
}

.session-button.active {
  color: rgb(var(--arcoblue-6));
  background-color: rgb(var(--arcoblue-1));
  border-color: rgb(var(--arcoblue-3));
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

.empty-session {
  padding: 6px 0;
  color: var(--color-text-3);
  font-size: 12px;
}
</style>
