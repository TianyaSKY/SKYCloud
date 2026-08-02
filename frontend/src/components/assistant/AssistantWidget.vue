<template>
  <div
    v-if="show"
    class="assistant-widget"
    :style="{ left: `${position.x}px`, top: `${position.y}px` }"
  >
    <div
      :class="['assistant-trigger', { active: visible }]"
      role="button"
      tabindex="0"
      :aria-label="visible ? '关闭 AI 助手' : '打开 AI 助手'"
      :aria-expanded="visible"
      @click="handleTriggerClick"
      @mousedown="handleMouseDown"
      @keydown.enter="toggle"
      @keydown.space.prevent="toggle"
    >
      <span class="trigger-core"><icon-close v-if="visible" :size="21" /><span v-else>✦</span></span>
    </div>

    <transition name="deck-rise">
      <section v-if="visible" class="assistant-deck" @mousedown.stop>
        <AssistantHeader
          :mode="assistant.currentMode"
          :can-use-expert="wsStore.canWrite && assistant.expertAvailable"
          :runtime-status="assistant.currentMode === 'expert' && assistant.activeRun ? 'running' : 'standby'"
          @select-mode="assistant.selectMode"
          @new-conversation="assistant.newConversation"
          @close="toggle"
        />
        <AssistantConversationList
          :conversations="assistant.currentConversations"
          :current-id="assistant.currentConversationId"
          @select="assistant.selectConversation"
          @rename="renameConversation"
          @archive="archiveConversation"
          @restore="restoreConversation"
          @delete="deleteConversation"
        />
        <AssistantMessageList
          :messages="assistant.messages"
          :mode="assistant.currentMode"
          :can-handoff="wsStore.canWrite && assistant.currentMode === 'fast'"
          :active-run-id="assistant.activeRun?.id"
          :permission-loading="permissionLoading"
          @handoff="handoff"
          @permission="respondPermission"
        />
        <AssistantComposer
          :mode="assistant.currentMode"
          :loading="assistant.loading || Boolean(assistant.activeRun)"
          :disabled="
            (assistant.currentMode === 'expert' && !assistant.expertAvailable)
              || assistant.currentConversation?.status === 'archived'
          "
          @send="assistant.send"
          @cancel="assistant.cancel"
        />
      </section>
    </transition>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { Message, Modal } from '@arco-design/web-vue'
import { IconClose } from '@arco-design/web-vue/es/icon'

import AssistantHeader from './AssistantHeader.vue'
import AssistantConversationList from './AssistantConversationList.vue'
import AssistantMessageList from './AssistantMessageList.vue'
import AssistantComposer from './AssistantComposer.vue'
import { useAssistantStore } from '@/stores/assistant'
import { useWorkspaceStore } from '@/stores/workspace'
import type { AssistantConversation } from '@/api/assistant'

defineProps<{ show: boolean }>()

const assistant = useAssistantStore()
const wsStore = useWorkspaceStore()
const visible = ref(false)
const permissionLoading = ref(false)
const position = ref({ x: window.innerWidth - 76, y: window.innerHeight - 76 })
const dragging = ref(false)
const dragOffset = ref({ x: 0, y: 0 })
let mouseDownAt = 0

watch(
  () => wsStore.currentWorkspace?.id,
  (id) => {
    void assistant.bindWorkspace(id ?? null).catch((error) => {
      Message.error((error as Error).message || '助手初始化失败')
    })
  },
  { immediate: true },
)

function toggle() { visible.value = !visible.value }

function handleMouseDown(event: MouseEvent) {
  dragging.value = false
  mouseDownAt = Date.now()
  dragOffset.value = { x: event.clientX - position.value.x, y: event.clientY - position.value.y }
  window.addEventListener('mousemove', handleMouseMove)
  window.addEventListener('mouseup', handleMouseUp)
}

function handleMouseMove(event: MouseEvent) {
  dragging.value = true
  position.value = {
    x: Math.max(10, Math.min(window.innerWidth - 62, event.clientX - dragOffset.value.x)),
    y: Math.max(10, Math.min(window.innerHeight - 62, event.clientY - dragOffset.value.y)),
  }
}

function handleMouseUp() {
  window.removeEventListener('mousemove', handleMouseMove)
  window.removeEventListener('mouseup', handleMouseUp)
}

function handleTriggerClick() {
  if (!dragging.value || Date.now() - mouseDownAt < 200) toggle()
}

async function respondPermission(response: 'once' | 'reject') {
  permissionLoading.value = true
  try { await assistant.respondPermission(response) }
  catch (error) { Message.error((error as Error).message || '权限响应失败') }
  finally { permissionLoading.value = false }
}

async function handoff(messageId: number) {
  try {
    await assistant.handoff(messageId)
  } catch (error) {
    Message.error((error as Error).message || '转交专家失败')
  }
}

async function renameConversation(conversation: AssistantConversation) {
  const title = window.prompt('重命名会话', conversation.title || '')
  if (title === null || !title.trim()) return
  try {
    await assistant.renameConversation(conversation.id, title.trim())
    Message.success('会话已重命名')
  } catch (error) {
    Message.error((error as Error).message || '重命名失败')
  }
}

function archiveConversation(id: number) {
  Modal.confirm({
    title: '归档会话',
    content: '归档后仍可在会话列表中恢复。',
    okText: '归档',
    onOk: async () => {
      try {
        await assistant.archiveConversation(id)
        Message.success('会话已归档')
      } catch (error) {
        Message.error((error as Error).message || '归档失败')
      }
    },
  })
}

async function restoreConversation(id: number) {
  try {
    await assistant.restoreConversation(id)
    Message.success('会话已恢复')
  } catch (error) {
    Message.error((error as Error).message || '恢复失败')
  }
}

function deleteConversation(id: number) {
  Modal.confirm({
    title: '删除会话',
    content: '删除后消息和运行记录不可恢复，确定继续？',
    okText: '删除',
    okButtonProps: { status: 'danger' },
    onOk: async () => {
      try {
        await assistant.removeConversation(id)
        Message.success('会话已删除')
      } catch (error) {
        Message.error((error as Error).message || '删除失败')
      }
    },
  })
}

function handleResize() {
  position.value = {
    x: Math.min(position.value.x, window.innerWidth - 62),
    y: Math.min(position.value.y, window.innerHeight - 62),
  }
}

onMounted(() => window.addEventListener('resize', handleResize))
onUnmounted(() => {
  handleMouseUp()
  window.removeEventListener('resize', handleResize)
})
</script>

<style scoped>
.assistant-widget {
  position: fixed;
  z-index: 1000;
}

/* 悬浮触发球：与项目主色对齐，hover/active 状态使用 Arco 语义色 */
.assistant-trigger {
  display: grid;
  width: 52px;
  height: 52px;
  color: #fff;
  background-color: rgb(var(--arcoblue-6));
  border: 1px solid rgb(var(--arcoblue-6));
  border-radius: 50%;
  box-shadow: 0 6px 20px rgba(22, 93, 255, 0.28);
  cursor: move;
  place-items: center;
  user-select: none;
  transition: transform 0.2s ease, box-shadow 0.2s ease, background-color 0.2s ease;
}

.assistant-trigger:hover {
  transform: scale(1.06);
  box-shadow: 0 8px 24px rgba(22, 93, 255, 0.36);
}

.assistant-trigger.active {
  color: rgb(var(--arcoblue-6));
  background-color: #fff;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.12);
  cursor: pointer;
}

.trigger-core {
  display: grid;
  width: 26px;
  height: 26px;
  color: inherit;
  font: 22px/1 Georgia, serif;
  place-items: center;
}

/* 对话面板：白底卡片风格，与 FileTable 等保持一致 */
.assistant-deck {
  position: absolute;
  right: 0;
  bottom: 64px;
  display: flex;
  width: 440px;
  height: min(680px, calc(100vh - 90px));
  flex-direction: column;
  overflow: hidden;
  background-color: #fff;
  border: 1px solid var(--color-border-2);
  border-radius: 8px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.16);
}

.deck-rise-enter-active,
.deck-rise-leave-active {
  transition: opacity 0.2s ease, transform 0.22s cubic-bezier(0.2, 0.8, 0.2, 1);
  transform-origin: bottom right;
}

.deck-rise-enter-from,
.deck-rise-leave-to {
  opacity: 0;
  transform: translateY(10px) scale(0.98);
}

@media (max-width: 560px) {
  .assistant-deck {
    position: fixed;
    right: 10px;
    bottom: 70px;
    left: 10px;
    width: auto;
    height: min(690px, calc(100vh - 90px));
  }
}
</style>
