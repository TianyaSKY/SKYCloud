<template>
  <!-- 新建文件夹弹窗 -->
  <a-modal :visible="showCreateFolder" title="新建文件夹" @cancel="$emit('update:showCreateFolder', false)"
           @ok="$emit('create-folder')">
    <a-form :model="folderForm">
      <a-form-item field="name" label="文件夹名称">
        <a-input :model-value="folderForm.name" placeholder="请输入文件夹名称"
                 @update:model-value="$emit('update:folderName', $event)"/>
      </a-form-item>
    </a-form>
  </a-modal>

  <!-- 重命名弹窗 -->
  <a-modal :visible="showRenameModal" title="重命名" @cancel="$emit('update:showRenameModal', false)"
           @ok="$emit('confirm-rename')">
    <a-form :model="renameForm">
      <a-form-item field="name" label="名称">
        <a-input :model-value="renameForm.name" placeholder="请输入新名称"
                 @update:model-value="$emit('update:renameName', $event)"/>
      </a-form-item>
    </a-form>
  </a-modal>

  <!-- 移动文件弹窗 -->
  <a-modal :visible="showMoveModal" title="移动到" @cancel="$emit('update:showMoveModal', false)"
           @ok="$emit('confirm-move')">
    <div style="max-height: 400px; overflow-y: auto;">
      <a-tree
          :data="folderTree"
          :field-names="{ key: 'id', title: 'name', children: 'children' }"
          block-node
          selectable
          @select="onFolderSelect"
      />
    </div>
  </a-modal>

  <!-- 创建分享弹窗 -->
  <a-modal :visible="showShareModal" title="创建分享" @cancel="$emit('update:showShareModal', false)"
           @ok="$emit('confirm-share')">
    <a-form :model="shareForm">
      <a-form-item label="文件名">
        <span>{{ selectedFile?.name }}</span>
      </a-form-item>
      <a-form-item label="过期时间">
        <a-date-picker :model-value="shareForm.expires_at" format="YYYY-MM-DD HH:mm:ss" show-time
                       @change="$emit('update:expiresAt', $event)"/>
      </a-form-item>
    </a-form>
  </a-modal>

  <!-- 分享成功弹窗 -->
  <a-modal :footer="false" :visible="showShareResult" title="分享成功" @cancel="$emit('update:showShareResult', false)">
    <div style="text-align: center; padding: 20px 0;">
      <p>分享链接已生成：</p>
      <a-typography-paragraph copyable>{{ shareUrl }}</a-typography-paragraph>
      <p style="color: var(--color-text-3); font-size: 12px; margin-top: 10px;">
        复制链接发送给好友即可访问
      </p>
    </div>
  </a-modal>
</template>

<script lang="ts" setup>
defineProps<{
  showCreateFolder: boolean
  folderForm: { name: string }
  showRenameModal: boolean
  renameForm: { name: string }
  showMoveModal: boolean
  folderTree: any[]
  showShareModal: boolean
  selectedFile: any
  shareForm: { expires_at: string }
  showShareResult: boolean
  shareUrl: string
}>()

const emit = defineEmits([
  'update:showCreateFolder',
  'update:folderName',
  'create-folder',
  'update:showRenameModal',
  'update:renameName',
  'confirm-rename',
  'update:showMoveModal',
  'confirm-move',
  'folder-select',
  'update:showShareModal',
  'update:expiresAt',
  'confirm-share',
  'update:showShareResult'
])

const onFolderSelect = (selectedKeys: Array<string | number>, data: any) => {
  if (selectedKeys.length > 0) {
    emit('folder-select', selectedKeys[0])
  }
}
</script>
