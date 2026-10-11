import { describe, expect, it } from 'vitest'
import { shareLinkUrl } from '../shareLink'

describe('public share link', () => {
  it('builds the /s/<token> route against the given origin', () => {
    expect(shareLinkUrl('tok-9', 'https://cloud.example.test')).toBe('https://cloud.example.test/s/tok-9')
  })

  it('defaults to the current origin', () => {
    expect(shareLinkUrl('tok-9')).toBe(`${window.location.origin}/s/tok-9`)
  })
})
