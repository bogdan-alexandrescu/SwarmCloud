import { useEffect, useRef, useState, type ReactNode } from 'react'
import { loadRepositories, loadRepositoryIndex, patchRepositoryFileIssues, patchRepositoryWorkflowFiles, runRepositoryIndex } from './api'
import { Banner, Button, Card, Dash, EmptyState, NamedMark } from './components'
import { HEAD_UNREAD, WORKFLOW_FILES_WHY, freshness, indexCredentialWords, repoName, scheduleWords, shortSha, workflowsNotGranted, type RepoRecord } from './RepositoriesData'
import { REGISTER, GT_PAGE, TestsMapped, UrConfirmIndex, UrFreshPill, UrLink, UrNavButton, UrRefresh, UrRegion, repoAddress, useUrRead, writeFailure } from './RepositoriesParts'
import { RegisterRepository } from './RepositoriesRegister'
import { RepositoryDetail, WorkflowsGrantLine } from './RepositoriesDetail'
import { GitTokensPage, PermissionsPage } from './GitTokens'
import { RepoDot, useForgePermissions } from './PermissionCards'
import { QA_TARGETS_PAGE, QaTargetsScreen } from './QaTargets'
import type { ForgeRepoStatus } from './api'
import type { QaLink } from './qaTargetsApi'
import { useRoleGate } from './Profile'
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
export function RepositoriesScreen({ view, go, qaLink }: { view: string | null; go: (to: string) => void; qaLink?: QaLink | null }) {
  const q = new URLSearchParams(view ?? '')
  const page = q.get('page')
  const repo = q.get('repo')
  if (page === 'register') return <RegisterRepository go={go} />
  if (page === 'tokens') return <GitTokensPage go={go} />
  if (page === 'permissions') return <PermissionsPage go={go} />
  // QA targets (VQ-6, docs/design/visual-qa-any-app.md §6): Repositories → QA targets → Run.
  // `qaLink` is the credential link's target and token when the shell read
  // them off the bar before it rewrote the address (as App.tsx's `keyLink`
  // does for ProviderKeys); left undefined, the page reads the bar itself.
  if (page === 'qa-targets') return <QaTargetsScreen go={go} link={qaLink} />
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
  // The orgs too, for the not-granted line's installation link (#2029).
  const forge = useForgePermissions().state
  const forgeDoc = forge.status === 'ok' || forge.status === 'stale' ? forge.data : null
  const perms = new Map((forgeDoc?.repositories ?? []).map((p) => [p.repo_id, p]))
  const [failed, setFailed] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  // The card whose Index now is waiting for its confirmation (RSA-15): one at a time.
  const [confirm, setConfirm] = useState<string | null>(null)
  // A VIEWER REGISTERS AND INDEXES NOTHING (docs/design/workspace-roles.md
  // §4.8): register and index:run need `member`. The server still decides.
  const roleGate = useRoleGate('member', 'register and index repositories')
  // ONLY THE WORKSPACE'S ADMIN TURNS AGENTS' ISSUE FILING ON OR OFF (§3.2, Q2):
  // below admin the card shows the setting with no control. The server decides.
  const fileGate = useRoleGate('admin', 'set whether agents may file issues')
  const [filing, setFiling] = useState<string | null>(null)
  const [fileFailed, setFileFailed] = useState<string | null>(null)
  // WORKFLOW FILES ARE THE WORKSPACE ADMIN'S TO TURN ON, PER REPOSITORY (#2029;
  // BGD, 2026-10-22). Below admin the card shows the setting with no control.
  const workflowGate = useRoleGate('admin', 'set whether agents may push workflow files')
  const [workflowBusy, setWorkflowBusy] = useState<string | null>(null)
  const [workflowFailed, setWorkflowFailed] = useState<string | null>(null)
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

  async function setFileIssues(r: RepoRecord, on: boolean) {
    setFiling(r.repo_id)
    setFileFailed(null)
    const res = await patchRepositoryFileIssues(r.repo_id, on)
    setFiling(null)
    if (res.status === 'error') {
      setFileFailed(`${repoName(r)}: ${writeFailure(res.error)}`)
      return
    }
    reload()
  }

  async function setWorkflowFiles(r: RepoRecord, on: boolean) {
    setWorkflowBusy(r.repo_id)
    setWorkflowFailed(null)
    const res = await patchRepositoryWorkflowFiles(r.repo_id, on)
    setWorkflowBusy(null)
    if (res.status === 'error') {
      setWorkflowFailed(`${repoName(r)}: ${writeFailure(res.error)}`)
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
          <UrNavButton to={QA_TARGETS_PAGE} go={go} size="sm">
            QA targets
          </UrNavButton>
          {roleGate === null ? (
            <UrNavButton to={REGISTER} go={go} kind="primary" size="sm">
              Register repository
            </UrNavButton>
          ) : (
            <Button kind="primary" size="sm" disabled>
              Register repository
            </Button>
          )}
        </span>,
      )}
      {roleGate !== null && (
        <p className="ur-small" data-role-gate="member">
          {roleGate}
        </p>
      )}
      {failed !== null && (
        <Banner tone="bad" title="Index now was refused">
          {failed}
        </Banner>
      )}
      {fileFailed !== null && (
        <Banner tone="bad" title="Agents may file issues was not changed">
          {fileFailed}
        </Banner>
      )}
      {workflowFailed !== null && (
        <Banner tone="bad" title="Workflow files was not changed">
          {workflowFailed}
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
              roleGate === null ? (
                <UrNavButton to={REGISTER} go={go} kind="primary">
                  Register repository
                </UrNavButton>
              ) : (
                <Button kind="primary" disabled>
                  Register repository
                </Button>
              )
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
                viewerOnly={roleGate !== null}
                fileIssues={
                  <FileIssues
                    on={r.agents_may_file_issues}
                    admin={fileGate === null}
                    busy={filing === r.repo_id}
                    onSet={(on) => void setFileIssues(r, on)}
                  />
                }
                workflowFiles={
                  <WorkflowFiles
                    on={r.workflow_files}
                    admin={workflowGate === null}
                    busy={workflowBusy === r.repo_id}
                    onSet={(on) => void setWorkflowFiles(r, on)}
                    grant={workflowsNotGranted(r.workflow_files, perms.get(r.repo_id), forgeDoc?.orgs ?? [])}
                  />
                }
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
          agent
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

/**
 * The card's line for `agents_may_file_issues` (AU-0 REG-FLAG,
 * docs/design/agent-autonomy.md §3.2, owner decision Q2), with a control for
 * an admin only. It draws `RepoRecord`'s value as served and never re-derives
 * the platform default; null is a dash, with no control to set it.
 */
function FileIssues({ on, admin, busy, onSet }: { on: boolean | null; admin: boolean; busy: boolean; onSet: (on: boolean) => void }) {
  return (
    <div className="ur-cmeta ur-file-issues" data-file-issues={on === null ? undefined : String(on)}>
      <span>
        Agents may file issues{' '}
        {on === null ? <Dash why="Whether agents may file issues was not served" /> : <b>{on ? 'on' : 'off'}</b>}
      </span>
      {on !== null &&
        (admin ? (
          <Button size="sm" busy={busy} onClick={() => onSet(!on)} data-file-issues-set={String(!on)}>
            {on ? 'Turn off' : 'Turn on'}
          </Button>
        ) : (
          <span className="ur-small" data-role-gate="admin">
            set by a workspace admin
          </span>
        ))}
    </div>
  )
}

/**
 * The card's line for `workflow_files` (#2029, WF-APP-TOKEN; BGD's decision
 * 2026-10-22: self-serve, per-repo opt-in, off by default), with a control for
 * the workspace admin only, exactly as `FileIssues`. It draws the value as
 * served; null is a dash, with no control to set it. Under it, what turning it
 * on means, and "Workflows: not granted" where it is on and the App
 * installation has not accepted Workflows.
 */
function WorkflowFiles({
  on,
  admin,
  busy,
  onSet,
  grant,
}: {
  on: boolean | null
  admin: boolean
  busy: boolean
  onSet: (on: boolean) => void
  grant: { installed: boolean; link: string | null } | null
}) {
  return (
    <div className="ur-workflow-files" data-workflow-files={on === null ? undefined : String(on)}>
      <div className="ur-cmeta">
        <span>
          Workflow files{' '}
          {on === null ? <Dash why="Whether agents may push workflow files was not served" /> : <b>{on ? 'on' : 'off'}</b>}
        </span>
        {on !== null &&
          (admin ? (
            <Button size="sm" busy={busy} onClick={() => onSet(!on)} data-workflow-files-set={String(!on)}>
              {on ? 'Turn off' : 'Turn on'}
            </Button>
          ) : (
            <span className="ur-small" data-role-gate="admin">
              set by a workspace admin
            </span>
          ))}
      </div>
      <p className="ur-hint">{WORKFLOW_FILES_WHY}</p>
      <WorkflowsGrantLine grant={grant} />
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
  viewerOnly = false,
  fileIssues,
  workflowFiles,
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
  /** The caller's role is below `member`: Index now is drawn disabled, the list says why. */
  viewerOnly?: boolean
  /** The `agents_may_file_issues` line (AU-0 REG-FLAG). */
  fileIssues?: ReactNode
  /** The `workflow_files` line (#2029). */
  workflowFiles?: ReactNode
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
      {fileIssues}
      {workflowFiles}
      <FailureLine r={r} go={go} />
      <div className="ur-cacts" ref={acts}>
        <UrNavButton to={repoAddress(r.repo_id)} go={go} size="sm">
          Open
        </UrNavButton>
        {r.index.in_flight_task_id !== null ? (
          <NamedMark mark="running" hue="live" word="indexing now" title={r.index.in_flight_task_id} />
        ) : (
          <Button size="sm" busy={busy} disabled={confirming || viewerOnly} onClick={onAsk} data-index-now="">
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
