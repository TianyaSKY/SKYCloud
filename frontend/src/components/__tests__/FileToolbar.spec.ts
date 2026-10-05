import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import FileToolbar from '../FileToolbar.vue'
const stubs = { 'a-button': { template: '<button><slot /></button>' }, 'a-space': { template: '<div><slot /></div>' }, 'a-button-group': { template: '<div><slot /></div>' }, 'a-doption': true, 'a-dropdown': { template: '<div><slot /></div>' } }
describe('file toolbar upload and workspace role', () => {
  it('opens its own file input and forwards the actual selected files, permitting re-selection', async () => {
    const upload = vi.fn(); const wrapper = mount(FileToolbar, { props: { handleBatchUpload: upload, canWrite: true }, global: { stubs } })
    const input = wrapper.find('input[type="file"]'); const chooser = vi.spyOn(input.element as HTMLInputElement, 'click').mockImplementation(() => {})
    const button = wrapper.findAll('button').find(item => item.text() === '上传')!
    await button.trigger('click'); expect(chooser).toHaveBeenCalledTimes(1)
    const files = [new File(['a'], 'a.txt'), new File(['b'], 'b.txt')]
    Object.defineProperty(input.element, 'files', { configurable: true, value: files })
    await input.trigger('change'); await vi.waitFor(() => expect(upload).toHaveBeenCalledWith(files))
    expect((input.element as HTMLInputElement).value).toBe('')
    await button.trigger('click'); await input.trigger('change'); await vi.waitFor(() => expect(upload).toHaveBeenCalledTimes(2))
    wrapper.unmount()
  })
  it('discards picker selections and deferred uploads after navigation or permission removal', async () => {
    const upload = vi.fn()
    const wrapper = mount(FileToolbar, { props: { handleBatchUpload: upload, uploadContextKey: 'space-1/folder-1' }, global: { stubs } })
    const input = wrapper.find('input[type="file"]')
    vi.spyOn(input.element as HTMLInputElement, 'click').mockImplementation(() => {})
    Object.defineProperty(input.element, 'files', { configurable: true, value: [new File(['a'], 'a.txt')] })
    const button = wrapper.findAll('button').find(item => item.text() === '上传')!
    await button.trigger('click')
    await wrapper.setProps({ uploadContextKey: 'space-1/folder-2' })
    await wrapper.setProps({ uploadContextKey: 'space-1/folder-1' })
    await input.trigger('change')
    await new Promise(resolve => setTimeout(resolve, 30))
    expect(upload).not.toHaveBeenCalled()
    await button.trigger('click'); await input.trigger('change')
    await wrapper.setProps({ canWrite: false })
    await new Promise(resolve => setTimeout(resolve, 30))
    expect(upload).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('ignores a dismissed empty picker without starting an upload', async () => {
    const upload = vi.fn(); const wrapper = mount(FileToolbar, { props: { handleBatchUpload: upload }, global: { stubs } })
    await wrapper.find('input[type="file"]').trigger('change'); expect(upload).not.toHaveBeenCalled(); wrapper.unmount()
  })
  it('removes write controls immediately for a viewer while keeping refresh', async () => {
    const wrapper = mount(FileToolbar, { props: { handleBatchUpload: vi.fn(), canWrite: true }, global: { stubs } })
    await wrapper.setProps({ canWrite: false }); expect(wrapper.find('input[type="file"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('上传'); expect(wrapper.text()).not.toContain('新建文件夹'); expect(wrapper.text()).not.toContain('更多')
    await wrapper.findAll('button').find(item => item.text() === '刷新')!.trigger('click')
    expect(wrapper.emitted('refresh')).toHaveLength(1); wrapper.unmount()
  })
})
