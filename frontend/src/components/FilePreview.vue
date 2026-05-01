<template>
  <a-modal :footer="false" :title="title" :visible="visible" width="80%"
           @cancel="handleClose" @close="handleClose">
    <div
        style="height: 75vh; overflow: auto; display: flex; justify-content: center; align-items: flex-start; background: #f8f9fa;">
      <!-- 图片预览 -->
      <div v-if="type === 'image'" style="text-align: center; width: 100%; padding: 20px;">
        <a-image :preview-visible="false" :src="url"/>
      </div>

      <!-- Markdown 渲染预览 -->
      <div v-else-if="type === 'markdown'"
           style="width: 100%; background: #fff; padding: 24px 32px; border-radius: 4px; box-shadow: 0 2px 12px 0 rgba(0,0,0,0.1);">
        <MarkdownRenderer :content="textContent"/>
      </div>

      <!-- 代码预览（带行号） -->
      <div v-else-if="type === 'code'"
           class="code-preview-container">
        <div class="code-header">
          <span class="code-lang">{{ title.split('.').pop()?.toUpperCase() }}</span>
        </div>
        <div class="code-body">
          <div class="line-numbers">
            <span v-for="n in lineCount" :key="n">{{ n }}</span>
          </div>
          <pre class="code-content"><code>{{ textContent }}</code></pre>
        </div>
      </div>

      <!-- 文本/CSV 预览 -->
      <div v-else-if="type === 'text'"
           style="width: 100%; background: #fff; padding: 20px; border-radius: 4px; box-shadow: 0 2px 12px 0 rgba(0,0,0,0.1);">
          <pre
              style="white-space: pre-wrap; word-break: break-all; margin: 0; font-family: 'Courier New', Courier, monospace; font-size: 14px; line-height: 1.6;">{{
              textContent
            }}</pre>
      </div>

      <!-- 视频预览 -->
      <div v-else-if="type === 'video'"
           style="width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; background: #000;">
        <video :src="url" autoplay controls style="max-width: 100%; max-height: 100%;"></video>
      </div>

      <!-- 音频预览 -->
      <div v-else-if="type === 'audio'"
           style="width: 100%; height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; background: #f0f2f5;">
        <div class="audio-player-container">
          <div class="audio-icon">
            <icon-music :style="{ fontSize: '64px', color: '#165dff' }"/>
          </div>
          <div class="audio-info">
            <div class="audio-title">{{ title }}</div>
          </div>
          <audio :src="url" autoplay controls style="width: 400px; margin-top: 20px;"></audio>
        </div>
      </div>

      <!-- Docx 预览 -->
      <vue-office-docx v-else-if="type === 'docx'" :src="url" style="width: 100%; height: 100%;"/>

      <!-- PDF 预览 -->
      <vue-office-pdf v-else-if="type === 'pdf'" :src="url" style="width: 100%; height: 100%;"/>

      <!-- Excel 预览 -->
      <vue-office-excel v-else-if="type === 'excel'" :src="url" style="width: 100%; height: 100%;"/>

      <a-empty v-else description="该文件类型暂不支持预览"/>
    </div>
  </a-modal>
</template>

<script lang="ts" setup>
import {computed} from 'vue'
import VueOfficeDocx from '@vue-office/docx'
import VueOfficeExcel from '@vue-office/excel'
import VueOfficePdf from '@vue-office/pdf'
import '@vue-office/docx/lib/index.css'
import '@vue-office/excel/lib/index.css'
import {IconMusic} from '@arco-design/web-vue/es/icon'
import MarkdownRenderer from './MarkdownRenderer.vue'

const props = defineProps<{
  visible: boolean
  title: string
  url: string
  type: string
  textContent: string
}>()

const emit = defineEmits(['update:visible', 'close'])

const lineCount = computed(() => {
  if (!props.textContent) return 0
  return props.textContent.split('\n').length
})

const handleClose = () => {
  emit('update:visible', false)
  emit('close')
}
</script>

<style scoped>
.audio-player-container {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 40px;
  background: #fff;
  border-radius: 8px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
}

.audio-icon {
  margin-bottom: 20px;
}

.audio-info {
  text-align: center;
}

.audio-title {
  font-size: 18px;
  font-weight: bold;
  color: var(--color-text-1);
  max-width: 350px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 代码预览样式 */
.code-preview-container {
  width: 100%;
  background: #1e1e1e;
  border-radius: 6px;
  overflow: hidden;
  box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.15);
}

.code-header {
  display: flex;
  align-items: center;
  padding: 8px 16px;
  background: #2d2d2d;
  border-bottom: 1px solid #3e3e3e;
}

.code-lang {
  font-size: 12px;
  color: #858585;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  letter-spacing: 0.5px;
}

.code-body {
  display: flex;
  overflow: auto;
}

.line-numbers {
  display: flex;
  flex-direction: column;
  padding: 16px 0;
  min-width: 50px;
  text-align: right;
  user-select: none;
  background: #1e1e1e;
  border-right: 1px solid #3e3e3e;
  position: sticky;
  left: 0;
  z-index: 1;
}

.line-numbers span {
  display: block;
  padding: 0 12px;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 13px;
  line-height: 1.6;
  color: #858585;
}

.code-content {
  margin: 0;
  padding: 16px;
  flex: 1;
  overflow-x: auto;
}

.code-content code {
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 13px;
  line-height: 1.6;
  color: #d4d4d4;
  tab-size: 4;
}
</style>

