<template>
  <MainLayout
      v-model:is-vector-search="isVectorSearch"
      v-model:search-key="searchKey"
      v-model:search-type="searchType"
      :breadcrumbs="breadcrumbs"
      active-menu="all"
      show-breadcrumbs
      show-search
      @search="handleSearch"
      @go-root="goRoot"
      @go-breadcrumb="goBreadcrumb"
      @search-clear="handleSearchClear"
      @update:search-type="saveSearchType"
  >
    <div
        class="file-content"
        @dragenter="handleDragEnter"
        @dragleave="handleDragLeave"
        @drop="handleDrop"
        @dragover.prevent
    >
      <FileDragOverlay :is-dragging="isDragging"/>

      <FileToolbar
          :handle-upload="handleUpload"
          @organize="handleOrganize"
          @refresh="fetchFiles"
          @create-folder="showCreateFolder = true"
          @rebuild-indexes="handleRebuildIndexes"
      />

      <FileTable
          v-model:selected-keys="selectedKeys"
          :data="fileList"
          :loading="loading"
          :pagination="pagination"
          @delete="handleDelete"
          @download="handleDownload"
          @move="handleMove"
          @rename="handleRename"
          @share="handleShare"
          @batch-delete="handleBatchDelete"
          @file-click="handleFileClickWrapper"
          @page-change="handlePageChange"
          @page-size-change="handlePageSizeChange"
          @sorter-change="handleSorterChange"
          @retry-embedding="handleRetryEmbedding"
      />
    </div>

    <FileModals
        v-model:expires-at="shareForm.expires_at"
        v-model:folder-name="folderForm.name"
        v-model:rename-name="renameForm.name"
        v-model:show-create-folder="showCreateFolder"
        v-model:show-move-modal="showMoveModal"
        v-model:show-rename-modal="showRenameModal"
        v-model:show-share-modal="showShareModal"
        v-model:show-share-result="showShareResult"
        :folder-form="folderForm"
        :folder-tree="folderTree"
        :rename-form="renameForm"
        :selected-file="selectedFile"
        :share-form="shareForm"
        :share-url="shareUrl"
        @create-folder="handleCreateFolder"
        @confirm-share="confirmShare"
        @confirm-rename="confirmRename"
        @confirm-move="confirmMove"
        @folder-select="handleFolderSelect"
    />

    <FilePreview
        v-model:visible="previewVisible"
        :text-content="textContent"
        :title="previewTitle"
        :type="previewType"
        :url="previewUrl"
        @close="handlePreviewClose"
    />
  </MainLayout>
</template>

<script lang="ts" setup>
import MainLayout from '../components/MainLayout.vue'
import FileTable from '../components/FileTable.vue'
import FileModals from '../components/FileModals.vue'
import FileToolbar from '../components/FileToolbar.vue'
import FilePreview from '../components/FilePreview.vue'
import FileDragOverlay from '../components/FileDragOverlay.vue'
import {useFileDrag} from '../hooks/useFileDrag'
import {useFilePreview} from '../hooks/useFilePreview'
import {useFileOperations} from '../hooks/useFileOperations'
import {useFileBrowser} from '../hooks/useFileBrowser'

// 文件浏览相关
const {
  loading,
  fileList,
  searchKey,
  searchType,
  isVectorSearch,
  currentParentId,
  breadcrumbs,
  selectedKeys,
  pagination,
  fetchFiles,
  handleSorterChange,
  goRoot,
  goBreadcrumb,
  enterFolder,
  handleSearch,
  handleSearchClear,
  handlePageChange,
  handlePageSizeChange,
  saveSearchType
} = useFileBrowser()

// 拖拽上传相关
const {isDragging, handleDragEnter, handleDragLeave, handleDrop} = useFileDrag(currentParentId, fetchFiles)

// 预览相关
const {
  previewVisible,
  previewTitle,
  previewUrl,
  previewType,
  textContent,
  handleFileClick,
  handlePreviewClose
} = useFilePreview()

// 文件操作相关
const {
  folderForm,
  showCreateFolder,
  showRenameModal,
  renameForm,
  showMoveModal,
  folderTree,
  showShareModal,
  showShareResult,
  selectedFile,
  shareUrl,
  shareForm,
  handleUpload,
  handleCreateFolder,
  handleDelete,
  handleBatchDelete,
  handleDownload,
  handleShare,
  confirmShare,
  handleRetryEmbedding,
  handleRebuildIndexes,
  handleRename,
  confirmRename,
  handleMove,
  handleFolderSelect,
  confirmMove,
  handleOrganize
} = useFileOperations(currentParentId, fetchFiles, selectedKeys, fileList)

const handleFileClickWrapper = async (record: any) => {
  if (record.is_folder) {
    enterFolder(record)
  } else {
    await handleFileClick(record)
  }
}
</script>

<style scoped>
.file-content {
  height: 100%;
  position: relative;
}
</style>
