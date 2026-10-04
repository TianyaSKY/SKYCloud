import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import MarkdownRenderer from '../MarkdownRenderer.vue'
import { downloadFileBlob } from '../../api/file'
import { downloadAttachment } from '../../utils/fileDownload'

vi.mock('../../api/file', () => ({ downloadFileBlob: vi.fn() }))
vi.mock('../../utils/fileDownload', async (original) => ({
  ...await original<typeof import('../../utils/fileDownload')>(), downloadAttachment: vi.fn(),
}))

const wrappers: VueWrapper[] = []
function render(content: string) {
  const wrapper = mount(MarkdownRenderer, { props: { content }, attachTo: document.body })
  wrappers.push(wrapper)
  return wrapper
}
beforeEach(() => {
  vi.mocked(downloadAttachment).mockResolvedValue(undefined)
  vi.mocked(downloadFileBlob).mockReset()
  vi.mocked(downloadFileBlob).mockResolvedValue(new Blob(['image']))
  URL.createObjectURL = vi.fn(() => 'blob:loaded-image')
  URL.revokeObjectURL = vi.fn()
})
afterEach(() => {
  for (const wrapper of wrappers.splice(0)) { if (wrapper.exists()) wrapper.unmount() }
  document.body.innerHTML = ''
})

describe('protected Markdown references', () => {
  it('fetches Markdown and raw HTML images with the authenticated client, never query tokens', async () => {
    const wrapper = render('![one](/api/file/download/7?token=old-jwt)\n<img src="/api/files/9/download?token=old-jwt" onerror="alert(1)">')
    await flushPromises()
    expect(downloadFileBlob).toHaveBeenCalledWith(7, expect.any(AbortSignal))
    expect(downloadFileBlob).toHaveBeenCalledWith(9, expect.any(AbortSignal))
    expect(wrapper.html()).not.toContain('old-jwt')
    expect(wrapper.html()).not.toContain('onerror')
    expect(wrapper.findAll('img').every((image) => image.attributes('src') === 'blob:loaded-image')).toBe(true)
    wrapper.unmount()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:loaded-image')
  })

  it('does not use authenticated requests for external lookalike URLs', async () => {
    const wrapper = render('![remote](https://evil.example/api/files/7/download)')
    await flushPromises()
    expect(downloadFileBlob).not.toHaveBeenCalled()
    expect(wrapper.find('img').attributes('src')).toBe('https://evil.example/api/files/7/download')
  })

  it('aborts stale image requests and ignores their late response', async () => {
    let resolveOld!: (blob: Blob) => void
    vi.mocked(downloadFileBlob).mockImplementationOnce(() => new Promise<Blob>((resolve) => { resolveOld = resolve }))
    const wrapper = render('![old](/api/files/7/download)')
    const oldSignal = vi.mocked(downloadFileBlob).mock.calls[0]![1]!
    await wrapper.setProps({ content: '![new](/api/files/8/download)' })
    await flushPromises()
    expect(oldSignal.aborted).toBe(true)
    resolveOld(new Blob(['old']))
    await flushPromises()
    expect(URL.createObjectURL).toHaveBeenCalledOnce()
    expect(wrapper.find('img').attributes('src')).toBe('blob:loaded-image')
  })

  it('routes protected links through authenticated downloads and strips stale query tokens', async () => {
    const wrapper = render('[report](/api/files/7/download?token=old-jwt)')
    const link = wrapper.find('a')
    expect(link.attributes('href')).toBe('/api/files/7/download')
    await link.trigger('click')
    expect(downloadAttachment).toHaveBeenCalledWith(7, 'report')
  })
})
