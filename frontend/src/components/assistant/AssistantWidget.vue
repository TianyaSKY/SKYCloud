<template>
  <div
    v-show="show"
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
          :permission-loading="permissionLoading || assistant.activeRun?.cancelRequested"
          @handoff="handoff"
          @permission="respondPermission"
        />
        <AssistantComposer
          v-model="assistant.draft"
          :mode="assistant.currentMode"
          :loading="assistant.loading || Boolean(assistant.activeRun)"
          :disabled="
            !assistant.initialized || assistant.initializing
              || (assistant.currentMode === 'expert' && (!assistant.expertAvailable || !wsStore.canWrite))
              || assistant.currentConversation?.status === 'archived'
          "
          @send="assistant.send"
          @cancel="cancel"
        />
      </section>
    </transition>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { Message, Modal } from '@arco-design/web-vue'
import { IconClose } from '@arco-design/web-vue/es/icon'

import AssistantHeader from './AssistantHeader.vue'
import AssistantConversationList from './AssistantConversationList.vue'
import AssistantMessageList from './AssistantMessageList.vue'
import AssistantComposer from './AssistantComposer.vue'
import { useAssistantStore, type AssistantContext } from '@/stores/assistant'
import { useWorkspaceStore } from '@/stores/workspace'
import { useAuthStore } from '@/stores/auth'
import type { AssistantConversation } from '@/api/assistant'

const props = defineProps<{ show: boolean }>()

const assistant = useAssistantStore()
const wsStore = useWorkspaceStore()
const auth = useAuthStore()
const visible = ref(false)
const permissionLoading = ref(false)
const position = ref({ x: window.innerWidth - 76, y: window.innerHeight - 76 })
const dragging = ref(false)
const dragOffset = ref({ x: 0, y: 0 })
let mouseDownAt = 0

const currentContext = computed<AssistantContext | null>(() => {
  const userId = auth.user.id
  const workspaceId = wsStore.currentWorkspace?.id
  if (!userId || !workspaceId) return null
  return { userId, sessionVersion: auth.sessionVersion, workspaceId }
})

function reportInitializationFailure(error: unknown) {
  Message.error((error as Error).message || '助手初始化失败')
}

watch(
  [currentContext, () => props.show],
  () => {
    const context = currentContext.value
    // 没有有效身份时只清空上下文；有身份时由 show 决定是否真正建会话（非首页不初始化）。
    if (!context) {
      void assistant.bindContext(null, false)
      return
    }
    void assistant.bindContext(context, props.show).catch(reportInitializationFailure)
  },
  { immediate: true },
)

function toggle() {
  visible.value = !visible.value
  if (!visible.value) return
  const context = currentContext.value
  if (!context) return
  // 同上下文 watcher 不会重跑；打开触发器显式重试，初始化失败后可关闭再打开。
  void assistant.bindContext(context, true).catch(reportInitializationFailure)
}

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

async function cancel() {
  try { await assistant.cancel() }
  catch (error) { Message.error((error as Error).message || '取消任务失败') }
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
