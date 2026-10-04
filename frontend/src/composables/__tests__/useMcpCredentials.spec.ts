import { defineComponent } from 'vue'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { getMcpToken, refreshMcpToken, type McpTokenResult, type McpTokenIssuedResult } from '@/api/auth'
import { useMcpCredentials } from '../useMcpCredentials'

vi.mock('@/api/auth', () => ({ getMcpToken: vi.fn(), refreshMcpToken: vi.fn() }))
const metadata = { id: 1, name: 'MCP Token', token_preview: 'sha256:123456', created_at: null, expires_at: null }
const read: McpTokenResult = { token: metadata, user_id: 1 }
const issued: McpTokenIssuedResult = { ...read, token: metadata, mcp_token: 'one-time-credential' }

function setup() {
  let state!: ReturnType<typeof useMcpCredentials>
  const wrapper = mount(defineComponent({ setup() { state = useMcpCredentials(); return () => null } }))
  return { state, wrapper }
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(getMcpToken).mockResolvedValue(read)
  vi.mocked(refreshMcpToken).mockResolvedValue(issued)
})

describe('MCP one-time credentials', () => {
  it('reads metadata without issuing or revealing a credential', async () => {
    const { state, wrapper } = setup()
    await state.open()
    expect(state.metadata.value).toEqual(metadata)
    expect(state.rawToken.value).toBe('')
    expect(refreshMcpToken).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('clears the issued credential when closed and never restores it on reopening', async () => {
    const { state, wrapper } = setup()
    await state.open()
    expect(await state.issue()).toBe(true)
    expect(state.rawToken.value).toBe(issued.mcp_token)
    state.visible.value = false
    expect(state.rawToken.value).toBe('')
    await state.open()
    expect(state.rawToken.value).toBe('')
    wrapper.unmount()
  })

  it('discards a late issuance response after closing', async () => {
    let resolve!: (result: McpTokenIssuedResult) => void
    vi.mocked(refreshMcpToken).mockReturnValue(new Promise(r => { resolve = r }))
    const { state, wrapper } = setup()
    await state.open()
    const request = state.issue()
    state.visible.value = false
    resolve(issued)
    expect(await request).toBe(false)
    expect(state.rawToken.value).toBe('')
    wrapper.unmount()
  })

  it('clears credentials on unmount', async () => {
    const { state, wrapper } = setup()
    await state.open()
    await state.issue()
    wrapper.unmount()
    expect(state.rawToken.value).toBe('')
  })
})
