import type { ReactNode } from 'react'
import { loadRepositories, loadRepositoryIndex, queryRepositoryImpact } from './api'
import { Card, Chip, Dash, ToneMark } from './components'
import type { ApiError, Result } from './fetch'
import { fmt, fullSuiteWords, type ImpactPlan } from './RepoGraphData'
import { freshness, notServed, pct, repoName, shortSha, type IndexDoc, type RepoRecord } from './RepositoriesData'
import { UrFreshPill, UrLink, UrNavButton, repoAddress, useUrRead } from './RepositoriesParts'
import type { IssueRun } from './types'
import './styles/repograph.css'

/**
 * AN ISSUE RUN AND ITS REPOSITORY'S INDEX: the Context card above the plan
 * (repositories.html screen 5, pick B) and the selected-tests gate row on the
 * PR card (screen 10, pick C = merge policy P3; PICKS.md 2026-10-05).
 *
 * ONE READ OF EACH, SHARED BY BOTH: the registration from `GET
 * /v1/repositories` matched on the issue's owner/repo, its index document,
 * and -- once the run has a pull request -- the impact of that pull request
 * from `POST /v1/repositories/{repo_id}/impact`, asked once per head sha, so a
 * 15s poll of the run never asks the forge again for the same head.
 *
 * WHAT IS NOT SERVED IS SAID, NEVER FILLED IN:
 *   * which index VERSION the planner was given: the run carries no
 *     `index_sha` (repo-index.md §4.1, the field the planner lane adds), so
 *     the card shows the repository's index now, said as now, and the version
 *     given is a dash with that reason;
 *   * whether the selected tests PASSED: the `swarmcloud/selected-tests`
 *     check run's conclusion (`impact_plans.conclusion`, lane RI12) is served
 *     by no route, so "passed" is a dash with that reason. The count selected
 *     is the impact plan's own.
 */

const REPOS_ROUTE = 'GET /v1/repositories'
const INDEX_ROUTE = 'GET /v1/repositories/{repo_id}/index'
const IMPACT_ROUTE = 'POST /v1/repositories/{repo_id}/impact'
const PASSED_WHY =
  'Whether the selected tests passed is not served: no route serves the swarmcloud/selected-tests check run or its conclusion (impact_plans.conclusion, lane RI12)'
const GIVEN_WHY =
  'Which index version the planner was given is not served: the run carries no index_sha (repo-index.md §4.1)'

const EMPTY = <T,>(): Promise<Result<T>> => Promise.resolve({ status: 'empty', fetchedAt: Date.now() })

/** The registration for the run's repository: the record, `null` when none is registered, or the read's state. */
export type RegRead =
  | { kind: 'reading' }
  | { kind: 'notserved' }
  | { kind: 'failed'; error: ApiError }
  | { kind: 'none' }
  | { kind: 'found'; r: RepoRecord }

export interface RunIndexRead {
  reg: RegRead
  index: Result<IndexDoc | null>
  impact: Result<ImpactPlan | null>
  pr: number | null
}

function regOf(state: Result<RepoRecord[]>, owner: string, repo: string): RegRead {
  if (state.status === 'loading') return { kind: 'reading' }
  if (state.status === 'error') return notServed(state.error) ? { kind: 'notserved' } : { kind: 'failed', error: state.error }
  if (state.status === 'empty') return { kind: 'none' }
  const want = `${owner}/${repo}`.toLowerCase()
  const r = state.data.find((x) => `${x.owner}/${x.repo}`.toLowerCase() === want)
  return r === undefined ? { kind: 'none' } : { kind: 'found', r }
}

/** The run's registration, index and pull-request impact, each read once. */
export function useRunIndex(run: IssueRun): RunIndexRead {
  const repos = useUrRead(() => loadRepositories(), 'run-index:repos')
  const reg = regOf(repos.state, run.issue.owner, run.issue.repo)
  const repoId = reg.kind === 'found' ? reg.r.repo_id : null
  const pr = run.pull_request?.number ?? null
  const head = run.pull_request?.head_sha ?? ''
  const index = useUrRead<IndexDoc | null>(
    () => (repoId === null ? EMPTY<IndexDoc | null>() : loadRepositoryIndex(repoId)),
    `run-index:index:${repoId ?? ''}`,
  )
  const impact = useUrRead<ImpactPlan | null>(
    () => (repoId === null || pr === null ? EMPTY<ImpactPlan | null>() : queryRepositoryImpact(repoId, { pull_request: pr })),
    `run-index:impact:${repoId ?? ''}:${pr ?? ''}:${head}`,
  )
  return { reg, index: index.state, impact: impact.state, pr }
}

function NotServedYet({ route }: { route: string }) {
  return (
    <i className="ctl-em" data-notserved={route} title={`Not served: ${route}`}>
      not served yet
    </i>
  )
}

// ---------------------------------------------------------------------------
// Screen 5, Context B
// ---------------------------------------------------------------------------

export function RunContextCard({ run, ctx, go }: { run: IssueRun; ctx: RunIndexRead; go: (to: string) => void }) {
  const r = ctx.reg.kind === 'found' ? ctx.reg.r : null
  return (
    <Card level={3} className="rc-ctx" title="Context the planner was given" action={<Chip>Context used</Chip>}>
      <ul>
        <li data-ctx="index">
          <b>Repo index</b> <IndexLine run={run} ctx={ctx} />
        </li>
        <li data-ctx="impact">
          <b>Impact</b> <ImpactLine ctx={ctx} />
        </li>
        <li data-ctx="issue">
          <b>Issue</b> #{run.issue.number}
          {run.issue_read ? ` with ${run.issue_read.comments} ${run.issue_read.comments === 1 ? 'comment' : 'comments'}` : (
            <>
              {' '}
              <Dash why="The issue was not read when the run was created" />
            </>
          )}
        </li>
        <li data-ctx="open-work">
          <b>Open work</b>{' '}
          {run.open_work ? (
            `${plural(run.open_work.issues.length, 'issue')}${run.open_work.issues_truncated ? '+' : ''}, ${plural(run.open_work.pull_requests.length, 'pull request')}${run.open_work.pull_requests_truncated ? '+' : ''}, read when the run was created`
          ) : (
            <Dash why="Open work was not read for this run: it was created before the open-work read, or the read failed" />
          )}
        </li>
      </ul>
      {r !== null && (
        <UrLink className="c-link is-card rc-open" to={repoAddress(r.repo_id)} go={go}>
          Open the index ›
        </UrLink>
      )}
    </Card>
  )
}

function plural(n: number, one: string): string {
  return `${n} ${n === 1 ? one : `${one}s`}`
}

function IndexLine({ run, ctx }: { run: IssueRun; ctx: RunIndexRead }): ReactNode {
  const reg = ctx.reg
  if (reg.kind === 'reading') return <span className="ur-mu">reading the registration…</span>
  if (reg.kind === 'notserved') return <NotServedYet route={REPOS_ROUTE} />
  if (reg.kind === 'failed') return <span className="ur-bad">the registrations could not be read: {reg.error.message}</span>
  if (reg.kind === 'none') {
    return (
      <span>
        none · {run.issue.owner}/{run.issue.repo} is not registered in this tenant, so it has no index to give a planner
      </span>
    )
  }
  const r = reg.r
  const f = freshness(r.index)
  return (
    <span>
      given to the planner <Dash why={GIVEN_WHY} />
      {' · '}
      {r.index.current_sha === null ? (
        <>{repoName(r)} has no index yet</>
      ) : (
        <>
          index now at <code>{shortSha(r.index.current_sha)}</code>
        </>
      )}
      <UrFreshPill f={f} />
      {' · '}
      <Modules state={ctx.index} />
      {' · '}
      test map {r.index.coverage === null ? <Dash why="The index did not report its test-map coverage" /> : pct(r.index.coverage)}
    </span>
  )
}

function Modules({ state }: { state: Result<IndexDoc | null> }): ReactNode {
  if (state.status === 'loading') return <span className="ur-mu">modules: reading…</span>
  if (state.status === 'error') {
    return notServed(state.error) ? <>modules <NotServedYet route={INDEX_ROUTE} /></> : <>modules <Dash why={`The index could not be read: ${state.error.message}`} /></>
  }
  if (state.status === 'empty' || state.data === null) return <>modules <Dash why="The index document was not served" /></>
  return <>{plural(state.data.modules.length, 'module')}</>
}

function ImpactLine({ ctx }: { ctx: RunIndexRead }): ReactNode {
  if (ctx.pr === null) return <span>no pull request yet: the impact is read from the pull request's diff once the workflow opens one</span>
  if (ctx.reg.kind !== 'found') return <span>no registered index to walk the pull request's diff through</span>
  const s = ctx.impact
  if (s.status === 'loading') return <span className="ur-mu">reading the impact of #{ctx.pr}…</span>
  if (s.status === 'error') {
    return notServed(s.error) ? <NotServedYet route={IMPACT_ROUTE} /> : <span className="ur-bad">the impact could not be read: {s.error.message}</span>
  }
  if (s.status === 'empty' || s.data === null) return <Dash why="The impact route answered with no plan" />
  const p = s.data
  const of = p.total_tests === null ? '' : ` of ${fmt(p.total_tests)}`
  const tests = p.selection === 'full_suite' ? 'the full suite (fallback)' : `${p.targeted === null ? '—' : fmt(p.targeted)}${of} tests selected`
  const num = (v: number | null, one: string) => (v === null ? `— ${one}s` : `${fmt(v)} ${v === 1 ? one : `${one}s`}`)
  return (
    <span>
      pull request #{ctx.pr}: {num(p.diff.length, 'file')} · {num(p.changed_symbols, 'changed symbol')} · {num(p.affected_callers, 'affected caller')} · {tests}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Screen 10, the gate row under policy P3
// ---------------------------------------------------------------------------

export function SelectedTestsGate({ ctx, go }: { ctx: RunIndexRead; go: (to: string) => void }) {
  if (ctx.pr === null) return null
  const reg = ctx.reg
  const s = ctx.impact
  const plan = s.status === 'ok' || s.status === 'stale' ? s.data : null
  const policy = plan?.policy ?? null
  return (
    <div className="rc-gates">
      <div className="rc-gate-h">
        <b>Selected tests</b>
        {policy === null ? (
          plan !== null && <Chip title="No selection policy is set for this repository or its tenant: the selection is advisory, and the required checks stay the gate">policy not set</Chip>
        ) : (
          <Chip>{`policy ${policy}`}</Chip>
        )}
        {reg.kind === 'found' && (
          <UrNavButton size="sm" to={repoAddress(reg.r.repo_id, 'impact', ctx.pr)} go={go}>
            Impact
          </UrNavButton>
        )}
      </div>
      <GateRows ctx={ctx} plan={plan} />
    </div>
  )
}

function GateRow({ k, tone, label, cnt, small }: { k: string; tone: string; label: ReactNode; cnt: ReactNode; small?: ReactNode }) {
  return (
    <div className="rc-gate" data-gate={k}>
      <ToneMark tone={tone} />
      <span>{label}</span>
      <span className="rc-cnt">{cnt}</span>
      {small !== undefined && <small>{small}</small>}
    </div>
  )
}

function GateRows({ ctx, plan }: { ctx: RunIndexRead; plan: ImpactPlan | null }): ReactNode {
  const reg = ctx.reg
  if (reg.kind === 'reading') return <GateRow k="selected" tone="unknown" label="Selected tests" cnt={<span className="ur-mu">reading…</span>} />
  if (reg.kind === 'notserved') return <GateRow k="selected" tone="unknown" label="Selected tests" cnt={<NotServedYet route={REPOS_ROUTE} />} />
  if (reg.kind === 'failed') {
    return <GateRow k="selected" tone="unknown" label="Selected tests" cnt={<Dash why={`The registrations could not be read: ${reg.error.message}`} />} />
  }
  if (reg.kind === 'none') {
    return (
      <GateRow k="selected" tone="unknown" label="Selected tests" cnt={<Dash why="This repository is not registered here, so no index selects its tests" />}
        small="not registered: the required checks are the whole gate" />
    )
  }
  const s = ctx.impact
  if (s.status === 'loading') return <GateRow k="selected" tone="unknown" label="Selected tests" cnt={<span className="ur-mu">reading the selection…</span>} />
  if (s.status === 'error') {
    return (
      <GateRow k="selected" tone="unknown" label="Selected tests"
        cnt={notServed(s.error) ? <NotServedYet route={IMPACT_ROUTE} /> : <Dash why={`The selection could not be read: ${s.error.message}`} />} />
    )
  }
  if (plan === null) return <GateRow k="selected" tone="unknown" label="Selected tests" cnt={<Dash why="The impact route answered with no plan" />} />
  const full = fullSuiteWords(plan)
  const passed = (
    <>
      {' · passed '}
      <Dash why={PASSED_WHY} />
    </>
  )
  if (full !== null && full.needed) {
    return (
      <GateRow
        k="selected"
        tone="warn"
        label="Selected tests → full suite"
        cnt={<>fallback · {plan.selected === null ? <Dash why="The suite's size was not served" /> : <b>{fmt(plan.selected)}</b>} tests{passed}</>}
        small={`fallback reason: ${full.reason}`}
      />
    )
  }
  return (
    <>
      <GateRow
        k="selected"
        // The ring, not the check: whether the selection passed is not served
        // (PASSED_WHY), so a filled mark here would claim a result unseen.
        tone="unknown"
        label="Selected tests"
        cnt={
          <>
            {plan.targeted === null ? <Dash why="The selected-test count was not served" /> : <b>{fmt(plan.targeted)}</b>} of{' '}
            {plan.total_tests === null ? <Dash why="The suite's size was not served: the graph counts no test symbols" /> : fmt(plan.total_tests)} selected
            {passed}
          </>
        }
        small={full?.reason ?? undefined}
      />
      <GateRow
        k="full"
        tone="unknown"
        label="Full suite"
        cnt={full === null ? <Dash why="Whether the full suite is needed was not served" /> : full.words}
        small={plan.index_sha === null ? undefined : `chosen by the impact query at ${shortSha(plan.index_sha)}`}
      />
    </>
  )
}
