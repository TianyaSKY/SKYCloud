import type { Ref } from 'vue'
import { ref } from 'vue'

/**
 * 文件拖拽上传：用 enter/leave 计数抵消子元素冒泡造成的闪烁。
 * currentParentId 仅透传语义，实际上传由 startUpload 决定目标目录。
 */
export function useFileDrag(currentParentId: Ref<number | null>, startUpload: (files: File[]) => Promise<void>) {
  const isDragging = ref(false)
  // 子元素间移动会反复触发 enter/leave；计数归零才真正离开放置区
  let dragCounter = 0

  const handleDragEnter = (e: DragEvent) => {
    e.preventDefault()
    dragCounter += 1
    if (e.dataTransfer?.items && e.dataTransfer.items.length > 0) {
      isDragging.value = true
    }
  }

  const handleDragLeave = (e: DragEvent) => {
    e.preventDefault()
    dragCounter -= 1
    if (dragCounter === 0) {
      isDragging.value = false
    }
  }

  const handleDrop = async (e: DragEvent) => {
    e.preventDefault()
    isDragging.value = false
    dragCounter = 0

    const droppedFiles = e.dataTransfer?.files
    if (!droppedFiles || droppedFiles.length === 0) return

    await startUpload(Array.from(droppedFiles))
  }

  return {
    isDragging,
    handleDragEnter,
    handleDragLeave,
    handleDrop,
  }
}
