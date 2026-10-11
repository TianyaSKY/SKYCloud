import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import MySharesView from '../MySharesView.vue'

const shareApi = vi.hoisted(() => ({ getMyShares: vi.fn(), cancelShare: vi.fn() }))
const messages = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }))
const logger = vi.hoisted(() => ({ warn: vi.fn() }))

vi.mock('@/api/share', () => shareApi)
vi.mock('@arco-design/web-vue', () => ({ Message: messages }))
vi.mock('@/utils/logger', () => ({ logger }))
vi.mock('@arco-design/web-vue/es/icon', () => ({
  IconFile: defineComponent({ template: '<i />' }),
  IconRefresh: defineComponent({ template: '<i />' }),
}))

const slot = defineComponent({ template: '<div><slot /></div>' })
const table = defineComponent({
  props: { data: { type: Array, default: () => [] }, loading: { type: Boolean, default: false } },
  template: '<div class="table" />',
})

const share = {
  id: 3, token: 'tok-3', file_id: 7, link: '/s/tok-3',
  expires_at: null, created_at: '2026-10-11T00:00:00Z', file_name: 'report.txt',
}
type CancelHandler = { handleCancelShare: (record: typeof share) => Promise<void> }

function render() {
  return shallowMount(MySharesView, {
    global: { stubs: { MainLayout: slot, 'a-card': slot, 'a-table': table, 'a-button': slot } },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  shareApi.getMyShares.mockResolvedValue([share])
  shareApi.cancelShare.mockResolvedValue(undefined)
})

describe('my shares view', () => {
  it('loads the caller shares on mount and hands them to the table', async () => {
    const wrapper = render()
    await flushPromises()

    expect(shareApi.getMyShares).toHaveBeenCalledTimes(1)
    expect(wrapper.findComponent(table).props('data')).toEqual([share])
    expect(wrapper.findComponent(table).props('loading')).toBe(false)
    wrapper.unmount()
  })

  it('keeps the current list and stops loading when the list request fails', async () => {
    shareApi.getMyShares.mockRejectedValueOnce(new Error('network down'))
    const wrapper = render()
    await flushPromises()

    expect(wrapper.findComponent(table).props('data')).toEqual([])
    expect(wrapper.findComponent(table).props('loading')).toBe(false)
    expect(logger.warn).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('cancels a share and refreshes the list', async () => {
    const wrapper = render()
    await flushPromises()
    shareApi.getMyShares.mockResolvedValue([])

    // `<script setup>` bindings are not part of the component's public type, so the
    // test seam reads the cancel handler once through a documented assertion.
    const view = wrapper.vm as unknown as CancelHandler
    await view.handleCancelShare(share)
    await flushPromises()

    expect(shareApi.cancelShare).toHaveBeenCalledWith(3)
    expect(messages.success).toHaveBeenCalled()
    expect(shareApi.getMyShares).toHaveBeenCalledTimes(2)
    expect(wrapper.findComponent(table).props('data')).toEqual([])
    wrapper.unmount()
  })

  it('keeps the list intact when cancelling fails', async () => {
    const wrapper = render()
    await flushPromises()
    shareApi.cancelShare.mockRejectedValueOnce(new Error('forbidden'))

    const view = wrapper.vm as unknown as CancelHandler
    await view.handleCancelShare(share)
    await flushPromises()

    expect(messages.success).not.toHaveBeenCalled()
    expect(logger.warn).toHaveBeenCalled()
    expect(wrapper.findComponent(table).props('data')).toEqual([share])
    wrapper.unmount()
  })
})
