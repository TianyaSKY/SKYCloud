import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import PdfPreview from '../PdfPreview.vue'
import { getDocument } from 'pdfjs-dist'
vi.mock('pdfjs-dist', () => ({ getDocument: vi.fn(), GlobalWorkerOptions: {} }))
const destroy = vi.fn().mockResolvedValue(undefined)
const rendered = vi.fn(() => ({ promise: Promise.resolve(), cancel: vi.fn() }))
const getPage = vi.fn()
const pdfPage = { getViewport: () => ({ width: 400, height: 300 }), render: rendered }
beforeEach(() => {
  vi.clearAllMocks(); getPage.mockResolvedValue(pdfPage)
  vi.mocked(getDocument).mockReturnValue({ promise: Promise.resolve({ numPages: 2, getPage }), destroy } as never)
})
describe('local PDF rendering lifecycle', () => {
  it('uses same-origin resources and renders pages on demand', async () => {
    const wrapper = mount(PdfPreview, { props: { src: 'blob:private-document' } }); await flushPromises()
    expect(getDocument).toHaveBeenCalledWith(expect.objectContaining({ url: 'blob:private-document',
      cMapUrl: '/pdfjs/cmaps/', standardFontDataUrl: '/pdfjs/standard_fonts/',
      wasmUrl: '/pdfjs/wasm/', iccUrl: '/pdfjs/iccs/' }))
    expect(wrapper.emitted('rendered')).toHaveLength(1)
    const next = wrapper.findAll('button').find(button => button.text() === '下一页')!
    await next.trigger('click'); await flushPromises()
    expect(getPage).toHaveBeenLastCalledWith(2)
    expect(wrapper.text()).toContain('2 / 2')
    wrapper.unmount(); expect(destroy).toHaveBeenCalledOnce()
  })
  it('destroys a pending load on close and ignores its late completion', async () => {
    let finish!: (value: unknown) => void
    vi.mocked(getDocument).mockReturnValue({ promise: new Promise(resolve => { finish = resolve }), destroy } as never)
    const wrapper = mount(PdfPreview, { props: { src: 'blob:private-document' } })
    wrapper.unmount(); finish({ numPages: 2, getPage }); await flushPromises()
    expect(destroy).toHaveBeenCalledOnce()
    expect(getPage).not.toHaveBeenCalled()
  })
  it('rejects excessive canvas dimensions before allocating or rendering', async () => {
    getPage.mockResolvedValue({ getViewport: () => ({ width: 100000, height: 100000 }), render: rendered })
    const wrapper = mount(PdfPreview, { props: { src: 'blob:large-document' } }); await flushPromises()
    expect(wrapper.emitted('error')?.[0]?.[0]).toEqual(new Error('PDF page is too large'))
    expect(rendered).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
