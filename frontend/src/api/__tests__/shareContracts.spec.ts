import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { AxiosAdapter } from 'axios'
import request from '../request'
import { cancelShare, createShare, getMyShares } from '../share'

vi.mock('../../router', () => ({ default: { currentRoute: { value: { name: 'home' } }, push: vi.fn() } }))
vi.mock('@arco-design/web-vue', () => ({ Message: { error: vi.fn(), success: vi.fn() } }))

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
})

function envelope(data: unknown) {
  return { success: true, data }
}

describe('share API contract', () => {
  it('creates a share for the file id and returns the unwrapped record', async () => {
    const seen: Array<{ method?: string; url?: string; body?: unknown }> = []
    const adapter: AxiosAdapter = async (config) => {
      seen.push({ method: config.method, url: config.url, body: config.data })
      return {
        data: envelope({ id: 5, token: 'tok', file_id: 7, link: '/s/tok', expires_at: null, created_at: null, file_name: 'report.txt' }),
        config, headers: {}, status: 201, statusText: 'Created',
      }
    }
    request.defaults.adapter = adapter

    const record = await createShare({ file_id: 7 })

    expect(seen[0]).toMatchObject({ method: 'post', url: '/share' })
    expect(JSON.parse(String(seen[0].body))).toEqual({ file_id: 7 })
    expect(record).toMatchObject({ id: 5, token: 'tok' })
  })

  it('lists the caller shares and cancels by numeric id', async () => {
    const seen: Array<{ method?: string; url?: string }> = []
    const adapter: AxiosAdapter = async (config) => {
      seen.push({ method: config.method, url: config.url })
      return {
        data: envelope([{ id: 3, token: 'tok', file_id: 1, link: '/s/tok', expires_at: null, created_at: null, file_name: null }]),
        config, headers: {}, status: 200, statusText: 'OK',
      }
    }
    request.defaults.adapter = adapter

    const shares = await getMyShares()

    expect(seen[0]).toMatchObject({ method: 'get', url: '/share/my' })
    expect(shares).toHaveLength(1)

    const removal: AxiosAdapter = async (config) => {
      seen.push({ method: config.method, url: config.url })
      return { data: undefined, config, headers: {}, status: 204, statusText: 'No Content' }
    }
    request.defaults.adapter = removal
    await cancelShare(3)

    expect(seen[1]).toMatchObject({ method: 'delete', url: '/share/3' })
    expect(seen.map((call) => call.url).join(' ')).not.toMatch(/token|authorization/i)
  })
})
