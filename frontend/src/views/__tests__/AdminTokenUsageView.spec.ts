/**
 * 管理员用量页的用户操作测试：真实视图 + 真实 API 函数，仅用 Axios adapter
 * 替换 HTTP 边界。断言集中在分页参数、筛选回第一页和失败降级。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, type PropType } from 'vue'
import { AxiosError, type InternalAxiosRequestConfig } from 'axios'
import request from '@/api/request'
import AdminTokenUsageView from '../AdminTokenUsageView.vue'

const messages = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn(), warning: vi.fn() }))
vi.mock('@arco-design/web-vue', () => ({ Message: messages }))
vi.mock('@/router', () => ({
  default: { currentRoute: { value: { name: 'admin-token-usage' } }, push: vi.fn() },
}))
vi.mock('@/utils/logger', () => ({
  logger: { warn: vi.fn(), error: vi.fn(), info: vi.fn(), debug: vi.fn() },
}))
vi.mock('@arco-design/web-vue/es/icon', () => {
  const icon = defineComponent({ template: '<i />' })
  return {
    IconArrowRise: icon,
    IconDownload: icon,
    IconEmpty: icon,
    IconList: icon,
    IconRefresh: icon,
    IconThunderbolt: icon,
    IconUpload: icon,
    IconUserGroup: icon,
  }
})

const slot = defineComponent({ template: '<div><slot /></div>' })

interface LogsPaginationState {
  current: number
  pageSize: number
  total: number
}

const table = defineComponent({
  props: {
    data: { type: Array, default: () => [] },
    loading: { type: Boolean, default: false },
    pagination: {
      type: [Object, Boolean] as PropType<LogsPaginationState | false>,
      default: () => false,
    },
  },
  emits: ['page-change', 'page-size-change'],
  template: '<div class="table" />',
})
const select = defineComponent({
  props: { modelValue: { type: [String, Number], default: undefined } },
  emits: ['update:modelValue', 'change'],
  template: '<div class="select" />',
})
const rangePicker = defineComponent({
  emits: ['change'],
  template: '<div class="range" />',
})
const radioGroup = defineComponent({
  props: { modelValue: { type: Number, default: undefined } },
  emits: ['update:modelValue', 'change'],
  template: '<div class="radio-group" />',
})

const users = [
  {
    user_id: 5,
    username: 'alice',
    role: 'common',
    total_prompt_tokens: 1,
    total_completion_tokens: 2,
    total_tokens: 3,
    last_active_at: null,
  },
]
const daily = [
  { date: '2026-10-30', prompt_tokens: 1, completion_tokens: 2, total_tokens: 3, request_count: 1 },
  { date: '2026-10-31', prompt_tokens: 4, completion_tokens: 5, total_tokens: 9, request_count: 2 },
]
const row = (id: number) => ({
  id,
  user_id: 5,
  username: 'alice',
  action: 'chat',
  model_name: 'model',
  prompt_tokens: 1,
  completion_tokens: 2,
  total_tokens: 3,
  query_summary: null,
  extra_info: null,
  created_at: '2026-10-30T00:00:00Z',
})

interface Recorded {
  url: string
  params: Record<string, unknown>
}

let requests: Recorded[] = []
let failures = new Set<string>()

function ok(config: InternalAxiosRequestConfig, data: unknown) {
  return {
    status: 200,
    statusText: 'OK',
    headers: {},
    config,
    data: { success: true, data },
  }
}

function render() {
  return shallowMount(AdminTokenUsageView, {
    global: {
      stubs: {
        MainLayout: slot,
        'a-table': table,
        'a-select': select,
        'a-range-picker': rangePicker,
        'a-radio-group': radioGroup,
        'a-spin': slot,
        'a-tooltip': slot,
        'a-button': slot,
        'a-radio': slot,
        'a-empty': slot,
      },
    },
  })
}

function lastParams(url: string): Record<string, unknown> {
  const matches = requests.filter((entry) => entry.url === url)
  return matches[matches.length - 1]?.params ?? {}
}

/** The users table receives `pagination=false`; the log table receives the state. */
function logsTable(wrapper: VueWrapper): VueWrapper {
  const found = wrapper.findAllComponents(table).find((candidate) => {
    const value = candidate.props('pagination')
    return typeof value === 'object' && value !== null
  })
  if (!found) throw new Error('logs table must receive the pagination state')
  return found
}

function logsPagination(wrapper: VueWrapper): LogsPaginationState {
  const value = logsTable(wrapper).props('pagination')
  if (typeof value !== 'object' || value === null) {
    throw new Error('logs table must receive the pagination state')
  }
  return value
}

/** The user filter select comes before the action filter select. */
function selectAt(wrapper: VueWrapper, index: number): VueWrapper {
  const found = wrapper.findAllComponents(select)[index]
  if (!found) throw new Error(`missing select ${index}`)
  return found
}

beforeEach(() => {
  setActivePinia(createPinia())
  requests = []
  failures = new Set()
  request.defaults.adapter = async (config) => {
    const url = config.url ?? ''
    requests.push({ url, params: (config.params ?? {}) as Record<string, unknown> })
    if (failures.has(url)) {
      throw new AxiosError('upstream down', 'ERR_BAD_RESPONSE', config, undefined, {
        status: 502,
        statusText: 'Bad Gateway',
        headers: {},
        config,
        data: { detail: 'upstream down' },
      })
    }
    if (url === '/admin/token-usage/users') return ok(config, users)
    if (url === '/admin/token-usage/daily') return ok(config, daily)
    if (url === '/admin/token-usage/logs') {
      return ok(config, { items: [row(1)], total: 52, page: 1, page_size: 15 })
    }
    throw new Error(`unexpected request ${url}`)
  }
})

describe('admin token usage view', () => {
  it('shows the first log page, its total and the daily trend on mount', async () => {
    const wrapper = render()
    await flushPromises()

    expect(lastParams('/admin/token-usage/logs')).toMatchObject({ page: 1, page_size: 15 })
    expect(logsTable(wrapper).props('data')).toEqual([row(1)])
    const pagination = logsPagination(wrapper)
    expect(pagination.current).toBe(1)
    expect(pagination.total).toBe(52)
    expect(wrapper.findAll('.bar-item')).toHaveLength(2)

    wrapper.unmount()
  })

  it('filters by user and action from page 1 after paging', async () => {
    const wrapper = render()
    await flushPromises()

    logsTable(wrapper).vm.$emit('page-change', 2)
    await flushPromises()
    expect(lastParams('/admin/token-usage/logs')).toMatchObject({ page: 2, page_size: 15 })

    selectAt(wrapper, 0).vm.$emit('update:modelValue', 5)
    selectAt(wrapper, 0).vm.$emit('change')
    await flushPromises()
    expect(lastParams('/admin/token-usage/logs')).toMatchObject({ page: 1, user_id: 5 })

    logsTable(wrapper).vm.$emit('page-change', 2)
    await flushPromises()
    selectAt(wrapper, 1).vm.$emit('update:modelValue', 'embedding')
    selectAt(wrapper, 1).vm.$emit('change')
    await flushPromises()
    expect(lastParams('/admin/token-usage/logs')).toMatchObject({
      page: 1,
      page_size: 15,
      user_id: 5,
      action: 'embedding',
    })

    logsTable(wrapper).vm.$emit('page-change', 3)
    await flushPromises()
    wrapper.findComponent(rangePicker).vm.$emit('change', ['2026-10-01', '2026-10-02'])
    await flushPromises()
    expect(lastParams('/admin/token-usage/logs')).toMatchObject({
      page: 1,
      user_id: 5,
      action: 'embedding',
      start_date: '2026-10-01',
      end_date: '2026-10-02',
    })

    wrapper.unmount()
  })

  it('returns to page 1 when the page size changes', async () => {
    const wrapper = render()
    await flushPromises()

    logsTable(wrapper).vm.$emit('page-change', 3)
    await flushPromises()
    logsTable(wrapper).vm.$emit('page-size-change', 50)
    await flushPromises()

    expect(lastParams('/admin/token-usage/logs')).toMatchObject({ page: 1, page_size: 50 })
    expect(logsPagination(wrapper).current).toBe(1)

    wrapper.unmount()
  })

  it('keeps the previous trend when the daily request fails', async () => {
    const wrapper = render()
    await flushPromises()
    expect(wrapper.findAll('.bar-item')).toHaveLength(2)

    failures.add('/admin/token-usage/daily')
    wrapper.findComponent(radioGroup).vm.$emit('update:modelValue', 90)
    wrapper.findComponent(radioGroup).vm.$emit('change')
    await flushPromises()

    expect(lastParams('/admin/token-usage/daily')).toMatchObject({ days: 90 })
    expect(wrapper.findAll('.bar-item')).toHaveLength(2)
    expect(messages.error).toHaveBeenCalledTimes(1)

    wrapper.unmount()
  })

  it('clears rows and total when the log request fails, keeping the trend', async () => {
    const wrapper = render()
    await flushPromises()
    expect(logsTable(wrapper).props('data')).toEqual([row(1)])

    failures.add('/admin/token-usage/logs')
    wrapper.findComponent(rangePicker).vm.$emit('change', ['2026-10-01', '2026-10-02'])
    await flushPromises()

    expect(logsTable(wrapper).props('data')).toEqual([])
    expect(logsPagination(wrapper).total).toBe(0)
    expect(wrapper.findAll('.bar-item')).toHaveLength(2)
    expect(messages.error).toHaveBeenCalledTimes(1)

    wrapper.unmount()
  })
})
