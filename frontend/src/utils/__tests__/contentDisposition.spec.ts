import { describe, expect, it } from 'vitest'
import { contentDispositionFilename } from '../contentDisposition'

describe('content-disposition file name', () => {
  it.each([
    ["attachment; filename*=UTF-8''%E6%8A%A5%E5%91%8A.txt", '报告.txt'],
    ['attachment; filename="report.txt"', 'report.txt'],
    ['attachment; filename=report.txt', 'report.txt'],
    ["inline; filename*=utf-8'en'%E6%8A%A5%E5%91%8A%20v2.txt", '报告 v2.txt'],
    ['attachment; filename="caf%C3%A9.txt"', 'café.txt'],
  ])('reads %s as %s', (header, expected) => {
    expect(contentDispositionFilename(header)).toBe(expected)
  })

  it('prefers the RFC 5987 form over the legacy form', () => {
    expect(contentDispositionFilename("attachment; filename=legacy.txt; filename*=UTF-8''%E6%96%B0.txt")).toBe('新.txt')
  })

  it('keeps a value that is not valid percent-encoding instead of dropping it', () => {
    expect(contentDispositionFilename('attachment; filename=100%')).toBe('100%')
    expect(contentDispositionFilename("attachment; filename*=UTF-8''100%")).toBe('100%')
  })

  it('returns null when no file name is declared', () => {
    expect(contentDispositionFilename('attachment')).toBeNull()
    expect(contentDispositionFilename('')).toBeNull()
    expect(contentDispositionFilename(undefined)).toBeNull()
  })
})
