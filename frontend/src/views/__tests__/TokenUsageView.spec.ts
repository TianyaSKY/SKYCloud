/**
 * 用户操作层面的用量页测试：真实视图 + 真实 API 函数，仅用 Axios adapter
 * 替换 HTTP 边界，不 mock 自家 API/hook，也不断言内部转调顺序。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, type PropType } from 'vue'
import { AxiosError, type InternalAxiosRequestConfig } from 'axios'
import request from '@/api/request'
import TokenUsageView from '../TokenUsageView.vue'

const messages = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn(), warning: vi.fn() }))
vi.mock('@arco-design/web-vue', () => ({ Message: messages }))
vi.mock('@/router', () => ({
  default: { currentRoute: { value: { name: 'token-usage' } }, push: vi.fn() },
}))
vi.mock('@/utils/logger', () => ({
  logger: { warn: vi.fn(), error: vi.fn(), info: vi.fn(), debug: vi.fn() },
}))
vi.mock('@arco-design/web-vue/es/icon', () => {
  const icon = defineComponent({ template: '<i />' })
  return {
    IconArrowRise: icon,
    IconBarChart: icon,
    IconClockCircle: icon,
    IconDownload: icon,
    IconEmpty: icon,
    IconList: icon,
    IconRefresh: icon,
    IconThunderbolt: icon,
    IconUpload: icon,
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
      type: Object as PropType<LogsPaginationState | false>,
      default: () => ({ current: 1, pageSize: 15, total: 0 }),
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

const stats = {
  user_id: 1,
  total_prompt_tokens: 10,
  total_completion_tokens: 20,
  total_tokens: 30,
  last_active_at: null,
}
const daily = [
  { date: '2026-10-30', prompt_tokens: 1, completion_tokens: 2, total_tokens: 3, request_count: 1 },
  { date: '2026-10-31', prompt_tokens: 4, completion_tokens: 5, total_tokens: 9, request_count: 2 },
]
const row = (id: number) => ({
  id,
  user_id: 1,
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
  return shallowMount(TokenUsageView, {
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

/** Read the pagination state handed to the logs table; the users table passes false. */
function logsPagination(wrapper: VueWrapper): LogsPaginationState {
  const value = wrapper.findComponent(table).props('pagination')
  if (typeof value !== 'object' || value === null) {
    throw new Error('logs table must receive the pagination state')
  }
  return value
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
    if (url === '/token-usage/stats') return ok(config, stats)
    if (url === '/token-usage/daily') return ok(config, daily)
    if (url === '/token-usage/logs') {
      return ok(config, { items: [row(1)], total: 37, page: 1, page_size: 15 })
    }
    throw new Error(`unexpected request ${url}`)
  }
})

describe('personal token usage view', () => {
  it('shows the first log page, its total and the daily trend on mount', async () => {
    const wrapper = render()
    await flushPromises()

    expect(lastParams('/token-usage/logs')).toMatchObject({ page: 1, page_size: 15 })
    expect(wrapper.findComponent(table).props('data')).toEqual([row(1)])
    const pagination = logsPagination(wrapper)
    expect(pagination.current).toBe(1)
    expect(pagination.total).toBe(37)
    expect(wrapper.findAll('.bar-item')).toHaveLength(2)

    wrapper.unmount()
  })

  it('moves to the requested page and returns to page 1 after filter or date changes', async () => {
    const wrapper = render()
    await flushPromises()

    wrapper.findComponent(table).vm.$emit('page-change', 2)
    await flushPromises()
    expect(lastParams('/token-usage/logs')).toMatchObject({ page: 2, page_size: 15 })

    wrapper.findComponent(select).vm.$emit('update:modelValue', 'chat')
    wrapper.findComponent(select).vm.$emit('change')
    await flushPromises()
    expect(lastParams('/token-usage/logs')).toMatchObject({ page: 1, page_size: 15, action: 'chat' })

    wrapper.findComponent(table).vm.$emit('page-change', 3)
    await flushPromises()
    wrapper.findComponent(rangePicker).vm.$emit('change', ['2026-10-01', '2026-10-02'])
    await flushPromises()
    expect(lastParams('/token-usage/logs')).toMatchObject({
      page: 1,
      action: 'chat',
      start_date: '2026-10-01',
      end_date: '2026-10-02',
    })

    wrapper.unmount()
  })

  it('returns to page 1 when the page size changes', async () => {
    const wrapper = render()
    await flushPromises()

    wrapper.findComponent(table).vm.$emit('page-change', 3)
    await flushPromises()
    wrapper.findComponent(table).vm.$emit('page-size-change', 30)
    await flushPromises()

    expect(lastParams('/token-usage/logs')).toMatchObject({ page: 1, page_size: 30 })
    expect(logsPagination(wrapper).current).toBe(1)

    wrapper.unmount()
  })

  it('keeps the previous trend when the daily request fails', async () => {
    const wrapper = render()
    await flushPromises()
    expect(wrapper.findAll('.bar-item')).toHaveLength(2)

    failures.add('/token-usage/daily')
    wrapper.findComponent(radioGroup).vm.$emit('update:modelValue', 7)
    wrapper.findComponent(radioGroup).vm.$emit('change')
    await flushPromises()

    expect(lastParams('/token-usage/daily')).toMatchObject({ days: 7 })
    expect(wrapper.findAll('.bar-item')).toHaveLength(2)
    // 拦截器是唯一提示点：一次失败只弹一条错误
    expect(messages.error).toHaveBeenCalledTimes(1)

    wrapper.unmount()
  })

  it('clears rows and total when the log request fails, keeping the trend', async () => {
    const wrapper = render()
    await flushPromises()
    expect(wrapper.findComponent(table).props('data')).toEqual([row(1)])

    failures.add('/token-usage/logs')
    wrapper.findComponent(rangePicker).vm.$emit('change', ['2026-10-01', '2026-10-02'])
    await flushPromises()

    expect(wrapper.findComponent(table).props('data')).toEqual([])
    expect(logsPagination(wrapper).total).toBe(0)
    expect(wrapper.findAll('.bar-item')).toHaveLength(2)
    expect(messages.error).toHaveBeenCalledTimes(1)

    wrapper.unmount()
  })
})
