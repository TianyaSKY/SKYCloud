/** Strict parsing for public JSON routes migrated to the success contract. */
export function apiSuccessData(payload: unknown): unknown {
  if (payload && typeof payload === 'object' && !Array.isArray(payload) &&
      'success' in payload && payload.success === true && 'data' in payload && Object.prototype.hasOwnProperty.call(payload, 'data')) {
    return payload.data
  }
  throw new Error('服务响应格式无效')
}

export function usesSuccessEnvelope(url: string): boolean {
  const path = new URL(url, 'http://skycloud.local/api/').pathname.replace(/^\/api(?=\/)/, '')
  if (/^\/share(?:\/(?:my|[0-9]+))?$/.test(path)) return true
  return /^\/(inbox|sys_dicts|auth|users|workspace|folder|files)(?:\/|$)/.test(path)
}
