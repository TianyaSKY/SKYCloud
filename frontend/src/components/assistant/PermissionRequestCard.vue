<template>
  <div class="permission-card">
    <div class="permission-kicker">
      <icon-exclamation-circle :size="13" />
      <span>权限确认</span>
    </div>
    <div class="permission-title">{{ permission.title }}</div>
    <code v-if="permission.tool" class="permission-tool">{{ permission.tool }}</code>
    <div class="permission-actions">
      <a-button size="mini" type="primary" :loading="loading" @click="$emit('respond', 'once')">本次允许</a-button>
      <a-button size="mini" status="danger" type="outline" :disabled="loading" @click="$emit('respond', 'reject')">拒绝</a-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { IconExclamationCircle } from '@arco-design/web-vue/es/icon'
import type { AssistantPermission } from '@/stores/assistant'

defineProps<{ permission: AssistantPermission; loading?: boolean }>()
defineEmits<{ (event: 'respond', response: 'once' | 'reject'): void }>()
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
