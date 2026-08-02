<template>
  <div v-if="files?.length" class="diff-card">
    <div class="diff-head">
      <span class="diff-label">文件变更</span>
      <span class="diff-count">{{ files.length }} 个文件</span>
    </div>
    <div v-for="file in files" :key="String(file.file || file.path || file.filename)" class="diff-row">
      <icon-file :size="13" />
      <span class="diff-path">{{ file.file || file.path || file.filename || 'workspace file' }}</span>
      <span class="diff-stat add">+{{ file.additions ?? file.added ?? 0 }}</span>
      <span class="diff-stat remove">-{{ file.deletions ?? file.removed ?? 0 }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { IconFile } from '@arco-design/web-vue/es/icon'

defineProps<{ files?: Array<Record<string, unknown>> }>()
</script>

<style scoped>
.diff-card {
  margin-top: 8px;
  padding: 8px 10px;
  background-color: rgb(var(--green-1));
  border: 1px solid rgb(var(--green-3));
  border-radius: 5px;
}

.diff-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}

.diff-label {
  font-size: 12px;
  font-weight: 500;
  color: rgb(var(--green-7));
}

.diff-count {
  font-size: 11px;
  color: var(--color-text-3);
}

.diff-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
  color: var(--color-text-1);
  font-size: 12px;
}

.diff-path {
  min-width: 0;
  flex: 1;
  font-family: 'JetBrains Mono', SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.diff-stat {
  font: 11px 'JetBrains Mono', SFMono-Regular, Menlo, monospace;
}

.diff-stat.add {
  color: rgb(var(--green-6));
}

.diff-stat.remove {
  color: rgb(var(--red-6));
}
</style>
