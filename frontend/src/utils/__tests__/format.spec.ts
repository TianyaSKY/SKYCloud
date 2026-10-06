import { describe, expect, it, vi } from 'vitest'
import { formatDate } from '../format'

describe('explicit Beijing date presentation', () => {
  it.each([
    '2025-06-01T04:30:00Z',
    '2025-06-01T00:30:00-04:00',
    '2025-06-01T12:30:00+08:00',
    '2025-06-01T12:30:00',
    '2025-06-01 12:30:00',
    new Date('2025-06-01T04:30:00Z'),
    Date.parse('2025-06-01T04:30:00Z'),
  ])('shows the same instant for %s', (value) => {
    expect(formatDate(value)).toBe('2025-06-01 12:30')
  })

  it('treats legacy date-only input as Beijing midnight', () => {
    expect(formatDate('2025-06-01')).toBe('2025-06-01 00:00')
    expect(formatDate('2025-05-31T16:00:00Z')).toBe('2025-06-01 00:00')
  })

  it.each([null, undefined, '', 'not-a-date', Number.NaN])('handles absent/invalid values', (value) => {
    expect(formatDate(value)).toBe('-')
  })

  it('does not display UTC wall time when timezone formatting fails', () => {
    const parts = vi.spyOn(Intl.DateTimeFormat.prototype, 'formatToParts').mockReturnValue([])
    try {
      expect(formatDate('2025-06-01T04:30:00Z')).toBe('-')
    } finally {
      parts.mockRestore()
    }
  })
})
