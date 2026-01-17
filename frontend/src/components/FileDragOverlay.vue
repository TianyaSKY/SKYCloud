<template>
  <div v-if="isDragging" class="drag-overlay">
    <div class="drag-content">
      <icon-upload :style="{ fontSize: '48px', color: 'var(--color-primary-light-4)' }"/>
      <div style="margin-top: 16px; font-size: 18px; color: var(--color-text-1);">
        释放文件以上传到当前目录
      </div>
    </div>
  </div>
</template>

<script lang="ts" setup>
import {IconUpload} from '@arco-design/web-vue/es/icon'

defineProps<{
  isDragging: boolean
}>()
</script>

<style scoped>
.drag-overlay {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background-color: var(--color-primary-light-1);
  border: 2px dashed var(--color-primary-light-3);
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
  border-radius: 4px;
  opacity: 0.9;
}

.drag-content {
  text-align: center;
}
</style>
