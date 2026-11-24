// The guide's addresses (Guide.tsx), apart from the page so App.tsx and the
// pages that link to the guide reach them without loading the guide itself.

export const GUIDE_PATH = '/guide'
export const GUIDE_INDEX = 'index'

/** The guide page a pathname names (`''` for the index), or null off the guide. */
export function guideSlugFrom(pathname: string): string | null {
  const p = pathname.replace(/\/+$/, '')
  if (p === GUIDE_PATH) return ''
  if (!p.startsWith(`${GUIDE_PATH}/`)) return null
  const slug = p.slice(GUIDE_PATH.length + 1)
  return slug === GUIDE_INDEX ? '' : slug
}

export function guidePath(slug: string): string {
  return slug === '' || slug === GUIDE_INDEX ? GUIDE_PATH : `${GUIDE_PATH}/${slug}`
}
