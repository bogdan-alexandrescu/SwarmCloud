import { useState, type ReactNode } from 'react'
import { loadAccess, putAccessGrant, revokeAccessGrant, verifyAccessGrant } from './api'
import { AccessRefusal, AskFirst, GitHubLink, LossAsk, takesAccessAway, type Choice } from './Access'
import { Banner, Button, Card, Dash, EmptyState } from './components'
import type { ApiError } from './fetch'
import { UrCap, UrRadio, UrRegion, useUrRead } from './RepositoriesParts'
import type { AccessCheck, AccessCheckName, AccessGrant, AccessMode, AccessVerifyFailure } from './types'
import { timeAgo } from './types'
import './styles/repositories.css'
import './styles/onboarding.css'
import './styles/reachable-repositories.css'

/**
 * "REPOSITORIES SWARMCLOUD MAY REACH AS YOU" (GITHUB-ACCESS-PAGE; part of
 * #2157). It was the last card of Work › Access, under Choose repositories.
 * BGD's decision of 2026-10-23 puts it on Config › Repositories, which is
 * repository management, and leaves Config › GitHub access only the GitHub
 * App integration and Choose repositories (proposal E, config-e.html S2 and
 * S3). So it is its own component, moved whole and unchanged in behaviour:
 * Verify A's grid, failures first, every action asking first where Access
 * asked (RSA-1), the push test D6 allows. Access.tsx keeps the pieces the
 * chooser shares with it (AskFirst, LossAsk, AccessRefusal), and this file
 * imports them rather than restating them; Access does not import this file.
 *
 * Two ways to mount it: `ReachableRepositories` with the grants of a
 * `GET /v1/access` the page already read, or `ReachableRepositoriesRead`,
 * which makes that read itself.
 *
 * ITS COPY IS THE PAGE'S, BY PROP (REPOS-FOLLOWUPS; BGD, 2026-10-25). Left
 * alone it reads as it did on GitHub access, where the owners sit above it
 * and orgs are shown: "Choose repositories from an enabled owner above" and
 * the org-removal sentence. Config › Repositories shows no owner and no org,
 * so it passes its own empty-state sentence (a link to GitHub access,
 * config-e.html S3c) and `orgNote={false}`, and the Registered column
 * (S3a), which only that page can fill: it holds the registry.
 */

/** The empty state as GitHub access words it: the owners are above it there. */
const EMPTY_HERE = 'Choose repositories from an enabled owner above, or type owner/repo.'

const CHECKS: readonly { key: AccessCheckName; label: string }[] = [
  { key: 'clone', label: 'Clone' },
  { key: 'push', label: 'Push' },
  { key: 'pull_request', label: 'Pull request' },
]

/** 0 a failure, 1 not yet known, 2 passed: a failure leads the grid (`reachOrder`). */
export function grantRank(g: AccessGrant): number {
  const needed: AccessCheckName[] = g.mode === 'write' ? ['clone', 'push', 'pull_request'] : ['clone']
  const states = needed.map((k) => g.checks[k]?.state)
  if (states.some((s) => s === 'missing')) return 0
  if (states.some((s) => s !== 'ok')) return 1
  return 2
}

/**
 * THE GRID'S ORDER: FAILURES FIRST, THEN BY NAME (console visual QA pass 2,
 * config2-09; part of #2694). Ordered by every rank, a grant not verified yet
 * (rank 1) went above the ones that passed, so the fixture console's table
 * read example-web, example-api, payments while the registered cards under
 * it read example-api, example-web, payments: one page, the same three
 * repositories in two orders. A failure still leads, because it asks for
 * something; everything else is in the cards' order, by full name.
 */
export function reachOrder(a: AccessGrant, b: AccessGrant): number {
  return Number(grantRank(b) === 0) - Number(grantRank(a) === 0) || a.repository.localeCompare(b.repository)
}

/**
 * A CHECK A READ GRANT DOES NOT NEED: THE DASH AND ITS REASON, BOTH SEEN
 * (console visual QA pass 2, config2-07, was config-18; part of #2694). As a
 * bare Dash the read row's Push, Pull request and Push test read as three
 * unknowns, the reason only on hover. config-e.html S3a's reach table draws
 * "— read grant" there, so the words sit beside the dash; the Dash keeps the
 * full reason as its name, and the words are hidden from a screen reader,
 * which already hears it.
 */
function ReadGrantDash({ why }: { why: string }) {
  return (
    <span className="ac-why" data-why="read grant">
      <Dash why={why} />{' '}
      <small aria-hidden="true">read grant</small>
    </span>
  )
}

function CheckCell({ grant, name, label }: { grant: AccessGrant; name: AccessCheckName; label: string }) {
  const c: AccessCheck | undefined = grant.checks[name]
  if (c?.state === 'not_required' || (c === undefined && grant.mode === 'read' && name !== 'clone')) {
    return <ReadGrantDash why={`${label} is not needed for a read grant`} />
  }
  if (c === undefined) return <UrCap label={label} cell={{ state: 'unknown', reason: 'Not checked yet' }} word="not checked" />
  const state = c.state === 'ok' || c.state === 'missing' ? c.state : 'unknown'
  const reason = [c.code, c.checked_at !== null ? `checked ${timeAgo(c.checked_at)}` : null].filter(Boolean).join(' · ') || null
  return <UrCap label={label} cell={{ state, reason }} word={state} />
}

/**
 * D6's opt-in push test, as the grid draws it. A read grant is never offered
 * one: SwarmCloud enforces read (D9), and the API refuses it before GitHub is
 * reached. A write grant shows its last answer -- ok, missing or unknown,
 * unknown never a pass -- or that it has not been run.
 */
function PushTestCell({ grant, check }: { grant: AccessGrant; check: AccessCheck | undefined }) {
  if (grant.mode !== 'write') return <ReadGrantDash why="Push test is not offered on a read grant: SwarmCloud never pushes there (D9)" />
  if (check === undefined) return <UrCap label="Push test" cell={{ state: 'unknown', reason: 'Not run: it is opt-in' }} word="not run" />
  const state = check.state === 'ok' || check.state === 'missing' ? check.state : 'unknown'
  const reason = [check.code, check.checked_at ? `run ${timeAgo(check.checked_at)}` : null].filter(Boolean).join(' · ') || null
  return <UrCap label="Push test" cell={{ state, reason }} word={state} />
}

/** "Repositories SwarmCloud may reach as you": the person's grants, verified (Verify A). */
export function ReachableRepositories({
  grants,
  onChange,
  empty = EMPTY_HERE,
  orgNote = true,
  registered,
}: {
  grants: AccessGrant[]
  onChange: () => void
  /** The empty state's sentence; GitHub access' own when not given. */
  empty?: ReactNode
  /** The org-removal sentence: only where orgs are shown (GitHub access). */
  orgNote?: boolean
  /** Each row's Registered cell (S3a); no column when not given. */
  registered?: (g: AccessGrant) => ReactNode
}) {
  const [busy, setBusy] = useState<string | null>(null)
  const [failures, setFailures] = useState<Record<string, AccessVerifyFailure[]>>({})
  /** The grant whose push test is waiting for a yes. Nothing is written before it. */
  const [confirming, setConfirming] = useState<string | null>(null)
  /** Each push test's own answer, newest first over the overview's: the person just ran it. */
  const [pushed, setPushed] = useState<Record<string, AccessCheck>>({})
  const [refused, setRefused] = useState<{ repo: string; error: ApiError } | null>(null)
  /** A Remove or a move down from Write, waiting for a yes (RSA-1). Nothing is written before it. */
  const [asking, setAsking] = useState<{ repo: string; to: Choice } | null>(null)
  const [all, setAll] = useState(false)
  const [filter, setFilter] = useState('')
  const sorted = [...grants]
    .filter((g) => filter.trim() === '' || g.repository.toLowerCase().includes(filter.trim().toLowerCase()))
    .sort(reachOrder)

  async function verify(g: AccessGrant): Promise<void> {
    setBusy(g.repo_id)
    setRefused(null)
    const res = await verifyAccessGrant(g.repo_id)
    setBusy(null)
    if (res.status === 'error') {
      setRefused({ repo: g.repo_id, error: res.error })
      return
    }
    if (res.status === 'ok') setFailures((f) => ({ ...f, [g.repo_id]: res.data.failures }))
  }

  async function pushTest(g: AccessGrant): Promise<void> {
    setConfirming(null)
    setBusy(g.repo_id)
    setRefused(null)
    const res = await verifyAccessGrant(g.repo_id, ['push_test'])
    setBusy(null)
    if (res.status === 'error') {
      setRefused({ repo: g.repo_id, error: res.error })
      return
    }
    if (res.status !== 'ok') return
    const check = res.data.grant.checks.push_test
    const answered = res.data.push_test
    if (check !== undefined) {
      setPushed((p) => ({ ...p, [g.repo_id]: answered !== undefined ? { ...check, ...answered } : check }))
    }
    setFailures((f) => ({ ...f, [g.repo_id]: res.data.failures }))
    onChange()
  }

  async function verifyAll() {
    setAll(true)
    for (const g of grants) await verify(g)
    setAll(false)
    onChange()
  }

  async function change(g: AccessGrant, mode: AccessMode | 'remove') {
    setAsking(null)
    setBusy(g.repo_id)
    setRefused(null)
    const res = mode === 'remove' ? await revokeAccessGrant(g.repo_id) : await putAccessGrant(g.repo_id, g.repository, mode)
    setBusy(null)
    if (res.status === 'error') {
      setRefused({ repo: g.repo_id, error: res.error })
      return
    }
    onChange()
  }

  const newest = grants.map((g) => g.verified_at).filter((v): v is string => v !== null).sort().pop() ?? null

  // `.ac-page` carries the phone's 44px targets (onboarding.css). The card
  // brings it along, so it keeps them on a page that is not GitHub access.
  return (
    <div className={`ac-page ac-reach${registered !== undefined ? ' has-registered' : ''}`}>
      <Card
        className="ac-grants"
        title="Repositories SwarmCloud may reach as you"
        action={
          <span className="ur-acts">
            <Button
              size="sm"
              busy={all}
              disabled={grants.length === 0}
              onClick={() => {
                void verifyAll()
              }}
            >
              Re-check all
            </Button>
          </span>
        }
        foot={<>Last verified {newest !== null ? timeAgo(newest) : <Dash why="No grant has been verified yet" />}. An agent for a repository not on this list is refused at submission.</>}
      >
        {grants.length === 0 ? (
          <EmptyState kind="empty" heading="No repository chosen yet">
            {empty}
          </EmptyState>
        ) : (
          <>
            {grants.length > 8 && (
              <input
                type="search"
                className="ur-search ac-filter"
                aria-label="Filter your repositories"
                placeholder="Filter"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              />
            )}
            <div className="ac-grid-h" aria-hidden="true">
              <span>Repository</span>
              <span>Access</span>
              {CHECKS.map((c) => (
                <span key={c.key}>{c.label}</span>
              ))}
              <span>Push test</span>
              {registered !== undefined && <span>Registered</span>}
              <span />
            </div>
            <ul className="ac-grid" aria-label="Your granted repositories, failures first">
              {sorted.map((g) => {
                const rank = grantRank(g)
                const fails = failures[g.repo_id] ?? []
                const pushCheck = pushed[g.repo_id] ?? g.checks.push_test
                return (
                  <li key={g.repo_id} className={`ac-grant is-rank-${rank}`} data-repo={g.repository} data-rank={rank}>
                    <div className="ac-row-name">
                      <b>{g.repository}</b>
                      <small className="ur-mu">
                        {g.verified_at !== null ? `verified ${timeAgo(g.verified_at)}` : 'not verified yet'}
                        {g.archived === true && ' · archived'}
                      </small>
                    </div>
                    <UrRadio<AccessMode>
                      label={`Access to ${g.repository}`}
                      value={g.mode}
                      disabled={busy === g.repo_id}
                      onChange={(m) => {
                        if (m === g.mode) return
                        if (takesAccessAway(g.mode, m)) setAsking({ repo: g.repo_id, to: m })
                        else void change(g, m)
                      }}
                      options={[
                        { key: 'read', label: 'Read' },
                        {
                          key: 'write',
                          label: 'Write',
                          disabled: g.mode !== 'write' && (g.can_push === false || g.archived === true),
                          title: g.archived === true ? 'Archived on GitHub' : g.can_push === false ? 'GitHub does not let you push here' : undefined,
                        },
                      ]}
                    />
                    {CHECKS.map((c) => (
                      <span key={c.key} className="ac-cell" data-check={c.key}>
                        <span className="ac-cell-l">{c.label}</span>
                        <CheckCell grant={g} name={c.key} label={c.label} />
                      </span>
                    ))}
                    <span className="ac-cell" data-check="push_test">
                      <span className="ac-cell-l">Push test</span>
                      <PushTestCell grant={g} check={pushCheck} />
                    </span>
                    {registered !== undefined && (
                      <span className="ac-cell" data-check="registered">
                        <span className="ac-cell-l">Registered</span>
                        {registered(g)}
                      </span>
                    )}
                    <span className="ac-grant-acts">
                      <Button size="sm" busy={busy === g.repo_id} onClick={() => void verify(g).then(onChange)}>
                        Verify
                      </Button>
                      {g.mode === 'write' && (
                        <Button size="sm" disabled={busy === g.repo_id} onClick={() => setConfirming(g.repo_id)}>
                          Push test
                        </Button>
                      )}
                      {/* "REMOVE ACCESS", NOT "REMOVE" (QA1 config-15). Each
                          registered card below has a Remove of its own, which
                          unregisters the repository; this one deletes the
                          person's grant, so the two controls for one
                          repository said the same word for two different
                          writes. It is the word the ask already acts with, so
                          while that ask is open it holds the one Remove
                          access in the row and this trigger steps aside. */}
                      {!(asking !== null && asking.repo === g.repo_id && asking.to === 'none') && (
                        <Button size="sm" kind="ghost" disabled={busy === g.repo_id} onClick={() => setAsking({ repo: g.repo_id, to: 'none' })}>
                          Remove access
                        </Button>
                      )}
                    </span>
                    {asking !== null && asking.repo === g.repo_id && (
                      <div className="ac-fails">
                        <LossAsk
                          repository={g.repository}
                          from={g.mode}
                          to={asking.to}
                          busy={busy === g.repo_id}
                          onAct={() => void change(g, asking.to === 'none' ? 'remove' : asking.to)}
                          onKeep={() => setAsking(null)}
                        />
                      </div>
                    )}
                    {confirming === g.repo_id && (
                      <div className="ac-fails">
                        <AskFirst
                          title={`Write a test branch to ${g.repository}?`}
                          act="Run push test"
                          actKind="primary"
                          keep="Cancel"
                          onAct={() => void pushTest(g)}
                          onKeep={() => setConfirming(null)}
                        >
                          <p className="ur-small">
                            SwarmCloud creates one branch, <code>swarmcloud/onboarding-check-&lt;nonce&gt;</code>, at the
                            head of {g.repository}'s default branch, as you, and deletes it straight away. It is the only
                            check that writes to GitHub.
                          </p>
                        </AskFirst>
                      </div>
                    )}
                    {pushCheck?.leftover === true && typeof pushCheck.branch === 'string' && (
                      <div className="ac-fails">
                        <Banner tone="warn" title="A test branch was left behind" role="status">
                          <p className="ur-small">
                            The push worked, but GitHub did not confirm the delete, so <code>{pushCheck.branch}</code> was
                            left behind in {g.repository}. Delete it on GitHub; SwarmCloud does not push to it.
                          </p>
                        </Banner>
                      </div>
                    )}
                    {fails.length > 0 && (
                      <div className="ac-fails">
                        {fails.map((f, i) => (
                          <div key={`${f.check}:${f.code}:${i}`} className="ob-issue" data-code={f.code}>
                            <p className="ur-small">
                              <b className="ur-bad">{f.code}</b> {f.copy}
                            </p>
                            {typeof f.url === 'string' && f.url.startsWith('https://github.com/') && <GitHubLink url={f.url}>Open on GitHub</GitHubLink>}
                          </div>
                        ))}
                      </div>
                    )}
                    {refused !== null && refused.repo === g.repo_id && (
                      <div className="ac-fails">
                        <AccessRefusal error={refused.error} title={`${g.repository} was not changed`} onRetry={() => void verify(g)} />
                      </div>
                    )}
                  </li>
                )
              })}
            </ul>
          </>
        )}
        {orgNote && (
          <p className="ur-hint">
            Removing an org deletes its grants at once, and agents naming its repositories are refused from then on. GitHub
            still lets your token reach an installed org until an org owner uninstalls the App or you disconnect.
          </p>
        )}
      </Card>
    </div>
  )
}

/** The card with its own `GET /v1/access` read, for a page that has none (Config › Repositories). */
export function ReachableRepositoriesRead() {
  const overview = useUrRead(loadAccess, 'access')
  return (
    <UrRegion state={overview.state} route="GET /v1/access" what="Your access" onRetry={overview.reload} lines={4}>
      {(o) => <ReachableRepositories grants={o.grants} onChange={overview.reload} />}
    </UrRegion>
  )
}
