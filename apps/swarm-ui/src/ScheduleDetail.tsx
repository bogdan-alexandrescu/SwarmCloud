/**
 * ONE SCHEDULE (docs/schedules.md §6.2, variant A: tabs). History is the
 * first tab and the bare address, because run history is the reason to open
 * the page; Budget, Gate, Settings and Audit follow, Audit and Settings last
 * because they are long and rarely read.
 *
 * THE GATE CARD CARRIES THE SD3 SWITCH (§4.2; owner decision 2026-10-08):
 * "asks before merge" or "fully automatic", for a type whose floor allows
 * `merge: auto` (today `issue-sweep`). It posts to its own audited route,
 * `:merge-mode`, never to the edit, which refuses `merge: auto` outright. The
 * API decides who may switch -- a platform admin in a `platform: true`
 * repository, otherwise any member but the gate's last editor, and in a
 * one-person tenant the person after typing the schedule's name -- and the
 * card says each refusal in place. The hard stops are listed as what the
 * switch cannot touch, because they are.
 */
import { useEffect, useState } from 'react'
import {
  deleteSchedule,
  loadSchedule,
  loadScheduleAudit,
  loadScheduleTypes,
  pauseSchedule,
  resumeSchedule,
  runScheduleNow,
  setMergeMode,
  type Schedule,
  type ScheduleAuditEntry,
  type ScheduleFiring,
  type ScheduleType,
} from './api'
import { Banner, Button, Card, Tabs, TypedConfirm } from './components'
import { Dash } from './components/Chip'
import type { ApiError, Result } from './fetch'
import { addressToPath } from './paths'
import { Absent } from './primitives'
import { LastRun, RoutedLink, SCHEDULES, SpendCell, scheduleAddress, stateWord, whenIn } from './Schedules'
import { Screen } from './Shell'
import { pluralise, timeAgo } from './types'
import { useNow } from './useNow'

const TABS = ['history', 'budget', 'gate', 'settings', 'audit'] as const
type DetailTab = (typeof TABS)[number]
const TAB_LABEL: Readonly<Record<DetailTab, string>> = { history: 'History', budget: 'Budget', gate: 'Gate', settings: 'Settings', audit: 'Audit' }

/** The hard stops of §4.4, which no gate and no switch turns off. */
export const HARD_STOPS: readonly string[] = [
  'changes under .github/workflows/',
  'IAM and Terraform bootstrap',
  'the frozen contract',
  'security-class plans',
  'budget exhausted, and a run over budget',
  'auto-pause',
]

function refusal(e: ApiError): string {
  return e.code === null ? e.message : `${e.message} (${e.code})`
}

export function ScheduleDetail({ id, tab, go }: { id: string; tab: string | null; go: (to: string) => void }) {
  const at: DetailTab = (TABS as readonly string[]).includes(tab ?? '') ? (tab as DetailTab) : 'history'
  const [reloads, setReloads] = useState(0)
  const [name, setName] = useState<string | null>(null)
  // A local, so the nav-heading test reads this as one schedule's page.
  const pageTitle = name ?? id
  return (
    <div className="au-detail">
      <RoutedLink to={SCHEDULES} go={go} className="rn-back">
        ‹ All schedules
      </RoutedLink>
      <Screen key={`sch:${id}:${reloads}`} title={pageTitle} load={() => loadSchedule(id)} summary={(d) => `${d.schedule.type} · ${d.schedule.schedule_id} · owned by ${d.schedule.owner || '—'}`}>
        {(d) => (
          <DetailBody
            schedule={d.schedule}
            firings={d.firings}
            tab={at}
            go={go}
            onName={setName}
            reread={() => setReloads((n) => n + 1)}
          />
        )}
      </Screen>
    </div>
  )
}

function DetailBody({
  schedule: s,
  firings,
  tab,
  go,
  onName,
  reread,
}: {
  schedule: Schedule
  firings: ScheduleFiring[]
  tab: DetailTab
  go: (to: string) => void
  onName: (name: string) => void
  reread: () => void
}) {
  useEffect(() => onName(s.name), [s.name])
  const tabs = TABS.map((t) => {
    const to = t === 'history' ? scheduleAddress(s.schedule_id) : scheduleAddress(s.schedule_id, { tab: t })
    return { key: t, label: TAB_LABEL[t], href: addressToPath(to), to }
  })
  return (
    <>
      <Actions s={s} go={go} reread={reread} />
      <Tabs
        className="au-tabs"
        label="Views of this schedule"
        current={tab}
        tabs={tabs.map(({ key, label, href }) => ({ key, label, href }))}
        onGo={(h) => go(tabs.find((t) => t.href === h)?.to ?? scheduleAddress(s.schedule_id))}
      />
      {tab === 'history' && <History s={s} firings={firings} />}
      {tab === 'budget' && <Budget s={s} />}
      {tab === 'gate' && <Gate s={s} reread={reread} />}
      {tab === 'settings' && <Settings s={s} go={go} />}
      {tab === 'audit' && <Audit id={s.schedule_id} />}
    </>
  )
}

/** Pause or resume, run now, a dry run, and edit. Each refusal is said in place. */
function Actions({ s, go, reread }: { s: Schedule; go: (to: string) => void; reread: () => void }) {
  const [busy, setBusy] = useState<string | null>(null)
  const [said, setSaid] = useState<{ tone: 'info' | 'error'; text: string } | null>(null)
  async function act(what: string, call: () => Promise<Result<unknown>>, done: string) {
    setBusy(what)
    setSaid(null)
    const r = await call()
    setBusy(null)
    if (r.status === 'error') {
      setSaid({ tone: 'error', text: refusal(r.error) })
      return
    }
    setSaid({ tone: 'info', text: done })
    reread()
  }
  const paused = s.state !== 'enabled'
  return (
    <div className="au-acts">
      <span className={`au-state is-${s.state}`}>{stateWord(s.state)}</span>
      {s.pause !== null && s.pause !== undefined && s.state !== 'enabled' && (
        <span className="au-dim">
          by {s.pause.by ?? '—'}
          {s.pause.reason ? `: ${s.pause.reason}` : ''}
        </span>
      )}
      <span className="au-grow" />
      {s.state === 'disabled' ? (
        <span className="au-dim">Only an admin may re-enable it.</span>
      ) : paused ? (
        <Button size="sm" busy={busy === 'resume'} onClick={() => void act('resume', () => resumeSchedule(s.schedule_id), 'Resumed.')}>
          Resume
        </Button>
      ) : (
        <Button size="sm" busy={busy === 'pause'} onClick={() => void act('pause', () => pauseSchedule(s.schedule_id, ''), 'Paused.')}>
          Pause
        </Button>
      )}
      <Button size="sm" busy={busy === 'dry'} onClick={() => void act('dry', () => runScheduleNow(s.schedule_id, true), 'A dry run was made; it creates nothing.')}>
        Dry run
      </Button>
      <Button size="sm" busy={busy === 'run'} onClick={() => void act('run', () => runScheduleNow(s.schedule_id, false), 'A run-now firing was made, under the gate.')}>
        Run now
      </Button>
      <Button size="sm" kind="primary" onClick={() => go(scheduleAddress(s.schedule_id, { page: 'edit' }))}>
        Edit
      </Button>
      {said !== null && (
        <div className="au-said" role={said.tone === 'error' ? 'alert' : 'status'}>
          {said.text}
        </div>
      )}
    </div>
  )
}

function History({ s, firings }: { s: Schedule; firings: ScheduleFiring[] }) {
  const now = useNow()
  if (firings.length === 0) {
    return (
      <Absent kind="zero" heading="This schedule has not fired" say="No firing yet: a real zero, read from the API">
        {s.state === 'enabled' ? `The next is ${whenIn(s.next_run_at, s.timezone) ?? 'not yet set'} (${s.timezone}).` : `It is ${stateWord(s.state).toLowerCase()}, so it makes no firings.`}
      </Absent>
    )
  }
  return (
    <div className="au-table-wrap">
      <table className="au-table">
        <thead>
          <tr>
            <th scope="col">Slot</th>
            <th scope="col">Trigger</th>
            <th scope="col">State</th>
            <th scope="col">Outcome</th>
            <th scope="col">Work</th>
            <th scope="col">Cost</th>
          </tr>
        </thead>
        <tbody>
          {firings.map((f) => (
            <tr key={f.firing_id} className="au-row">
              <td data-label="Slot">
                {whenIn(f.slot ?? f.fired_at, s.timezone) ?? <Dash why="not served: the firing carries no slot" />}
                {f.fired_at !== null && <span className="au-dim"> · {timeAgo(f.fired_at, now)}</span>}
              </td>
              <td data-label="Trigger">{f.trigger}</td>
              <td data-label="State">{f.state}</td>
              <td data-label="Outcome">
                {f.outcome ?? (f.skip ? `skipped: ${f.skip.code}` : <span className="au-dim">not ended</span>)}
              </td>
              <td data-label="Work">
                {f.work === undefined || f.work.length === 0 ? <span className="au-dim">nothing created</span> : <WorkLinks work={f.work} />}
              </td>
              <td data-label="Cost">
                <FiringCost f={f} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function WorkLinks({ work }: { work: NonNullable<ScheduleFiring['work']> }) {
  return (
    <span className="au-work">
      {work.map((w) => {
        const path = w.kind === 'issue_run' ? `/runs/${encodeURIComponent(w.id)}` : w.kind === 'workflow' ? `/workflows/${encodeURIComponent(w.id)}` : w.kind === 'task' ? `/agents/recent/${encodeURIComponent(w.id)}` : null
        return path === null ? (
          <span key={w.id} className="mono">
            {w.kind} {w.id}
          </span>
        ) : (
          <a key={w.id} className="ctl-link mono" href={path}>
            {w.id}
          </a>
        )
      })}
    </span>
  )
}

function FiringCost({ f }: { f: ScheduleFiring }) {
  const c = f.cost
  if (c === null || c === undefined || typeof c.reported_usd !== 'number') return <Dash why="not recorded: the firing carries no cost yet" />
  const n = c.unreported_attempts ?? 0
  return <SpendCell spend={{ day: '', reported_usd: c.reported_usd, unreported_attempts: n, coverage: n > 0 ? 'partial' : 'complete' }} />
}

function money(v: number | null | undefined): string | null {
  return typeof v === 'number' ? `$${v.toFixed(2)}` : null
}

function Budget({ s }: { s: Schedule }) {
  const b = s.budget ?? {}
  return (
    <Card title="Budget">
      <dl className="au-dl">
        <dt>Spend today ({s.spend_today?.day ?? 'today'})</dt>
        <dd>
          <SpendCell spend={s.spend_today} />
        </dd>
        <dt>Per run</dt>
        <dd>{money(b.per_run_usd) ?? <Dash why="not set on this schedule" />}</dd>
        <dt>Per day</dt>
        <dd>{money(b.per_day_usd) ?? <Dash why="not set on this schedule" />}</dd>
        <dt>At once</dt>
        <dd>{typeof b.max_concurrent === 'number' ? b.max_concurrent : <Dash why="not set on this schedule" />}</dd>
      </dl>
      <p className="au-dim">
        A budget is checked against reported spend. An attempt whose cost was never reported is not counted as zero: it makes today’s figure a floor, marked partial.
      </p>
    </Card>
  )
}

const GATE_WORDS: Readonly<Record<string, string>> = { auto: 'automatic', approve: 'asks first', off: 'never merges' }

function approverWords(a: Schedule['gate']['approvers']): string {
  if (Array.isArray(a)) return a.join(', ')
  return a === 'owner_only' ? 'the platform owner only' : 'any member of this tenant'
}

function Gate({ s, reread }: { s: Schedule; reread: () => void }) {
  const [types, setTypes] = useState<ScheduleType[] | null>(null)
  useEffect(() => {
    let live = true
    void loadScheduleTypes().then((r) => {
      if (live) setTypes(r.status === 'ok' || r.status === 'stale' ? r.data.types : [])
    })
    return () => {
      live = false
    }
  }, [])
  const g = s.gate
  const entry = types?.find((t) => t.name === s.type) ?? null
  return (
    <>
      <Card title="Gate">
        <dl className="au-dl">
          <dt>Each run</dt>
          <dd>{GATE_WORDS[g.run] ?? g.run}</dd>
          <dt>Each plan</dt>
          <dd>{GATE_WORDS[g.plan] ?? g.plan}</dd>
          <dt>Merge</dt>
          <dd>{GATE_WORDS[g.merge] ?? g.merge}</dd>
          <dt>Who approves</dt>
          <dd>{approverWords(g.approvers)}</dd>
          <dt>An approval waits</dt>
          <dd>{typeof g.approval_ttl_hours === 'number' ? `${g.approval_ttl_hours} h, then expires` : <Dash why="not served" />}</dd>
        </dl>
      </Card>
      <MergeSwitch s={s} floorAllowsAuto={entry === null ? null : entry.floor_gate.merge === 'auto'} reread={reread} />
    </>
  )
}

/**
 * The SD3 switch. `floorAllowsAuto` is null until the catalogue is read: the
 * card then says it is reading rather than offering a switch the type may
 * refuse.
 */
export function MergeSwitch({ s, floorAllowsAuto, reread }: { s: Schedule; floorAllowsAuto: boolean | null; reread: () => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const [confirming, setConfirming] = useState(false)
  const auto = s.gate.merge === 'auto'
  const personal = s.tenant_id.startsWith('u-')

  async function send(mode: 'approve' | 'auto', confirm?: string) {
    setBusy(true)
    setError(null)
    const r = await setMergeMode(s.schedule_id, mode, s.revision, confirm)
    setBusy(false)
    if (r.status === 'error') {
      // A one-person tenant switches after typing the name (§4.2).
      if (r.error.code === 'confirmation_required') {
        setConfirming(true)
        return
      }
      setConfirming(false)
      setError(r.error)
      return
    }
    setConfirming(false)
    reread()
  }

  return (
    <Card title="Merging" className="au-switch">
      {floorAllowsAuto === null ? (
        <p className="au-dim">Reading whether this type may merge unattended…</p>
      ) : !floorAllowsAuto && !auto ? (
        <p>
          {s.type} never merges unattended: its floor keeps merging at “{GATE_WORDS[s.gate.merge] ?? s.gate.merge}”.
        </p>
      ) : (
        <>
          <p>
            Now: <b>{auto ? 'fully automatic' : 'asks before merge'}</b>.{' '}
            {auto
              ? 'A green pull request with a MERGE review verdict merges without asking.'
              : 'A green, reviewed pull request waits in Approvals before it merges.'}
          </p>
          <div className="au-acts">
            {auto ? (
              <Button size="sm" busy={busy} onClick={() => void send('approve')}>
                Ask before merge
              </Button>
            ) : (
              <Button size="sm" kind="danger" busy={busy} onClick={() => void send('auto')}>
                Switch to fully automatic
              </Button>
            )}
          </div>
          <p className="au-dim">
            {personal
              ? 'You are the only member of this workspace, so no second person can switch it: you type the schedule’s name instead, and the audit records it.'
              : 'Switching to automatic is audited and needs a member other than the last editor of this gate, scope or spec; in a platform repository only a platform admin may. Switching back is always allowed.'}
          </p>
        </>
      )}
      <p className="au-dim">
        Neither setting turns off the hard stops: {HARD_STOPS.join(', ')}. A run held by one waits for its approver whatever this says.
      </p>
      {error !== null && (
        <Banner tone="bad" title="The switch was refused">
          {refusal(error)}
        </Banner>
      )}
      {confirming && (
        <TypedConfirm
          title="Switch to fully automatic"
          name={s.name}
          verb="Switch"
          keep="Keep asking"
          busy={busy}
          onConfirm={() => void send('auto', s.name)}
          onClose={() => setConfirming(false)}
        >
          Pull requests from this schedule will merge without asking once green and reviewed MERGE.
        </TypedConfirm>
      )}
    </Card>
  )
}

function Settings({ s, go }: { s: Schedule; go: (to: string) => void }) {
  const [deleting, setDeleting] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  async function remove() {
    setBusy(true)
    setError(null)
    const r = await deleteSchedule(s.schedule_id, s.name)
    setBusy(false)
    if (r.status === 'error') {
      setError(refusal(r.error))
      return
    }
    go(SCHEDULES)
  }
  const p = s.policy ?? {}
  return (
    <>
      <Card title="Settings">
        <dl className="au-dl">
          <dt>Cron</dt>
          <dd>
            <span className="mono">{s.cron}</span> · {s.words ?? '—'} · {s.timezone}
          </dd>
          <dt>Scope</dt>
          <dd>{s.scope.mode === 'all' ? 'all repositories, as registered when it fires' : pluralise((s.scope.repo_ids ?? []).length, 'repository')}</dd>
          <dt>Overlap</dt>
          <dd>{p.overlap ?? '—'}</dd>
          <dt>After an outage</dt>
          <dd>{p.catch_up ?? '—'}</dd>
          <dt>Jitter</dt>
          <dd>{p.jitter === undefined ? '—' : p.jitter ? 'on' : 'off'}</dd>
          <dt>Dry run only</dt>
          <dd>{p.dry_run === undefined ? '—' : p.dry_run ? 'yes' : 'no'}</dd>
          <dt>Owner</dt>
          <dd>{s.owner}</dd>
          <dt>Last run</dt>
          <dd>
            <LastRun s={s} now={Date.now()} />
          </dd>
          <dt>Revision</dt>
          <dd>{s.revision}</dd>
        </dl>
        <details className="au-params">
          <summary>Parameters</summary>
          <pre className="mono">{JSON.stringify(s.params ?? {}, null, 2)}</pre>
        </details>
      </Card>
      <Card title="Delete">
        <p>Deleting removes the schedule. Its firings and audit stay, and work it already made is not cancelled.</p>
        <Button kind="danger" size="sm" onClick={() => setDeleting(true)}>
          Delete schedule
        </Button>
        {deleting && (
          <TypedConfirm title="Delete this schedule" name={s.name} verb="Delete" keep="Keep it" busy={busy} error={error ?? undefined} onConfirm={() => void remove()} onClose={() => setDeleting(false)}>
            It stops firing at once. Nothing it already created is cancelled.
          </TypedConfirm>
        )}
      </Card>
    </>
  )
}

function Audit({ id }: { id: string }) {
  const now = useNow()
  const [state, setState] = useState<Result<{ audit: ScheduleAuditEntry[] }>>({ status: 'loading', since: Date.now() })
  useEffect(() => {
    let live = true
    void loadScheduleAudit(id).then((r) => {
      if (live) setState(r)
    })
    return () => {
      live = false
    }
  }, [id])
  if (state.status === 'loading') return <p className="au-dim">Reading the audit…</p>
  if (state.status === 'error') {
    return (
      <Absent kind="failed" heading="The audit was not read" say={`Not read: ${state.error.message}`}>
        {refusal(state.error)}
      </Absent>
    )
  }
  const rows = state.status === 'empty' ? [] : state.data.audit
  if (rows.length === 0) return <Absent kind="zero" heading="Nothing audited yet" say="No audit entry: a real zero, read from the API" />
  return (
    <ol className="au-audit">
      {rows.map((a, i) => (
        <li key={`${a.at}:${i}`}>
          <b>{a.action}</b> by {a.by} <span className="au-dim">· {timeAgo(a.at, now)}</span>
        </li>
      ))}
    </ol>
  )
}
