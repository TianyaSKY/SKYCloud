import { beforeEach, describe, expect, it, vi } from 'vitest'
import { respondAssistantPermission } from '../assistant'
const post = vi.hoisted(() => vi.fn())
vi.mock('../request', () => ({ default: { post } }))
beforeEach(() => vi.resetAllMocks())
describe('one-shot assistant permission transport', () => {
  it.each(['once', 'reject'] as const)('encodes permission identity and never remembers %s', async response => {
    post.mockResolvedValue({ status: 'waiting_permission' })
    await respondAssistantPermission(42, 'permission/with?query#fragment', response)
    expect(post).toHaveBeenCalledWith('/assistant/runs/42/permissions/permission%2Fwith%3Fquery%23fragment', { response, remember: false })
  })
})
