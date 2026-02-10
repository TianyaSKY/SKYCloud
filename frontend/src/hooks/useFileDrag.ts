import {ref} from 'vue'
import {Message, Notification} from '@arco-design/web-vue'
import {uploadFileOptimized} from '../utils/upload'

const FILE_UPLOAD_CONCURRENCY = 1

export function useFileDrag(currentParentId: any, fetchFiles: () => Promise<void>) {
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
        if (!droppedFiles || droppedFiles.length === 0) {
            return
        }

        const files = Array.from(droppedFiles)
        let cursor = 0
        let successCount = 0
        let failureCount = 0

        const worker = async () => {
            while (true) {
                // 原子操作获取任务，避免并发竞争
                const current = cursor++
                if (current >= files.length) return

                const file = files[current]
                if (!file) return

                const notificationId = `upload-drag-${file.name}-${Date.now()}`
                
                try {
                    Notification.info({
                        id: notificationId,
                        title: '上传中',
                        content: `正在准备上传 ${file.name}...`,
                        duration: 0,
                        closable: false
                    })

                    await uploadFileOptimized(file, currentParentId.value, (percent) => {
                        Notification.info({
                            id: notificationId,
                            title: '上传中',
                            content: `正在上传 ${file.name}: ${Math.round(percent)}%`,
                            duration: 0,
                            closable: false
                        })
                    })
                    
                    successCount += 1
                    Notification.success({
                        id: notificationId,
                        title: '上传完成',
                        content: `${file.name} 上传成功`,
                        duration: 3000,
                        closable: true
                    })
                } catch (error: any) {
                    failureCount += 1
                    Notification.error({
                        id: notificationId,
                        title: '上传失败',
                        content: `${file.name} 上传失败: ${error.message || '未知错误'}`,
                        duration: 4000,
                        closable: true
                    })
                }
            }
        }

        const workers = Array.from(
            {length: Math.min(FILE_UPLOAD_CONCURRENCY, files.length)},
            () => worker()
        )
        await Promise.all(workers)

        if (successCount > 0) {
            await fetchFiles()
        }
        if (failureCount > 0) {
            Message.error(`有 ${failureCount} 个文件上传失败`)
        }
    }

    return {
        isDragging,
        handleDragEnter,
        handleDragLeave,
        handleDrop
    }
}
