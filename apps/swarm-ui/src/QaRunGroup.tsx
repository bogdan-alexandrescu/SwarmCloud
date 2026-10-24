import { useEffect, useState, type ReactNode } from 'react'
import { Banner, Button, Card, Dash, EmptyState, LoadingState, Segmented, StatePill } from './components'
import { STATE_MARK } from './marks'
import { QA_LINK_ADDRESS } from './paths'
import {
  SEVERITIES,
  ended,
  loadLegDetail,
  loadQaRunGroup,
  loadTargetFacts,
  notStartedWhy,
  type Figure,
  type LegDetail,
  type QaRunGroup,
  type QaRunLeg,
  type TargetFacts,
} from './qaRunGroupApi'
import { UrCrumb, UrLink, UrRefresh, useUrRead } from './RepositoriesParts'
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
 * ON A PHONE THE COLUMNS BECOME A SWITCH (S9d): one leg at a time, the
 * one-leg-only routes still above it.
 *
 * Proposal E leads every Config page with one next-action sentence and
 * button: here, the leg that did not start, else the leg still running, else
 * the QA targets the run came from.
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

  // Each leg's figures, and the target's, once the group is read; again on every read of it.
  useEffect(() => {
    if (group === null) return
    let live = true
    for (const leg of group.legs) {
      if (leg.workflow_id === null) continue
      void loadLegDetail(leg.workflow_id, leg.environment).then((d) => {
        if (live) setDetails((was) => ({ ...was, [leg.environment]: d }))
      })
    }
    if (group.target_id !== null) {
      void loadTargetFacts(group.target_id, group.legs.map((l) => l.environment)).then((f) => {
        if (live) setFacts(f)
      })
    } else {
      setFacts({ kind: 'absent', why: 'The run group names no target.' })
    }
    return () => {
      live = false
    }
  }, [group])

  // While a leg runs, read again on a cadence; nothing reads once every leg has ended.
  const running = group !== null && group.legs.some((l) => l.workflow_id !== null && !ended(l.workflow_state))
  const reload = read.reload
  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(reload, QA_RUN_POLL_MS)
    return () => window.clearInterval(timer)
  }, [running, reload])

  const name = facts?.kind === 'value' ? facts.value.name : null
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
