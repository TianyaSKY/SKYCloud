import { describe, expect, it } from 'vitest'
import { apiSuccessData, usesSuccessEnvelope } from '../apiSuccess'

describe('public JSON envelope parsing', () => {
  it.each(['/workspace', '/api/workspace/1?refresh=1', '/users/1', '/auth/refresh', 'workspace'])('recognizes migrated route %s', path => {
    expect(usesSuccessEnvelope(path)).toBe(true)
  })
  it.each(['/workspaces', '/file/workspace', '/runtime-ui/session', '/admin/users'])('keeps other protocols separate %s', path => {
    expect(usesSuccessEnvelope(path)).toBe(false)
  })
  it('requires an own data property and supports intentionally null data', () => {
    expect(apiSuccessData({success:true,data:null})).toBeNull()
    expect(() => apiSuccessData(Object.assign(Object.create({data:'private'}),{success:true}))).toThrow('无效')
  })
})
