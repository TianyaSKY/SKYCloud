import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import InboxView from '../InboxView.vue'
import type { Message as InboxMessage } from '@/api/inbox'

const inboxApi = vi.hoisted(() => ({
  getInboxMessages: vi.fn(),
  markAsRead: vi.fn(),
  markAllAsRead: vi.fn(),
  deleteMessage: vi.fn(),
}))
const messages = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }))
const logger = vi.hoisted(() => ({ warn: vi.fn(), error: vi.fn() }))

vi.mock('@/api/inbox', () => inboxApi)
vi.mock('@arco-design/web-vue', () => ({ Message: messages }))
vi.mock('@/utils/logger', () => ({ logger }))
vi.mock('@arco-design/web-vue/es/icon', () => ({
  IconCheck: defineComponent({ template: '<i />' }),
  IconNotification: defineComponent({ template: '<i />' }),
}))

const slot = defineComponent({ template: '<div><slot /></div>' })

interface InboxViewState {
  // The instance proxy unwraps refs, so these read as plain values.
  loading: boolean
  messages: InboxMessage[]
  total: number
  modalVisible: boolean
  currentMessage: InboxMessage | null
  showDetail: (item: InboxMessage) => void
  handleMarkRead: (id: number) => Promise<void>
  handleMarkAllRead: () => Promise<void>
  handleDelete: (id: number) => Promise<void>
  handlePageChange: (page: number) => Promise<void>
}

const unread = { id: 1, title: '任务完成', content: '整理已完成', is_read: false, created_at: '2026-10-11T00:00:00Z' } as InboxMessage
const read = { id: 2, title: '欢迎', content: '欢迎使用', is_read: true, created_at: '2026-10-10T00:00:00Z' } as InboxMessage

function render() {
  return shallowMount(InboxView, {
    global: {
      stubs: {
        MainLayout: slot, 'a-list': slot, 'a-list-item': slot, 'a-list-item-meta': slot,
        'a-link': slot, 'a-badge': slot, 'a-avatar': slot, 'a-popconfirm': slot,
        'a-button': slot, 'a-empty': slot, 'a-modal': slot, 'a-pagination': slot,
      },
    },
  })
}

/** `<script setup>` bindings are not part of the component's public type. */
function state(wrapper: ReturnType<typeof render>): InboxViewState {
  return wrapper.vm as unknown as InboxViewState
}

beforeEach(() => {
  vi.clearAllMocks()
  inboxApi.getInboxMessages.mockResolvedValue({ items: [unread, read], total: 12 })
  inboxApi.markAsRead.mockResolvedValue(undefined)
  inboxApi.markAllAsRead.mockResolvedValue(undefined)
  inboxApi.deleteMessage.mockResolvedValue(undefined)
})

describe('inbox view', () => {
  it('loads the first page on mount', async () => {
    const wrapper = render()
    await flushPromises()

    expect(inboxApi.getInboxMessages).toHaveBeenCalledWith(1, 10)
    const view = state(wrapper)
    expect(view.messages).toEqual([unread, read])
    expect(view.total).toBe(12)
    expect(view.loading).toBe(false)
    wrapper.unmount()
  })

  it('keeps the list empty and stops loading when the request fails', async () => {
    inboxApi.getInboxMessages.mockRejectedValueOnce(new Error('offline'))
    const wrapper = render()
    await flushPromises()

    const view = state(wrapper)
    expect(view.messages).toEqual([])
    expect(view.loading).toBe(false)
    expect(logger.warn).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('marks an unread message as read when its detail is opened', async () => {
    const wrapper = render()
    await flushPromises()
    const view = state(wrapper)

    view.showDetail(unread)
    await flushPromises()

    expect(inboxApi.markAsRead).toHaveBeenCalledWith(1)
    expect(inboxApi.getInboxMessages).toHaveBeenCalledTimes(2)
    expect(view.modalVisible).toBe(true)
    expect(view.currentMessage).toEqual(unread)
    wrapper.unmount()
  })

  it('does not re-mark an already read message', async () => {
    const wrapper = render()
    await flushPromises()
    const view = state(wrapper)

    view.showDetail(read)
    await flushPromises()

    expect(inboxApi.markAsRead).not.toHaveBeenCalled()
    expect(inboxApi.getInboxMessages).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('marks every message read and refreshes', async () => {
    const wrapper = render()
    await flushPromises()

    await state(wrapper).handleMarkAllRead()
    await flushPromises()

    expect(inboxApi.markAllAsRead).toHaveBeenCalledTimes(1)
    expect(messages.success).toHaveBeenCalled()
    expect(inboxApi.getInboxMessages).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('deletes a message and keeps the list when deletion fails', async () => {
    const wrapper = render()
    await flushPromises()
    const view = state(wrapper)

    await view.handleDelete(1)
    await flushPromises()
    expect(inboxApi.deleteMessage).toHaveBeenCalledWith(1)
    expect(inboxApi.getInboxMessages).toHaveBeenCalledTimes(2)

    inboxApi.deleteMessage.mockRejectedValueOnce(new Error('forbidden'))
    await view.handleDelete(2)
    await flushPromises()

    expect(logger.warn).toHaveBeenCalled()
    expect(inboxApi.getInboxMessages).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('loads the requested page with the configured size', async () => {
    const wrapper = render()
    await flushPromises()

    await state(wrapper).handlePageChange(2)
    await flushPromises()

    expect(inboxApi.getInboxMessages).toHaveBeenLastCalledWith(2, 10)
    wrapper.unmount()
  })
})
