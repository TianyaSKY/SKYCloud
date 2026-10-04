import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { AxiosAdapter } from 'axios'
import request from '../request'
import { downloadFileBlob } from '../file'
import { useAuthStore } from '../../stores/auth'
import { useWorkspaceStore } from '../../stores/workspace'

vi.mock('../../router', () => ({ default: { currentRoute: { value: { name: 'home' } }, push: vi.fn() } }))
vi.mock('@arco-design/web-vue', () => ({ Message: { error: vi.fn() } }))

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
})

describe('authenticated download request', () => {
  it('uses the live Authorization and workspace headers, with no credential in the URL', async () => {
    useAuthStore().setAuth('access-header-value', { id: 1, username: 'user', role: 'common' })
    useWorkspaceStore().currentWorkspace = { id: 9, name: 'space', owner_id: 1 }
    const blob = new Blob(['download bytes'])
    const adapter: AxiosAdapter = async (config) => {
      expect(config.url).toBe('/files/7/download')
      expect(config.headers.Authorization).toBe('Bearer access-header-value')
      expect(config.headers['X-Workspace-Id']).toBe('9')
      expect(config.responseType).toBe('blob')
      expect(config.url).not.toContain('access-header-value')
      return { data: blob, config, headers: {}, status: 200, statusText: 'OK' }
    }
    request.defaults.adapter = adapter
    expect(await downloadFileBlob(7)).toBe(blob)
  })
})
