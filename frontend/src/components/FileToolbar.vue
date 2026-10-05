<template>
  <div class="toolbar">
    <a-space size="medium">
      <!-- 核心操作组（仅 editor+ 可见） -->
      <a-button-group v-if="canWrite">
        <a-button type="primary" @click="triggerUpload">
          <template #icon>
            <icon-upload />
          </template>
          上传
        </a-button>
        <input ref="fileInputRef" type="file" multiple class="hidden-file-input" @change="onFilesSelected" />
        <a-button type="primary" @click="$emit('create-folder')">
          <template #icon>
            <icon-folder-add />
          </template>
          新建文件夹
        </a-button>
      </a-button-group>

      <!-- 常用辅助操作 -->
      <a-button @click="$emit('refresh')">
        <template #icon>
          <icon-refresh />
        </template>
        刷新
      </a-button>

      <!-- 低频/系统操作收纳（仅 editor+ 可见） -->
      <a-dropdown v-if="canWrite" trigger="click">
        <a-button>
          <template #icon>
            <icon-more />
          </template>
          更多
        </a-button>
        <template #content>
          <a-doption @click="$emit('organize')">
            <template #icon>
              <icon-bulb />
            </template>
            智能整理
          </a-doption>
          <a-doption @click="$emit('rebuild-indexes')">
            <template #icon>
              <icon-tool />
            </template>
            重建索引
          </a-doption>
        </template>
      </a-dropdown>
    </a-space>
  </div>
</template>

<script lang="ts" setup>
import { onUnmounted, ref, watch } from 'vue'
import { IconBulb, IconFolderAdd, IconMore, IconRefresh, IconTool, IconUpload } from '@arco-design/web-vue/es/icon'

const props = withDefaults(
  defineProps<{
    handleBatchUpload: (files: File[]) => void
    canWrite?: boolean
    uploadContextKey?: string
  }>(),
  { canWrite: true, uploadContextKey: '' },
)

defineEmits(['create-folder', 'organize', 'refresh', 'rebuild-indexes'])

const fileInputRef = ref<HTMLInputElement | null>(null)

let generation = 0
let pickerGeneration: number | null = null
const pendingFrames = new Set<number>()
const invalidatePicker = () => {
  generation += 1
  pickerGeneration = null
  for (const frame of pendingFrames) cancelAnimationFrame(frame)
  pendingFrames.clear()
}
watch([() => props.uploadContextKey, () => props.canWrite], invalidatePicker, { flush: 'sync' })
onUnmounted(invalidatePicker)

const triggerUpload = () => {
  if (!props.canWrite) return
  pickerGeneration = generation
  fileInputRef.value?.click()
}

const onFilesSelected = (event: Event) => {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = ''
  const selectedGeneration = pickerGeneration
  pickerGeneration = null
  if (!files.length || !props.canWrite || selectedGeneration !== generation) return

  // Allow the picker to close before hashing; discard callbacks after navigation.
  const frame = requestAnimationFrame(() => {
    pendingFrames.delete(frame)
    if (props.canWrite && selectedGeneration === generation) props.handleBatchUpload(files)
  })
  pendingFrames.add(frame)
}

</script>

<style scoped>
.toolbar {
  margin-bottom: 16px;
  display: flex;
  align-items: center;
}

.hidden-file-input {
  display: none;
}

/* 按钮组轻微阴影，与周围工具栏层次区分 */
:deep(.arco-btn-group) {
  box-shadow: 0 2px 4px rgba(0, 0, 0, 0.05);
}
</style>
