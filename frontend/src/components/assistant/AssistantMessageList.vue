<template>
  <div ref="container" class="message-list">
    <div v-if="!messages.length" class="empty-state">
      <span class="empty-icon">
        <icon-robot :size="28" />
      </span>
      <div class="empty-title">开始与 AI 助手对话</div>
      <div class="empty-copy">快速模式从资料中寻找答案，专家模式可以继续分析、执行和交付。</div>
    </div>
    <article v-for="message in messages" :key="message.id" class="message-row" :class="message.role">
      <div class="message-avatar">
        <icon-user v-if="message.role === 'user'" :size="14" />
        <icon-robot v-else :size="14" />
      </div>
      <div class="message-body">
        <div class="message-meta">
          <span class="message-author">{{
            message.role === 'user' ? '我' : mode === 'expert' ? '专家 · OpenCode' : '助手 · 快速模式'
          }}</span>
          <span v-if="message.role === 'user'" class="message-type">询问</span>
          <span v-if="message.status !== 'completed'" class="message-status">{{ statusLabel(message.status) }}</span>
        </div>
        <div class="message-bubble">
          <div v-if="message.role === 'assistant' && message.title" class="assistant-title">
            <span class="section-label">标题</span>
            <span>{{ message.title }}</span>
          </div>
          <div v-if="message.role === 'assistant'" class="answer-block">
            <div class="section-label answer-label">回答</div>
            <div class="answer-content">
              <div v-if="message.keywords" class="keyword-line">
                <span class="meta-label">检索词</span>{{ message.keywords }}
              </div>
              <MarkdownRenderer :content="message.content || (message.status === 'streaming' ? '正在处理…' : '')" />
            </div>
          </div>
          <div v-else class="user-content">{{ message.content }}</div>
          <div v-if="message.sources?.length" class="source-line">
            <span class="meta-label">来源</span>
            <span
              v-for="source in message.sources.slice(0, 3)"
              :key="String(source.file_id) + String(source.page_number)"
              class="source-chip"
            >
              {{ source.file_name || `文件 ${source.file_id}` }}<em v-if="source.page_number"> · p{{ source.page_number }}</em>
            </span>
          </div>
          <ExpertToolTimeline v-if="message.role === 'assistant'" :tools="message.tools" />
          <PermissionRequestCard
            v-if="message.permission && activeRunId"
            :permission="message.permission"
            :loading="permissionLoading"
            @respond="$emit('permission', $event)"
          />
          <FileDiffCard v-if="message.role === 'assistant'" :files="message.diff" />
        </div>
        <button
          v-if="message.role === 'assistant' && mode === 'fast' && message.content && message.status === 'completed' && canHandoff"
          class="handoff-button"
          type="button"
          @click="$emit('handoff', message.id)"
        >
          <icon-arrow-right :size="12" /> 交给专家继续
        </button>
      </div>
    </article>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, nextTick } from 'vue'
import { IconArrowRight, IconRobot, IconUser } from '@arco-design/web-vue/es/icon'
import MarkdownRenderer from '../MarkdownRenderer.vue'
import ExpertToolTimeline from './ExpertToolTimeline.vue'
import FileDiffCard from './FileDiffCard.vue'
import PermissionRequestCard from './PermissionRequestCard.vue'
import type { AssistantMode } from '@/api/assistant'
import type { AssistantMessage } from '@/stores/assistant'

const props = defineProps<{
  messages: AssistantMessage[]
  mode: AssistantMode
  canHandoff: boolean
  activeRunId?: number
  permissionLoading?: boolean
}>()

defineEmits<{
  (event: 'handoff', messageId: number): void
  (event: 'permission', response: 'once' | 'reject'): void
}>()

const container = ref<HTMLElement | null>(null)

watch(
  () => props.messages.length,
  async () => {
    await nextTick()
    if (container.value) container.value.scrollTop = container.value.scrollHeight
  },
)

function statusLabel(status: string) {
  if (status === 'streaming') return '生成中'
  if (status === 'failed') return '失败'
  if (status === 'cancelled') return '已取消'
  return '排队中'
}
</script>

<style scoped>
.message-list {
  flex: 1;
  min-height: 0;
  padding: 16px 14px 12px;
  overflow-y: auto;
  background-color: var(--color-fill-2);
  scrollbar-color: var(--color-fill-4) transparent;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  max-width: 280px;
  margin: 64px auto 0;
  text-align: center;
  color: var(--color-text-3);
}

.empty-icon {
  display: grid;
  width: 56px;
  height: 56px;
  margin-bottom: 6px;
  color: #fff;
  background-color: rgb(var(--arcoblue-6));
  border-radius: 14px;
  place-items: center;
  box-shadow: 0 6px 20px rgba(22, 93, 255, 0.18);
}

.empty-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--color-text-1);
}

.empty-copy {
  font-size: 12px;
  line-height: 1.6;
  color: var(--color-text-3);
}

.message-row {
  display: flex;
  gap: 10px;
  margin-bottom: 14px;
}

.message-row.user {
  flex-direction: row-reverse;
}

.message-avatar {
  display: grid;
  width: 28px;
  height: 28px;
  flex: 0 0 auto;
  color: #fff;
  background-color: rgb(var(--arcoblue-6));
  border-radius: 6px;
  place-items: center;
  box-shadow: 0 2px 6px rgba(22, 93, 255, 0.18);
}

.message-row.user .message-avatar {
  color: #fff;
  background-color: rgb(var(--gray-7));
  box-shadow: none;
}

.message-body {
  min-width: 0;
  max-width: calc(100% - 40px);
}

.message-row.user .message-body {
  text-align: right;
}

.message-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 2px 4px;
  color: var(--color-text-3);
  font-size: 12px;
}

.message-row.user .message-meta {
  justify-content: flex-end;
}

.message-author {
  font-weight: 500;
  color: var(--color-text-2);
}

.message-type,
.section-label {
  display: inline-flex;
  align-items: center;
  padding: 1px 6px;
  color: rgb(var(--arcoblue-6));
  background-color: rgb(var(--arcoblue-1));
  border-radius: 999px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.04em;
  line-height: 1.5;
}

.message-row.user .message-type {
  color: rgba(255, 255, 255, 0.9);
  background-color: rgba(255, 255, 255, 0.18);
}

.message-status {
  padding: 0 6px;
  font-size: 11px;
  color: rgb(var(--orange-6));
  background-color: rgb(var(--orange-1));
  border-radius: 999px;
}

.message-bubble {
  display: inline-block;
  padding: 9px 12px;
  color: var(--color-text-1);
  background-color: #fff;
  border: 1px solid var(--color-border-2);
  border-radius: 4px 10px 10px 10px;
  font-size: 13px;
  line-height: 1.6;
  text-align: left;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
  overflow-wrap: anywhere;
  max-width: 100%;
}

.message-row.user .message-bubble {
  color: #fff;
  background-color: rgb(var(--arcoblue-6));
  border-color: rgb(var(--arcoblue-6));
  border-radius: 10px 4px 10px 10px;
}

.assistant-title {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin: -2px 0 10px;
  padding: 8px 10px;
  color: var(--color-text-1);
  background: linear-gradient(135deg, rgb(var(--arcoblue-1)), #fff);
  border-left: 3px solid rgb(var(--arcoblue-6));
  border-radius: 4px 8px 8px 4px;
  font-size: 13px;
  font-weight: 600;
  line-height: 1.45;
}

.answer-block {
  margin-top: 2px;
}

.answer-label {
  margin-bottom: 5px;
}

.answer-content {
  min-width: 0;
}

.user-content {
  white-space: pre-wrap;
}

.keyword-line {
  margin: -2px 0 8px;
  padding-bottom: 6px;
  color: var(--color-text-2);
  border-bottom: 1px dashed var(--color-border-2);
  font-size: 12px;
  line-height: 1.5;
  word-break: break-word;
}

.meta-label {
  display: inline-block;
  margin-right: 6px;
  padding: 1px 5px;
  font-size: 11px;
  color: var(--color-text-3);
  background-color: var(--color-fill-2);
  border-radius: 3px;
  vertical-align: middle;
}

.message-row.user .meta-label {
  color: rgba(255, 255, 255, 0.85);
  background-color: rgba(255, 255, 255, 0.18);
}

.source-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed var(--color-border-2);
}

.message-row.user .source-line {
  border-top-color: rgba(255, 255, 255, 0.3);
}

.source-chip {
  display: inline-flex;
  align-items: center;
  padding: 2px 6px;
  color: var(--color-text-2);
  background-color: var(--color-fill-2);
  border-radius: 3px;
  font-size: 11px;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.message-row.user .source-chip {
  color: rgba(255, 255, 255, 0.95);
  background-color: rgba(255, 255, 255, 0.18);
}

.source-chip em {
  font-style: normal;
  color: var(--color-text-3);
}

.message-row.user .source-chip em {
  color: rgba(255, 255, 255, 0.7);
}

.handoff-button {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-top: 6px;
  padding: 0;
  color: rgb(var(--arcoblue-6));
  background: none;
  border: 0;
  font-size: 12px;
  cursor: pointer;
  transition: color 0.18s ease;
}

.handoff-button:hover {
  color: rgb(var(--arcoblue-5));
}
</style>
