/** Public share link: the /s/<token> route is what unauthenticated visitors open. */
export function shareLinkUrl(token: string, origin: string = window.location.origin): string {
  return `${origin}/s/${token}`
}
