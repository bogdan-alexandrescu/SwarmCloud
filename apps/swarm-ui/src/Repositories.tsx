import { useEffect, useRef, useState, type MouseEvent, type ReactNode } from 'react'
import { deleteRepository, loadAccess, loadRepositories, loadRepositoryCounts, loadRepositoryIndex, runRepositoryIndex } from './api'
import { ADD_REPOSITORY_ADDRESS } from './Access'
import { Banner, Button, Card, Dash, EmptyState, NamedMark, Tabs, TypedConfirm, routedClick } from './components'
import { useSetupSteps } from './ConfigSetup'
import { WORKSPACE_SETUP_ADDRESS, addressToPath } from './paths'
import { ReachableRepositories } from './ReachableRepositories'
import { HEAD_UNREAD, WORKFLOW_FILES_WHY, freshness, indexCredentialWords, repoName, scheduleWords, shortSha, type RepoRecord } from './RepositoriesData'
import { REGISTER, GT_PAGE, TestsMapped, UrConfirmIndex, UrFreshPill, UrGraphCacheSection, UrLink, UrNavButton, UrRefresh, UrRegion, repoAddress, useUrRead, writeFailure } from './RepositoriesParts'
import { RegisterRepository } from './RepositoriesRegister'
import { RepositoryDetail } from './RepositoriesDetail'
import { GitTokensPage, PermissionsPage } from './GitTokens'
import { RepoDot, repoStatus, useForgePermissions } from './PermissionCards'
import { QA_TARGETS_PAGE, QaTargetsScreen } from './QaTargets'
import type { ForgeGap, ForgeRepoStatus, RepoCounts } from './api'
import type { AccessGrant } from './types'
import type { QaLink } from './qaTargetsApi'
import { useRoleGate } from './Profile'
import { PageHead } from './Shell'
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
 *
 * CONFIG › REPOSITORIES IS REPOSITORY MANAGEMENT (REPOS-PAGE; part of #2157,
 * BGD's decisions of 2026-10-23, proposal E, config-e.html S3a-S3c). The list
 * page is led by what is missing, then "Repositories SwarmCloud may reach as
 * you" (ReachableRepositories.tsx, moved off GitHub access), then the
 * registry's cards with a dashed card for each chosen repository not yet
 * registered. Choose more on GitHub (Choose on GitHub while nothing is
 * chosen) goes to GitHub access with Choose repositories open; nothing is
 * added to the person's reach from here.
 * Remove on a reach row revokes the person's own grant, as it did on
 * Work › Access.
 *
 * BGD'S ANSWERS TO REPOS-PAGE (REPOS-FOLLOWUPS, 2026-10-25): Remove on each
 * registered card unregisters (`DELETE /v1/repositories/{repo_id}`, member
 * role), behind the typed owner/name, and says first that its secrets'
 * grants are disabled with it; the registry is read again, and a repository
 * still chosen is a dashed card again. The dashed card's Register opens
 * Register with `repo=owner/name`, on step 2. The reach table's Registered
 * column and each card's "Secrets: N" and "QA targets: N" (S3a) come from the
 * registry and ONE `GET /v1/repositories/counts`, never a read per card.
 */
export function RepositoriesScreen({ view, go, qaLink }: { view: string | null; go: (to: string) => void; qaLink?: QaLink | null }) {
  const q = new URLSearchParams(view ?? '')
  const page = q.get('page')
  const repo = q.get('repo')
  if (page === 'register') return <RegisterRepository go={go} repo={repo} />
  if (page === 'tokens') return <GitTokensPage go={go} />
  if (page === 'permissions') return <PermissionsPage go={go} />
  // QA targets (VQ-6, docs/design/visual-qa-any-app.md §6): Repositories → QA targets → Run.
  // `qaLink` is the credential link's target and token when the shell read
  // them off the bar before it rewrote the address (as App.tsx's `keyLink`
  // does for ProviderKeys); left undefined, the page reads the bar itself.
  if (page === 'qa-targets') {
    return (
      <QaTargetsScreen go={go} link={qaLink} repository={q.get('repository')} tabs={<RepositoriesTabs current="qa-targets" go={go} />} />
    )
  }
  if (repo !== null && repo !== '') return <RepositoryDetail key={repo} repoId={repo} tab={q.get('tab')} go={go} pr={q.get('pr')} graph={{ view: q.get('view'), q: q.get('q') }} />
  // The list's heading is written HERE, as a literal, because
  // tests/unit/control_plane/test_nav_headings_agree.py reads the tab's
  // heading out of this function's source: the tab says Repositories, so the
  // page it opens is headed Repositories.
  return (
    <RepositoryList go={go}>
      {(actions) => <PageHead title="Repositories">{actions}</PageHead>}
    </RepositoryList>
  )
}

/**
 * THE PAGE'S TWO TABS, REPOSITORIES AND QA TARGETS (config-e.html S3: "QA
 * targets as the page's second tab"). QA targets was a button in the list's
 * head; it is the same address (`page=qa-targets`), drawn as the strip the
 * other Config pages carry under their head (`cfg-parts`, config.css).
 */
function RepositoriesTabs({ current, go }: { current: 'list' | 'qa-targets'; go: (to: string) => void }) {
  return (
    <Tabs
      className="cfg-parts"
      label="Repositories pages"
      current={current}
      onGo={go}
      tabs={[
        { key: 'list', label: 'Repositories', href: addressToPath(REPOSITORIES_LIST) },
        { key: 'qa-targets', label: 'QA targets', href: addressToPath(QA_TARGETS_PAGE) },
      ]}
    />
  )
}

/** The list's own address: the strip's first tab. */
const REPOSITORIES_LIST = 'config/repositories'

/**
 * Register with this repository picked (REPOS-FOLLOWUPS): RepositoriesRegister
 * opens on step 2 when `repo` is in the reader's reach list, on step 1 when not.
 */
export function registerAddress(repository: string): string {
  return `${REPOSITORIES_LIST}?${new URLSearchParams({ page: 'register', repo: repository }).toString()}`
}

/** QA targets narrowed to one repository's (`QaTargetsScreen`'s `repository`). */
export function qaTargetsOf(repository: string): string {
  return `${REPOSITORIES_LIST}?${new URLSearchParams({ page: 'qa-targets', repository }).toString()}`
}

/** The id a registered card is drawn with, so the reach table's Registered link can bring it into view. */
const cardId = (repoId: string) => `ur-card-${repoId}`

/** `owner/repo` as GitHub compares it: names are not case-sensitive there. */
const fullName = (owner: string, name: string) => `${owner}/${name}`.toLowerCase()

/**
 * THE GRANTS NOT IN THE REGISTRY (config-e.html S3a: "a chosen repository
 * that is not registered yet is a dashed card with Register"). A grant is
 * the person's (`GET /v1/access`); a registration is the workspace's
 * (`GET /v1/repositories`). They are joined on owner/repo, because the two
 * documents' `repo_id`s are not promised to be the same identifier.
 */
export function chosenNotRegistered(grants: readonly AccessGrant[], registered: readonly RepoRecord[]): AccessGrant[] {
  const held = new Set(registered.map((r) => fullName(r.owner, r.repo)))
  return grants.filter((g) => !held.has(g.repository.toLowerCase()))
}

/**
 * The lead, drawn as GitHub access draws its own (Access.tsx's `Lead`, which
 * that file does not export): the card surface, a kicker, one sentence, the
 * next action.
 */
function Lead({ kicker, title, actions, children }: { kicker: string; title: string; actions: ReactNode; children: ReactNode }) {
  return (
    <section className="ac-lead" aria-label="Next step">
      <Card title={title}>
        <p className="ur-small">
          <b className="ur-mu">{kicker}</b> {children}
        </p>
        <div className="ur-gh-acts">{actions}</div>
      </Card>
    </section>
  )
}

/**
 * GitHub access with Choose repositories open (GITHUB-ACCESS-PAGE's deep link).
 *
 * NAMED FOR WHAT IT DOES, NOT "ADD NEW REPOSITORY" (console visual QA pass 1,
 * config-16, part of #2596). The head's Register repository registers a
 * repository already chosen; this one leaves for GitHub to choose one, and as
 * "Add new repository" the two read as two buttons for one job. "more" only
 * once something is chosen: with nothing chosen there is nothing to add to.
 */
function AddRepository({ go, more, kind = 'primary' }: { go: (to: string) => void; more: boolean; kind?: 'primary' | 'secondary' }) {
  return (
    <UrNavButton to={ADD_REPOSITORY_ADDRESS} go={go} kind={kind} size="sm">
      {more ? 'Choose more on GitHub' : 'Choose on GitHub'}
    </UrNavButton>
  )
}

/** Register, or the same words disabled below `member` (the list's note says why). */
function RegisterButton({
  go,
  gated,
  kind = 'primary',
  to = REGISTER,
}: {
  go: (to: string) => void
  gated: boolean
  kind?: 'primary' | 'secondary'
  /** Register with a repository picked (`registerAddress`), or the bare page. */
  to?: string
}) {
  return gated ? (
    <Button kind={kind} size="sm" disabled>
      Register
    </Button>
  ) : (
    <UrNavButton to={to} go={go} kind={kind} size="sm">
      Register
    </UrNavButton>
  )
}

/**
 * THE ONE NEXT ACTION THAT LEADS THE PAGE (proposal E). It is read off the
 * page's two reads, so it never says a step is left that a read says is
 * done: nothing chosen is the strip's step 2 (its place counted from the
 * strip, three steps in a personal workspace and four in a group one); a
 * chosen repository not registered is Register; otherwise Choose more on
 * GitHub. Null while either read is not in hand: the head then carries the
 * choose button, and nothing is claimed.
 */
function RepositoriesLead({
  grants,
  missing,
  go,
  gated,
}: {
  grants: readonly AccessGrant[]
  missing: readonly AccessGrant[] | null
  go: (to: string) => void
  gated: boolean
}) {
  const steps = useSetupSteps().length
  if (grants.length === 0) {
    return (
      <Lead kicker={`Step 2 of ${steps} · next.`} title="No repository chosen yet" actions={<AddRepository go={go} more={false} />}>
        Choose repositories from an owner on GitHub access, or type owner/repo there.
      </Lead>
    )
  }
  if (missing === null) return null
  if (missing.length > 0) {
    const one = missing.length === 1
    return (
      <Lead
        kicker={one ? 'Repositories · one thing left.' : `Repositories · ${missing.length} left.`}
        title={one ? `${missing[0]!.repository} is chosen but not registered` : `${missing.length} chosen repositories are not registered`}
        actions={<RegisterButton go={go} gated={gated} to={one ? registerAddress(missing[0]!.repository) : REGISTER} />}
      >
        Register {one ? 'it' : 'each'} so it is indexed on a schedule and agents start from its map.
      </Lead>
    )
  }
  const n = grants.length
  return (
    <Lead kicker="Repositories · nothing left." title="Every chosen repository is registered" actions={<AddRepository go={go} more />}>
      {n === 1 ? '1 repository chosen, and registered.' : `${n} repositories chosen, every one registered.`} Choose more on
      GitHub opens GitHub access › Choose repositories.
    </Lead>
  )
}

/** A chosen repository not registered yet: the dashed card with Register (S3a). */
function ChosenCard({ g, go, gated }: { g: AccessGrant; go: (to: string) => void; gated: boolean }) {
  return (
    <Card className="ur-card is-chosen" title={g.repository}>
      <div className="ur-cbadge" data-badge="">
        <NamedMark mark="warn" hue="warn" word="chosen, not registered" />
      </div>
      <div className="ur-cmeta">
        <span>{g.mode === 'write' ? 'Write grant' : 'Read grant'}</span>
      </div>
      <div className="ur-cmeta">
        <span>Not indexed: registering starts it</span>
      </div>
      <div className="ur-cacts">
        <RegisterButton go={go} gated={gated} to={registerAddress(g.repository)} />
      </div>
    </Card>
  )
}

function RepositoryList({
  go,
  children,
}: {
  go: (to: string) => void
  children: (actions: ReactNode) => ReactNode
}) {
  const { state, reload } = useUrRead(loadRepositories, 'list')
  // "REPOSITORIES SWARMCLOUD MAY REACH AS YOU" LEADS THE PAGE (BGD, 2026-10-23;
  // config-e.html S3). One `GET /v1/access` feeds the card, the lead and the
  // dashed cards of what is chosen and not registered.
  const access = useUrRead(loadAccess, 'access')
  const grants = access.state.status === 'ok' || access.state.status === 'stale' ? access.state.data.grants : null
  // GitHub permissions per repository (#1086): one dot each, absent until read.
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
  const count = state.status === 'ok' || state.status === 'stale' ? state.data.length : state.status === 'empty' ? 0 : null
  // EVERY CARD'S COUNTS IN ONE READ (REPOS-FOLLOWUPS, S3a's "Secrets: N" and
  // "QA targets: N"): never a read per card. Counts only, never a name.
  const counts = useUrRead(loadRepositoryCounts, 'counts')
  const countDoc = counts.state.status === 'ok' || counts.state.status === 'stale' ? counts.state.data : null
  const countsWhy =
    counts.state.status === 'loading' ? 'The counts are being read' : 'The counts were not read (GET /v1/repositories/counts)'
  // THE REGISTRATION WHOSE REMOVE WAITS FOR ITS TYPED NAME. Nothing is sent before it.
  const [removing, setRemoving] = useState<RepoRecord | null>(null)
  const [removeBusy, setRemoveBusy] = useState(false)
  const [removeFailed, setRemoveFailed] = useState<string | null>(null)

  async function remove(r: RepoRecord) {
    setRemoveBusy(true)
    setRemoveFailed(null)
    const res = await deleteRepository(r.repo_id)
    setRemoveBusy(false)
    if (res.status === 'error') {
      setRemoveFailed(writeFailure(res.error))
      return
    }
    setRemoving(null)
    // The registry again: a repository still chosen is drawn dashed from it.
    reload()
    counts.reload()
  }

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

  const registered = state.status === 'ok' || state.status === 'stale' ? state.data : state.status === 'empty' ? [] : null
  const missing = grants !== null && registered !== null ? chosenNotRegistered(grants, registered) : null
  // THE REACH TABLE'S REGISTERED COLUMN (S3a), joined as the dashed cards are:
  // registered links to its card on this page, not registered to Register
  // with it picked (below `member`, the words without the link).
  const registeredCell = (g: AccessGrant): ReactNode => {
    if (registered === null) return <Dash why="The registered repositories were not read" />
    const r = registered.find((x) => fullName(x.owner, x.repo) === g.repository.toLowerCase())
    if (r === undefined) {
      const word = <NamedMark mark="warn" hue="warn" word="not registered" />
      return roleGate !== null ? (
        word
      ) : (
        <UrLink to={registerAddress(g.repository)} go={go}>
          {word}
        </UrLink>
      )
    }
    return (
      <a href={addressToPath(REPOSITORIES_LIST)} onClick={(e) => toCard(e, r.repo_id)}>
        <NamedMark mark="succeeded" hue="neu" word="registered" />
      </a>
    )
  }
  // THE CHOOSE BUTTON IS DRAWN ONCE (S3c: "its lead carries Add new
  // repository, so the header does not repeat it"): in the lead when the lead
  // offers it, in the head otherwise.
  const leadAdds = grants !== null && (grants.length === 0 || (missing !== null && missing.length === 0))
  // ONE PRIMARY ADD ACTION, AND IT IS THE LEAD'S (console visual QA pass 2,
  // config2-08, was config-16; part of #2694). With the lead drawn, the head's
  // Register repository was a second add path beside the lead's Choose more
  // on GitHub (or beside its own Register). The lead names the one next
  // action -- Choose on GitHub, Register the chosen repository, or Choose
  // more on GitHub -- and every chosen repository not registered has its
  // dashed card's Register, so the head keeps Register repository only while
  // the lead is not drawn (a read not in hand). Choose more on GitHub stays in
  // the head beside a lead that registers, as a secondary.
  const leadDrawn = grants !== null && (grants.length === 0 || missing !== null)
  const chosen = (missing ?? []).map((g) => <ChosenCard key={g.repo_id} g={g} go={go} gated={roleGate !== null} />)
  // THE COUNT LINE IS THERE IN EVERY STATE (V224). Absent while the list was
  // read and when it failed, it arrived with the cards and pushed them ~37px
  // down. Reading, it says so; failed, the count is a dash with its reason.
  const meta =
    count !== null ? (
      // The dashed cards are drawn in the same grid, so the note counts them apart (S3a).
      missing !== null && missing.length > 0 ? (
        `Registered repositories · ${count} · ${missing.length} chosen, not registered`
      ) : (
        `Registered repositories · ${count}`
      )
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
        <span className="ur-acts">
          {/* The registry's age, on the control that renews both the page's
              reads (#98). The age is the registry's alone (V223): the access
              read answering first must not say "read 0 s ago" for a list that
              is still reading, or failed. */}
          <UrRefresh reads={[{ state, reload }, access, counts]} ageOf={[{ state, reload }]} />
          <UrNavButton to={GT_PAGE} go={go} size="sm">
            Git tokens
          </UrNavButton>
          {leadDrawn ? null : roleGate === null ? (
            <UrNavButton to={REGISTER} go={go} size="sm">
              Register repository
            </UrNavButton>
          ) : (
            <Button size="sm" disabled>
              Register repository
            </Button>
          )}
          {!leadAdds && <AddRepository go={go} more={grants !== null && grants.length > 0} kind={leadDrawn ? 'secondary' : 'primary'} />}
        </span>,
      )}
      <RepositoriesTabs current="list" go={go} />
      {roleGate !== null && (
        <p className="ur-small" data-role-gate="member">
          {roleGate}
        </p>
      )}
      {grants !== null && <RepositoriesLead grants={grants} missing={missing} go={go} gated={roleGate !== null} />}
      <UrRegion state={access.state} route="GET /v1/access" what="Your access" onRetry={access.reload} lines={4}>
        {(o) => (
          <ReachableRepositories
            grants={o.grants}
            onChange={access.reload}
            empty={<ChooseOnGitHub go={go} />}
            orgNote={false}
            registered={registeredCell}
          />
        )}
      </UrRegion>
      {failed !== null && (
        <Banner tone="bad" title="Index now was refused">
          {failed}
        </Banner>
      )}
      {/* THE COUNT HEADS THE CARD GRID (QA1 config-19). As a lone note,
          `3 registered` floated between the reach table and the cards. */}
      <h3 className="c-count-note">{meta}</h3>
      <UrRegion
        state={state}
        route="GET /v1/repositories"
        what="The tenant's registered repositories"
        plural
        onRetry={reload}
        empty={
          chosen.length > 0 ? (
            <div className="ur-cards">{chosen}</div>
          ) : (
            <EmptyState
              kind="empty"
              heading="No repositories registered"
              action={
                roleGate === null ? (
                  <UrNavButton to={REGISTER} go={go}>
                    Register repository
                  </UrNavButton>
                ) : (
                  <Button disabled>Register repository</Button>
                )
              }
            >
              None yet. A repository is registered after it is chosen; registering starts its index, so planners and
              agents start from its map.
            </EmptyState>
          )
        }
      >
        {(repos) => (
          <div className="ur-cards">
            {/* BY FULL NAME, THE REACH TABLE'S ORDER (config2-09): in the order
                served, the cards and the table above them listed the same
                repositories in two orders. */}
            {[...repos].sort((a, b) => repoName(a).localeCompare(repoName(b))).map((r) => (
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
                counts={countDoc?.[r.repo_id] ?? null}
                countsWhy={countDoc === null ? countsWhy : 'Registered after the counts were read: Refresh counts it'}
                onRemove={
                  roleGate === null
                    ? () => {
                        setRemoveFailed(null)
                        setRemoving(r)
                      }
                    : undefined
                }
              />
            ))}
            {chosen}
          </div>
        )}
      </UrRegion>
      {/* WHAT WORKFLOW FILES MEANS, SAID ONCE FOR EVERY CARD (visual QA VQ-043). Under each card's switch the
          same three lines repeated on all six cards and doubled their height against S3a's compact card; the
          switch itself is on the repository's page now, which says it again beside it. */}
      {registered !== null && registered.length > 0 && (
        <p className="ur-hint" data-note="workflow-files">
          <b>Workflow files:</b> {WORKFLOW_FILES_WHY}
        </p>
      )}
      {removing !== null && (
        <TypedConfirm
          title={`Remove ${repoName(removing)}?`}
          name={repoName(removing)}
          verb="Remove registration"
          keep="Keep it registered"
          busy={removeBusy ? 'Removing…' : false}
          onConfirm={() => void remove(removing)}
          onClose={() => {
            if (!removeBusy) setRemoving(null)
          }}
          error={removeFailed === null ? undefined : `Not removed: ${removeFailed}`}
        >
          <RemoveWords r={removing} counts={countDoc?.[removing.repo_id] ?? null} />
        </TypedConfirm>
      )}
    </div>
  )
}

/**
 * WHAT REMOVE DOES, SAID BEFORE IT IS SENT (BGD, 2026-10-25). The API
 * disables every grant on the registration's secrets in the transaction that
 * removes it (`delete_repository`), so a re-registration revives none; the
 * person's own access on GitHub and in the reach list is not touched.
 */
function RemoveWords({ r, counts }: { r: RepoRecord; counts: RepoCounts | null }) {
  const n = counts?.secrets ?? null
  // Zero is said as zero (review shot light-desktop-03): "no grant is
  // disabled, and registering it again turns none back on" read as a riddle.
  const secrets =
    n === 0
      ? 'It has no secrets, so no secret grant is disabled with it.'
      : `Every grant on its ${n === null ? 'secrets' : n === 1 ? 'secret' : `${n} secrets`} is disabled with it, and registering it again turns none back on.`
  return (
    <>
      <p>
        {repoName(r)} leaves this workspace's registry: it is no longer indexed, and agents no longer start from its map.{' '}
        {secrets}
      </p>
      <p className="ur-small">
        Your own access to it is not changed: if it is still chosen, it stays in Repositories SwarmCloud may reach as you,
        and comes back here as chosen, not registered.
      </p>
    </>
  )
}

/** The empty reach list's sentence on this page: the owners are on GitHub access (config-e.html S3c). */
function ChooseOnGitHub({ go }: { go: (to: string) => void }) {
  return (
    <>
      Choose repositories from an enabled owner on{' '}
      <UrLink to={ADD_REPOSITORY_ADDRESS} go={go}>
        GitHub access
      </UrLink>
      , or type owner/repo there.
    </>
  )
}

/** The Registered link: this page's own card, brought into view, never a navigation. */
function toCard(e: MouseEvent, repoId: string) {
  if (!routedClick(e)) return
  e.preventDefault()
  document.getElementById(cardId(repoId))?.scrollIntoView?.({ block: 'center', behavior: 'smooth' })
}

/**
 * A count as a card says it: none for zero, a dash with its reason when
 * unread. A linked count is linked whole ("QA targets: 1"): a link on the
 * digit alone was a one-character target on a phone (review shot
 * dark-phone390-02).
 */
function CountLine({ row, label, n, why, to, go }: { row: string; label: string; n: number | null | undefined; why: string; to?: string; go: (to: string) => void }) {
  const words = n === null || n === undefined ? null : n === 0 ? 'none' : String(n)
  // A span in its card's shared `.ur-cmeta` line, which draws the dot between the two counts.
  return (
    <span data-row={row}>
      {words === null ? (
        <span>
          {label}: <Dash why={why} />
        </span>
      ) : to !== undefined ? (
        <UrLink to={to} go={go}>
          {label}: {words}
        </UrLink>
      ) : (
        <span>
          {label}: <b>{words}</b>
        </span>
      )}
    </span>
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
 * names the credential it uses -- the workspace token, the workspace owner's
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
 * THE CARD SAYS THE TWO SWITCHES' STATE AND NOTHING ELSE (BGD's answer to
 * visual QA VQ-043, 2026-10-26: "Show state only on the card"). One read-only
 * line, "Agents may file issues on · Workflow files off", each word the
 * registration's value as served: `agents_may_file_issues` (AU-0 REG-FLAG,
 * agent-autonomy.md §3.2) and `workflow_files` (#2029). The switches, who may
 * use them (a workspace admin) and "Workflows: not granted" are on the
 * repository's own page, the card's Open (RepositoriesDetail.tsx, Settings):
 * two switches with their read-only lines doubled the card against S3a's
 * compact one. Null is a dash, never a confident off.
 */
function SwitchStates({ r }: { r: RepoRecord }) {
  const fi = r.agents_may_file_issues
  const wf = r.workflow_files
  return (
    <div className="ur-cmeta" data-row="switches">
      <span className="ur-file-issues" data-file-issues={fi === null ? undefined : String(fi)}>
        Agents may file issues {fi === null ? <Dash why="Whether agents may file issues was not served" /> : <b>{fi ? 'on' : 'off'}</b>}
      </span>
      <span className="ur-workflow-files" data-workflow-files={wf === null ? undefined : String(wf)}>
        Workflow files {wf === null ? <Dash why="Whether agents may push workflow files was not served" /> : <b>{wf ? 'on' : 'off'}</b>}
      </span>
    </div>
  )
}

/**
 * THE DOT'S WORDS, BESIDE IT (console visual QA pass 1, config-17, part of
 * #2596). A red ◆ after the freshness chip on every card read as an
 * unexplained mark: its words were only the tooltip and the accessible name.
 * A dot that asks for something says what, in a word a person can read
 * without hovering. A green dot asks for nothing and stays a dot. The full
 * reason stays the tooltip.
 *
 * IT NAMES THE PERMISSION AND WHERE IT IS GRANTED (console visual QA pass 2,
 * config2-01; part of #2694). "1 GitHub permission missing" never said which,
 * and on the fixture console it sat on example-api, whose reach row above
 * reads Clone ok, Push ok, Pull request ok: the two measure different things
 * (the reach table what you may do as yourself, this the App's grant), so
 * the line says the permission by name -- "Not granted on GitHub: Actions
 * (read)" -- and links where it is fixed: an App permission on Workspace
 * setup, where the App and each org's grants are drawn; a token's on Git
 * tokens. A refusal that names no permission (`permission: null`, a token's
 * measured one) is counted, as before.
 */
function permissionName(g: ForgeGap): string {
  const plain = g.permission!.replace(/_/g, ' ')
  const word = plain.charAt(0).toUpperCase() + plain.slice(1)
  return g.access === null ? word : `${word} (${g.access})`
}

function PermWord({ row, go }: { row: ForgeRepoStatus | undefined; go: (to: string) => void }) {
  if (row === undefined) return null
  const s = repoStatus(row)
  if (s.tone === 'ok') return null
  if (s.tone === 'unknown') {
    return (
      <span className="ur-mu" data-perm-word={s.tone} title={s.tip} aria-hidden="true">
        GitHub permissions not all checked
      </span>
    )
  }
  const named = new Map<string, ForgeGap>()
  for (const g of row.blocked_by) if (g.permission !== null && !named.has(g.permission)) named.set(g.permission, g)
  const unnamed = row.blocked_by.filter((g) => g.permission === null).length
  const names = [...named.values()].map(permissionName)
  const word =
    names.length > 0
      ? `Not granted on GitHub: ${names.join(', ')}${unnamed > 0 ? ` and ${unnamed} more` : ''}`
      : unnamed > 1
        ? `${unnamed} GitHub permissions missing`
        : 'A GitHub permission is missing'
  const where = row.credential === 'token' ? { to: GT_PAGE, label: 'Git tokens' } : { to: WORKSPACE_SETUP_ADDRESS, label: 'Workspace setup' }
  return (
    <span className="ur-mu ur-perm" data-perm-word={s.tone}>
      <span title={s.tip} aria-hidden="true">
        {word}
      </span>{' '}
      <UrLink to={where.to} go={go}>
        Grant it in {where.label}
      </UrLink>
    </span>
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
  counts = null,
  countsWhy = 'The counts were not read',
  onRemove,
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
  /** Its secrets and QA targets, from the page's one counts read; null while not read. */
  counts?: RepoCounts | null
  /** Why the counts are a dash, when they are. */
  countsWhy?: string
  /** Remove, which asks for the typed name first; absent below `member`, so a viewer has none. */
  onRemove?: () => void
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
    <Card className="ur-card" title={repoName(r)} id={cardId(r.repo_id)}>
      {/* THE BADGE HAS ONE SLOT (visual QA run 6, work-a-009, part of #1680).
          As the head's action it sat right of the name, and a long name pushed
          it onto a line of its own, so no two cards drew it in the same place.
          It is the first row under the name now, from the card's left edge,
          whatever the name's length. */}
      <div className="ur-cbadge" data-badge="">
        <UrFreshPill f={f} />
        <RepoDot row={perm} />
        <PermWord row={perm} go={go} />
      </div>
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
      <div className="ur-cmeta">{lastIndexed(r)}</div>
      {/* SCHEDULE ON ITS OWN LINE (work-a-009): beside the last-indexed item it
          wrapped under it on one card and stayed beside it on the next, so the
          cards' rows did not line up. */}
      <div className="ur-cmeta" data-row="schedule">
        <span>Schedule {sched === null ? <Dash why="The schedule was not served" /> : <b>{sched}</b>}</span>
      </div>
      <IndexingUses r={r} go={go} />
      {/* Secrets and QA targets share a line (VQ-043): two short counts were two of the card's rows. */}
      <div className="ur-cmeta">
        <CountLine row="secrets" label="Secrets" n={counts?.secrets} why={countsWhy} go={go} />
        <CountLine row="qa-targets" label="QA targets" n={counts?.qa_targets} why={countsWhy} to={qaTargetsOf(repoName(r))} go={go} />
      </div>
      <SwitchStates r={r} />
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
        {onRemove !== undefined && (
          <Button size="sm" kind="danger" onClick={onRemove}>
            Unregister
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
      {/* THE CACHE SECTION, LAST AND FOLDED (graph-answer-cache.md §5.1): it reads nothing until opened. */}
      <UrGraphCacheSection r={r} />
    </Card>
  )
}
