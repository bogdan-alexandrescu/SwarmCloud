import { useEffect, useState, type ReactNode } from 'react'
import { Banner, Button, Card, Dash, EmptyState, LoadingState, Segmented, StatePill, Table, type Column } from './components'
import { usePhoneTables } from './capacityPoll'
import { STATE_MARK } from './marks'
import { QA_LINK_ADDRESS } from './paths'
import {
  SEVERITIES,
  dismissQaRoute,
  ended,
  loadLegDetail,
  loadQaRunGroup,
  loadTargetFacts,
  notStartedWhy,
  restoreQaRoute,
  type DismissedRoute,
  type Figure,
  type LegDetail,
  type NewRoute,
  type QaRunGroup,
  type QaRunLeg,
  type TargetFacts,
} from './qaRunGroupApi'
import { promoteQaScreen, type QaScreen, type QaTarget } from './qaTargetsApi'
import { useRoleGate } from './Profile'
import { UrCrumb, UrLink, UrRefresh, useUrRead, writeFailure } from './RepositoriesParts'
import { PageHead } from './Shell'
import type { TaskState } from './types'
import './styles/qarungroup.css'

/**
 * ONE QA DISPATCH, ONE COLUMN PER LEG (lane QAENV-UI-RUNPAGE, part of #2136;
 * docs/design/qa-environments.md §7; Config proposal E, S9b desktop and S9d
 * phone, BGD's pick 2026-10-24).
 *
 * `/qa-runs/<group_id>` is where the issues a leg files link (swarm_api
 * `qaspec._group_sentence`). It is drawn under Config › Repositories › QA
 * targets, as S9b's breadcrumb puts it, so the setup strip heads it like
 * every Config page.
 *
 * EACH COLUMN IS ONE LEG: its workflow and that workflow's state, linked to
 * its own page; its steps; screens captured, stored and new; §3.5's
 * coverage; findings by severity; issues filed; and the gate's refusals.
 * Every figure is read from the leg's own steps (qaRunGroupApi.ts), and one
 * not read yet is a dash saying why, never a zero.
 *
 * ABOVE THE COLUMNS, the routes one leg's `discovered` holds and another's
 * does not (§5 "Between legs"): a feature on staging not shipped to
 * production yet. That is information, never a finding, so it carries no
 * action.
 *
 * A LEG THAT NEVER STARTED says so, with why: a refusal's own sentence, or
 * §2.1's crash window between two submissions.
 *
 * BELOW THE COLUMNS, "New since last run" (S9b; QA-RUN-NEW-ROUTES, part of
 * #2157): the routes the legs found that the target does not store as
 * screens yet, each leg's `new_since_last_run` as the target serves it. A
 * workspace admin may Promote one, `promoteQaScreen`'s PATCH of `screens`
 * (§5: a run never writes `screens`; this is the one way a found route
 * enters it), and it leaves the table. Anyone else reads the table and the
 * role sentence, never a Promote the API would refuse.
 *
 * BESIDE PROMOTE, REMOVE (S9b; QA-RUN-REMOVE, part of #2157): a workspace
 * admin's `:dismiss-route`, which puts the route on the target's
 * `dismissed_routes`, so no leg's later run offers it again either. It leaves
 * the table at once. Below the table, "N dismissed · Show" opens the list,
 * each with Restore (`:restore-route`, an admin's), so a mistaken Remove is
 * one click to undo. A member reads the count and the list, never a button.
 *
 * ON A PHONE THE COLUMNS BECOME A SWITCH (S9d): one leg at a time, the
 * one-leg-only routes still above it.
 *
 * Proposal E leads every Config page with one next-action sentence and
 * button: here, the leg that did not start, else the leg still running, else
 * the QA targets the run came from. When the run found routes not stored
 * yet, a lead counting them goes above that one, and its button scrolls to
 * the table.
 */

/** How often the page reads again while a leg's workflow is still running. */
export const QA_RUN_POLL_MS = 30_000

const SEVERITY_WORD: Record<(typeof SEVERITIES)[number], string> = { blocker: 'blocker', major: 'major', minor: 'minor', nit: 'nit' }

function isTaskState(s: string | null): s is TaskState {
  return s !== null && Object.prototype.hasOwnProperty.call(STATE_MARK, s)
}

/** "23 Oct 21:12", in the reader's own time zone; null when the instant is not one. */
function when(at: string | null): string | null {
  if (at === null) return null
  const d = new Date(at)
  if (Number.isNaN(d.getTime())) return null
  const day = d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })
  const time = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false })
  return `${day} ${time}`
}

function workflowAddress(id: string): string {
  return `work/workflows?${new URLSearchParams({ wf: id }).toString()}`
}

/** A figure's value drawn, or its dash with why; its note (how much was read) after it. */
function Fig<T>({ f, block = false, children }: { f: Figure<T>; block?: boolean; children: (v: T) => ReactNode }) {
  if (f.kind === 'absent') {
    return (
      <span className="qr-absent">
        <Dash why={f.why} /> <span className="qr-why">{f.why}</span>
      </span>
    )
  }
  return (
    <>
      {children(f.value)}
      {f.note !== null && (block ? <span className="qr-note qr-note-line">{f.note}</span> : <span className="qr-note"> · {f.note}</span>)}
    </>
  )
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`
}

function LegFigures({ d }: { d: LegDetail }) {
  return (
    <>
      <dt>Steps</dt>
      <dd data-figure="steps">
        <Fig f={d.steps}>
          {(steps) => {
            const done = steps.filter((s) => ended(s.state)).length
            const running = steps.filter((s) => s.state === 'RUNNING').map((s) => s.step_id)
            return (
              <>
                {done} of {steps.length} ended
                {running.length > 0 && <span className="qr-note"> · running {running.join(', ')}</span>}
              </>
            )
          }}
        </Fig>
      </dd>
      <dt>Screens captured</dt>
      <dd data-figure="screens">
        <Fig f={d.screens}>{(v) => `${v.stored} stored · ${v.new} new`}</Fig>
      </dd>
      <dt>Coverage</dt>
      <dd data-figure="coverage">
        <Fig f={d.coverage} block>
          {(c) => (
            <span className="qr-lines">
              <span>
                {c.newPaths.length === 0 ? 'No new route captured' : `${plural(c.newPaths.length, 'new route')} captured: `}
                {c.newPaths.length > 0 && <span className="qr-paths">{c.newPaths.join(' · ')}</span>}
              </span>
              {c.overCap > 0 && <span>{plural(c.overCap, 'route')} found past the step&rsquo;s cap</span>}
              {c.notReached > 0 && <span>{plural(c.notReached, 'route')} found, not reached by the step that owns it</span>}
              <span className={c.stoppedBy.length > 0 ? 'qr-warn' : undefined}>
                {c.stoppedBy.length === 0
                  ? 'Every crawl finished'
                  : `A crawl stopped at its ${c.stoppedBy.join(' and ')} bound: a route it did not see may still be there`}
              </span>
            </span>
          )}
        </Fig>
      </dd>
      <dt>Findings</dt>
      <dd data-figure="findings">
        <Fig f={d.findings}>
          {(by) => {
            const named = SEVERITIES.filter((s) => by[s] > 0)
            return named.length === 0 ? 'None' : named.map((s) => `${by[s]} ${SEVERITY_WORD[s]}`).join(' · ')
          }}
        </Fig>
      </dd>
      <dt>Issues filed</dt>
      <dd data-figure="issues">
        <Fig f={d.issues}>{(n) => String(n)}</Fig>
      </dd>
      <dt>Gate refusals</dt>
      <dd data-figure="refusals">
        <Fig f={d.refusals}>{(n) => String(n)}</Fig>
      </dd>
    </>
  )
}

function LegState({ leg }: { leg: QaRunLeg }) {
  if (leg.workflow_id === null) {
    return (
      <span data-leg-state={leg.state} className="qr-notstarted">
        not started
      </span>
    )
  }
  const s = leg.workflow_state
  return (
    <span data-leg-state={s ?? 'unknown'}>
      {isTaskState(s) ? <StatePill state={s} small /> : <span className="qr-notstarted">state not read</span>}
    </span>
  )
}

function LegColumn({ leg, group, host, detail, off, go }: {
  leg: QaRunLeg
  group: QaRunGroup
  host: string | null
  detail: LegDetail | null
  off: boolean
  go: (to: string) => void
}) {
  return (
    <section className={`c-card qa-leg${off ? ' is-off' : ''}`} data-environment={leg.environment} aria-label={`${leg.environment} leg`}>
      <div className="c-card-h qr-leg-h">
        <h2>{leg.environment}</h2>
        <LegState leg={leg} />
      </div>
      {host !== null && <p className="qr-host">{host}</p>}
      {leg.workflow_id === null ? (
        <p className="qr-reason" data-leg-reason>
          {notStartedWhy(leg, group)}
        </p>
      ) : (
        <dl className="qr-kv">
          <dt>Workflow</dt>
          <dd>
            <UrLink to={workflowAddress(leg.workflow_id)} go={go} className="qr-wf">
              {leg.workflow_id}
            </UrLink>
          </dd>
          {detail === null ? (
            <>
              <dt>Steps</dt>
              <dd data-figure="steps" className="qr-why">Reading the leg&rsquo;s steps…</dd>
            </>
          ) : (
            <LegFigures d={detail} />
          )}
        </dl>
      )}
    </section>
  )
}

function OneLegOnlyCard({ facts, legs }: { facts: Figure<TargetFacts> | null; legs: readonly QaRunLeg[] }) {
  if (legs.length < 2) return null
  let body: ReactNode
  if (facts === null) body = <p className="qr-why">Reading the target&rsquo;s routes…</p>
  else if (facts.kind === 'absent') body = <p className="qr-absent"><Dash why={facts.why} /> <span className="qr-why">{facts.why}</span></p>
  else if (facts.value.oneLegOnly.length === 0) body = <p className="qr-why">Every route found is on every environment.</p>
  else {
    body = facts.value.oneLegOnly.map((o) => (
      <div className="qr-only" key={o.on} data-only-on={o.on}>
        <b>On {o.on}, not on {o.notOn.join(' or ')}</b>
        <span className="qr-paths">{o.paths.join(' · ')}</span>
      </div>
    ))
  }
  return (
    <section data-one-leg-only>
      <Card title="Seen on one environment only">
        <p className="qr-hint">From each environment&rsquo;s found routes: information about what is shipped where, never a finding.</p>
        {body}
      </Card>
    </section>
  )
}

const NEW_ROUTES_ID = 'qr-new-routes'

function NewRoutesLead({ count }: { count: number }) {
  if (count === 0) return null
  const review = () => document.getElementById(NEW_ROUTES_ID)?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
  return (
    <div data-new-routes-lead>
      <Banner tone="info" title={`${plural(count, 'route')} found that ${count === 1 ? 'is' : 'are'} not stored yet`}
        actions={<Button kind="primary" size="sm" onClick={review}>Review below</Button>}>
        New since last run. Promote adds a route to the target&rsquo;s stored screens, Remove stops offering it; admins only.
      </Banner>
    </div>
  )
}

function DismissedLine({ dismissed, admin, busy, onRestore }: {
  dismissed: readonly DismissedRoute[]
  admin: boolean
  /** What is being changed (`NewRoutesCard`'s `busy`), else null. */
  busy: string | null
  onRestore: (d: DismissedRoute) => void
}) {
  const [open, setOpen] = useState(false)
  if (dismissed.length === 0) return null
  return (
    <div className="qr-dismissed" data-dismissed>
      <p className="qr-hint qr-dismissed-line">
        {dismissed.length} dismissed &middot;{' '}
        <button type="button" className="qr-linkish" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          {open ? 'Hide' : 'Show'}
        </button>
      </p>
      {open && (
        <ul className="qr-dismissed-list" aria-label="Dismissed routes">
          {dismissed.map((d) => (
            <li key={d.id} data-dismissed-route={d.path}>
              <span className="qr-dismissed-tx">
                <code className="qr-route">{d.path}</code>
                <small className="qr-note">
                  {[d.by !== null ? `by ${d.by}` : null, when(d.at)].filter((x) => x !== null).join(', ') || 'Dismissed.'}
                </small>
              </span>
              {admin && (
                <Button size="sm" busy={busy === `restore:${d.id}`} disabled={busy !== null} onClick={() => onRestore(d)}
                  aria-label={`Restore ${d.path}`}>
                  Restore
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function NewRoutesCard({ routes, dismissed, admin, gate, canPromote, canRemove, busy, failed, onPromote, onRemove, onRestore }: {
  routes: readonly NewRoute[]
  /** The target's dismissed routes (S9b's Remove), shown on request below the table. */
  dismissed: readonly DismissedRoute[]
  /** The caller is a workspace admin (or the role is not read yet: the API decides). */
  admin: boolean
  /** The role sentence for anyone below admin, else null. */
  gate: string | null
  /** The target was read in a shape Promote's PATCH can be built from. */
  canPromote: boolean
  /** The group names its target, which Remove and Restore are posted to. */
  canRemove: boolean
  /** What is being changed: a path being promoted, `remove:<path>`, or `restore:<id>`; else null. */
  busy: string | null
  failed: string | null
  onPromote: (r: NewRoute) => void
  onRemove: (r: NewRoute) => void
  onRestore: (d: DismissedRoute) => void
}) {
  const phone = usePhoneTables()
  if (routes.length === 0 && dismissed.length === 0) return null
  const seen = (r: NewRoute) => (r.thisRun ? 'this run' : (when(r.firstSeen) ?? <Dash why="Its first sighting was not served." />))
  const acts = (r: NewRoute) => (
    <span className="qr-acts">
      {canPromote && (
        <Button kind="primary" size="sm" busy={busy === r.path} disabled={busy !== null} onClick={() => onPromote(r)}
          aria-label={`Promote ${r.path}`}>
          Promote
        </Button>
      )}
      {canRemove && (
        <Button size="sm" busy={busy === `remove:${r.path}`} disabled={busy !== null} onClick={() => onRemove(r)}
          aria-label={`Remove ${r.path}`}>
          Remove
        </Button>
      )}
    </span>
  )
  const columns: Column<NewRoute>[] = [
    { key: 'route', head: 'Route', cell: (r) => <code className="qr-route">{r.path}</code> },
    { key: 'on', head: 'Found on', cell: (r) => r.on.join(', ') },
    { key: 'seen', head: 'First seen', cell: seen },
  ]
  const act = admin && (canPromote || canRemove)
  if (act) columns.push({ key: 'act', head: <span className="qr-sr">Actions</span>, cell: acts })
  // S9d: below the phone breakpoint each route is a record (`.is-stacked`,
  // the People pattern): at 390 the four columns overflowed the card and cut
  // the row's actions to a sliver past its edge.
  const table = routes.length === 0 ? (
    <p className="qr-hint">Every route the last run found is stored or dismissed.</p>
  ) : phone ? (
    <div className="table-wrap is-stacked qr-new-wrap">
      <table className="qr-new-table" aria-label="New since last run">
        <thead>
          <tr>
            <th scope="col">Route</th>
            <th scope="col">Found on</th>
            <th scope="col">First seen</th>
            {act && <th scope="col"><span className="qr-sr">Actions</span></th>}
          </tr>
        </thead>
        <tbody>
          {routes.map((r) => (
            <tr key={r.path}>
              <th scope="row"><code className="qr-route">{r.path}</code></th>
              <td data-label="Found on">{r.on.join(', ')}</td>
              <td data-label="First seen">{seen(r)}</td>
              {act && <td data-label="Actions" className="qr-act-cell">{acts(r)}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  ) : (
    <Table caption="New since last run" columns={columns} rows={routes} rowKey={(r) => r.path} />
  )
  return (
    <section data-new-routes id={NEW_ROUTES_ID}>
      <Card title="New since last run">
        <p className="qr-hint">
          Routes a run found that the target does not store as screens yet. A stored screen is captured on every run;
          a removed route is not offered again.
        </p>
        {failed !== null && <Banner tone="bad" title="The route was not changed">{failed}</Banner>}
        {table}
        <DismissedLine dismissed={dismissed} admin={admin && canRemove} busy={busy} onRestore={onRestore} />
        {gate !== null && <p className="qr-hint qr-gate" data-role-gate="admin">{gate}</p>}
      </Card>
    </section>
  )
}

function Lead({ group, details, go }: { group: QaRunGroup; details: Record<string, LegDetail>; go: (to: string) => void }) {
  const idle = group.legs.find((l) => l.workflow_id === null)
  if (idle !== undefined) {
    return (
      <Banner tone="warn" title={`${idle.environment} did not start`}
        actions={<Button kind="primary" size="sm" onClick={() => go(QA_LINK_ADDRESS)}>Open QA targets</Button>}>
        Nothing of it ran, so it cost nothing; its column says why. Run it again from the target&rsquo;s card.
      </Banner>
    )
  }
  const running = group.legs.find((l) => !ended(l.workflow_state))
  if (running !== undefined && running.workflow_id !== null) {
    const steps = details[running.environment]?.steps
    const at = steps?.kind === 'value' ? `: ${steps.value.filter((s) => ended(s.state)).length} of ${steps.value.length} steps ended` : ''
    return (
      <Banner tone="info" title={`${running.environment} is still running${at}`}
        actions={<Button kind="primary" size="sm" onClick={() => go(workflowAddress(running.workflow_id!))}>Open its workflow</Button>}>
        Its figures fill in as its steps end; this page reads again every {QA_RUN_POLL_MS / 1000} s.
      </Banner>
    )
  }
  return (
    <Banner tone="info" title={group.legs.length === 1 ? 'The run ended' : `All ${group.legs.length} environments ended`}
      actions={<Button kind="primary" size="sm" onClick={() => go(QA_LINK_ADDRESS)}>Open QA targets</Button>}>
      Each column links its workflow, where every step&rsquo;s files are.
    </Banner>
  )
}

export function QaRunGroupScreen({ groupId, go }: { groupId: string; go: (to: string) => void }) {
  const read = useUrRead(() => loadQaRunGroup(groupId), `qa-run:${groupId}`)
  const group = read.state.status === 'ok' || read.state.status === 'stale' ? read.state.data : null
  const [details, setDetails] = useState<Record<string, LegDetail>>({})
  const [facts, setFacts] = useState<Figure<TargetFacts> | null>(null)
  const [shown, setShown] = useState<string | null>(null)
  // Promote and Remove: the routes stored or removed from this page, the one
  // being changed (a path, or a dismissed route's id), and why the last one was not.
  const [promoted, setPromoted] = useState<QaScreen[]>([])
  const [removed, setRemoved] = useState<string[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [routeFailed, setRouteFailed] = useState<string | null>(null)
  const [factsNonce, setFactsNonce] = useState(0)
  const adminGate = useRoleGate('admin', 'promote or remove a found route')

  // Each leg's figures once the group is read; again on every read of it.
  useEffect(() => {
    if (group === null) return
    let live = true
    for (const leg of group.legs) {
      if (leg.workflow_id === null) continue
      void loadLegDetail(leg.workflow_id, leg.environment).then((d) => {
        if (live) setDetails((was) => ({ ...was, [leg.environment]: d }))
      })
    }
    return () => {
      live = false
    }
  }, [group])

  // The target's facts: on every read of the group, and again after a Promote.
  useEffect(() => {
    if (group === null) return
    let live = true
    if (group.target_id !== null) {
      const workflows = Object.fromEntries(group.legs.flatMap((l) => (l.workflow_id === null ? [] : [[l.environment, l.workflow_id]])))
      void loadTargetFacts(group.target_id, group.legs.map((l) => l.environment), workflows).then((f) => {
        if (live) setFacts(f)
      })
    } else {
      setFacts({ kind: 'absent', why: 'The run group names no target.' })
    }
    return () => {
      live = false
    }
  }, [group, factsNonce])

  // While a leg runs, read again on a cadence; nothing reads once every leg has ended.
  const running = group !== null && group.legs.some((l) => l.workflow_id !== null && !ended(l.workflow_state))
  const reload = read.reload
  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(reload, QA_RUN_POLL_MS)
    return () => window.clearInterval(timer)
  }, [running, reload])

  const name = facts?.kind === 'value' ? facts.value.name : null
  const target = facts?.kind === 'value' ? facts.value.target : null
  // A route stored or removed from here leaves the table at once, whether or not the read after it has landed.
  const newRoutes = facts?.kind === 'value'
    ? facts.value.newRoutes.filter((r) => !promoted.some((p) => p.path === r.path) && !removed.includes(r.path))
    : []
  const dismissed = facts?.kind === 'value' ? facts.value.dismissed : []
  const targetId = group?.target_id ?? null

  async function promote(t: QaTarget, r: NewRoute) {
    setBusy(r.path)
    setRouteFailed(null)
    // The stored list as read, plus every route stored from here since: a second Promote keeps the first.
    const since = promoted.filter((p) => !t.screens.some((s) => s.id === p.id || s.path === p.path))
    const res = await promoteQaScreen({ ...t, screens: [...t.screens, ...since] }, { id: r.id, path: r.path })
    setBusy(null)
    if (res.status === 'error') {
      setRouteFailed(writeFailure(res.error))
      return
    }
    setPromoted((was) => [...was, { id: r.id, path: r.path }])
    setFactsNonce((n) => n + 1)
  }

  async function remove(id: string, r: NewRoute) {
    setBusy(`remove:${r.path}`)
    setRouteFailed(null)
    const res = await dismissQaRoute(id, r.id)
    setBusy(null)
    if (res.status === 'error') {
      setRouteFailed(writeFailure(res.error))
      return
    }
    setRemoved((was) => [...was, r.path])
    setFactsNonce((n) => n + 1)
  }

  async function restore(id: string, d: DismissedRoute) {
    setBusy(`restore:${d.id}`)
    setRouteFailed(null)
    const res = await restoreQaRoute(id, d.id)
    setBusy(null)
    if (res.status === 'error') {
      setRouteFailed(writeFailure(res.error))
      return
    }
    // Offered again: the read after the restore draws it where a leg's last run found it.
    setRemoved((was) => was.filter((p) => p !== d.path))
    setFactsNonce((n) => n + 1)
  }
  const hosts = facts?.kind === 'value' ? facts.value.hosts : {}
  const at = group !== null ? when(group.at) : null
  const legs = group?.legs ?? []
  const current = shown !== null && legs.some((l) => l.environment === shown) ? shown : legs[0]?.environment ?? null

  return (
    <div className="ur-page qr-page">
      <UrCrumb
        trail={[
          { label: 'Config' },
          { label: 'Repositories', to: 'config/repositories' },
          { label: 'QA targets', to: QA_LINK_ADDRESS },
          { label: name ?? 'QA run' },
        ]}
        go={go}
      />
      <PageHead title={at === null ? 'Run group' : `Run group · ${at}`}>
        <span className="ur-acts">
          <UrRefresh reads={[read]} />
        </span>
      </PageHead>
      {read.state.status === 'loading' && <LoadingState lines={4} label="Reading the run group…" />}
      {read.state.status === 'error' && (read.state.error.httpStatus === 404 ? (
        <EmptyState kind="failed" heading="No such run group">
          This workspace has no run group {groupId}. A run group of another workspace answers the same way.
        </EmptyState>
      ) : (
        <Banner tone="bad" title="The run group was not read" actions={<Button size="sm" onClick={read.reload}>Retry</Button>}>
          {read.state.error.message}
        </Banner>
      ))}
      {read.state.status === 'stale' && (
        <Banner tone="warn" title="Not refreshed">
          The last read failed: {read.state.error.message} What is drawn is the read before it.
        </Banner>
      )}
      {group !== null && (
        <>
          <NewRoutesLead count={newRoutes.length} />
          <Lead group={group} details={details} go={go} />
          <OneLegOnlyCard facts={facts} legs={legs} />
          {legs.length > 1 && current !== null && (
            <Segmented
              className="qr-switch"
              label="Environment"
              options={legs.map((l) => ({ key: l.environment, label: l.environment }))}
              value={current}
              onChange={setShown}
            />
          )}
          <div className="qr-cols" data-legs={legs.length}>
            {legs.map((leg) => (
              <LegColumn
                key={leg.environment}
                leg={leg}
                group={group}
                host={hosts[leg.environment] ?? null}
                detail={details[leg.environment] ?? null}
                off={leg.environment !== current}
                go={go}
              />
            ))}
          </div>
          <NewRoutesCard
            routes={newRoutes}
            dismissed={dismissed}
            admin={adminGate === null}
            gate={adminGate}
            canPromote={target !== null}
            canRemove={targetId !== null}
            busy={busy}
            failed={routeFailed}
            onPromote={(r) => {
              if (target !== null) void promote(target, r)
            }}
            onRemove={(r) => {
              if (targetId !== null) void remove(targetId, r)
            }}
            onRestore={(d) => {
              if (targetId !== null) void restore(targetId, d)
            }}
          />
          {group.by !== null && (
            <p className="qr-hint">
              Started by {group.by}
              {group.file === 'none' ? ', report only: no issues filed.' : '.'}
            </p>
          )}
        </>
      )}
    </div>
  )
}
