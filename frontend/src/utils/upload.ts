import { sha256 } from '@noble/hashes/sha2.js'
import { isCancel, type AxiosRequestConfig } from 'axios'
import type { MultipartInitRequest, MultipartInitResponse } from '../api/file'
import { completeMultipartUpload, initMultipartUpload, preflightFileUpload, uploadFile, uploadMultipartChunk } from '../api/file'
import { assertRequestContext, captureRequestContext, type RequestContext } from './requestContext'

type ProgressCallback = (percent: number) => void
interface UploadResult { instantUpload: boolean }
export interface UploadOptions { signal?: AbortSignal; context?: RequestContext }
const LARGE_FILE_THRESHOLD = 20 * 1024 * 1024
const CHUNK_SIZE = 2 * 1024 * 1024
const CHUNK_UPLOAD_ATTEMPTS = 3
const restartedUploads = new Map<string, string>()

function statusOf(error: unknown): number {
  return error && typeof error === 'object' && 'status' in error ? Number(error.status) : 0
}

async function wait(ms: number, signal?: AbortSignal) {
  if (signal?.aborted) throw new DOMException('Upload cancelled', 'AbortError')
  await new Promise<void>((resolve, reject) => {
    const abort = () => { clearTimeout(timer); signal?.removeEventListener('abort', abort); reject(new DOMException('Upload cancelled', 'AbortError')) }
    const timer = setTimeout(() => { signal?.removeEventListener('abort', abort); resolve() }, ms)
    signal?.addEventListener('abort', abort, { once: true })
  })
}

// Incremental SHA-256 keeps at most one 2 MiB slice in memory; no whole-file copy.
async function calculateFileHash(file: File, check: () => void): Promise<string> {
  const hash = sha256.create()
  try {
    for (let offset = 0; offset < file.size; offset += CHUNK_SIZE) {
      check()
      const bytes = await file.slice(offset, offset + CHUNK_SIZE).arrayBuffer()
      check()
      hash.update(new Uint8Array(bytes))
    }
    check()
    return Array.from(hash.digest(), value => value.toString(16).padStart(2, '0')).join('')
  } finally { hash.destroy() }
}

function resumeIdentity(file: File, contentHash: string, parentId: number | null | undefined, context: RequestContext): string {
  const bytes = new TextEncoder().encode(JSON.stringify([context.userId, context.workspaceId, parentId ?? null, file.name, file.size, file.lastModified, contentHash]))
  return 'up_' + Array.from(sha256(bytes), value => value.toString(16).padStart(2, '0')).join('')
}
function resumeId(identity: string): string {
  let stored = restartedUploads.get(identity)
  try { stored ||= localStorage.getItem('skycloud-upload-' + identity) || undefined } catch { /* Storage may be disabled. */ }
  return stored && /^[A-Za-z0-9_-]{8,128}$/.test(stored) ? stored : identity
}
function saveRestart(identity: string, id: string) {
  restartedUploads.set(identity, id)
  try { localStorage.setItem('skycloud-upload-' + identity, id) } catch { /* In-memory resume remains available. */ }
}
function clearRestart(identity: string) {
  restartedUploads.delete(identity)
  try { localStorage.removeItem('skycloud-upload-' + identity) } catch { /* Storage may be disabled. */ }
}

export async function uploadFileOptimized(file: File, parentId?: number | null, onProgress?: ProgressCallback, options: UploadOptions = {}): Promise<UploadResult> {
  const context = options.context ?? captureRequestContext()
  const check = () => assertRequestContext(context, options.signal)
  const config: AxiosRequestConfig = { signal: options.signal, skycloudContext: context }
  let progress = 0
  const report = (percent: number, committed = false) => {
    check()
    if (!Number.isFinite(percent)) return
    progress = Math.max(progress, Math.min(committed ? 100 : 99, Math.max(0, percent)))
    onProgress?.(progress)
  }
  check()
  const contentHash = await calculateFileHash(file, check)
  if (file.size <= LARGE_FILE_THRESHOLD) {
    const preflight = await preflightFileUpload({ filename: file.name, total_size: file.size,
      parent_id: parentId ?? undefined, mime_type: file.type || undefined, content_hash: contentHash }, config)
    check()
    if (preflight.instant_upload) { report(100, true); return { instantUpload: true } }
    const data = new FormData(); data.append('file', file)
    if (parentId !== undefined && parentId !== null) data.append('parent_id', String(parentId))
    await uploadFile(data, { ...config, onUploadProgress: event => {
      try { check() } catch { return }
      const total = event.total || file.size
      if (total > 0) report(event.loaded / total * 100)
    } })
    report(100, true)
    return { instantUpload: false }
  }

  const identity = resumeIdentity(file, contentHash, parentId, context)
  const request: MultipartInitRequest = { filename: file.name, total_size: file.size, chunk_size: CHUNK_SIZE,
    parent_id: parentId ?? undefined, mime_type: file.type || undefined, content_hash: contentHash, upload_id: resumeId(identity) }
  let init: MultipartInitResponse
  try { init = await initMultipartUpload(request, config) } catch (error) {
    check()
    if (statusOf(error) !== 409) throw error
    // Consumed/expired reservations cannot be resumed; retain the fresh ID for later retries.
    const restartedId = crypto.randomUUID()
    saveRestart(identity, restartedId)
    init = await initMultipartUpload({ ...request, upload_id: restartedId }, config)
  }
  check()
  if (init.instant_upload) { clearRestart(identity); report(100, true); return { instantUpload: true } }
  const { upload_id: id, chunk_size: chunkSize, total_chunks: totalChunks } = init
  if (!id || !/^[A-Za-z0-9_-]{8,128}$/.test(id) || !Number.isSafeInteger(chunkSize) || chunkSize <= 0 || !Number.isSafeInteger(totalChunks) || totalChunks !== Math.ceil(file.size / chunkSize) || !Array.isArray(init.uploaded_chunks)) {
    throw new Error('服务器返回的分片上传参数无效')
  }
  if (init.uploaded_chunks.some(index => !Number.isSafeInteger(index) || index < 0 || index >= totalChunks)) throw new Error('服务器返回的已上传分片索引无效')
  const uploaded = new Set(init.uploaded_chunks)
  const bytesFor = (index: number) => Math.min(chunkSize, file.size - index * chunkSize)
  let uploadedBytes = [...uploaded].reduce((sum, index) => sum + bytesFor(index), 0)
  report(uploadedBytes / file.size * 100)
  for (let index = 0; index < totalChunks; index++) {
    check()
    if (uploaded.has(index)) continue
    for (let attempt = 1; ; attempt++) {
      check()
      try {
        const data = new FormData(); data.append('upload_id', id); data.append('chunk_index', String(index))
        data.append('chunk', file.slice(index * chunkSize, (index + 1) * chunkSize), `${file.name}.part${index}`)
        await uploadMultipartChunk(data, undefined, config)
        check()
        break
      } catch (error) {
        check()
        const status = statusOf(error)
        if (isCancel(error) || attempt >= CHUNK_UPLOAD_ATTEMPTS || ![0, 408, 429, 500, 502, 503, 504].includes(status)) throw error
        await wait(500 * attempt, options.signal)
      }
    }
    uploadedBytes += bytesFor(index)
    report(uploadedBytes / file.size * 100)
  }
  check()
  await completeMultipartUpload(id, config)
  check()
  clearRestart(identity)
  report(100, true)
  return { instantUpload: false }
}
