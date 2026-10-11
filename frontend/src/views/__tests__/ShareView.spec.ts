import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import axios, { type AxiosResponse } from 'axios'
import ShareView from '../ShareView.vue'

const messages = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }))
const revoke = vi.hoisted(() => vi.fn())

vi.mock('vue-router', () => ({ useRoute: () => ({ params: { token: 'tok-123' } }) }))
vi.mock('axios', () => ({ default: { request: vi.fn() } }))
vi.mock('@arco-design/web-vue', () => ({ Message: messages }))
vi.mock('@/utils/blob', () => ({ safeRevokeObjectURL: revoke }))
vi.mock('@arco-design/web-vue/es/icon', () => ({
  IconDownload: defineComponent({ template: '<i />' }),
  IconFile: defineComponent({ template: '<i />' }),
}))

const button = defineComponent({ template: '<button><slot name="icon" /><slot /></button>' })

const clicked: Array<{ href: string; download: string }> = []

beforeEach(() => {
  vi.clearAllMocks()
  clicked.length = 0
  Object.defineProperty(globalThis.URL, 'createObjectURL', { value: vi.fn(() => 'blob:fixture'), configurable: true })
  Object.defineProperty(globalThis.URL, 'revokeObjectURL', { value: vi.fn(), configurable: true })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    clicked.push({ href: this.href, download: this.download })
  })
})

afterEach(() => vi.restoreAllMocks())

function render() {
  return mount(ShareView, { global: { stubs: { 'a-button': button } } })
}

describe('public share download', () => {
  it('downloads without credentials and honours the declared file name', async () => {
    const response = {
      data: new Blob(['fixture-bytes']), status: 200, statusText: 'OK',
      headers: { 'content-disposition': 'attachment; filename="%E6%8A%A5%E5%91%8A.txt"' },
      config: { headers: {} },
    }
    // Library response type is structural here; the named const keeps every read checked.
    const requestResult = response as unknown as AxiosResponse<Blob>
    vi.mocked(axios.request).mockResolvedValue(requestResult)
    const wrapper = render()

    await wrapper.find('button').trigger('click')
    await flushPromises()

    expect(vi.mocked(axios.request)).toHaveBeenCalledTimes(1)
    const call = vi.mocked(axios.request).mock.calls[0][0]
    expect(call).toMatchObject({ url: '/api/share/tok-123', method: 'GET', responseType: 'blob' })
    expect(JSON.stringify(call)).not.toMatch(/authorization/i)
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
    expect(clicked).toHaveLength(1)
    expect(clicked[0].download).toBe('报告.txt')
    expect(clicked[0].href).toBe('blob:fixture')
    expect(revoke).toHaveBeenCalledWith('blob:fixture')
    expect(messages.success).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('falls back to the default name when the header is not decodable', async () => {
    const response = {
      data: new Blob(['x']), status: 200, statusText: 'OK',
      headers: { 'content-disposition': 'attachment; filename="%E6%8A%A5%E5%91%8A.txt"' },
      config: { headers: {} },
    }
    const requestResult = response as unknown as AxiosResponse<Blob>
    vi.mocked(axios.request).mockResolvedValue({ ...requestResult, headers: { 'content-disposition': 'attachment; filename=100%' } })
    const wrapper = render()

    await wrapper.find('button').trigger('click')
    await flushPromises()

    expect(clicked[0].download).toBe('100%')
    wrapper.unmount()
  })

  it('reports a failed download without creating an object URL', async () => {
    vi.mocked(axios.request).mockRejectedValue(new Error('share revoked'))
    const wrapper = render()

    await wrapper.find('button').trigger('click')
    await flushPromises()

    expect(URL.createObjectURL).not.toHaveBeenCalled()
    expect(clicked).toHaveLength(0)
    expect(revoke).not.toHaveBeenCalled()
    expect(messages.error).toHaveBeenCalled()
    expect(messages.success).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
