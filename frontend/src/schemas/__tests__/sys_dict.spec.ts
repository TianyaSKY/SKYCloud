import { describe, expect, it } from 'vitest'
import { sysDictSchema } from '../sys_dict'

describe('system settings storage bounds', () => {
  const allowed = {key: 'k'.repeat(100), value: 'v'.repeat(255), des: 'd'.repeat(2048), enable: true}
  it('accepts the database limits and filters response metadata before submission', () => {
    expect(sysDictSchema.parse({...allowed, id: 7, created_at: 'private'})).toEqual(allowed)
  })
  it.each([['key', 101], ['value', 256], ['des', 2049]] as const)('rejects oversized %s', (field, size) => {
    expect(sysDictSchema.safeParse({...allowed, [field]: 'x'.repeat(size)}).success).toBe(false)
  })
})
