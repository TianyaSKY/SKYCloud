import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { downloadFileBlob } from '../../api/file'
import { downloadAttachment, protectedFileId } from '../fileDownload'

vi.mock('../../api/file', () => ({ downloadFileBlob: vi.fn() }))

beforeEach(() => {
  URL.createObjectURL = vi.fn(() => 'blob:protected-file')
  URL.revokeObjectURL = vi.fn()
})
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

describe('protected downloads', () => {
  it('normalizes local historical paths and never accepts another origin', () => {
    const origin = 'https://cloud.example'
    expect(protectedFileId('/api/files/7/download?token=old', origin)).toBe(7)
    expect(protectedFileId('/api/file/download/8', origin)).toBe(8)
    expect(protectedFileId('https://evil.example/api/files/7/download', origin)).toBeNull()
    expect(protectedFileId('//evil.example/api/file/download/8', origin)).toBeNull()
    expect(protectedFileId('/other/api/files/7/download', origin)).toBeNull()
    expect(protectedFileId('javascript:alert(1)', origin)).toBeNull()
  })

  it('downloads authenticated bytes with a local object URL and releases it', async () => {
    vi.useFakeTimers()
    const blob = new Blob(['bytes'])
    vi.mocked(downloadFileBlob).mockResolvedValue(blob)
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      expect(this.href).toBe('blob:protected-file')
      expect(this.download).toBe('report.txt')
    })
    await downloadAttachment(7, 'report.txt')
    expect(downloadFileBlob).toHaveBeenCalledWith(7)
    expect(URL.createObjectURL).toHaveBeenCalledWith(blob)
    expect(click).toHaveBeenCalledOnce()
    expect(document.querySelector('a')).toBeNull()
    await vi.runAllTimersAsync()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:protected-file')
  })
})
