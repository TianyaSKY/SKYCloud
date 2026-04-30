import { Notification, Message } from '@arco-design/web-vue'
import { uploadFileOptimized } from '../utils/upload'
import type { Ref } from 'vue'

/** 并发上传数 */
const UPLOAD_CONCURRENCY = 3

/**
 * 统一的文件上传管理器。
 * 处理单文件 / 多文件上传，并提供一致的通知反馈。
 */
export function useUploadManager(
  currentParentId: Ref<number | null>,
  fetchFiles: () => Promise<void>,
) {
  /**
   * 上传一组文件（≥1 个）。
   * - 单文件：显示独立的实时进度通知
   * - 多文件：显示聚合计数通知 + 并发队列
   */
  const startUpload = async (files: File[]) => {
    if (!files || files.length === 0) return

    if (files.length === 1) {
      await uploadSingle(files[0]!)
    } else {
      await uploadBatch(files)
    }
  }

  // ─── 单文件上传 ──────────────────────────────────────────

  const uploadSingle = async (file: File) => {
    const nid = `upload-${file.name}-${Date.now()}`

    Notification.info({
      id: nid,
      title: '上传中',
      content: `正在准备上传 ${file.name}...`,
      duration: 0,
      closable: false,
    })

    try {
      const result = await uploadFileOptimized(
        file,
        currentParentId.value,
        (percent) => {
          Notification.info({
            id: nid,
            title: '上传中',
            content: `正在上传 ${file.name}: ${Math.round(percent)}%`,
            duration: 0,
            closable: false,
          })
        },
      )

      Notification.success({
        id: nid,
        title: result.instantUpload ? '秒传完成' : '上传完成',
        content: `${file.name} ${result.instantUpload ? '秒传成功' : '上传成功'}`,
        duration: 3000,
        closable: true,
      })

      await fetchFiles()
    } catch (error: any) {
      Notification.error({
        id: nid,
        title: '上传失败',
        content: `${file.name} 上传失败: ${error.message || '未知错误'}`,
        duration: 4000,
        closable: true,
      })
    }
  }

  // ─── 多文件批量上传 ──────────────────────────────────────

  const uploadBatch = async (files: File[]) => {
    const batchId = `batch-upload-${Date.now()}`
    const total = files.length
    let completed = 0
    let failed = 0
    let currentFileName = ''

    const updateNotification = () => {
      const inProgress = total - completed - failed
      Notification.info({
        id: batchId,
        title: '批量上传中',
        content: `总计 ${total} 个文件：已完成 ${completed}，失败 ${failed}，进行中 ${inProgress}${
          currentFileName ? `\n当前：${currentFileName}` : ''
        }`,
        duration: 0,
        closable: false,
      })
    }

    updateNotification()
    await new Promise((resolve) => setTimeout(resolve, 0))

    // 并发队列
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

        // 让出主线程，避免 UI 卡顿
        await new Promise((resolve) => setTimeout(resolve, 0))
      }
    }

    const workers = Array.from(
      { length: Math.min(UPLOAD_CONCURRENCY, files.length) },
      () => worker(),
    )
    await Promise.all(workers)

    // 结果通知
    if (failed === 0) {
      Notification.success({
        id: batchId,
        title: '批量上传完成',
        content: `全部 ${total} 个文件上传成功`,
        duration: 4000,
        closable: true,
      })
    } else {
      Notification.warning({
        id: batchId,
        title: '批量上传完成',
        content: `共 ${total} 个文件：成功 ${completed}，失败 ${failed}`,
        duration: 6000,
        closable: true,
      })
    }

    if (completed > 0) {
      await fetchFiles()
    }
    if (failed > 0) {
      Message.error(`有 ${failed} 个文件上传失败`)
    }
  }

  return { startUpload }
}
