import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { runtimeUiLaunchUrl, startRuntimeUiRenewal } from '../runtimeUi'
import { issueRuntimeUiTicket } from '../../api/workspace'
vi.mock('../../api/workspace', () => ({ issueRuntimeUiTicket: vi.fn() }))
const issued = vi.mocked(issueRuntimeUiTicket)
const nonce = 'a'.repeat(43)
beforeEach(() => vi.clearAllMocks())
afterEach(() => { vi.useRealTimers(); document.body.replaceChildren() })

describe('runtime UI launch', () => {
  it('opens a one-use ticket on a distinct origin', async () => {
    const url = `http://r42.skycloud.localhost:8081/__skycloud/launch?ticket=${nonce}`
    issued.mockResolvedValue({ launch_url: url, expires_in: 30 })
    expect(await runtimeUiLaunchUrl(7)).toBe(url)
    expect(issued).toHaveBeenCalledWith(7)
  })
  it.each([
    `${window.location.origin}/__skycloud/launch?ticket=${nonce}`,
    `http://${window.location.hostname}:8081/__skycloud/launch?ticket=${nonce}`,
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

describe('runtime UI renewal', () => {
  const url = `http://r42.skycloud.localhost:8081/__skycloud/launch?ticket=${nonce}`
  it('accepts only the exact frame, origin and consumed ticket before scheduling another renewal', async () => {
    vi.useFakeTimers()
    issued.mockResolvedValue({ launch_url: url, expires_in: 30 })
    const failed = vi.fn()
    const stop = startRuntimeUiRenewal(7, url, failed)
    await vi.advanceTimersByTimeAsync(300000)
    const frame = document.querySelector('iframe')!
    expect(frame.src).toContain('/__skycloud/renew?ticket=')
    expect(frame.referrerPolicy).toBe('no-referrer')
    const send = (source: MessageEventSource | null, origin: string, ticket: string) => window.dispatchEvent(
      new MessageEvent('message', { source, origin, data: { type: 'skycloud-runtime-ui-renewed', ticket } }))
    send(window, new URL(url).origin, nonce)
    send(frame.contentWindow, 'http://r999.skycloud.localhost:8081', nonce)
    send(frame.contentWindow, new URL(url).origin, 'wrong-ticket')
    expect(document.querySelector('iframe')).toBe(frame)
    send(frame.contentWindow, new URL(url).origin, nonce)
    expect(document.querySelector('iframe')).toBeNull()
    await vi.advanceTimersByTimeAsync(300000)
    expect(issued).toHaveBeenCalledTimes(2)
    stop()
    expect(document.querySelector('iframe')).toBeNull()
    await vi.advanceTimersByTimeAsync(600000)
    expect(failed).not.toHaveBeenCalled()
    expect(issued).toHaveBeenCalledTimes(2)
  })

  it('closes on denied or unacknowledged renewal without retrying', async () => {
    vi.useFakeTimers()
    issued.mockResolvedValue({ launch_url: url, expires_in: 30 })
    const failed = vi.fn()
    startRuntimeUiRenewal(7, url, failed)
    await vi.advanceTimersByTimeAsync(330000)
    expect(failed).toHaveBeenCalledTimes(1)
    expect(document.querySelector('iframe')).toBeNull()
    await vi.advanceTimersByTimeAsync(600000)
    expect(issued).toHaveBeenCalledTimes(1)
  })

  it('ignores a late issuance after closing the workspace', async () => {
    vi.useFakeTimers()
    let finish!: (value: { launch_url: string; expires_in: number }) => void
    issued.mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const failed = vi.fn()
    const stop = startRuntimeUiRenewal(7, url, failed)
    await vi.advanceTimersByTimeAsync(300000)
    stop()
    finish({ launch_url: url, expires_in: 30 })
    await vi.advanceTimersByTimeAsync(0)
    expect(document.querySelector('iframe')).toBeNull()
    expect(failed).not.toHaveBeenCalled()
  })

  it('refuses renewal into a different runtime origin', async () => {
    vi.useFakeTimers()
    issued.mockResolvedValue({ launch_url: url.replace('r42.', 'r43.'), expires_in: 30 })
    const failed = vi.fn()
    startRuntimeUiRenewal(7, url, failed)
    await vi.advanceTimersByTimeAsync(300000)
    expect(failed).toHaveBeenCalledTimes(1)
    expect(document.querySelector('iframe')).toBeNull()
  })
})
