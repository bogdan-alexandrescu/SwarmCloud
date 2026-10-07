/**
 * TABS, THE SEGMENTED CONTROL AND THE BREADCRUMB (components.html A, "Tabs
 * and breadcrumbs").
 *
 * UNDERLINE TABS switch views of ONE object, and each has its own route
 * (/agents/live/<id>/attempts), so each tab is a link that opens in a new
 * tab. A tab with nothing behind it shows a dash, not 0.
 *
 * THE TABLIST FORM (`onSelect`) is the same strip for panes that live inside
 * one region rather than at an address of their own -- the agent split's
 * Details / Children / Attempts / Artifacts / Checkpoints, whose Children
 * pane has no route. Buttons with `role="tab"` and `aria-selected`, drawn
 * exactly as the link form is. It replaced the split's local `.ag-tabs`.
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
  /** The tab's address. Not read by the tablist form. */
  href?: string
  /**
   * The count behind the tab. `null` is unknown and draws a dash; omitted,
   * nothing. A string is a count that needs its denominator, drawn as given:
   * the agent's Checkpoints tab says `0 of 3` -- kept of written -- where a
   * bare `0` read as none ever written (G2-12).
   */
  count?: number | string | null
  /** Why the count is what it is (a dash's reason), as the count's title. */
  why?: string
}

function TabCount({ t }: { t: TabDef }) {
  if (t.count === undefined) return null
  return <em title={t.why}>{t.count === null ? '—' : t.count}</em>
}

/** A plain left click a router should take; anything else is the browser's. */
export function routedClick(e: MouseEvent): boolean {
  return !(e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
}

export function Tabs({
  tabs,
  current,
  label,
  onGo,
  onSelect,
  className,
}: {
  tabs: readonly TabDef[]
  current: string
  label: string
  onGo?: (href: string) => void
  /** Set, the strip is a `tablist` of buttons and this is told the key pressed. */
  onSelect?: (key: string) => void
  /** Layout only (margins in the region that holds it); never a restyle. */
  className?: string
}) {
  const cls = className === undefined ? 'c-tabs' : `c-tabs ${className}`
  if (onSelect !== undefined) {
    return (
      <div className={cls} role="tablist" aria-label={label}>
        {tabs.map((t) => (
          <button key={t.key} role="tab" type="button" aria-selected={t.key === current} onClick={() => onSelect(t.key)}>
            <span className="c-tab-label">{t.label}</span>
            <TabCount t={t} />
          </button>
        ))}
      </div>
    )
  }
  return (
    <nav className={cls} aria-label={label}>
      {tabs.map((t) => (
        <a
          key={t.key}
          href={t.href}
          aria-current={t.key === current ? 'page' : undefined}
          onClick={(e) => {
            if (onGo === undefined || t.href === undefined || !routedClick(e)) return
            e.preventDefault()
            onGo(t.href)
          }}
        >
          <span className="c-tab-label">{t.label}</span>
          <TabCount t={t} />
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
  /** What choosing it does, as the segment's title. */
  title?: string
  /** Layout only (`is-wide-only`: a segment the phone sheet carries instead). */
  className?: string
}

/**
 * `value` null presses nothing (a span the reader zoomed away from, a toggle
 * that is off). `role="tablist"` is for a strip that opens a pane under it
 * (Accounts' Holding now | History): tabs with `aria-selected`, same drawing.
 * `disabled` disables every segment (a list still loading).
 */
export function Segmented<K extends string>({
  options,
  value,
  onChange,
  label,
  small = false,
  className,
  disabled = false,
  role = 'group',
}: {
  options: readonly SegOption<K>[]
  value: K | null
  onChange?: (k: K) => void
  label: string
  small?: boolean
  /** Layout only, in the region that holds it; never a restyle. */
  className?: string
  disabled?: boolean
  role?: 'group' | 'tablist'
}) {
  const cls = ['c-seg', small ? 'is-sm' : '', className ?? ''].filter(Boolean).join(' ')
  const count = (o: SegOption<K>) => o.count !== undefined && <em>{o.count === null ? '—' : o.count}</em>
  if (options.length > 0 && options.every((o) => o.href !== undefined)) {
    return (
      <nav className={cls} aria-label={label}>
        {options.map((o) => (
          <a
            key={o.key}
            href={o.href}
            className={o.className}
            title={o.title}
            aria-current={o.key === value ? 'page' : undefined}
            onClick={(e) => {
              if (onChange === undefined || !routedClick(e)) return
              e.preventDefault()
              onChange(o.key)
            }}
          >
            {o.label}
            {count(o)}
          </a>
        ))}
      </nav>
    )
  }
  const tabs = role === 'tablist'
  return (
    <div className={cls} role={role} aria-label={label}>
      {options.map((o) => (
        <button
          key={o.key}
          type="button"
          role={tabs ? 'tab' : undefined}
          className={o.className}
          title={o.title}
          disabled={disabled || undefined}
          aria-pressed={tabs ? undefined : o.key === value}
          aria-selected={tabs ? o.key === value : undefined}
          onClick={() => onChange?.(o.key)}
        >
          {o.label}
          {count(o)}
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
