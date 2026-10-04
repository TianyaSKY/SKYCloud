import { downloadFileBlob } from '../api/file'

/** Accept only same-origin protected file paths, including historical AI links. */
export function protectedFileId(href: string, origin = window.location.origin): number | null {
  try {
    const url = new URL(href, origin)
    if (url.origin !== origin) return null
    const match = url.pathname.match(/^\/api\/files?\/(?:(\d+)\/download|download\/(\d+))\/?$/)
    const id = match ? Number(match[1] || match[2]) : 0
    return Number.isSafeInteger(id) && id > 0 ? id : null
  } catch {
    return null
  }
}

export async function downloadAttachment(id: number, filename = 'download') {
  const blob = await downloadFileBlob(id)
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  // A later task lets the browser begin consuming the object URL first.
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}
