import {ref} from 'vue'
import {Message, Notification} from '@arco-design/web-vue'
import {uploadFileOptimized} from '../utils/upload'

const FILE_UPLOAD_CONCURRENCY = 3

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

        // 单文件保留原有独立通知
        if (files.length === 1) {
            const file = files[0]
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
                const result = await uploadFileOptimized(file, currentParentId.value, (percent) => {
                    Notification.info({
                        id: notificationId,
                        title: '上传中',
                        content: `正在上传 ${file.name}: ${Math.round(percent)}%`,
                        duration: 0,
                        closable: false
                    })
                })
                Notification.success({
                    id: notificationId,
                    title: result.instantUpload ? '秒传完成' : '上传完成',
                    content: `${file.name} ${result.instantUpload ? '秒传成功' : '上传成功'}`,
                    duration: 3000,
                    closable: true
                })
                await fetchFiles()
            } catch (error: any) {
                Notification.error({
                    id: notificationId,
                    title: '上传失败',
                    content: `${file.name} 上传失败: ${error.message || '未知错误'}`,
                    duration: 4000,
                    closable: true
                })
            }
            return
        }

        // 多文件 —— 聚合通知 + 并发队列
        const batchId = `batch-drag-upload-${Date.now()}`
        const total = files.length
        let completed = 0
        let failed = 0
        let currentFileName = ''

        const updateNotification = () => {
            const inProgress = total - completed - failed
            Notification.info({
                id: batchId,
                title: '批量上传中',
                content: `总计 ${total} 个文件：已完成 ${completed}，失败 ${failed}，进行中 ${inProgress}${currentFileName ? `\n当前：${currentFileName}` : ''}`,
                duration: 0,
                closable: false
            })
        }

        updateNotification()
        await new Promise(resolve => setTimeout(resolve, 0))

        let cursor = 0
        const worker = async () => {
            while (true) {
                const current = cursor++
                if (current >= files.length) return
                const file = files[current]
                if (!file) return

                currentFileName = file.name
                updateNotification()

                try {
                    await uploadFileOptimized(file, currentParentId.value)
                    completed++
                } catch {
                    failed++
                }
                updateNotification()
                await new Promise(resolve => setTimeout(resolve, 0))
            }
        }

        const workers = Array.from(
            {length: Math.min(FILE_UPLOAD_CONCURRENCY, files.length)},
            () => worker()
        )
        await Promise.all(workers)

        if (failed === 0) {
            Notification.success({
                id: batchId,
                title: '批量上传完成',
                content: `全部 ${total} 个文件上传成功`,
                duration: 4000,
                closable: true
            })
        } else {
            Notification.warning({
                id: batchId,
                title: '批量上传完成',
                content: `共 ${total} 个文件：成功 ${completed}，失败 ${failed}`,
                duration: 6000,
                closable: true
            })
        }

        if (completed > 0) {
            await fetchFiles()
        }
        if (failed > 0) {
            Message.error(`有 ${failed} 个文件上传失败`)
        }
    }

    return {
        isDragging,
        handleDragEnter,
        handleDragLeave,
        handleDrop
    }
}
