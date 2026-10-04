import { issueRuntimeUiTicket } from '../api/workspace'

export async function runtimeUiLaunchUrl(workspaceId: number): Promise<string> {
  const result = await issueRuntimeUiTicket(workspaceId)
  const url = new URL(result.launch_url)
  const ticket = url.searchParams.get('ticket') || ''
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password ||
      url.origin === window.location.origin || url.pathname !== '/__skycloud/launch' ||
      url.hash || !/^[A-Za-z0-9_-]{43}$/.test(ticket) ||
      Array.from(url.searchParams.keys()).length !== 1 || !url.searchParams.has('ticket')) {
    throw new Error('工作区入口地址无效')
  }
  return url.href
}
