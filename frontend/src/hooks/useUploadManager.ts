import { Notification } from '@arco-design/web-vue'
import { CanceledError, isCancel } from 'axios'
import { onScopeDispose, watch, type Ref } from 'vue'
import { uploadFileOptimized } from '../utils/upload'
import { assertRequestContext, captureRequestContext, type RequestContext } from '../utils/requestContext'
import { useWorkspaceStore } from '../stores/workspace'
import { logger } from '../utils/logger'

const UPLOAD_CONCURRENCY = 3
interface Batch { controller: AbortController; context: RequestContext; parentId: number | null; notification: string }

/** Each batch owns its target, cancellation signal and notification; all batches share three slots. */
export function useUploadManager(currentParentId: Ref<number | null>, fetchFiles: () => Promise<void>) {
  const workspace = useWorkspaceStore()
  const batches = new Set<Batch>()
  let sequence = 0
  let disposed = false
  let active = 0
  const waiting: Array<{ resolve: () => void; reject: (error: Error) => void; signal: AbortSignal; abort: () => void }> = []
  const acquire = (signal: AbortSignal): Promise<void> => {
    if (signal.aborted) return Promise.reject(new CanceledError('Upload cancelled'))
    if (active < UPLOAD_CONCURRENCY) { active++; return Promise.resolve() }
    return new Promise((resolve, reject) => {
      const entry = { resolve, reject, signal, abort: () => {
        const index = waiting.indexOf(entry)
        if (index >= 0) waiting.splice(index, 1)
        signal.removeEventListener('abort', entry.abort)
        reject(new CanceledError('Upload cancelled'))
      } }
      waiting.push(entry); signal.addEventListener('abort', entry.abort, { once: true })
    })
  }
  const release = () => {
    const entry = waiting.shift()
    if (entry) { entry.signal.removeEventListener('abort', entry.abort); entry.resolve() }
    else active--
  }
  const current = (batch: Batch) => {
    if (!workspace.canWrite) return false
    try { assertRequestContext(batch.context, batch.controller.signal); return true } catch { return false }
  }
  const cancelAll = () => {
    for (const batch of batches) { batch.controller.abort(); Notification.remove(batch.notification) }
  }
  watch([() => captureRequestContext().workspaceId, () => captureRequestContext().userId, () => captureRequestContext().sessionVersion], cancelAll, { flush: 'sync' })
  watch(() => workspace.canWrite, allowed => { if (!allowed) cancelAll() }, { flush: 'sync' })
  onScopeDispose(() => { disposed = true; cancelAll() })

  const refresh = async (batch: Batch) => {
    if (!current(batch)) return
    try { await fetchFiles() } catch (error) { logger.warn('上传已完成，但文件列表刷新失败: {}', error) }
  }
  const startUpload = async (files: File[]) => {
    if (disposed || !workspace.canWrite || !files.length) return
    const batch: Batch = { controller: new AbortController(), context: captureRequestContext(),
      parentId: currentParentId.value, notification: `upload-${Date.now()}-${++sequence}` }
    batches.add(batch)
    let completed = 0
    let failed = 0
    const single = files.length === 1
    const update = (content: string) => {
      if (current(batch)) Notification.info({ id: batch.notification, title: single ? '上传中' : '批量上传中', content, duration: 0, closable: false })
    }
    update(`正在准备上传 ${single ? files[0]!.name : files.length + ' 个文件'}...`)
    let cursor = 0
    const worker = async () => {
      while (current(batch)) {
        const file = files[cursor++]
        if (!file) return
        let acquired = false
        try {
          await acquire(batch.controller.signal); acquired = true
          if (!current(batch)) return
          const result = await uploadFileOptimized(file, batch.parentId,
            single ? percent => update(`正在上传 ${file.name}: ${Math.round(percent)}%`) : undefined,
            { signal: batch.controller.signal, context: batch.context })
          if (!current(batch)) return
          completed++
          if (single) Notification.success({ id: batch.notification, title: result.instantUpload ? '秒传完成' : '上传完成', content: `${file.name} 上传成功`, duration: 3000, closable: true })
        } catch (error) {
          if (!current(batch) || isCancel(error)) return
          failed++
          if (single) Notification.error({ id: batch.notification, title: '上传失败', content: `${file.name} 上传失败: ${error instanceof Error ? error.message : '未知错误'}`, duration: 4000, closable: true })
          else logger.warn('uploadBatch 单文件上传失败 name={} error={}', file.name, error)
        } finally { if (acquired) release() }
        if (!single) update(`总计 ${files.length} 个文件：已完成 ${completed}，失败 ${failed}，剩余 ${files.length - completed - failed}`)
      }
    }
    try {
      await Promise.all(Array.from({ length: Math.min(UPLOAD_CONCURRENCY, files.length) }, worker))
      if (!current(batch)) { Notification.remove(batch.notification); return }
      if (!single) Notification[failed ? 'warning' : 'success']({ id: batch.notification, title: '批量上传完成', content: `共 ${files.length} 个文件：成功 ${completed}，失败 ${failed}`, duration: 4000, closable: true })
      if (completed) await refresh(batch)
    } finally { batches.delete(batch) }
  }
  return { startUpload, cancelAll }
}
