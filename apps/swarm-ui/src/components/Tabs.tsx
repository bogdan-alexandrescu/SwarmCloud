/**
 * TABS, THE SEGMENTED CONTROL AND THE BREADCRUMB (components.html A, "Tabs
 * and breadcrumbs").
 *
 * UNDERLINE TABS switch views of ONE object, and each has its own route
 * (/agents/live/<id>/attempts), so each tab is a link that opens in a new
 * tab. A tab with nothing behind it shows a dash, not 0.
 *
 * THE SEGMENTED CONTROL filters a list. It is a group of toggle buttons
 * (`aria-pressed`), not a tablist: nothing behind it is a separate view.
 *
 * THE BREADCRUMB is the trail TO the page; every segment but the open object
 * is a link back, and the object is the last segment, in mono, not a link.
 */
import { Fragment, type MouseEvent, type ReactNode } from 'react'

export interface TabDef {
  key: string
  label: string
  href: string
  /** The count behind the tab. `null` is unknown and draws a dash; omitted, nothing. */
  count?: number | null
}

/** A plain left click a router should take; anything else is the browser's. */
export function routedClick(e: MouseEvent): boolean {
  return !(e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
}

export function Tabs({ tabs, current, label, onGo }: { tabs: readonly TabDef[]; current: string; label: string; onGo?: (href: string) => void }) {
  return (
    <nav className="c-tabs" aria-label={label}>
      {tabs.map((t) => (
        <a
          key={t.key}
          href={t.href}
          aria-current={t.key === current ? 'page' : undefined}
          onClick={(e) => {
            if (onGo === undefined || !routedClick(e)) return
            e.preventDefault()
            onGo(t.href)
          }}
        >
          {t.label}
          {t.count !== undefined && <em>{t.count === null ? '—' : t.count}</em>}
        </a>
      ))}
    </nav>
  )
}

export interface SegOption<K extends string> {
  key: K
  label: ReactNode
  count?: number | null
  /**
   * The option's own address. Set on every option, the control is NAVIGATION
   * between views that each have a route (Capacity's Ceilings | By runner
   * profile): links in a labelled `nav`, the current one `aria-current`,
   * drawn exactly as the toggle form is.
   */
  href?: string
}

export function Segmented<K extends string>({ options, value, onChange, label, small = false }: { options: readonly SegOption<K>[]; value: K; onChange?: (k: K) => void; label: string; small?: boolean }) {
  if (options.length > 0 && options.every((o) => o.href !== undefined)) {
    return (
      <nav className={`c-seg${small ? ' is-sm' : ''}`} aria-label={label}>
        {options.map((o) => (
          <a
            key={o.key}
            href={o.href}
            aria-current={o.key === value ? 'page' : undefined}
            onClick={(e) => {
              if (onChange === undefined || !routedClick(e)) return
              e.preventDefault()
              onChange(o.key)
            }}
          >
            {o.label}
            {o.count !== undefined && <em>{o.count === null ? '—' : o.count}</em>}
          </a>
        ))}
      </nav>
    )
  }
  return (
    <div className={`c-seg${small ? ' is-sm' : ''}`} role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o.key} type="button" aria-pressed={o.key === value} onClick={() => onChange?.(o.key)}>
          {o.label}
          {o.count !== undefined && <em>{o.count === null ? '—' : o.count}</em>}
        </button>
      ))}
    </div>
  )
}

export interface Crumb {
  key: string
  label: string
  /** Where the segment goes; null for the open object, which is not a link. */
  href: string | null
  /** An id: drawn in mono, never restyled. */
  id?: boolean
}

export function Breadcrumb({ crumbs, onGo }: { crumbs: readonly Crumb[]; onGo?: (href: string) => void }) {
  if (crumbs.length === 0) return null
  return (
    <nav aria-label="Breadcrumb">
      <ol className="c-crumb">
        {crumbs.map((c, i) => (
          <Fragment key={c.key}>
            {i > 0 && (
              <li aria-hidden className="c-crumb-sep">
                /
              </li>
            )}
            <li>
              {c.href === null ? (
                <span aria-current="page" className={c.id === true ? 'c-crumb-id id' : undefined}>
                  {c.label}
                </span>
              ) : (
                <a
                  href={c.href}
                  onClick={(e) => {
                    if (onGo === undefined || !routedClick(e)) return
                    e.preventDefault()
                    onGo(c.href!)
                  }}
                >
                  {c.label}
                </a>
              )}
            </li>
          </Fragment>
        ))}
      </ol>
    </nav>
  )
}
