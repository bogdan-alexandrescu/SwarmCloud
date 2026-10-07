import { useState, type ReactNode } from 'react'
import { loadRepositories, runRepositoryIndex } from './api'
import { Banner, Button, Card, Dash, EmptyState, NamedMark } from './components'
import { HEAD_UNREAD, freshness, repoName, scheduleWords, shortSha, type RepoRecord } from './RepositoriesData'
import { REGISTER, GT_PAGE, TestsMapped, UrFreshPill, UrNavButton, UrRefresh, UrRegion, repoAddress, useUrRead, writeFailure } from './RepositoriesParts'
import { RegisterRepository } from './RepositoriesRegister'
import { RepositoryDetail } from './RepositoriesDetail'
import { GitTokensPage, PermissionsPage } from './GitTokens'
import { CountNote, PageHead } from './Shell'
import { timeAgo } from './types'
import './styles/repositories.css'

/**
 * WORK › REPOSITORIES (repositories.html, picked 2026-10-05; PICKS.md).
 *
 * One tab, five pages, the page on the tab's query the way one run is
 * `work/runs?run=<id>` (paths.ts spells them as paths):
 *
 *   /repositories                     the list, pick B: one card per repository
 *   /repositories/register            Register, pick C: pick from the token
 *   /repositories/<repo_id>[/<tab>]   one repository, pick A: six tabs
 *   /repositories/tokens              Git tokens, pick B: cards + command line
 *   /repositories/tokens/permissions  Permissions, pick A: the grid
 *
 * THE ROUTES ARE BEING BUILT IN PARALLEL (lanes RI1, RI2, GT1). Every region
 * calls the route the design names (docs/repo-index.md §6.1, git-tokens.md
 * §7) and, while it is not there, says "not served yet" naming it -- never a
 * fixture, never an invented figure. An unknown value is a dash with its
 * reason (redesign-v2.md, "Honesty rules").
 *
 * INDEXING IS ORDINARY CAPACITY-ACCOUNTED WORK (repo-index.md §3.2): Index now
 * queues a task through the API like any other, which holds capacity only
 * once it is LEASED (invariants 1 and 3). This page reserves nothing itself.
 *
 * Classes are `ur-` so a later pass can swap them for lane U0's components.
 */
export function RepositoriesScreen({ view, go }: { view: string | null; go: (to: string) => void }) {
  const q = new URLSearchParams(view ?? '')
  const page = q.get('page')
  const repo = q.get('repo')
  if (page === 'register') return <RegisterRepository go={go} />
  if (page === 'tokens') return <GitTokensPage go={go} />
  if (page === 'permissions') return <PermissionsPage go={go} />
  if (repo !== null && repo !== '') return <RepositoryDetail key={repo} repoId={repo} tab={q.get('tab')} go={go} pr={q.get('pr')} graph={{ view: q.get('view'), q: q.get('q') }} />
  // The list's heading is written HERE, as a literal, because
  // tests/unit/control_plane/test_nav_headings_agree.py reads the tab's
  // heading out of this function's source: the tab says Repositories, so the
  // page it opens is headed Repositories.
  return (
    <RepositoryList go={go}>
      {(meta, actions) => (
        <>
          <PageHead title="Repositories">
            {actions}
          </PageHead>
          <CountNote>{meta}</CountNote>
        </>
      )}
    </RepositoryList>
  )
}

function RepositoryList({
  go,
  children,
}: {
  go: (to: string) => void
  children: (meta: ReactNode, actions: ReactNode) => ReactNode
}) {
  const { state, reload } = useUrRead(loadRepositories, 'list')
  const [failed, setFailed] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const count = state.status === 'ok' || state.status === 'stale' ? state.data.length : state.status === 'empty' ? 0 : null

  async function indexNow(r: RepoRecord) {
    setBusy(r.repo_id)
    setFailed(null)
    const res = await runRepositoryIndex(r.repo_id, r.index.current_sha === null ? 'full' : 'incremental')
    setBusy(null)
    if (res.status === 'error') {
      setFailed(`${repoName(r)}: ${writeFailure(res.error)}`)
      return
    }
    reload()
  }

  return (
    <div className="ur-page ur-list">
      {children(
        count === null ? undefined : `${count} registered`,
        <span className="ur-acts">
          {/* The list's age, on the control that renews it (#98). */}
          <UrRefresh reads={[{ state, reload }]} />
          <UrNavButton to={GT_PAGE} go={go} size="sm">
            Git tokens
          </UrNavButton>
          <UrNavButton to={REGISTER} go={go} kind="primary" size="sm">
            Register repository
          </UrNavButton>
        </span>,
      )}
      {failed !== null && (
        <Banner tone="bad" title="Index now was refused">
          {failed}
        </Banner>
      )}
      <UrRegion
        state={state}
        route="GET /v1/repositories"
        what="The tenant's registered repositories"
        onRetry={reload}
        empty={
          <EmptyState
            kind="empty"
            heading="No repositories registered"
            action={
              <UrNavButton to={REGISTER} go={go} kind="primary">
                Register repository
              </UrNavButton>
            }
          >
            A registered repository is indexed on a schedule so planners and agents start from its map. Pick one the
            tenant's git token can read.
          </EmptyState>
        }
      >
        {(repos) => (
          <div className="ur-cards">
            {repos.map((r) => (
              <RepoCard key={r.repo_id} r={r} go={go} busy={busy === r.repo_id} onIndex={() => void indexNow(r)} />
            ))}
          </div>
        )}
      </UrRegion>
    </div>
  )
}

function lastIndexed(r: RepoRecord): ReactNode {
  if (r.index.last_indexed_at !== null) {
    return (
      <span>
        Last indexed <b>{timeAgo(r.index.last_indexed_at)}</b>
      </span>
    )
  }
  const run = r.last_run
  if (run !== null && (run.state === 'FAILED' || run.state === 'DEAD_LETTERED' || run.state === 'CANCELLED')) {
    return (
      <span>
        Last run{' '}
        <b className="ur-bad">
          {run.state === 'CANCELLED' ? 'cancelled' : 'failed'}
          {run.end_cause !== null ? ` · ${run.end_cause}` : ''}
        </b>
      </span>
    )
  }
  return (
    <span>
      Last indexed <Dash why="This repository has never been indexed" />
    </span>
  )
}

function RepoCard({ r, go, busy, onIndex }: { r: RepoRecord; go: (to: string) => void; busy: boolean; onIndex: () => void }) {
  const f = freshness(r.index)
  const head = r.index.head_sha
  const sched = scheduleWords(r.index)
  return (
    <Card className="ur-card" title={repoName(r)} action={<UrFreshPill f={f} />}>
      <div className="ur-cmeta">
        <span>
          Default branch {r.default_branch === null ? <Dash why="The default branch was not served" /> : <b>{r.default_branch}</b>}
        </span>
        <span>
          head{' '}
          {head === null ? (
            <Dash why={HEAD_UNREAD} />
          ) : (
            <b className="ur-sha" title={head}>
              {shortSha(head)}
            </b>
          )}
        </span>
      </div>
      <TestsMapped r={r} />
      <div className="ur-cmeta">
        {lastIndexed(r)}
        <span>Schedule {sched === null ? <Dash why="The schedule was not served" /> : <b>{sched}</b>}</span>
      </div>
      <div className="ur-cacts">
        <UrNavButton to={repoAddress(r.repo_id)} go={go} size="sm">
          Open
        </UrNavButton>
        {r.index.in_flight_task_id !== null ? (
          <NamedMark mark="running" hue="live" word="indexing now" title={r.index.in_flight_task_id} />
        ) : (
          <Button size="sm" busy={busy} onClick={onIndex}>
            Index now
          </Button>
        )}
      </div>
    </Card>
  )
}
