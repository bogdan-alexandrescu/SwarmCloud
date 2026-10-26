/**
 * THE BROWSER TOOL, EGRESS AND JEV BADGES (lane BSTATUS-STRIP, part of #1030).
 *
 * Owner decision 2026-10-14 (~08:00 PDT): a browser task picks its tool on
 * Submit ("Browser tool: Auto / Playwright / dev-browser / Jev", Auto by
 * default), and the Browser tab and the theater say, in the strip above the
 * frame, which tool the agent is using, whether its traffic leaves through
 * the residential proxy, and whether Jev is on. Each badge explains itself on
 * hover AND focus -- what it means, the tool's last action and when, and
 * where it is changed -- because a badge a keyboard or a phone cannot open is
 * a fact only a mouse has.
 *
 * THE OBJECT IS OPTIONAL AND READ DEFENSIVELY. The live read gains `browser`
 * (the shape fixed in the brief, below); an older worker serves none, and
 * then nothing new is drawn. A half-formed object is refused whole rather than
 * drawn in part: a [Residential · US] badge read off a missing `state` would
 * say the proxy is fine when nothing reported it.
 *
 * [Jev off · Playwright] WHEN JEV WAS PICKED AND THE ATTEMPT FELL BACK (lane
 * JEV-OFF, part of #1345). Measured 2026-10-15: a jev task whose workspace
 * had no route to the decision API ran on Playwright while this chip read
 * "Jev". The runner reports why jev is off (`no_key`, `not_installed`,
 * `no_route`), and swarm-api serves the workspace's reason before it has;
 * either way the chip says what the agent actually drives.
 *
 * WHY [Proxy off] ONLY WHEN RESIDENTIAL WAS EXPECTED: a pod dispatched
 * without egress leaves through the NAT by design (docs/browser-egress.md);
 * calling that "off" would read as a fault on every ordinary task. Direct is
 * the word for it.
 */
import { useEffect, useId, useRef, useState, type ReactNode } from 'react'

import { timeAgo } from './types'

export type BrowserTool = 'playwright' | 'dev-browser' | 'jev'
export type BrowserToolChoice = 'auto' | BrowserTool

/** `browser` on the live read (BSTATUS shared shape, 2026-10-14). */
export interface LiveBrowserTools {
  tool_selected: BrowserToolChoice
  /** What this attempt actually registered. */
  tools_available: BrowserTool[]
  tool_last_used: { name: BrowserTool; at: string } | null
  jev: {
    enabled: boolean
    reason: 'no_key' | 'not_installed' | 'not_selected' | 'no_route' | null
    key_scope: 'user' | 'tenant' | null
  }
  egress: {
    mode: 'residential' | 'direct'
    country: string | null
    state: 'ok' | 'failing' | 'off'
    /** Short and vendor-free, from the worker. */
    last_error: string | null
  }
}

const TOOLS: readonly BrowserTool[] = ['playwright', 'dev-browser', 'jev']
const TOOL_LABEL: Record<BrowserTool, string> = { playwright: 'Playwright', 'dev-browser': 'dev-browser', jev: 'Jev' }

function isTool(v: unknown): v is BrowserTool {
  return typeof v === 'string' && (TOOLS as readonly string[]).includes(v)
}

function oneOf<T extends string>(v: unknown, allowed: readonly T[]): v is T {
  return typeof v === 'string' && (allowed as readonly string[]).includes(v)
}

function strOrNull(v: unknown): v is string | null {
  return v === null || typeof v === 'string'
}

/**
 * The `browser` object, or null when it is absent or does not hold together.
 * An unknown tool in `tools_available` is dropped, not fatal: a newer worker
 * registering a fourth tool must not blank the strip.
 */
export function browserStatusOf(raw: unknown): LiveBrowserTools | null {
  if (typeof raw !== 'object' || raw === null) return null
  const r = raw as Record<string, unknown>
  if (!(r.tool_selected === 'auto' || isTool(r.tool_selected))) return null
  if (!Array.isArray(r.tools_available)) return null
  const used = r.tool_last_used
  let lastUsed: LiveBrowserTools['tool_last_used'] = null
  if (used !== null && used !== undefined) {
    if (typeof used !== 'object') return null
    const u = used as Record<string, unknown>
    if (!isTool(u.name) || typeof u.at !== 'string') return null
    lastUsed = { name: u.name, at: u.at }
  }
  const j = r.jev
  if (typeof j !== 'object' || j === null) return null
  const jev = j as Record<string, unknown>
  if (typeof jev.enabled !== 'boolean') return null
  const reason = jev.reason ?? null
  const scope = jev.key_scope ?? null
  if (!(reason === null || oneOf(reason, ['no_key', 'not_installed', 'not_selected', 'no_route'] as const))) return null
  if (!(scope === null || oneOf(scope, ['user', 'tenant'] as const))) return null
  const e = r.egress
  if (typeof e !== 'object' || e === null) return null
  const eg = e as Record<string, unknown>
  if (!oneOf(eg.mode, ['residential', 'direct'] as const)) return null
  if (!oneOf(eg.state, ['ok', 'failing', 'off'] as const)) return null
  const country = eg.country ?? null
  const lastError = eg.last_error ?? null
  if (!strOrNull(country) || !strOrNull(lastError)) return null
  return {
    tool_selected: r.tool_selected,
    tools_available: r.tools_available.filter(isTool),
    tool_last_used: lastUsed,
    jev: { enabled: jev.enabled, reason, key_scope: scope },
    egress: { mode: eg.mode, country, state: eg.state, last_error: lastError },
  }
}

const HOVER_DELAY_MS = 120 // components/Overlay.tsx TOOLTIP_DELAY_MS: the console's one hover delay

/** One badge and its card: shown on hover (after the delay) and at once on focus; Escape and leaving close it. */
function Badge({
  kind,
  tone,
  label,
  children,
}: {
  kind: string
  tone: 'plain' | 'ok' | 'warn' | 'note'
  label: string
  children: ReactNode
}) {
  const id = useId()
  const [open, setOpen] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const clear = () => {
    if (timer.current !== undefined) clearTimeout(timer.current)
    timer.current = undefined
  }
  useEffect(() => clear, [])
  const show = (now: boolean) => {
    clear()
    if (now) setOpen(true)
    else timer.current = setTimeout(() => setOpen(true), HOVER_DELAY_MS)
  }
  const hide = () => {
    clear()
    setOpen(false)
  }
  return (
    <span
      className={`bb-badge is-${tone}`}
      data-testid={`browser-badge-${kind}`}
      tabIndex={0}
      aria-describedby={id}
      onMouseEnter={() => show(false)}
      onMouseLeave={hide}
      onFocus={() => show(true)}
      onBlur={hide}
      onKeyDown={(ev) => {
        if (ev.key === 'Escape') hide()
      }}
    >
      <span className="bb-label">{label}</span>
      <span role="tooltip" id={id} className="bb-card" hidden={!open}>
        {children}
      </span>
    </span>
  )
}

function lastAction(s: LiveBrowserTools, now: number): string {
  const u = s.tool_last_used
  return u === null ? 'No browser action yet.' : `Last browser action: ${TOOL_LABEL[u.name]}, ${timeAgo(u.at, now)}.`
}

const JEV_WHY: Record<NonNullable<LiveBrowserTools['jev']['reason']>, string> = {
  no_key: 'Jev is off: there is no TypeSafe key for this agent. Add one in Config › Secrets › Keys for agents and MCP.',
  not_installed: 'Jev is off: this runner image does not carry it.',
  not_selected: 'Jev is off: it was not chosen for this agent.',
  no_route:
    'Jev is off: this workspace judges with your internal decision API, and this agent has no route to it (the tailnet egress is off). The agent uses Playwright.',
}

const CHANGE_TOOL = 'The tool is chosen with Browser tool on Submit when an agent starts; it does not change while the agent runs.'

/**
 * [Signed in as QA identity (console)] (lane QI-UI, docs/design/qa-identity.md
 * §4.6): the task's input named the QA identity, so the browser holds the QA
 * account's session on this console, not the submitter's. It is a fact of the
 * TASK, read off its input by the caller, so it is drawn whether or not the
 * live read carries a `browser` object.
 */
function QaIdentityBadge({ identity }: { identity: string }) {
  return (
    <Badge kind="qa-identity" tone="note" label={`Signed in as QA identity (${identity})`}>
      <span className="bb-line">
        This agent asked for the QA identity: the browser is signed in to this console as the QA account, not as the person who
        submitted it, and what it does here is done in the QA workspace.
      </span>
      <span className="bb-line">The platform adds the credential to the console’s own requests; the agent never holds it.</span>
      <span className="bb-line bb-how">
        It is chosen on Submit, and only a workspace a platform admin has granted (Admin › Tenants › QA identity) is offered it.
      </span>
    </Badge>
  )
}

/** The strip's badges, right-aligned after the control state; null and no QA identity renders nothing at all. */
export function BrowserBadges({
  status,
  now = Date.now(),
  qaIdentity = null,
}: {
  status: LiveBrowserTools | null
  now?: number
  /** The QA identity the task's input named (`qaIdentityOf`), or null. */
  qaIdentity?: string | null
}) {
  if (status === null)
    return qaIdentity === null ? null : (
      <span className="bb-row" data-testid="browser-badges">
        <QaIdentityBadge identity={qaIdentity} />
      </span>
    )
  const sel = status.tool_selected
  // Jev picked and off for a known reason: the attempt runs on Playwright.
  const jevFellBack = sel === 'jev' && !status.jev.enabled && status.jev.reason !== null
  const toolLabel =
    sel === 'auto'
      ? status.tool_last_used === null
        ? 'Auto'
        : `Auto: ${TOOL_LABEL[status.tool_last_used.name]}`
      : jevFellBack
        ? 'Jev off · Playwright'
        : TOOL_LABEL[sel]
  const available = status.tools_available.map((t) => TOOL_LABEL[t]).join(', ')

  const eg = status.egress
  const residential = eg.mode === 'residential'
  const country = eg.country !== null && eg.country.trim() !== '' ? eg.country.toUpperCase() : null
  const egressLabel = !residential
    ? 'Direct'
    : eg.state === 'failing'
      ? 'Proxy failing'
      : eg.state === 'off'
        ? 'Proxy off'
        : country === null
          ? 'Residential'
          : `Residential · ${country}`
  const egressTone = !residential ? 'plain' : eg.state === 'ok' ? 'ok' : 'warn'

  return (
    <span className="bb-row" data-testid="browser-badges">
      {qaIdentity !== null && <QaIdentityBadge identity={qaIdentity} />}
      <Badge kind="tool" tone={jevFellBack ? 'warn' : 'plain'} label={toolLabel}>
        <span className="bb-line">
          {sel === 'auto'
            ? 'Auto: the agent picks the browser tool for each step.'
            : `${TOOL_LABEL[sel]} was chosen for this agent.`}
        </span>
        <span className="bb-line">
          {available === '' ? 'No browser tool registered in this attempt.' : `Registered in this attempt: ${available}.`}
        </span>
        {sel === 'jev' && !status.jev.enabled && status.jev.reason !== null ? (
          <span className="bb-line">{JEV_WHY[status.jev.reason]}</span>
        ) : null}
        <span className="bb-line">{lastAction(status, now)}</span>
        <span className="bb-line bb-how">{CHANGE_TOOL}</span>
      </Badge>
      <Badge kind="egress" tone={egressTone} label={egressLabel}>
        <span className="bb-line">
          {!residential
            ? 'Direct: the browser leaves through the platform’s own address, not a residential proxy.'
            : eg.state === 'ok'
              ? `The browser’s traffic leaves through a residential proxy${country === null ? '' : ` in ${country}`}.`
              : eg.state === 'failing'
                ? 'The browser’s traffic should leave through a residential proxy, and the proxy is failing.'
                : 'This agent expected a residential proxy, and the proxy is off: its traffic is not proxied.'}
        </span>
        {eg.last_error !== null && eg.last_error.trim() !== '' ? (
          <span className="bb-line">Last error: {eg.last_error}</span>
        ) : null}
        <span className="bb-line">{lastAction(status, now)}</span>
        <span className="bb-line bb-how">
          Egress is set for the deployment by an administrator, not per agent (docs/browser-egress.md).
        </span>
      </Badge>
      {status.jev.enabled ? (
        <Badge kind="jev" tone="note" label="Jev">
          <span className="bb-line">Jev is on: it sends page text to TypeSafe.</span>
          <span className="bb-line">
            {status.jev.key_scope === 'user'
              ? 'It uses your own TypeSafe key.'
              : status.jev.key_scope === 'tenant'
                ? 'It uses the workspace’s TypeSafe key.'
                : 'It uses a TypeSafe key.'}
          </span>
          <span className="bb-line">{lastAction(status, now)}</span>
          <span className="bb-line bb-how">
            Keys live in Config › Secrets › Keys for agents and MCP; pick another tool with Browser tool on Submit.
          </span>
        </Badge>
      ) : null}
    </span>
  )
}
