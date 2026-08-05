<template>
  <div v-if="tools?.length" class="tool-timeline">
    <div class="timeline-header">
      <span class="timeline-label">工具调用</span>
      <span class="timeline-count">{{ tools.length }}</span>
    </div>
    <div v-for="tool in tools" :key="tool.id || tool.name" class="tool-row">
      <span class="tool-state" :class="tool.status">
        <icon-check v-if="isDone(tool.status)" :size="10" />
        <icon-loading v-else :size="10" />
      </span>
      <span class="tool-name">{{ tool.name }}</span>
      <span class="tool-status">{{ statusLabel(tool.status) }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { IconCheck, IconLoading } from '@arco-design/web-vue/es/icon'
import type { AssistantToolCall } from '@/stores/assistant'

defineProps<{ tools?: AssistantToolCall[] }>()

function isDone(status: string) {
  return ['completed', 'complete', 'done', 'success', 'failed', 'error'].includes(status)
}

function statusLabel(status: string) {
  if (['completed', 'complete', 'done', 'success'].includes(status)) return '完成'
  if (['failed', 'error'].includes(status)) return '失败'
  return '处理中'
}
</script>

<style scoped>
.tool-timeline {
  margin-top: 8px;
  padding: 8px 10px;
  background-color: var(--color-fill-2);
  border: 1px solid var(--color-border-2);
  border-radius: 5px;
}

.timeline-header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}

.timeline-label {
  font-size: 11px;
  font-weight: 500;
  color: var(--color-text-2);
}

.timeline-count {
  font-size: 10px;
  color: var(--color-text-3);
  background-color: #fff;
  border: 1px solid var(--color-border-2);
  border-radius: 999px;
  padding: 0 5px;
  line-height: 14px;
}

.tool-row {
  display: flex;
  align-items: center;
  gap: 6px;
  min-height: 22px;
  color: var(--color-text-1);
  font-size: 12px;
}

.tool-state {
  display: grid;
  width: 16px;
  height: 16px;
  flex: 0 0 auto;
  color: rgb(var(--green-6));
  background-color: #fff;
  border: 1px solid rgb(var(--green-3));
  border-radius: 50%;
  place-items: center;
}

.tool-state.failed,
.tool-state.error {
  color: rgb(var(--red-6));
  border-color: rgb(var(--red-3));
}

.tool-name {
  flex: 1;
  min-width: 0;
  font-family: 'JetBrains Mono', SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  color: var(--color-text-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-status {
  flex: 0 0 auto;
  color: var(--color-text-3);
  font-size: 11px;
}
</style>
