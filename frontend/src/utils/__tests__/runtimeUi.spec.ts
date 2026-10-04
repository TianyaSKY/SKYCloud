import { beforeEach, describe, expect, it, vi } from 'vitest'
import { runtimeUiLaunchUrl } from '../runtimeUi'
import { issueRuntimeUiTicket } from '../../api/workspace'
vi.mock('../../api/workspace', () => ({ issueRuntimeUiTicket: vi.fn() }))
const issued = vi.mocked(issueRuntimeUiTicket)
const nonce = 'a'.repeat(43)
beforeEach(() => vi.clearAllMocks())

describe('runtime UI launch', () => {
  it('opens a one-use ticket on a distinct origin', async () => {
    const url = `http://r42.skycloud.localhost:8081/__skycloud/launch?ticket=${nonce}`
    issued.mockResolvedValue({ launch_url: url, expires_in: 30 })
    expect(await runtimeUiLaunchUrl(7)).toBe(url)
    expect(issued).toHaveBeenCalledWith(7)
  })
  it.each([
    `${window.location.origin}/__skycloud/launch?ticket=${nonce}`,
    `javascript:alert(1)`,
    `http://user:password@r42.skycloud.localhost:8081/__skycloud/launch?ticket=${nonce}`,
    `http://r42.skycloud.localhost:8081/__skycloud/launch?token=full-session-jwt`,
    `http://r42.skycloud.localhost:8081/__skycloud/launch?ticket=${nonce}&token=full-session-jwt`,
    `http://r42.skycloud.localhost:8081/other?ticket=${nonce}`,
    `http://r42.skycloud.localhost:8081/__skycloud/launch?ticket=${nonce}&ticket=another`,
  ])('rejects an unsafe or malformed launch URL: %s', async (launch_url) => {
    issued.mockResolvedValue({ launch_url, expires_in: 30 })
    await expect(runtimeUiLaunchUrl(7)).rejects.toThrow()
  })
})
