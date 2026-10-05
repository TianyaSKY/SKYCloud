import { createHash } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { uploadFileOptimized } from '../upload'
const mocks = vi.hoisted(() => ({ preflight: vi.fn(), init: vi.fn(), chunk: vi.fn(), complete: vi.fn(), upload: vi.fn(), context: { workspaceId: 11, userId: 7, sessionVersion: 1 } }))
vi.mock('../../api/file', () => ({ preflightFileUpload: mocks.preflight, initMultipartUpload: mocks.init, uploadMultipartChunk: mocks.chunk, completeMultipartUpload: mocks.complete, uploadFile: mocks.upload }))
vi.mock('../requestContext', async () => {
  const { CanceledError } = await import('axios')
  return { captureRequestContext: () => ({ ...mocks.context }), assertRequestContext: (context: typeof mocks.context, signal?: AbortSignal) => {
    if (signal?.aborted || JSON.stringify(context) !== JSON.stringify(mocks.context)) throw new CanceledError('context changed')
  } }
})
const MIB = 1024 * 1024
function file(size: number, name = 'file.bin', bytes?: Uint8Array) {
  const value = new File([bytes ? new Uint8Array(bytes).buffer : ''], name, { type: 'application/octet-stream', lastModified: 123 })
  Object.defineProperty(value, 'size', { value: size })
  const slice = vi.fn((start = 0, end = size) => {
    const data = bytes ? bytes.slice(start, Math.min(end, size)) : new Uint8Array(Math.min(end, size) - start)
    const blob = new Blob([new Uint8Array(data).buffer])
    Object.defineProperty(blob, 'arrayBuffer', { value: async () => new Uint8Array(data).buffer })
    return blob
  })
  Object.defineProperty(value, 'slice', { value: slice })
  Object.defineProperty(value, 'arrayBuffer', { value: vi.fn(() => { throw new Error('Whole-file read refused') }) })
  return value
}
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(yes => { resolve = yes }); return { promise, resolve } }
function session(size = 21 * MIB + 1, uploaded: number[] = []) {
  return { upload_id: 'server-upload-id', chunk_size: 2 * MIB, total_chunks: Math.ceil(size / (2 * MIB)), uploaded_chunks: uploaded }
}
beforeEach(() => {
  vi.resetAllMocks(); localStorage.clear(); mocks.context = { workspaceId: 11, userId: 7, sessionVersion: 1 }
  mocks.preflight.mockResolvedValue({ instant_upload: false, exists: false })
  mocks.init.mockImplementation(async request => ({ ...session(request.total_size), upload_id: request.upload_id }))
  mocks.chunk.mockResolvedValue(undefined); mocks.complete.mockResolvedValue(undefined); mocks.upload.mockResolvedValue(undefined)
})
afterEach(() => vi.useRealTimers())

describe('bounded file hash and small uploads', () => {
  it.each([0, 3, 2 * MIB + 17])('hashes %s real bytes incrementally and preserves the parent target', async size => {
    const bytes = new Uint8Array(size).fill(0x61); const value = file(size, 'a.bin', bytes)
    mocks.preflight.mockResolvedValue({ instant_upload: true })
    const progress = vi.fn(); expect(await uploadFileOptimized(value, 15, progress)).toEqual({ instantUpload: true })
    expect(mocks.preflight.mock.calls[0]?.[0]).toMatchObject({ parent_id: 15, total_size: size, content_hash: createHash('sha256').update(bytes).digest('hex') })
    expect(value.arrayBuffer).not.toHaveBeenCalled()
    for (const [start, end] of vi.mocked(value.slice).mock.calls) expect((end ?? 0) - (start ?? 0)).toBeLessThanOrEqual(2 * MIB)
    expect(progress).toHaveBeenLastCalledWith(100); expect(mocks.upload).not.toHaveBeenCalled()
  })
  it('keeps transport progress below 100 until the metadata commit succeeds', async () => {
    const pending = deferred<void>(); mocks.upload.mockReturnValue(pending.promise)
    const progress = vi.fn(); const uploading = uploadFileOptimized(file(5), 15, progress)
    await vi.waitFor(() => expect(mocks.upload).toHaveBeenCalledTimes(1))
    const config = mocks.upload.mock.calls[0]?.[1]
    config.onUploadProgress({ loaded: 5, total: 5 }); config.onUploadProgress({ loaded: Number.NaN, total: 5 }); config.onUploadProgress({ loaded: 1, total: 5 })
    expect(progress.mock.calls.map(call => call[0])).toEqual([99, 99])
    expect(mocks.upload.mock.calls[0]?.[0].get('parent_id')).toBe('15')
    pending.resolve(); await uploading; expect(progress).toHaveBeenLastCalledWith(100)
  })
  it('stops after hashing when the workspace changed before preflight', async () => {
    const value = file(3); const pending = deferred<ArrayBuffer>()
    vi.mocked(value.slice).mockImplementationOnce(() => ({ arrayBuffer: () => pending.promise }) as Blob)
    const uploading = uploadFileOptimized(value); const rejected = expect(uploading).rejects.toMatchObject({ code: 'ERR_CANCELED' })
    mocks.context.workspaceId = 12; pending.resolve(new ArrayBuffer(3)); await rejected
    expect(mocks.preflight).not.toHaveBeenCalled()
  })
  it('does not report stale progress or success after cancellation', async () => {
    const pending = deferred<void>(); mocks.upload.mockReturnValue(pending.promise); const controller = new AbortController()
    const progress = vi.fn(); const uploading = uploadFileOptimized(file(3), 15, progress, { signal: controller.signal })
    const rejected = expect(uploading).rejects.toMatchObject({ code: 'ERR_CANCELED' })
    await vi.waitFor(() => expect(mocks.upload).toHaveBeenCalledTimes(1)); controller.abort()
    mocks.upload.mock.calls[0]?.[1].onUploadProgress({ loaded: 3, total: 3 }); pending.resolve(); await rejected
    expect(progress).not.toHaveBeenCalled(); expect(mocks.upload.mock.calls[0]?.[1].signal).toBe(controller.signal)
  })
})

describe('multipart resume, retry and completion', () => {
  it('resumes only missing parts, honors server chunk size and counts the short last part once', async () => {
    const size = 21 * MIB + 1; mocks.init.mockResolvedValue({ upload_id: 'server-upload-id', chunk_size: 4 * MIB, total_chunks: 6, uploaded_chunks: [0, 5, 0] })
    const pending = deferred<void>(); mocks.complete.mockReturnValue(pending.promise)
    const progress = vi.fn(); const uploading = uploadFileOptimized(file(size), 15, progress)
    await vi.waitFor(() => expect(mocks.complete).toHaveBeenCalledTimes(1))
    expect(mocks.chunk.mock.calls.map(call => call[0].get('chunk_index'))).toEqual(['1', '2', '3', '4'])
    expect(mocks.chunk.mock.calls.map(call => call[0].get('chunk').size)).toEqual([4 * MIB, 4 * MIB, 4 * MIB, 4 * MIB])
    expect(progress.mock.calls[0]?.[0]).toBeCloseTo((5 * MIB + 1) / size * 100)
    expect(progress).toHaveBeenLastCalledWith(99)
    pending.resolve(); await uploading; expect(progress).toHaveBeenLastCalledWith(100)
  })
  it('still requires a successful commit when every part is already present', async () => {
    mocks.init.mockResolvedValue(session(undefined, Array.from({ length: 11 }, (_, index) => index)))
    mocks.complete.mockRejectedValue(new Error('commit failed')); const progress = vi.fn()
    await expect(uploadFileOptimized(file(21 * MIB + 1), null, progress)).rejects.toThrow('commit failed')
    expect(mocks.chunk).not.toHaveBeenCalled(); expect(progress).toHaveBeenLastCalledWith(99)
  })
  it.each([400, 401, 403, 409, 413])('does not retry permanent HTTP %s failures', async status => {
    mocks.chunk.mockRejectedValue(Object.assign(new Error('denied'), { status }))
    await expect(uploadFileOptimized(file(21 * MIB + 1))).rejects.toThrow('denied')
    expect(mocks.chunk).toHaveBeenCalledTimes(1); expect(mocks.complete).not.toHaveBeenCalled()
  })
  it('retries transient errors without double-counting acknowledged parts', async () => {
    vi.useFakeTimers(); mocks.init.mockResolvedValue(session(undefined, Array.from({ length: 10 }, (_, index) => index)))
    mocks.chunk.mockRejectedValueOnce(Object.assign(new Error('busy'), { status: 503 })).mockRejectedValueOnce(Object.assign(new Error('network'), { status: 0 })).mockResolvedValue(undefined)
    const progress = vi.fn(); const uploading = uploadFileOptimized(file(21 * MIB + 1), null, progress)
    await vi.runAllTimersAsync(); await uploading
    expect(mocks.chunk).toHaveBeenCalledTimes(3); expect(mocks.complete).toHaveBeenCalledTimes(1)
    expect(progress.mock.calls.map(call => call[0])).toEqual([expect.any(Number), 99, 100])
  })
  it('cancels during backoff instead of issuing another retry', async () => {
    vi.useFakeTimers(); mocks.chunk.mockRejectedValue(Object.assign(new Error('busy'), { status: 503 }))
    const controller = new AbortController(); const uploading = uploadFileOptimized(file(21 * MIB + 1), null, undefined, { signal: controller.signal })
    const rejected = expect(uploading).rejects.toMatchObject({ name: 'AbortError' })
    await vi.waitFor(() => expect(mocks.chunk).toHaveBeenCalledTimes(1)); controller.abort(); await rejected
    await vi.runAllTimersAsync(); expect(mocks.chunk).toHaveBeenCalledTimes(1); expect(mocks.complete).not.toHaveBeenCalled()
  })
  it('reinitializes an expired reservation once and retains the new ID through a failed attempt', async () => {
    const value = file(21 * MIB + 1, 'restart-unique.bin')
    mocks.init.mockRejectedValueOnce(Object.assign(new Error('expired'), { status: 409 }))
    mocks.chunk.mockRejectedValueOnce(Object.assign(new Error('denied'), { status: 403 }))
    await expect(uploadFileOptimized(value)).rejects.toThrow('denied')
    const original = mocks.init.mock.calls[0]?.[0].upload_id; const fresh = mocks.init.mock.calls[1]?.[0].upload_id
    expect(fresh).not.toBe(original); expect(localStorage.getItem('skycloud-upload-' + original)).toBe(fresh)
    await uploadFileOptimized(value); expect(mocks.init.mock.calls[2]?.[0].upload_id).toBe(fresh)
    expect(localStorage.getItem('skycloud-upload-' + original)).toBeNull()
  })
  it('scopes stable resume IDs to content, owner, workspace and destination', async () => {
    mocks.init.mockResolvedValue({ instant_upload: true })
    const value = file(21 * MIB + 1)
    await uploadFileOptimized(value); await uploadFileOptimized(value)
    mocks.context.workspaceId = 12; await uploadFileOptimized(value)
    mocks.context.userId = 8; await uploadFileOptimized(value)
    await uploadFileOptimized(value, 15)
    const ids = mocks.init.mock.calls.map(call => call[0].upload_id)
    expect(ids[0]).toBe(ids[1]); expect(new Set([ids[0], ...ids.slice(2)]).size).toBe(4)
  })
  it.each([
    { chunk_size: 0 }, { chunk_size: Number.NaN }, { total_chunks: 1 }, { upload_id: '../unsafe' },
    { uploaded_chunks: [-1] }, { uploaded_chunks: [11] }, { uploaded_chunks: [1.5] },
  ])('rejects inconsistent resume metadata before PUT or completion: %j', async invalid => {
    mocks.init.mockResolvedValue({ ...session(), ...invalid })
    await expect(uploadFileOptimized(file(21 * MIB + 1))).rejects.toThrow('无效')
    expect(mocks.chunk).not.toHaveBeenCalled(); expect(mocks.complete).not.toHaveBeenCalled()
  })
})
