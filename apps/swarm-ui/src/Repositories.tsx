import { useEffect, useRef, useState, type ReactNode } from 'react'
import { loadRepositories, loadRepositoryIndex, runRepositoryIndex } from './api'
import { Banner, Button, Card, Dash, EmptyState, NamedMark } from './components'
import { HEAD_UNREAD, freshness, indexCredentialWords, repoName, scheduleWords, shortSha, type RepoRecord } from './RepositoriesData'
import { REGISTER, GT_PAGE, TestsMapped, UrConfirmIndex, UrFreshPill, UrLink, UrNavButton, UrRefresh, UrRegion, repoAddress, useUrRead, writeFailure } from './RepositoriesParts'
import { RegisterRepository } from './RepositoriesRegister'
import { RepositoryDetail } from './RepositoriesDetail'
import { GitTokensPage, PermissionsPage } from './GitTokens'
import { RepoDot, useRepoStatuses } from './PermissionCards'
import type { ForgeRepoStatus } from './api'
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
 * once it is LEASED (invariants 1 and 3). This page reserves nothing itself,
 * and queues nothing until the run is confirmed (`UrConfirmIndex`, RSA-15).
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
  // GitHub permissions per repository (#1086): one dot each, absent until read.
  const perms = useRepoStatuses()
  const [failed, setFailed] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  // The card whose Index now is waiting for its confirmation (RSA-15): one at a time.
  const [confirm, setConfirm] = useState<string | null>(null)
  const count = state.status === 'ok' || state.status === 'stale' ? state.data.length : state.status === 'empty' ? 0 : null

  async function indexNow(r: RepoRecord) {
    setBusy(r.repo_id)
    setFailed(null)
    const res = await runRepositoryIndex(r.repo_id, r.index.current_sha === null ? 'full' : 'incremental')
    setBusy(null)
    setConfirm(null)
    if (res.status === 'error') {
      setFailed(`${repoName(r)}: ${writeFailure(res.error)}`)
      return
    }
    reload()
  }

  // THE COUNT LINE IS THERE IN EVERY STATE (V224). Absent while the list was
  // read and when it failed, it arrived with the cards and pushed them ~37px
  // down. Reading, it says so; failed, the count is a dash with its reason.
  const meta =
    count !== null ? (
      `${count} registered`
    ) : state.status === 'loading' ? (
      'Reading the registered repositories…'
    ) : (
      <>
        <Dash why="The list of registered repositories was not read" /> registered
      </>
    )

  return (
    <div className="ur-page ur-list">
      {children(
        meta,
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
        plural
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
              <RepoCard
                key={r.repo_id}
                r={r}
                go={go}
                busy={busy === r.repo_id}
                confirming={confirm === r.repo_id}
                onAsk={() => setConfirm(r.repo_id)}
                onCancel={() => setConfirm(null)}
                onIndex={() => void indexNow(r)}
                perm={perms.get(r.repo_id)}
              />
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

/**
 * WHETHER THIS CARD'S TEST MAP WAS CUT AT ITS CEILING (live QA RSA-4,
 * 2026-10-11). The list record serves `index.coverage.test_map_edges` but not
 * the document's `truncated`, so SwarmCloud's card read "1,000 edges" as a
 * complete count when 1,000 is where the indexer stopped. Each card with an
 * index and an edge count reads its own document for `truncated`; the key
 * carries `current_sha`, so a new index is read again and an unchanged one is
 * not. Until the document answers (or if it never does) the card says the
 * served count, exactly as the detail page does before its own read lands.
 */
function useTestMapCut(r: RepoRecord): boolean {
  const sha = r.index.current_sha
  const wanted = sha !== null && r.index.coverage !== null && r.index.coverage.test_map_edges !== null
  const read = useUrRead(
    () => (wanted ? loadRepositoryIndex(r.repo_id) : Promise.resolve({ status: 'empty' as const, fetchedAt: Date.now() })),
    wanted ? `cut:${r.repo_id}:${sha}` : `cut:${r.repo_id}:none`,
  )
  const doc = read.state.status === 'ok' || read.state.status === 'stale' ? read.state.data : null
  return doc?.truncated.includes('test_map') === true
}

/**
 * WHY THIS REPOSITORY'S INDEX RUNS FAIL (lane INDEX-FAIL, operator QA p14,
 * 2026-10-13). Every run of one repository failed on its clone every 15
 * minutes while this card said "no index" or "indexing now", so nobody knew
 * there was anything to fix. The line names the last failed run (the API's
 * sentence for its class, when, and the task), and for an ACCESS failure what
 * to do: SwarmCloud's GitHub access is granted under Setup › Git tokens. When
 * the poll stopped re-running the repository (`index.backoff`) it says so,
 * and that Index now retries. The git line itself is the cause's title: it
 * is evidence, not something to read on every card.
 */
function FailureLine({ r, go }: { r: RepoRecord; go: (to: string) => void }) {
  const f = r.index.last_failure
  if (f === null) return null
  const stopped = r.index.backoff
  return (
    <div className="ur-fail" role="status" data-class={f.kind} data-backoff={stopped === null ? undefined : '1'}>
      <span>
        <b>Last index run failed</b>
        {f.at !== null && <> {timeAgo(f.at)}</>}:{' '}
        <span className="ur-fail-cause" title={f.detail ?? undefined}>
          {f.cause}
        </span>{' '}
        (<UrLink to={`work/task/${f.task_id}`} go={go}>
          task
        </UrLink>
        )
      </span>
      {f.kind === 'access' && (
        <span>
          {' '}
          grant it under{' '}
          <UrLink to={GT_PAGE} go={go}>
            Setup › Git tokens
          </UrLink>
          .
        </span>
      )}
      {stopped !== null && (
        <span className="ur-fail-stop">
          {' '}
          Polling stopped after {stopped.failures ?? 'repeated'} failed runs; Index now retries.
        </span>
      )}
    </div>
  )
}

/**
 * WHICH CREDENTIAL THIS REPOSITORY'S INDEX RUNS CLONE WITH (lane INDEX-CRED,
 * owner report 2026-10-13: "it says all tests pass for /repositories/tokens
 * but it can't index the repo in my personal workspace?"). Try it checks the
 * access a person's own tasks use; an index run is the tenant's, so the card
 * names the credential it uses -- the team token, the workspace owner's
 * GitHub connection, or none -- beside the last result, and a passing check
 * and a failing index never look alike without saying why.
 */
function IndexingUses({ r, go }: { r: RepoRecord; go: (to: string) => void }) {
  const words = indexCredentialWords(r.index_credential)
  if (words === null) return null
  const none = r.index_credential?.source === 'none'
  return (
    <div className="ur-cmeta ur-cred" data-source={r.index_credential?.source} title={r.index_credential?.secret_name ?? undefined}>
      <span>
      Indexing uses <b className={none ? 'ur-bad' : undefined}>{words}</b>
      {none && (
        <>
          {' '}
          under{' '}
          <UrLink to={GT_PAGE} go={go}>
            Setup › Git tokens
          </UrLink>
        </>
      )}
      </span>
    </div>
  )
}

function RepoCard({
  r,
  go,
  busy,
  confirming,
  onAsk,
  onCancel,
  onIndex,
  perm,
}: {
  r: RepoRecord
  go: (to: string) => void
  busy: boolean
  /** Index now was pressed and waits for its confirmation (RSA-15). */
  confirming: boolean
  onAsk: () => void
  onCancel: () => void
  onIndex: () => void
  perm?: ForgeRepoStatus
}) {
  const f = freshness(r.index)
  const head = r.index.head_sha
  const sched = scheduleWords(r.index)
  const cut = useTestMapCut(r)
  // FOCUS GOES BACK TO INDEX NOW WHEN ITS CONFIRMATION CLOSES (V222). Index now
  // is disabled while the confirmation is open, so it loses focus; Escape or
  // Cancel then removed the focused Cancel and left focus on <body>.
  const acts = useRef<HTMLDivElement>(null)
  const wasConfirming = useRef(confirming)
  useEffect(() => {
    if (wasConfirming.current && !confirming) acts.current?.querySelector<HTMLButtonElement>('button[data-index-now]')?.focus()
    wasConfirming.current = confirming
  }, [confirming])
  return (
    <Card
      className="ur-card"
      title={repoName(r)}
      action={
        <span className="ur-acts">
          <RepoDot row={perm} />
          <UrFreshPill f={f} />
        </span>
      }
    >
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
      <TestsMapped r={r} cut={cut} />
      <div className="ur-cmeta">
        {lastIndexed(r)}
        <span>Schedule {sched === null ? <Dash why="The schedule was not served" /> : <b>{sched}</b>}</span>
      </div>
      <IndexingUses r={r} go={go} />
      <FailureLine r={r} go={go} />
      <div className="ur-cacts" ref={acts}>
        <UrNavButton to={repoAddress(r.repo_id)} go={go} size="sm">
          Open
        </UrNavButton>
        {r.index.in_flight_task_id !== null ? (
          <NamedMark mark="running" hue="live" word="indexing now" title={r.index.in_flight_task_id} />
        ) : (
          <Button size="sm" busy={busy} disabled={confirming} onClick={onAsk} data-index-now="">
            Index now
          </Button>
        )}
      </div>
      {confirming && r.index.in_flight_task_id === null && (
        <UrConfirmIndex
          name={repoName(r)}
          kind={r.index.current_sha === null ? 'full' : 'incremental'}
          base={r.index.current_sha}
          busy={busy}
          onConfirm={onIndex}
          onCancel={onCancel}
        />
      )}
    </Card>
  )
}
