import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { sseRequest } from '../sseRequest'
const mocks = vi.hoisted(() => ({ auth: { token: 'access', user: { id: 7 }, restore: vi.fn() }, workspace: { currentWorkspace: { id: 11 } } }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => mocks.auth }))
vi.mock('@/stores/workspace', () => ({ useWorkspaceStore: () => mocks.workspace }))
const fetchMock = vi.fn()
const encode = (text: string) => new TextEncoder().encode(text)
function response(chunks: Uint8Array[]) {
  return new Response(new ReadableStream<Uint8Array>({ start(controller) { chunks.forEach(chunk => controller.enqueue(chunk)); controller.close() } }), { headers: { 'Content-Type': 'text/event-stream' } })
}
beforeEach(() => { vi.resetAllMocks(); vi.stubGlobal('fetch', fetchMock); mocks.auth.token = 'access'; mocks.auth.user.id = 7; mocks.workspace.currentWorkspace.id = 11 })
afterEach(() => vi.unstubAllGlobals())

describe('authenticated assistant stream', () => {
  it('sends only the memory bearer and explicit workspace headers', async () => {
    fetchMock.mockResolvedValue(response([encode('data: {"type":"done","payload":{}}\n\n')]))
    const onEvent = vi.fn(); await sseRequest('/assistant/conversations/5/messages/stream', { query: 'hello' }, { onEvent })
    expect(fetchMock).toHaveBeenCalledWith('/api/assistant/conversations/5/messages/stream', expect.objectContaining({ headers: expect.objectContaining({ Authorization: 'Bearer access', 'X-Workspace-Id': '11' }) }))
    expect(fetchMock.mock.calls[0]?.[0]).not.toContain('access'); expect(onEvent).toHaveBeenCalledWith({ type: 'done', payload: {} })
  })
  it('decodes fragmented UTF-8, CRLF and multi-line data without losing tokens', async () => {
    const raw = encode(': heartbeat\r\ndata: {"type":"token",\r\ndata: "payload":{"content":"中文"}}\r\n\r\ndata: {"type":"done","payload":{}}')
    fetchMock.mockResolvedValue(response(Array.from(raw, byte => new Uint8Array([byte]))))
    const onEvent = vi.fn(); await sseRequest('/stream', {}, { onEvent })
    expect(onEvent.mock.calls.map(call => call[0])).toEqual([{ type: 'token', payload: { content: '中文' } }, { type: 'done', payload: {} }])
  })
  it('ignores malformed JSON while allowing subsequent valid events', async () => {
    fetchMock.mockResolvedValue(response([encode('data: broken\n\ndata: {"type":"done","payload":{}}\n\n')]))
    const onEvent = vi.fn(); await sseRequest('/stream', {}, { onEvent }); expect(onEvent).toHaveBeenCalledTimes(1)
  })
  it('propagates callback failures and cancels/releases the reader', async () => {
    const cancel = vi.fn(); const body = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(encode('data: {"type":"token","payload":{}}\n\n')) }, cancel })
    fetchMock.mockResolvedValue(new Response(body))
    await expect(sseRequest('/stream', {}, { onEvent: () => { throw new Error('render failure') } })).rejects.toThrow('render failure')
    expect(cancel).toHaveBeenCalledTimes(1); expect(body.locked).toBe(false)
  })
  it.each([false, true])('bounds unfinished single-line and multi-line events (multiline=%s)', async multiline => {
    const cancel = vi.fn(); const text = multiline ? ('data: ' + 'a'.repeat(1024) + '\n').repeat(1025) : 'data: ' + 'a'.repeat(1024 * 1024)
    const body = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(encode(text)) }, cancel })
    fetchMock.mockResolvedValue(new Response(body))
    await expect(sseRequest('/stream', {}, { onEvent: vi.fn() })).rejects.toThrow('size limit')
    expect(cancel).toHaveBeenCalledTimes(1); expect(body.locked).toBe(false)
  })
  it('cancels a blocked read on abort without delivering queued old events', async () => {
    const cancel = vi.fn(); const body = new ReadableStream<Uint8Array>({ cancel }); fetchMock.mockResolvedValue(new Response(body))
    const controller = new AbortController(); const onEvent = vi.fn()
    const promise = sseRequest('/stream', {}, { onEvent, signal: controller.signal })
    const rejected = expect(promise).rejects.toMatchObject({ name: 'AbortError' })
    await vi.waitFor(() => expect(body.locked).toBe(true)); controller.abort(); await rejected
    expect(cancel).toHaveBeenCalledTimes(1); expect(body.locked).toBe(false); expect(onEvent).not.toHaveBeenCalled()
  })
  it('cancels an accepted response when the workspace changed while fetch was pending', async () => {
    const cancel = vi.fn(); const body = new ReadableStream<Uint8Array>({ cancel })
    fetchMock.mockImplementation(async () => { mocks.workspace.currentWorkspace.id = 12; return new Response(body) })
    await expect(sseRequest('/stream', {}, { onEvent: vi.fn() })).rejects.toMatchObject({ name: 'AbortError' })
    expect(cancel).toHaveBeenCalledTimes(1); expect(body.locked).toBe(false)
  })
  it('refreshes a rejected expired token once, before accepting any stream', async () => {
    fetchMock.mockResolvedValueOnce(new Response('{}', { status: 401 })).mockResolvedValueOnce(response([encode('data: {"type":"done","payload":{}}\n\n')]))
    mocks.auth.restore.mockImplementation(async () => { mocks.auth.token = 'renewed'; return true })
    await sseRequest('/stream', {}, { onEvent: vi.fn() })
    expect(mocks.auth.restore).toHaveBeenCalledWith(true); expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls[1]?.[1].headers.Authorization).toBe('Bearer renewed')
  })
  it('does not retry a permission denial or replay a partly consumed failed stream', async () => {
    fetchMock.mockResolvedValueOnce(new Response('{"message":"denied"}', { status: 403 }))
    await expect(sseRequest('/stream', {}, { onEvent: vi.fn() })).rejects.toThrow('denied'); expect(mocks.auth.restore).not.toHaveBeenCalled()
    const body = new ReadableStream<Uint8Array>({ start(controller) { controller.error(new Error('broken connection')) } }); fetchMock.mockResolvedValueOnce(new Response(body))
    await expect(sseRequest('/stream', {}, { onEvent: vi.fn() })).rejects.toThrow('broken connection')
    expect(fetchMock).toHaveBeenCalledTimes(2); expect(mocks.auth.restore).not.toHaveBeenCalled()
  })
  it.each(['workspace', 'user'])('does not resend in a changed %s after refreshing', async kind => {
    fetchMock.mockResolvedValue(new Response('{}', { status: 401 })); mocks.auth.restore.mockImplementation(async () => { if (kind === 'workspace') mocks.workspace.currentWorkspace.id = 12; else mocks.auth.user.id = 8; return true })
    await expect(sseRequest('/stream', {}, { onEvent: vi.fn() })).rejects.toMatchObject({ name: 'AbortError' }); expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
