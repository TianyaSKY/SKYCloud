import { effectScope } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useFilePreview } from '../useFilePreview'
import { downloadFileBlob } from '@/api/file'
import { readBlobAsText } from '@/utils/blob'
vi.mock('@/api/file', () => ({ downloadFileBlob: vi.fn() }))
vi.mock('@/utils/blob', () => ({ readBlobAsText: vi.fn(), safeRevokeObjectURL: (url: string) => {
  if (url) URL.revokeObjectURL(url)
} }))
const file = (id: number, name = 'file.pdf') => ({ id, name, is_folder: false })
function deferred<T>() { let resolve!: (value: T) => void; let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
function setup() { const scope = effectScope(); const preview = scope.run(useFilePreview)!; return { scope, preview } }
beforeEach(() => {
  vi.resetAllMocks()
  URL.createObjectURL = vi.fn().mockReturnValueOnce('blob:first').mockReturnValueOnce('blob:second')
  URL.revokeObjectURL = vi.fn()
})
describe('preview request ownership', () => {
  it('keeps the newest selection when an older download finishes later', async () => {
    const old = deferred<Blob>(); const next = deferred<Blob>()
    vi.mocked(downloadFileBlob).mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise)
    const { scope, preview } = setup()
    const first = preview.handleFileClick(file(1)); const second = preview.handleFileClick(file(2, 'new.png'))
    expect(vi.mocked(downloadFileBlob).mock.calls[0]![1]?.aborted).toBe(true)
    next.resolve(new Blob()); await second; old.resolve(new Blob()); await first
    expect(preview.previewTitle.value).toBe('new.png')
    expect(preview.previewType.value).toBe('image')
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
    scope.stop(); expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:first')
  })
  it('cannot reopen a closed preview from a late download', async () => {
    const download = deferred<Blob>(); vi.mocked(downloadFileBlob).mockReturnValue(download.promise)
    const { scope, preview } = setup(); const opening = preview.handleFileClick(file(1))
    preview.handlePreviewClose(); download.resolve(new Blob()); await opening
    expect(preview.previewVisible.value).toBe(false)
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    scope.stop()
  })
  it('ignores old text decoding and failures without clearing the current preview', async () => {
    const text = deferred<string>()
    vi.mocked(downloadFileBlob).mockResolvedValue(new Blob())
    vi.mocked(readBlobAsText).mockReturnValue(text.promise)
    const { scope, preview } = setup(); const old = preview.handleFileClick(file(1, 'old.md'))
    await Promise.resolve()
    const next = preview.handleFileClick(file(2, 'new.png')); await next
    text.reject(new Error('old read failed')); await old
    expect(preview.previewUrl.value).toBe('blob:second')
    expect(preview.previewVisible.value).toBe(true)
    expect(preview.textContent.value).toBe('')
    expect(URL.revokeObjectURL).not.toHaveBeenCalledWith('blob:second')
    scope.stop()
  })
  it('disposes a pending read when the owning view unmounts', async () => {
    const text = deferred<string>(); vi.mocked(downloadFileBlob).mockResolvedValue(new Blob())
    vi.mocked(readBlobAsText).mockReturnValue(text.promise)
    const { scope, preview } = setup(); const opening = preview.handleFileClick(file(1, 'old.md'))
    await Promise.resolve(); scope.stop(); text.resolve('stale'); await opening
    expect(preview.textContent.value).toBe('')
    expect(preview.previewVisible.value).toBe(false)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:first')
  })
})
