import { describe, it, expect } from 'vitest'
import { apiErrorDetail } from '../apiError'

describe('REST error contract', () => {
  it('reads stable code and message', () => {
    expect(apiErrorDetail({ success: false, error: { code: 'CONFLICT', message: '已存在' } }, 'fallback'))
      .toEqual({ code: 'CONFLICT', message: '已存在' })
  })
  it.each([null, {}, { detail: 'private upstream detail' }, { success: true, error: { code: 'X', message: 'private' } },
    { success: false, error: { code: 500, message: 'private' } }])('does not display unconfirmed error data %s', (payload) => {
    expect(apiErrorDetail(payload, 'fallback')).toEqual({ code: 'HTTP_ERROR', message: 'fallback' })
  })
})
