import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import SysDictView from '../SysDictView.vue'

const dictApi = vi.hoisted(() => ({
  getSysDicts: vi.fn(),
  createSysDict: vi.fn(),
  updateSysDict: vi.fn(),
  deleteSysDict: vi.fn(),
}))
const messages = vi.hoisted(() => ({
  success: vi.fn(), warning: vi.fn(), error: vi.fn(),
}))
const logger = vi.hoisted(() => ({ error: vi.fn(), warn: vi.fn() }))

vi.mock('@/api/sys_dict', () => dictApi)
vi.mock('@arco-design/web-vue', () => ({ Message: messages }))
vi.mock('@/utils/logger', () => ({ logger }))
vi.mock('@arco-design/web-vue/es/icon', () => ({
  IconRefresh: defineComponent({ template: '<i />' }),
  IconPlus: defineComponent({ template: '<i />' }),
}))

const slot = defineComponent({ template: '<div><slot /></div>' })
const table = defineComponent({
  props: { data: { type: Array, default: () => [] }, loading: { type: Boolean, default: false } },
  template: '<div class="table" />',
})
const modal = defineComponent({
  props: { visible: { type: Boolean, default: false }, confirmLoading: { type: Boolean, default: false } },
  template: '<div class="modal" />',
})

interface SysDictViewHandlers {
  fetchDicts: () => Promise<void>
  handleAdd: () => void
  handleEdit: (record: Record<string, unknown>) => void
  handleDelete: (id: number) => Promise<void>
  handleOk: () => Promise<void>
  form: { id?: number; key: string; value: string; des: string; enable: boolean }
}

const entry = { id: 4, key: 'site_name', value: 'SKYCloud', des: '显示名称', created_at: null, enable: true }

function render() {
  return shallowMount(SysDictView, {
    global: { stubs: { MainLayout: slot, 'a-card': slot, 'a-table': table, 'a-modal': modal, 'a-button': slot } },
  })
}

/** `<script setup>` bindings are not part of the component's public type. */
function handlers(wrapper: ReturnType<typeof render>): SysDictViewHandlers {
  return wrapper.vm as unknown as SysDictViewHandlers
}

beforeEach(() => {
  vi.clearAllMocks()
  dictApi.getSysDicts.mockResolvedValue([entry])
  dictApi.createSysDict.mockResolvedValue(entry)
  dictApi.updateSysDict.mockResolvedValue(entry)
  dictApi.deleteSysDict.mockResolvedValue(undefined)
})

describe('system dictionary view', () => {
  it('loads the dictionary on mount', async () => {
    const wrapper = render()
    await flushPromises()

    expect(dictApi.getSysDicts).toHaveBeenCalledTimes(1)
    expect(wrapper.findComponent(table).props('data')).toEqual([entry])
    expect(wrapper.findComponent(table).props('loading')).toBe(false)
    wrapper.unmount()
  })

  it('rejects an empty key before calling the API', async () => {
    const wrapper = render()
    await flushPromises()
    const view = handlers(wrapper)
    view.handleAdd()
    view.form.key = '   '
    view.form.value = 'x'

    await view.handleOk()

    expect(dictApi.createSysDict).not.toHaveBeenCalled()
    expect(messages.warning).toHaveBeenCalled()
    expect(wrapper.findComponent(modal).props('visible')).toBe(true)
    wrapper.unmount()
  })

  it('creates an entry, closes the dialog and refreshes the list', async () => {
    const wrapper = render()
    await flushPromises()
    const view = handlers(wrapper)
    view.handleAdd()
    view.form.key = 'site_name'
    view.form.value = 'SKYCloud'
    view.form.des = '显示名称'

    await view.handleOk()
    await flushPromises()

    expect(dictApi.createSysDict).toHaveBeenCalledWith({ key: 'site_name', value: 'SKYCloud', des: '显示名称', enable: true })
    expect(messages.success).toHaveBeenCalled()
    expect(wrapper.findComponent(modal).props('visible')).toBe(false)
    expect(dictApi.getSysDicts).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('updates the edited entry by id', async () => {
    const wrapper = render()
    await flushPromises()
    const view = handlers(wrapper)
    view.handleEdit(entry)
    view.form.value = 'SKYCloud v2'

    await view.handleOk()
    await flushPromises()

    expect(dictApi.updateSysDict).toHaveBeenCalledWith(4, { key: 'site_name', value: 'SKYCloud v2', des: '显示名称', enable: true })
    expect(dictApi.createSysDict).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('keeps the dialog open and the list unchanged when the backend refuses the key', async () => {
    const wrapper = render()
    await flushPromises()
    dictApi.createSysDict.mockRejectedValueOnce(new Error('environment variables'))
    const view = handlers(wrapper)
    view.handleAdd()
    view.form.key = 'chat_api_key'
    view.form.value = 'secret'

    await view.handleOk()
    await flushPromises()

    expect(messages.success).not.toHaveBeenCalled()
    expect(logger.error).toHaveBeenCalled()
    expect(wrapper.findComponent(modal).props('visible')).toBe(true)
    expect(dictApi.getSysDicts).toHaveBeenCalledTimes(1)
    expect(wrapper.findComponent(table).props('data')).toEqual([entry])
    wrapper.unmount()
  })

  it('submits once while a save is in flight', async () => {
    const wrapper = render()
    await flushPromises()
    const pending = Promise.withResolvers<unknown>()
    dictApi.createSysDict.mockImplementation(() => pending.promise)
    const view = handlers(wrapper)
    view.handleAdd()
    view.form.key = 'site_name'
    view.form.value = 'SKYCloud'

    const first = view.handleOk()
    await view.handleOk()
    pending.resolve(entry)
    await first
    await flushPromises()

    expect(dictApi.createSysDict).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('removes a deleted entry and keeps the list on failure', async () => {
    const wrapper = render()
    await flushPromises()
    const view = handlers(wrapper)

    await view.handleDelete(4)
    await flushPromises()
    expect(dictApi.deleteSysDict).toHaveBeenCalledWith(4)
    expect(messages.success).toHaveBeenCalled()
    expect(dictApi.getSysDicts).toHaveBeenCalledTimes(2)

    dictApi.deleteSysDict.mockRejectedValueOnce(new Error('forbidden'))
    await view.handleDelete(4)
    await flushPromises()

    expect(logger.error).toHaveBeenCalled()
    expect(dictApi.getSysDicts).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })
})
