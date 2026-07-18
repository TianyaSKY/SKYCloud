import { Notification } from '@arco-design/web-vue'
import { uploadFileOptimized } from '../utils/upload'
import type { Ref } from 'vue'
import { logger } from '../utils/logger'

/** 批量上传并发上限，避免同时打满带宽与浏览器连接数 */
const UPLOAD_CONCURRENCY = 3

/**
 * 统一的文件上传管理器：单文件实时进度 / 多文件聚合通知 + 并发队列。
 *
 * 卸载时调用 cancelAll() 可阻止队列继续派发；进行中的 HTTP 请求无法中断
 *（uploadFileOptimized 未暴露 signal，保持签名以免破坏调用方），已发起的请求会自然完成。
 */
export function useUploadManager(currentParentId: Ref<number | null>, fetchFiles: () => Promise<void>) {
  /** cancelAll 置位后批量队列停止派发新任务 */
  let cancelled = false

  /** 阻止队列派发新任务；已发起的请求不中断 */
  const cancelAll = () => {
    cancelled = true
  }

  /**
   * 上传一组文件（≥1 个）。
   * 单文件：独立实时进度通知；多文件：聚合计数 + 并发队列。
   */
  const startUpload = async (files: File[]) => {
    if (!files || files.length === 0) return
    cancelled = false

    if (files.length === 1) {
      await uploadSingle(files[0]!)
    } else {
      await uploadBatch(files)
    }
  }

  const uploadSingle = async (file: File) => {
    if (cancelled) return
    const nid = `upload-${file.name}-${Date.now()}`

    Notification.info({
      id: nid,
      title: '上传中',
      content: `正在准备上传 ${file.name}...`,
      duration: 0,
      closable: false,
    })

    try {
      const result = await uploadFileOptimized(file, currentParentId.value, (percent) => {
        Notification.info({
          id: nid,
          title: '上传中',
          content: `正在上传 ${file.name}: ${Math.round(percent)}%`,
          duration: 0,
          closable: false,
        })
      })

      Notification.success({
        id: nid,
        title: result.instantUpload ? '秒传完成' : '上传完成',
        content: `${file.name} ${result.instantUpload ? '秒传成功' : '上传成功'}`,
        duration: 3000,
        closable: true,
      })

      await fetchFiles()
    } catch (error) {
      // 拦截器已弹唯一 Message.error，这里仅用 Notification 收尾「上传中」进度态
      const msg = error instanceof Error ? error.message : '未知错误'
      Notification.error({
        id: nid,
        title: '上传失败',
        content: `${file.name} 上传失败: ${msg}`,
        duration: 4000,
        closable: true,
      })
    }
  }

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

    // 固定并发 worker 抢占式消费队列
    let cursor = 0
    const worker = async () => {
      while (true) {
        if (cancelled) return
        const current = cursor++
        if (current >= files.length) return
        const file = files[current]
        if (!file) return

        currentFileName = file.name
        updateNotification()

        try {
          await uploadFileOptimized(file, currentParentId.value)
          completed++
        } catch (error) {
          failed++
          // 拦截器已弹 Message.error，此处仅记日志，最终由汇总 Notification 呈现失败计数
          logger.warn('uploadBatch 单文件上传失败 name={} error={}', file.name, error)
        }
        updateNotification()

        // 让出主线程，避免连续上传时 UI 卡顿
        await new Promise((resolve) => setTimeout(resolve, 0))
      }
    }

    const workers = Array.from({ length: Math.min(UPLOAD_CONCURRENCY, files.length) }, () => worker())
    await Promise.all(workers)

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
  }

  return { startUpload, cancelAll }
}
