<template>
  <div class="toolbar">
    <a-space size="medium">
      <!-- 核心操作组 -->
      <a-button-group>
        <a-upload :custom-request="handleUpload" :show-file-list="false" multiple>
          <template #upload-button>
            <a-button type="primary">
              <template #icon>
                <icon-upload/>
              </template>
              上传
            </a-button>
          </template>
        </a-upload>
        <a-button type="primary" @click="$emit('create-folder')">
          <template #icon>
            <icon-folder-add/>
          </template>
          新建文件夹
        </a-button>
      </a-button-group>

      <!-- 常用辅助操作 -->
      <a-button @click="$emit('refresh')">
        <template #icon>
          <icon-refresh/>
        </template>
        刷新
      </a-button>

      <!-- 低频/系统操作收纳 -->
      <a-dropdown trigger="click">
        <a-button>
          <template #icon>
            <icon-more/>
          </template>
          更多
        </a-button>
        <template #content>
          <a-doption @click="$emit('organize')">
            <template #icon>
              <icon-bulb/>
            </template>
            智能整理
          </a-doption>
          <a-doption @click="$emit('rebuild-indexes')">
            <template #icon>
              <icon-tool/>
            </template>
            重建索引
          </a-doption>
        </template>
      </a-dropdown>
    </a-space>
  </div>
</template>

<script lang="ts" setup>
import {
  IconBulb,
  IconFolderAdd,
  IconMore,
  IconRefresh,
  IconTool,
  IconUpload
} from '@arco-design/web-vue/es/icon'

defineProps<{
  handleUpload: (options: any) => void
}>()

defineEmits(['create-folder', 'organize', 'refresh', 'rebuild-indexes'])
</script>

<style scoped>
.toolbar {
  margin-bottom: 16px;
  display: flex;
  align-items: center;
}

/* 优化按钮组样式，使其更紧凑 */
:deep(.arco-btn-group) {
  box-shadow: 0 2px 4px rgba(0, 0, 0, 0.05);
}
</style>
