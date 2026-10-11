/**
 * Resolve a download name from a Content-Disposition header.
 *
 * Precedence follows RFC 6266: the RFC 5987 `filename*=` form (what this API sends)
 * wins over the legacy `filename=` form. The extended value may carry a
 * charset'language' prefix, both forms may be quoted, and a value that is not valid
 * percent-encoding is kept verbatim instead of being dropped.
 */
export function contentDispositionFilename(header: string | undefined | null): string | null {
  if (!header) return null
  const extended = /filename\*\s*=\s*([^;]+)/i.exec(header)?.[1]?.trim()
  const legacy = /filename\s*=\s*([^;]+)/i.exec(header)?.[1]?.trim()
  const raw = extended ?? legacy
  if (!raw) return null
  const withoutCharset = extended ? raw.replace(/^[\w-]+'[\w-]*'/, '') : raw
  const unquoted = withoutCharset.replace(/^"(.*)"$/s, '$1').trim()
  try {
    return decodeURIComponent(unquoted) || unquoted
  } catch {
    return unquoted
  }
}
