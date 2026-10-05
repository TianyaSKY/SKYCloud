import { issueRuntimeUiTicket } from '../api/workspace'

export async function runtimeUiLaunchUrl(workspaceId: number): Promise<string> {
  const result = await issueRuntimeUiTicket(workspaceId)
  const url = new URL(result.launch_url)
  const ticket = url.searchParams.get('ticket') || ''
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password ||
      url.hostname === window.location.hostname || url.origin === window.location.origin || url.pathname !== '/__skycloud/launch' ||
      url.hash || !/^[A-Za-z0-9_-]{43}$/.test(ticket) ||
      Array.from(url.searchParams.keys()).length !== 1 || !url.searchParams.has('ticket')) {
    throw new Error('工作区入口地址无效')
  }
  return url.href
}

/** Renew only while the parent retains its authenticated workspace view. */
export function startRuntimeUiRenewal(workspaceId: number, launchUrl: string, onFailure: () => void): () => void {
  const origin = new URL(launchUrl).origin
  let stopped = false
  let busy = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let timeout: ReturnType<typeof setTimeout> | undefined
  let frame: HTMLIFrameElement | undefined
  let acknowledgement: ((event: MessageEvent) => void) | undefined
  const cleanFrame = () => {
    if (timeout) clearTimeout(timeout)
    if (acknowledgement) window.removeEventListener('message', acknowledgement)
    frame?.remove()
    frame = undefined
    acknowledgement = undefined
    timeout = undefined
  }
  const schedule = () => { timer = setTimeout(() => void renew(), 5 * 60 * 1000) }
  const stop = () => {
    stopped = true
    if (timer) clearTimeout(timer)
    cleanFrame()
    document.removeEventListener('visibilitychange', visible)
  }
  const fail = () => { if (!stopped) { stop(); onFailure() } }
  const renew = async () => {
    if (stopped || busy) return
    busy = true
    if (timer) clearTimeout(timer)
    try {
      const url = new URL(await runtimeUiLaunchUrl(workspaceId))
      if (stopped) return
      if (url.origin !== origin) throw new Error('工作区运行环境已改变')
      url.pathname = '/__skycloud/renew'
      const ticket = url.searchParams.get('ticket')
      frame = document.createElement('iframe')
      frame.hidden = true
      frame.setAttribute('sandbox', 'allow-scripts allow-same-origin')
      frame.referrerPolicy = 'no-referrer'
      acknowledgement = (event) => {
        if (event.source !== frame?.contentWindow || event.origin !== origin ||
            event.data?.type !== 'skycloud-runtime-ui-renewed' || event.data?.ticket !== ticket) return
        cleanFrame()
        busy = false
        if (!stopped) schedule()
      }
      window.addEventListener('message', acknowledgement)
      timeout = setTimeout(fail, 30 * 1000)
      frame.src = url.href
      document.body.appendChild(frame)
    } catch {
      fail()
    }
  }
  const visible = () => { if (document.visibilityState === 'visible') void renew() }
  document.addEventListener('visibilitychange', visible)
  schedule()
  return stop
}
