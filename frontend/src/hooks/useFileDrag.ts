import {ref} from 'vue'
import {Message} from '@arco-design/web-vue'
import {uploadFile} from '../api/file'

export function useFileDrag(currentParentId: any, fetchFiles: () => Promise<void>) {
    const isDragging = ref(false)
    let dragCounter = 0

    const handleDragEnter = (e: DragEvent) => {
        e.preventDefault()
        dragCounter++
        if (e.dataTransfer?.items && e.dataTransfer.items.length > 0) {
            isDragging.value = true
        }
    }

    const handleDragLeave = (e: DragEvent) => {
        e.preventDefault()
        dragCounter--
        if (dragCounter === 0) {
            isDragging.value = false
        }
    }

    const handleDrop = async (e: DragEvent) => {
        e.preventDefault()
        isDragging.value = false
        dragCounter = 0
        const files = e.dataTransfer?.files
        if (files && files.length > 0) {
            const uploadPromises = Array.from(files).map(async (file) => {
                const formData = new FormData()
                formData.append('file', file)
                if (currentParentId.value) {
                    formData.append('parent_id', currentParentId.value.toString())
                }
                try {
                    await uploadFile(formData)
                    Message.success(`${file.name} 上传成功`)
                } catch (err) {
                    // 错误已由拦截器处理
                }
            })
            await Promise.all(uploadPromises)
            await fetchFiles()
        }
    }

    return {
        isDragging,
        handleDragEnter,
        handleDragLeave,
        handleDrop
    }
}
