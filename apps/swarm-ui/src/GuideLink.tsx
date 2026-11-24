// A link to the guide from any page, and the in-place opening it does, apart
// from Guide.tsx so a page that links to the guide does not load it.
import type { MouseEvent, ReactNode } from 'react'
import { guidePath, guideSlugFrom } from './guidePaths'

/** Opens a guide address in place: pushed, so Back returns, and read by App's `popstate`. */
export function openGuide(href: string) {
  window.history.pushState(null, '', href)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

export function onGuideClick(e: MouseEvent<HTMLAnchorElement>, href: string) {
  if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
  if (guideSlugFrom(href.split('#')[0] ?? '') === null) return
  e.preventDefault()
  openGuide(href)
}

/** A link to the guide from another console page, opened in place. */
export function GuideLink({ slug = '', children, className = 'ctl-link', label }: { slug?: string; children: ReactNode; className?: string; label?: string }) {
  const href = guidePath(slug)
  return (
    <a className={className} href={href} aria-label={label} onClick={(e) => onGuideClick(e, href)}>
      {children}
    </a>
  )
}
