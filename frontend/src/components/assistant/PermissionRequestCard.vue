<template>
  <div class="permission-card">
    <div class="permission-kicker">
      <icon-exclamation-circle :size="13" />
      <span>权限确认</span>
    </div>
    <div class="permission-title">{{ permission.title }}</div>
    <code v-if="toolLabel" class="permission-tool">{{ toolLabel }}</code>
    <div class="permission-actions">
      <a-button size="mini" type="primary" :loading="loading" @click="$emit('respond', 'once')">本次允许</a-button>
      <a-button size="mini" status="danger" type="outline" :disabled="loading" @click="$emit('respond', 'reject')">拒绝</a-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { IconExclamationCircle } from '@arco-design/web-vue/es/icon'
import type { AssistantPermission } from '@/stores/assistant'

const props = defineProps<{ permission: AssistantPermission; loading?: boolean }>()
defineEmits<{ (event: 'respond', response: 'once' | 'reject'): void }>()

function formatTool(value: unknown): string {
  if (typeof value === 'string') return value
  if (value && typeof value === 'object') {
    try {
      return JSON.stringify(value, null, 2)
    } catch {
      return '工具详情不可用'
    }
  }
  return value == null ? '' : String(value)
}

const toolLabel = computed(() => formatTool(props.permission.tool))
</script>

<style scoped>
.permission-card {
  margin-top: 8px;
  padding: 10px 12px;
  background-color: rgb(var(--orange-1));
  border: 1px solid rgb(var(--orange-3));
  border-radius: 5px;
}

.permission-kicker {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: rgb(var(--orange-6));
  font-size: 12px;
  font-weight: 500;
}

.permission-title {
  margin-top: 6px;
  color: var(--color-text-1);
  font-size: 13px;
  line-height: 1.5;
}

.permission-tool {
  display: block;
  margin-top: 6px;
  padding: 6px 8px;
  overflow-x: auto;
  color: var(--color-text-2);
  background-color: #fff;
  border: 1px solid var(--color-border-2);
  border-radius: 4px;
  font: 11px/1.5 'JetBrains Mono', SFMono-Regular, Menlo, monospace;
  white-space: pre-wrap;
  word-break: break-word;
}

.permission-actions {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}
</style>
