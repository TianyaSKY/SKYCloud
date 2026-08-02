<template>
  <header class="assistant-header">
    <div class="header-left">
      <span class="header-icon">
        <icon-robot :size="18" />
      </span>
      <div class="header-text">
        <div class="title">AI 助手</div>
        <div class="subtitle">{{ mode === 'expert' ? '专家模式 · OpenCode Runtime' : '快速模式 · 资料检索' }}</div>
      </div>
    </div>
    <div class="header-right">
      <span :class="['status-tag', { online: mode === 'expert' && runtimeStatus === 'running' }]">
        <span class="status-dot"></span>
        {{ mode === 'expert' ? (runtimeStatus === 'running' ? 'Runtime 运行中' : 'Runtime 待命') : 'RAG 就绪' }}
      </span>
      <button class="icon-btn" type="button" aria-label="新建会话" title="新建会话" @click="$emit('new-conversation')">
        <icon-plus :size="15" />
      </button>
      <button class="icon-btn" type="button" aria-label="关闭助手" title="关闭助手" @click="$emit('close')">
        <icon-close :size="15" />
      </button>
    </div>
  </header>
  <div class="mode-switch" role="tablist" aria-label="助手模式">
    <button
      v-for="item in modes"
      :key="item.value"
      class="mode-option"
      :class="{ active: mode === item.value, disabled: item.value === 'expert' && !canUseExpert }"
      type="button"
      role="tab"
      :aria-selected="mode === item.value"
      :disabled="item.value === 'expert' && !canUseExpert"
      @click="$emit('select-mode', item.value)"
    >
      <span class="mode-label">{{ item.label }}</span>
      <span class="mode-desc">{{ item.description }}</span>
      <span v-if="item.value === 'expert' && !canUseExpert" class="mode-lock">需可写权限</span>
    </button>
  </div>
</template>

<script setup lang="ts">
import { IconClose, IconPlus, IconRobot } from '@arco-design/web-vue/es/icon'
import type { AssistantMode } from '@/api/assistant'

defineProps<{
  mode: AssistantMode
  canUseExpert: boolean
  runtimeStatus?: string
}>()

defineEmits<{
  (event: 'select-mode', mode: AssistantMode): void
  (event: 'new-conversation'): void
  (event: 'close'): void
}>()

const modes = [
  { value: 'fast' as const, label: '快速模式', description: '从工作空间资料中检索答案' },
  { value: 'expert' as const, label: '专家模式', description: '执行、修改与交付型任务' },
]
</script>

<style scoped>
.assistant-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 16px 12px;
  background-color: #fff;
  border-bottom: 1px solid var(--color-border-2);
}

.header-left {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.header-icon {
  display: grid;
  width: 32px;
  height: 32px;
  flex: 0 0 auto;
  color: #fff;
  background-color: rgb(var(--arcoblue-6));
  border-radius: 8px;
  place-items: center;
}

.header-text {
  min-width: 0;
}

.title {
  font-size: 15px;
  font-weight: 600;
  line-height: 1.2;
  color: var(--color-text-1);
}

.subtitle {
  margin-top: 3px;
  font-size: 12px;
  line-height: 1.2;
  color: var(--color-text-3);
}

.header-right {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 0 0 auto;
}

.status-tag {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 8px;
  font-size: 12px;
  color: var(--color-text-3);
  background-color: var(--color-fill-2);
  border-radius: 999px;
  white-space: nowrap;
}

.status-tag.online {
  color: rgb(var(--green-6));
  background-color: rgb(var(--green-1));
}

.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background-color: var(--color-text-4);
}

.online .status-dot {
  background-color: rgb(var(--green-6));
  box-shadow: 0 0 0 2px rgba(var(--green-1), 0.6);
}

.icon-btn {
  display: grid;
  width: 28px;
  height: 28px;
  padding: 0;
  color: var(--color-text-2);
  background-color: transparent;
  border: 1px solid var(--color-border-2);
  border-radius: 6px;
  cursor: pointer;
  place-items: center;
  transition: color 0.18s ease, background-color 0.18s ease, border-color 0.18s ease;
}

.icon-btn:hover {
  color: rgb(var(--arcoblue-6));
  background-color: rgb(var(--arcoblue-1));
  border-color: rgb(var(--arcoblue-3));
}

.mode-switch {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  padding: 10px 12px 12px;
  background-color: #fff;
  border-bottom: 1px solid var(--color-border-2);
}

.mode-option {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  min-width: 0;
  padding: 9px 10px;
  color: var(--color-text-2);
  text-align: left;
  background-color: var(--color-fill-2);
  border: 1px solid transparent;
  border-radius: 6px;
  cursor: pointer;
  transition: background-color 0.18s ease, border-color 0.18s ease, color 0.18s ease;
}

.mode-option:hover:not(:disabled) {
  color: rgb(var(--arcoblue-6));
  background-color: rgb(var(--arcoblue-1));
}

.mode-option.active {
  color: rgb(var(--arcoblue-6));
  background-color: rgb(var(--arcoblue-1));
  border-color: rgb(var(--arcoblue-3));
}

.mode-option.disabled {
  color: var(--color-text-4);
  cursor: not-allowed;
}

.mode-label {
  font-size: 13px;
  font-weight: 600;
}

.mode-desc {
  font-size: 11px;
  color: var(--color-text-3);
  line-height: 1.4;
}

.mode-option.active .mode-desc {
  color: var(--color-text-2);
}

.mode-lock {
  margin-top: 2px;
  font-size: 10px;
  color: rgb(var(--orange-6));
}
</style>
