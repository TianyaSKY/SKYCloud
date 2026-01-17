<template>
  <div class="toolbar">
    <a-space>
      <a-upload :custom-request="handleUpload" :show-file-list="false" multiple>
        <template #upload-button>
          <a-button type="primary">
            <template #icon>
              <icon-upload/>
            </template>
            上传文件
          </a-button>
        </template>
      </a-upload>
      <a-button @click="triggerBatchUpload">
        <template #icon>
          <icon-upload/>
        </template>
        批量上传
      </a-button>
      <input
          ref="batchUploadInput"
          multiple
          style="display: none"
          type="file"
          @change="onBatchUploadChange"
      />
      <a-button @click="$emit('create-folder')">
        <template #icon>
          <icon-folder-add/>
        </template>
        新建文件夹
      </a-button>
      <a-button status="success" type="outline" @click="$emit('organize')">
        <template #icon>
          <icon-bulb/>
        </template>
        智能整理
      </a-button>
      <a-button status="warning" type="outline" @click="$emit('rebuild-indexes')">
        <template #icon>
          <icon-tool/>
        </template>
        重建索引
      </a-button>
      <a-button @click="$emit('refresh')">
        <template #icon>
          <icon-refresh/>
        </template>
        刷新
      </a-button>
    </a-space>
  </div>
</template>

<script lang="ts" setup>
import {IconBulb, IconFolderAdd, IconRefresh, IconTool, IconUpload} from '@arco-design/web-vue/es/icon'
import {ref} from 'vue'

const props = defineProps<{
  handleUpload: (options: any) => void
  handleBatchUpload?: (files: File[]) => void
}>()

defineEmits(['create-folder', 'organize', 'refresh', 'rebuild-indexes'])

const batchUploadInput = ref<HTMLInputElement | null>(null)

const triggerBatchUpload = () => {
  batchUploadInput.value?.click()
}

const onBatchUploadChange = (e: Event) => {
  const target = e.target as HTMLInputElement
  if (target.files && target.files.length > 0) {
    const files = Array.from(target.files)
    if (props.handleBatchUpload) {
      props.handleBatchUpload(files)
    }
    // 清空 input，以便下次选择相同文件也能触发 change
    target.value = ''
  }
}
</script>

<style scoped>
.toolbar {
  margin-bottom: 20px;
}
</style>
