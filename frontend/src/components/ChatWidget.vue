<template>
  <div
    v-if="show"
    class="chat-widget"
    :style="{
      left: position.x + 'px',
      top: position.y + 'px',
      bottom: 'auto',
      right: 'auto'
    }"
  >
    <!-- 悬浮球 -->
    <div
      class="chat-trigger"
      @mousedown="handleMouseDown"
      @click="handleClick"
      :class="{ 'active': visible }"
    >
      <icon-message v-if="!visible" :size="24" />
      <icon-close v-else :size="24" />
    </div>

    <!-- 聊天框 -->
    <transition name="slide-up">
      <div v-if="visible" class="chat-container" @mousedown.stop>
        <div class="chat-header">
          <div class="title">SKYCloud AI 助手</div>
          <div class="actions">
            <a-button type="text" size="mini" @click="clearHistory">
              <template #icon><icon-delete /></template>
            </a-button>
          </div>
        </div>

        <div class="chat-messages" ref="messageContainer">
          <div
            v-for="(msg, index) in messages"
            :key="index"
            :class="['message-wrapper', msg.role]"
          >
            <div class="avatar">
              <icon-robot v-if="msg.role === 'assistant'" />
              <icon-user v-else />
            </div>
            <div class="message-content">
              <div class="text">
                <div v-if="msg.role === 'assistant'">
                  <!-- 关键词展示 -->
                  <div v-if="msg.keywords" class="keywords-tag">
                    <icon-search /> 搜索关键词：{{ msg.keywords }}
                  </div>
                  <MarkdownRenderer :content="msg.content" />
                </div>
                <div v-else class="user-text">{{ msg.content }}</div>
              </div>
              <div v-if="msg.status" class="status-info">
                <icon-loading v-if="msg.loading" />
                {{ msg.status }}
              </div>
            </div>
          </div>
        </div>

        <div class="chat-input">
          <a-input-search
            v-model="inputValue"
            placeholder="问问 AI 关于你的文件..."
            button-text="发送"
            :loading="loading"
            @search="handleSend"
            @press-enter="handleSend"
          />
        </div>
      </div>
    </transition>
  </div>
</template>

<script lang="ts" setup>
import {nextTick, onMounted, onUnmounted, ref} from 'vue';
import {Message} from '@arco-design/web-vue';
import {
  IconClose,
  IconDelete,
  IconLoading,
  IconMessage,
  IconRobot,
  IconSearch,
  IconUser
} from '@arco-design/web-vue/es/icon';
import MarkdownRenderer from './MarkdownRenderer.vue';

defineProps<{
  show: boolean
}>();

const visible = ref(false);
const inputValue = ref('');
const loading = ref(false);
const messages = ref<ChatMessage[]>([]);
const messageContainer = ref<HTMLElement | null>(null);

// 拖拽相关
const position = ref({ x: window.innerWidth - 86, y: window.innerHeight - 86 });
const isDragging = ref(false);
const dragOffset = ref({ x: 0, y: 0 });
let startTime = 0;

interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  keywords?: string;
  status?: string;
  loading?: boolean;
}

// 过滤 AI 特殊标记的工具函数
const cleanSpecialTokens = (text: string) => {
  if (!text) return '';
  return text.replace(/<\|begin_of_box\|>|<\|end_of_box\|>|<\|thought\|>|<\/thought>/g, '').trim();
};

const handleMouseDown = (e: MouseEvent) => {
  isDragging.value = false;
  startTime = Date.now();
  dragOffset.value = {
    x: e.clientX - position.value.x,
    y: e.clientY - position.value.y
  };

  window.addEventListener('mousemove', handleMouseMove);
  window.addEventListener('mouseup', handleMouseUp);
};

const handleMouseMove = (e: MouseEvent) => {
  isDragging.value = true;
  let newX = e.clientX - dragOffset.value.x;
  let newY = e.clientY - dragOffset.value.y;

  const padding = 10;
  newX = Math.max(padding, Math.min(window.innerWidth - 66, newX));
  newY = Math.max(padding, Math.min(window.innerHeight - 66, newY));

  position.value = { x: newX, y: newY };
};

const handleMouseUp = () => {
  window.removeEventListener('mousemove', handleMouseMove);
  window.removeEventListener('mouseup', handleMouseUp);
};

const handleClick = () => {
  if (!isDragging.value || (Date.now() - startTime < 200)) {
    toggleChat();
  }
};

const toggleChat = () => {
  visible.value = !visible.value;
  if (visible.value) {
    void scrollToBottom();
  }
};

const scrollToBottom = async () => {
  await nextTick();
  if (messageContainer.value) {
    messageContainer.value.scrollTop = messageContainer.value.scrollHeight;
  }
};

const clearHistory = () => {
  messages.value = [];
};

const handleSend = async () => {
  if (!inputValue.value.trim() || loading.value) return;

  const query = inputValue.value;
  inputValue.value = '';

  messages.value.push({ role: 'user', content: query });

  const aiMsg: ChatMessage = {
    role: 'assistant',
    content: '',
    keywords: '',
    status: '正在思考...',
    loading: true
  };
  messages.value.push(aiMsg);

  loading.value = true;
  await scrollToBottom();

  try {
    const history = messages.value
      .slice(0, -1)
      .map(m => ({ role: m.role, content: m.content }));

    const token = localStorage.getItem('token');
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': token ? `Bearer ${token}` : ''
      },
      body: JSON.stringify({ query, history }),
    });

    if (!response.body) throw new Error('No response body');

    const reader = response.body.getReader();
    const decoder = new TextDecoder();

    aiMsg.status = '';
    aiMsg.loading = false;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      const chunk = decoder.decode(value);
      const lines = chunk.split('\n');

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const data = JSON.parse(line.slice(6));
            if (data.type === 'token') {
              aiMsg.content += data.content;
              void scrollToBottom();
            } else if (data.type === 'keywords') {
              aiMsg.keywords = cleanSpecialTokens(data.content);
            } else if (data.type === 'status') {
              aiMsg.status = data.content;
            }
          } catch (e) {
            console.error('Error parsing SSE data', e);
          }
        }
      }
    }
  } catch (error) {
    console.error('Chat error:', error);
    Message.error('发送失败，请稍后重试');
    aiMsg.content = '抱歉，发生了错误。';
  } finally {
    loading.value = false;
    aiMsg.status = '';
    aiMsg.loading = false;
  }
};

const handleResize = () => {
  position.value.x = Math.min(position.value.x, window.innerWidth - 66);
  position.value.y = Math.min(position.value.y, window.innerHeight - 66);
};

onMounted(() => {
  window.addEventListener('resize', handleResize);
});

onUnmounted(() => {
  window.removeEventListener('resize', handleResize);
});
</script>

<style scoped>
.chat-widget {
  position: fixed;
  z-index: 1000;
  /* 移除全局 user-select: none */
}

.chat-trigger {
  width: 56px;
  height: 56px;
  border-radius: 50%;
  background-color: rgb(var(--arcoblue-6));
  color: white;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: move;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  transition: transform 0.2s, box-shadow 0.2s;
  /* 仅在悬浮球上禁止选择，防止拖拽时误选 */
  user-select: none;
}

.chat-trigger:hover {
  transform: scale(1.05);
}

.chat-trigger.active {
  background-color: var(--color-bg-3);
  color: var(--color-text-1);
  cursor: pointer;
}

.chat-container {
  position: absolute;
  bottom: 70px;
  right: 0;
  width: 420px;
  height: 550px;
  background-color: var(--color-bg-3);
  border-radius: 12px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.15);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--color-border);
  cursor: default;
  /* 显式允许选择 */
  user-select: text;
}

.chat-header {
  padding: 16px;
  background-color: rgb(var(--arcoblue-6));
  color: white;
  display: flex;
  justify-content: space-between;
  align-items: center;
  /* 头部通常不需要选择文字 */
  user-select: none;
}

.chat-header .title {
  font-weight: 600;
  font-size: 16px;
}

.chat-messages {
  flex: 1;
  padding: 16px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.message-wrapper {
  display: flex;
  gap: 12px;
  max-width: 90%;
}

.message-wrapper.user {
  align-self: flex-end;
  flex-direction: row-reverse;
}

.avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background-color: var(--color-fill-3);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  user-select: none;
}

.user .avatar {
  background-color: rgb(var(--arcoblue-1));
  color: rgb(var(--arcoblue-6));
}

.message-content {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1;
}

.message-content .text {
  padding: 10px 14px;
  border-radius: 8px;
  background-color: var(--color-fill-2);
  font-size: 14px;
  line-height: 1.6;
  word-break: break-word;
}

.user .text {
  background-color: rgb(var(--arcoblue-6));
  color: white;
}

.user-text {
  white-space: pre-wrap;
}

.keywords-tag {
  font-size: 12px;
  color: rgb(var(--arcoblue-6));
  background-color: rgb(var(--arcoblue-1));
  padding: 4px 8px;
  border-radius: 4px;
  margin-bottom: 8px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: 1px solid rgb(var(--arcoblue-2));
  user-select: none;
}

.status-info {
  font-size: 12px;
  color: var(--color-text-3);
  display: flex;
  align-items: center;
  gap: 4px;
  user-select: none;
}

.chat-input {
  padding: 16px;
  border-top: 1px solid var(--color-border);
}

.slide-up-enter-active,
.slide-up-leave-active {
  transition: all 0.3s ease-out;
}

.slide-up-enter-from,
.slide-up-leave-to {
  transform: translateY(20px);
  opacity: 0;
}
</style>
