<template>
  <form class="composer" @submit.prevent="submit">
    <a-textarea
      v-model="value"
      :disabled="disabled"
      :placeholder="mode === 'expert' ? '让专家分析、处理或修改当前工作空间…' : '问问 AI 关于当前工作空间的资料…'"
      :auto-size="{ minRows: 1, maxRows: 5 }"
      allow-clear
      class="composer-textarea"
      @keydown.meta.enter.prevent="submit"
      @keydown.ctrl.enter.prevent="submit"
    />
    <div class="composer-foot">
      <span class="composer-hint">
        <span>{{ mode === 'expert' ? '专家任务' : '工作空间查询' }}</span>
        <span class="hint-divider">·</span>
        <span>⌘/Ctrl + ↵ 发送</span>
      </span>
      <button v-if="loading" class="stop-button" type="button" aria-label="停止任务" @click="$emit('cancel')">
        <icon-stop :size="14" />
        <span>停止</span>
      </button>
      <button v-else class="send-button" type="submit" aria-label="发送" :disabled="disabled">
        <icon-arrow-up :size="15" />
        <span>发送</span>
      </button>
    </div>
  </form>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { IconArrowUp, IconStop } from '@arco-design/web-vue/es/icon'
import type { AssistantMode } from '@/api/assistant'

const props = defineProps<{
  mode: AssistantMode
  loading: boolean
  disabled?: boolean
}>()

const emit = defineEmits<{
  (event: 'send', value: string): void
  (event: 'cancel'): void
}>()

const value = ref('')

function submit() {
  if (props.loading || props.disabled || !value.value.trim()) return
  const query = value.value.trim()
  value.value = ''
  emit('send', query)
}

watch(() => props.mode, () => { value.value = '' })
</script>

<style scoped>
.composer {
  padding: 10px 12px 12px;
  background-color: #fff;
  border-top: 1px solid var(--color-border-2);
}

.composer-textarea :deep(.arco-textarea-wrapper) {
  background-color: var(--color-fill-2);
  border: 1px solid var(--color-border-2);
  border-radius: 6px;
  transition: border-color 0.18s ease, box-shadow 0.18s ease;
}

.composer-textarea :deep(.arco-textarea-wrapper:focus-within) {
  background-color: #fff;
  border-color: rgb(var(--arcoblue-6));
  box-shadow: 0 0 0 2px rgba(var(--arcoblue-2), 0.4);
}

.composer-textarea :deep(.arco-textarea) {
  color: var(--color-text-1);
  font-size: 13px;
  line-height: 1.55;
}

.composer-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 8px;
}

.composer-hint {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--color-text-3);
}

.hint-divider {
  color: var(--color-text-4);
}

.send-button,
.stop-button {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 30px;
  padding: 0 12px;
  font-size: 12px;
  font-weight: 500;
  border: 0;
  border-radius: 5px;
  cursor: pointer;
  transition: background-color 0.18s ease, color 0.18s ease, transform 0.18s ease;
}

.send-button {
  color: #fff;
  background-color: rgb(var(--arcoblue-6));
}

.send-button:hover:not(:disabled) {
  background-color: rgb(var(--arcoblue-5));
}

.send-button:disabled {
  background-color: var(--color-fill-3);
  color: var(--color-text-4);
  cursor: not-allowed;
}

.stop-button {
  color: #fff;
  background-color: rgb(var(--red-5));
}

.stop-button:hover {
  background-color: rgb(var(--red-6));
}
</style>
