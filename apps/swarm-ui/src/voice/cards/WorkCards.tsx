/**
 * VOICE IT'S WORK CARDS: work, review, plan, budget and stop
 * (docs/design/voice-it.md §4's table; section 10, phase two).
 *
 * Each is drawn from the catalogue's fields only (CardList.tsx hands a
 * renderer the body fields its kind names, nothing else), with the
 * console's own components: the card, chips, tags and the usage bar. A
 * field the card does not carry is not invented: it is left out, and a
 * figure that is missing is a dash with its reason, never a 0.
 *
 * ACTIONS. Phase two spends nothing, so the only actions drawn are links to
 * where the thing already lives: Open in Work (an agent or a workflow), Open
 * diff (the pull request on GitHub), Open where you act (a stop). A link
 * goes only to the console's own pages or to `https://github.com/`
 * (`githubUrl`); any other address is shown as text, never as a link.
 * Cancel, pause, merge, start, raise and the rest act, and are phase three.
 *
 * A WORK CARD IS LIVE. CardList.tsx hands it its agent's, workflow's or run's
 * latest `roster.update` row (`live`); the card's state chip, its "Now" and
 * its steps done of total follow that row as it changes, with no reload.
 */

import type { ReactNode } from 'react'

import { githubUrl } from '../../api'
import { workflowHref } from '../../agentlist'
import { ButtonLink, Card, Chip, Dash, Tag, UsageBar, type ChipTone } from '../../components'
import { inspectorHref } from '../../decision'
import type { CardKindSpec, CardState } from '../cards'
import type { CardProps, RosterRow, VoiceCard } from '../CardList'

// ---- reading the body, without trusting it ------------------------------------

export function str(v: unknown): string | null {
  return typeof v === 'string' && v.trim() !== '' ? v : null
}

export function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

export function list(v: unknown): unknown[] {
  return Array.isArray(v) ? v : []
}

export function obj(v: unknown): Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
}

/** Dollars as the meter says them: whole dollars plain, cents when there are any. */
export function usd(n: number): string {
  return Number.isInteger(n) ? `$${n}` : `$${n.toFixed(2)}`
}

/** A figure in dollars, or a dash with its reason. */
function Usd({ n, why }: { n: number | null; why: string }) {
  return n === null ? <Dash why={why} /> : <>{usd(n)}</>
}

// ---- the frame every card shares --------------------------------------------------

const STATE_TONE: Partial<Record<CardState, ChipTone>> = { acting: 'live', waiting: 'park', failed: 'bad' }

const STATE_WORD: Readonly<Record<CardState, string>> = {
  proposed: 'proposed',
  acting: 'acting',
  waiting: 'waiting',
  done: 'done',
  failed: 'failed',
  cancelled: 'cancelled',
  dismissed: 'dismissed',
  accepted: 'accepted',
}

/** A roster state as a card state; a queued row leaves the card's own. */
const LIVE_CARD_STATE: Readonly<Partial<Record<RosterRow['state'], CardState>>> = {
  running: 'acting',
  waiting: 'waiting',
  done: 'done',
  failed: 'failed',
  cancelled: 'cancelled',
}

const CREATOR_WORD: Readonly<Record<string, string>> = { planner: 'the planner', platform: 'the platform', person: 'you' }

function When({ iso }: { iso: string | null }) {
  if (iso === null || iso === '') return null
  const d = new Date(iso)
  return (
    <>
      {' · '}
      <time dateTime={iso} title={iso}>
        {Number.isFinite(d.getTime()) ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : iso}
      </time>
    </>
  )
}

/** The kind's action, when its kind allows it. */
export function allows(spec: CardKindSpec, action: string): boolean {
  return spec.actions.includes(action)
}

/**
 * One card: its kind and state, the title, the line the voice may read out,
 * the body, the links out, and who drew it when. An `<article>` named by its
 * title, so a reader can move card to card.
 */
export function VoiceCardFrame({
  card,
  spec,
  children,
  actions,
  live = null,
}: {
  card: VoiceCard
  spec: CardKindSpec
  children?: ReactNode
  actions?: ReactNode
  /** The roster row the card follows: its state chip moves with it. */
  live?: RosterRow | null
}) {
  const followed = live === null ? undefined : LIVE_CARD_STATE[live.state]
  const shown = followed ?? card.state
  const state = STATE_WORD[shown] !== undefined ? shown : null
  return (
    <article
      className="vt-card"
      data-kind={spec.kind}
      data-card-id={card.card_id}
      data-version={card.version}
      data-state={state ?? ''}
      data-live-state={live?.state}
      aria-label={`${spec.label}: ${card.title}`}
    >
      <Card
        level={3}
        title={card.title}
        action={
          <span className="vt-card-tags">
            <Tag>{spec.label}</Tag> {state !== null && <Chip tone={STATE_TONE[state]}>{STATE_WORD[state]}</Chip>}
          </span>
        }
        foot={
          <>
            Drawn by {CREATOR_WORD[card.created_by] ?? 'Voice it'}
            <When iso={card.at} />
          </>
        }
      >
        {str(card.summary) !== null && <p className="vt-card-summary">{card.summary}</p>}
        {children}
        {actions !== undefined && actions !== null && actions !== false && <p className="vt-card-actions">{actions}</p>}
      </Card>
    </article>
  )
}

// ---- where a card points ----------------------------------------------------------

/** The console page of an agent or a workflow, or null. */
export function workHref(targetKind: string | null, targetId: string | null): string | null {
  if (targetId === null) return null
  if (targetKind === 'agent') return inspectorHref(targetId)
  if (targetKind === 'workflow') return workflowHref(targetId)
  return null
}

/** Open in Work: the target's page, else the agent or workflow the card references. */
function openInWork(card: VoiceCard, body: Readonly<Record<string, unknown>>): string | null {
  const refs = obj(card.references)
  return (
    workHref(str(body.target_kind), str(body.target_id)) ??
    workHref('agent', str(refs.agent)) ??
    workHref('workflow', str(refs.workflow))
  )
}

/** A console address (`#work/...`) or a GitHub page; anything else is not linked. */
export function safeHref(v: unknown): string | null {
  const s = str(v)
  if (s === null) return null
  if (/^#[a-z][\w/-]*([?][\w=&%.-]*)?$/i.test(s)) return s
  return githubUrl(s)
}

/** What the card references, as chips: a link when the console or GitHub has a page for it. */
function References({ card }: { card: VoiceCard }) {
  const refs = obj(card.references)
  const rows: { key: string; label: string; href: string | null }[] = []
  for (const [key, label] of [
    ['agent', 'agent'],
    ['workflow', 'workflow'],
    ['run', 'run'],
    ['schedule', 'schedule'],
    ['pull_request', 'pull request'],
    ['issue', 'issue'],
  ] as const) {
    const v = str(refs[key])
    if (v === null) continue
    const href = key === 'agent' || key === 'workflow' ? workHref(key, v) : key === 'pull_request' || key === 'issue' ? githubUrl(v) : null
    rows.push({ key, label: `${label} ${href !== null && href.startsWith('https://') ? shortGithub(v) : v}`, href })
  }
  if (rows.length === 0) return null
  return (
    <p className="vt-card-refs">
      {rows.map((r) => (
        <Chip key={r.key} href={r.href ?? undefined}>
          {r.label}
        </Chip>
      ))}
    </p>
  )
}

/** `https://github.com/o/r/pull/12` as `o/r#12`. */
function shortGithub(url: string): string {
  const m = /^https:\/\/github\.com\/([^/]+\/[^/]+)\/(?:pull|issues)\/(\d+)/.exec(url)
  return m === null ? url : `${m[1]}#${m[2]}`
}

// ---- lanes (work and plan) ------------------------------------------------------------

function Lanes({ lanes, label }: { lanes: unknown[]; label: string }) {
  if (lanes.length === 0) return null
  return (
    <ul className="vt-card-lanes" aria-label={label}>
      {lanes.map((raw, i) => {
        const l = obj(raw)
        const territory = list(l.territory).map(str).filter((t): t is string => t !== null)
        const reservation = num(l.reservation_usd)
        const state = str(l.state)
        return (
          <li key={`${str(l.name) ?? ''}-${i}`}>
            <b>{str(l.name) ?? `Lane ${i + 1}`}</b>
            {reservation !== null && <span className="sb-note"> · {usd(reservation)} reserved</span>}
            {state !== null && <span className="sb-note"> · {state.toLowerCase()}</span>}
            {territory.length > 0 && (
              <span className="vt-card-territory">
                {' '}
                {territory.map((t) => (
                  <code key={t} className="mono">
                    {t}
                  </code>
                ))}
              </span>
            )}
          </li>
        )
      })}
    </ul>
  )
}

// ---- work -------------------------------------------------------------------------

const TARGET_WORD: Readonly<Record<string, string>> = { issue: 'Issue', run: 'Issue run', agent: 'Agent', workflow: 'Workflow', schedule: 'Schedule' }

const LIVE_WORD: Readonly<Record<RosterRow['state'], string>> = {
  queued: 'queued',
  running: 'running',
  waiting: 'waiting on you',
  done: 'done',
  failed: 'failed',
  cancelled: 'cancelled',
}

export function WorkCard({ card, spec, body, live: row = null }: CardProps) {
  const target = str(body.target_kind)
  const targetId = str(body.target_id)
  const repo = str(body.repository)
  const live = row !== null ? LIVE_WORD[row.state] : str(body.live_state)?.toLowerCase() ?? null
  const steps = row !== null && row.done !== null && row.total !== null ? `${row.done} of ${row.total}` : null
  const href = allows(spec, 'open_in_work') ? openInWork(card, body) : null
  return (
    <VoiceCardFrame
      card={card}
      spec={spec}
      live={row}
      actions={href !== null && <ButtonLink size="sm" href={href}>Open in Work</ButtonLink>}
    >
      <dl className="vt-card-facts">
        {target !== null && (
          <>
            <dt>{TARGET_WORD[target] ?? target}</dt>
            <dd className="mono">{targetId ?? <Dash why="not filed yet" />}</dd>
          </>
        )}
        {repo !== null && (
          <>
            <dt>Repository</dt>
            <dd className="mono">{repo}</dd>
          </>
        )}
        {live !== null && (
          <>
            <dt>Now</dt>
            <dd>{live}</dd>
          </>
        )}
        {steps !== null && (
          <>
            <dt>{row?.kind === 'run' ? 'Lanes merged' : 'Steps'}</dt>
            <dd>{steps}</dd>
          </>
        )}
      </dl>
      <Lanes lanes={list(body.lanes)} label="Lanes" />
      <References card={card} />
    </VoiceCardFrame>
  )
}

// ---- review -----------------------------------------------------------------------

const CHECKS_WORD: Readonly<Record<string, string>> = { pending: 'checks running', passing: 'checks pass', failing: 'checks fail', none: 'no checks' }
const CHECKS_TONE: Readonly<Record<string, ChipTone | undefined>> = { pending: 'park', failing: 'bad' }
const VERDICT_WORD: Readonly<Record<string, string>> = { merge: 'review: merge', fix: 'review: fix first', hold: 'review: hold' }
const MERGE_WORD: Readonly<Record<string, string>> = { open: 'open', merged: 'merged', closed: 'closed unmerged' }

export function ReviewCard({ card, spec, body }: CardProps) {
  const checks = str(body.checks)
  const verdict = str(body.verdict)
  const merge = str(body.merge_state)
  const release = str(body.release_state)
  const pr = githubUrl(obj(card.references).pull_request)
  const diff = allows(spec, 'open_diff') && pr !== null ? `${pr.replace(/\/$/, '')}/files` : null
  return (
    <VoiceCardFrame card={card} spec={spec} actions={diff !== null && <ButtonLink size="sm" href={diff} target="_blank" rel="noopener noreferrer">Open diff</ButtonLink>}>
      {str(body.change_summary) !== null && <p className="vt-card-text">{body.change_summary as string}</p>}
      <p className="vt-card-refs">
        {checks !== null && <Chip tone={CHECKS_TONE[checks]}>{CHECKS_WORD[checks] ?? checks}</Chip>}
        {verdict !== null ? <Chip tone={verdict === 'merge' ? undefined : 'park'}>{VERDICT_WORD[verdict] ?? verdict}</Chip> : <Chip faint>not reviewed yet</Chip>}
        {merge !== null && <Chip>{MERGE_WORD[merge] ?? merge}</Chip>}
        {release !== null && <Chip>{release}</Chip>}
      </p>
      <References card={card} />
    </VoiceCardFrame>
  )
}

// ---- plan -------------------------------------------------------------------------

const SIZE_WORD: Readonly<Record<string, string>> = {
  answer: 'an answer',
  one_fix: 'one fix',
  feature: 'a feature',
  rescue: 'a rescue',
  approvals_walk: 'an approvals walk',
  product: 'a product',
}

/** A total against a cap, in the console's usage bar. */
function AgainstCap({ label, used, cap }: { label: string; used: number | null; cap: number | null }) {
  return <UsageBar label={label} used={used} ceiling={cap} unit="USD" why="not counted" />
}

export function PlanCard({ card, spec, body }: CardProps) {
  const size = str(body.size)
  const reserved = num(body.reserved_usd)
  const waves = list(body.waves)
  return (
    <VoiceCardFrame card={card} spec={spec}>
      {str(body.goal) !== null && <p className="vt-card-text">{body.goal as string}</p>}
      {size !== null && (
        <p className="vt-card-refs">
          <Chip>{SIZE_WORD[size] ?? size}</Chip>
          <Chip>
            {waves.length} {waves.length === 1 ? 'wave' : 'waves'}
          </Chip>
          <Chip>
            <Usd n={reserved} why="no reservation yet" /> reserved
          </Chip>
        </p>
      )}
      {waves.length > 0 && (
        <ol className="vt-card-waves" aria-label="Waves">
          {waves.map((raw, i) => {
            const w = obj(raw)
            return (
              <li key={`${str(w.name) ?? ''}-${i}`}>
                <b>{str(w.name) ?? `Wave ${i + 1}`}</b>
                <Lanes lanes={list(w.lanes)} label={`${str(w.name) ?? `Wave ${i + 1}`}: lanes`} />
              </li>
            )
          })}
        </ol>
      )}
      <AgainstCap label="Reserved against the session cap" used={reserved} cap={num(body.session_cap_usd)} />
      <AgainstCap label="Reserved against the day cap" used={reserved} cap={num(body.day_cap_usd)} />
    </VoiceCardFrame>
  )
}

// ---- budget -----------------------------------------------------------------------

export function BudgetCard({ card, spec, body }: CardProps) {
  const spent = num(body.spent_usd)
  const reserved = num(body.reserved_usd)
  const both = spent !== null && reserved !== null ? spent + reserved : null
  const lines = list(body.lines).map(obj)
  const notCounted = list(body.not_counted).map(str).filter((s): s is string => s !== null)
  return (
    <VoiceCardFrame card={card} spec={spec}>
      <AgainstCap label="Spent and reserved against the session cap" used={both} cap={num(body.session_cap_usd)} />
      <AgainstCap label="Spent and reserved against the day cap" used={both} cap={num(body.day_cap_usd)} />
      {lines.length > 0 && (
        <div className="c-tbl vt-card-lines">
          <table>
            <thead>
              <tr>
                <th scope="col">Line</th>
                <th scope="col" className="is-num">
                  Spent
                </th>
                <th scope="col" className="is-num">
                  Reserved
                </th>
              </tr>
            </thead>
            <tbody>
              {lines.map((l, i) => (
                <tr key={`${str(l.label) ?? ''}-${i}`}>
                  <td>{str(l.label) ?? `Line ${i + 1}`}</td>
                  <td className="is-num">
                    <Usd n={num(l.spent_usd)} why="not counted" />
                  </td>
                  <td className="is-num">
                    <Usd n={num(l.reserved_usd)} why="not counted" />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {notCounted.length > 0 && <p className="sb-note">Not counted here: {notCounted.join('; ')}.</p>}
    </VoiceCardFrame>
  )
}

// ---- stop -------------------------------------------------------------------------

/** Which rule held the work, in words, keyed by the catalogue's `rule` values. */
const RULE_WORD: ReadonlyMap<string, string> = new Map([
  ['iam', 'an IAM change'],
  ['secrets', 'something stored in Secret Manager'],
  ['tenancy', 'another workspace'],
  ['shared_project', 'the shared project'],
  ['workflow_file', 'a workflow file'],
])

export function StopCard({ card, spec, body }: CardProps) {
  const rule = str(body.rule)
  const where = str(body.where)
  const href = allows(spec, 'open_where') ? safeHref(where) : null
  const outside = href !== null && href.startsWith('https://')
  return (
    <VoiceCardFrame
      card={card}
      spec={spec}
      actions={
        href !== null && (
          <ButtonLink size="sm" href={href} {...(outside ? { target: '_blank', rel: 'noopener noreferrer' } : {})}>
            Open where you act
          </ButtonLink>
        )
      }
    >
      {str(body.what) !== null && <p className="vt-card-text">{body.what as string}</p>}
      {rule !== null && (
        <p className="vt-card-refs">
          <Chip tone="park">held: {RULE_WORD.get(rule) ?? rule}</Chip>
        </p>
      )}
      {where !== null && href === null && (
        <p className="sb-note">
          Where you act: <span className="mono">{where}</span>
        </p>
      )}
      <References card={card} />
    </VoiceCardFrame>
  )
}
