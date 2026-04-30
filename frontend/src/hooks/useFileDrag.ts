import { ref } from 'vue'
import type { Ref } from 'vue'

export function useFileDrag(
  currentParentId: Ref<number | null>,
  startUpload: (files: File[]) => Promise<void>,
) {
  const isDragging = ref(false)
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
