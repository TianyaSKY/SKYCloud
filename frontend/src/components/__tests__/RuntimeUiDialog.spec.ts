import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import RuntimeUiDialog from '../RuntimeUiDialog.vue'
import { runtimeUiLaunchUrl, startRuntimeUiRenewal } from '../../utils/runtimeUi'
vi.mock('../../utils/runtimeUi', () => ({ runtimeUiLaunchUrl: vi.fn(), startRuntimeUiRenewal: vi.fn() }))
vi.mock('@arco-design/web-vue', () => ({ Message: { warning: vi.fn() } }))
const issued = vi.mocked(runtimeUiLaunchUrl)
const renewed = vi.mocked(startRuntimeUiRenewal)
const workspace = { id: 7, name: 'Isolated workspace' } as never
const url = 'http://r42.skycloud.localhost:8081/__skycloud/launch?ticket=' + 'a'.repeat(43)
const stop = vi.fn()
const modal = defineComponent({ props: ['visible'], template: '<div v-if="visible"><slot /></div>' })
function open() { return mount(RuntimeUiDialog, { props: { modelValue: true, workspace },
  global: { stubs: { 'a-modal': modal, 'a-spin': true } } }) }
beforeEach(() => { vi.resetAllMocks(); issued.mockResolvedValue(url); renewed.mockReturnValue(stop) })

describe('actual runtime dialog lifecycle', () => {
  it('redeems a ticket, isolates the frame and cleans renewal when closing', async () => {
    const wrapper = open(); await flushPromises()
    expect(issued).toHaveBeenCalledWith(7)
    const frame = wrapper.find('iframe')
    expect(frame.attributes('src')).toBe(url)
    expect(frame.attributes('sandbox')).toBe('allow-scripts allow-same-origin allow-downloads allow-forms')
    expect(frame.attributes('referrerpolicy')).toBe('no-referrer')
    await wrapper.setProps({ modelValue: false })
    expect(wrapper.find('iframe').exists()).toBe(false)
    expect(stop).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
  it('cannot reopen a closed dialog from a late ticket response', async () => {
    let complete!: (value: string) => void
    issued.mockImplementation(() => new Promise(resolve => { complete = resolve }))
    const wrapper = open()
    await wrapper.setProps({ modelValue: false })
    complete(url); await flushPromises()
    expect(wrapper.find('iframe').exists()).toBe(false)
    expect(renewed).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('removes the frame and closes on renewal rejection', async () => {
    let fail!: () => void
    renewed.mockImplementation((_id, _url, rejected) => { fail = rejected; return stop })
    const wrapper = open(); await flushPromises(); fail(); await flushPromises()
    expect(wrapper.find('iframe').exists()).toBe(false)
    expect(wrapper.emitted('update:modelValue')).toEqual([[false]])
    expect(stop).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
})
