<template>
  <a-modal :footer="false" :title="title" :visible="visible" width="80%"
           @cancel="handleClose" @close="handleClose">
    <div
        style="height: 75vh; overflow: auto; display: flex; justify-content: center; align-items: flex-start; background: #f8f9fa;">
      <!-- 图片预览 -->
      <div v-if="type === 'image'" style="text-align: center; width: 100%; padding: 20px;">
        <a-image :preview-visible="false" :src="url"/>
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
import VueOfficeDocx from '@vue-office/docx'
import VueOfficeExcel from '@vue-office/excel'
import VueOfficePdf from '@vue-office/pdf'
import '@vue-office/docx/lib/index.css'
import '@vue-office/excel/lib/index.css'
import {IconMusic} from '@arco-design/web-vue/es/icon'

defineProps<{
  visible: boolean
  title: string
  url: string
  type: string
  textContent: string
}>()

const emit = defineEmits(['update:visible', 'close'])

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
</style>
