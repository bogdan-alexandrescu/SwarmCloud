import { useState } from 'react'
import { loadReadableRepositories, loadRunnerProfiles, registerRepository, runRepositoryIndex, type RegisterRepositoryBody } from './api'
import { Banner, Button, Card, EmptyState } from './components'
import { normRepo, type Readable, type ReadableList } from './RepositoriesData'
import { LIST, UrCrumb, UrNavButton, UrRadio, UrRefresh, UrRegion, repoAddress, useUrRead, writeFailure } from './RepositoriesParts'
import { PageHead } from './Shell'
import { timeAgo } from './types'

/**
 * REGISTER REPOSITORY, pick C (repositories.html screen 3; PICKS.md, owner
 * 2026-10-05): list what the tenant's git token can read, pick one, then set
 * the schedule and the change trigger.
 *
 * THE LIST IS EVERY PAGE THE API SERVES (OB0, docs/onboarding.md §5): the
 * loader follows `next_page`, and a list the API `capped`, or one a later
 * page failed to extend, says so. Owner chips narrow it to one owner.
 *
 * A REPOSITORY THE LIST DOES NOT CARRY IS TYPED AS owner/repo (OB0; owner,
 * 2026-10-07, #780). It is registered through the same POST /v1/repositories,
 * which reads the forge before it stores anything, so a typed name the
 * platform cannot clone is refused there and the refusal is shown in the
 * API's words: that refusal is what names SSO or a token policy (OB0b).
 * NO CREDENTIAL IS ASKED FOR OR SHOWN:
 * the list names the secret it was read with by NAME (`swarm-tenant-eng-git`),
 * and the registration record holds no credential field (repo-index.md §1).
 *
 * Runner profiles are offered BY NAME from the catalogue (invariant 10): a
 * registration narrows what may run in the repository and never says what a
 * profile is.
 */

const INTERVALS = [
  { key: '6', label: '6 h' },
  { key: '12', label: '12 h' },
  { key: '24', label: '24 h' },
  { key: '168', label: '7 days' },
  { key: 'off', label: 'Off' },
] as const
type IntervalKey = (typeof INTERVALS)[number]['key']

type Trigger = 'poll' | 'webhook' | 'off'

/** repo-index.md §1: a registration's profiles default to claude-code. */
const DEFAULT_PROFILE = 'claude-code'

export function RegisterRepository({ go }: { go: (to: string) => void }) {
  const readable = useUrRead(loadReadableRepositories, 'readable')
  const [filter, setFilter] = useState('')
  const [owner, setOwner] = useState<string | null>(null)
  const [picked, setPicked] = useState<Readable | null>(null)
  const [typed, setTyped] = useState('')
  const [step, setStep] = useState<1 | 2>(1)
  const name = picked === null ? null : `${picked.owner}/${picked.repo}`
  const byName = typedRepository(typed)
  // A typed name is the pick while the field holds one; choosing a row empties the field.
  const chosen = typed.trim() !== '' ? byName : picked

  return (
    <div className="ur-page ur-register">
      <UrCrumb trail={[{ label: 'Work' }, { label: 'Repositories', to: LIST }, { label: 'Register repository' }]} go={go} />
      <PageHead title="Register repository">
        {/* The readable list's age, on the control that renews it (#98). */}
        <UrRefresh reads={[readable]} />
      </PageHead>
      <div className="ur-steps" aria-label="Steps">
        {step === 1 ? (
          <b>
            <i>1</i> Choose repository
          </b>
        ) : (
          <span>
            <i>1</i> Choose repository
          </span>
        )}
        <span aria-hidden> › </span>
        {step === 2 ? (
          <b>
            <i>2</i> Schedule and profiles
          </b>
        ) : (
          <span>
            <i>2</i> Schedule and profiles
          </span>
        )}
        <span aria-hidden> › </span>
        <span>
          <i>3</i> Register
        </span>
      </div>
      {step === 1 ? (
        <>
          <UrRegion
            state={readable.state}
            route="GET /v1/repositories/readable"
            what="The repositories the tenant's git token can read"
            // Until the API declares `readable` (it is not in repo-index.md §6.1),
            // `GET /v1/repositories/{repo_id}` matches it and answers its own
            // not_found for a repository called "readable": the route is absent.
            alsoNotServed={(e) => e.httpStatus === 404 && e.code === 'not_found'}
            onRetry={readable.reload}
            empty={
              <EmptyState kind="empty" heading="The token can read no repositories">
                The tenant's git token answered with no repositories. Grant it access to one, or store a token that has it
                with <code>scripts/create-secrets.sh --stdin</code>.
              </EmptyState>
            }
          >
            {(list) => {
              const n = list.total ?? list.repositories.length
              const by = list.secret_name ?? "the tenant's git token"
              const f = filter.trim().toLowerCase()
              const owners = ownerCounts(list)
              // A chip whose owner a refresh no longer lists stops filtering.
              const only = owner !== null && owners.some(([o]) => o === owner) ? owner : null
              const shown = list.repositories.filter(
                (r) => (only === null || r.owner === only) && (f === '' || `${r.owner}/${r.repo}`.toLowerCase().includes(f)),
              )
              const note = listNote(list, by)
              return (
                <div className="c-card ur-picker">
                  {note !== null && <p className="ur-hint is-warn ur-capped ur-picker-h">{note}</p>}
                  <div className="ur-picker-h">
                    <div className="ur-checks" role="group" aria-label="Owner">
                      <Button size="sm" kind={only === null ? 'secondary' : 'ghost'} aria-pressed={only === null} onClick={() => setOwner(null)}>
                        All owners <span className="ur-mu">{list.repositories.length}</span>
                      </Button>
                      {owners.map(([o, count]) => (
                        <Button key={o} size="sm" kind={only === o ? 'secondary' : 'ghost'} aria-pressed={only === o} onClick={() => setOwner(o)}>
                          {o} <span className="ur-mu">{count}</span>
                        </Button>
                      ))}
                    </div>
                    <input
                      type="search"
                      className="ur-search"
                      aria-label="Filter repositories"
                      placeholder={`Filter the ${n} repositories ${by} can read`}
                      value={filter}
                      onChange={(e) => setFilter(e.target.value)}
                    />
                  </div>
                  <div role="radiogroup" aria-label="Repository">
                    {shown.map((r) => {
                      const full = `${r.owner}/${r.repo}`
                      const on = typed.trim() === '' && name === full
                      return (
                        <button
                          key={full}
                          type="button"
                          role="radio"
                          className="ur-pick"
                          aria-checked={on}
                          aria-disabled={r.registered || undefined}
                          disabled={r.registered}
                          onClick={() => {
                            setPicked(r)
                            setTyped('')
                          }}
                        >
                          <span className="ur-dot" aria-hidden />
                          <b>{full}</b>
                          <span className="ur-mu">{r.default_branch ?? 'default branch not read'}</span>
                          <small>{r.registered ? 'already registered' : pickNote(r)}</small>
                        </button>
                      )
                    })}
                    {shown.length === 0 && (
                      <p className="ur-none">
                        {f === '' ? `No readable repository is owned by ${only ?? 'anyone'}.` : `No readable repository matches “${filter}”.`}
                      </p>
                    )}
                  </div>
                </div>
              )
            }}
          </UrRegion>
          {/* Outside the list's region: a token that lists nothing, or a list
              that is not served, still leaves a repository to be registered by name. */}
          <div className="ur-picker ur-typed">
            <label className="ur-fl" htmlFor="ur-typed">
              Not listed? Type owner/repo
            </label>
            <input
              id="ur-typed"
              type="text"
              className="ur-search"
              placeholder="owner/repo"
              autoComplete="off"
              spellCheck={false}
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
            />
            <p className="ur-hint">
              Registering reads the repository on GitHub first. If SwarmCloud cannot reach it, the refusal says why, in
              GitHub's terms.
            </p>
          </div>
          <div className="ur-formacts">
            <UrNavButton to={LIST} go={go} kind="ghost">
              Cancel
            </UrNavButton>
            <Button
              kind="primary"
              disabled={chosen === null}
              onClick={() => {
                if (chosen === null) return
                setPicked(chosen)
                setStep(2)
              }}
            >
              Next: schedule
            </Button>
          </div>
        </>
      ) : (
        picked !== null && <ScheduleStep picked={picked} go={go} onBack={() => setStep(1)} />
      )}
    </div>
  )
}

/**
 * A typed `owner/repo` as a pick, or null while it is not one. Only the shape
 * is checked here (one slash, two non-empty halves, no spaces): whether the
 * repository exists and can be read is the API's answer, shown verbatim.
 */
function typedRepository(text: string): Readable | null {
  const m = /^([^\s/]+)\/([^\s/]+)$/.exec(text.trim())
  if (m === null) return null
  return { owner: m[1]!, repo: m[2]!, default_branch: null, visibility: null, archived: null, pushed_at: null, registered: false }
}

/** Each owner in the list with how many of its repositories it carries, by name. */
function ownerCounts(list: ReadableList): [string, number][] {
  const counts = new Map<string, number>()
  for (const r of list.repositories) counts.set(r.owner, (counts.get(r.owner) ?? 0) + 1)
  return [...counts.entries()].sort(([a], [b]) => a.localeCompare(b))
}

/** Why the list may not be everything the token reads, or null when it is. */
function listNote(list: ReadableList, by: string): string | null {
  if (list.gap !== null) {
    return (
      `Page ${list.gap.page} of the list could not be read (${list.gap.message}), so this shows the first ` +
      `${list.repositories.length} only. Refresh to try again, or type owner/repo below.`
    )
  }
  if (!list.capped) return null
  const pages = list.max_pages ?? list.pages
  const size = list.per_page === null ? '' : ` of ${list.per_page}`
  return (
    `The list stops at ${pages} pages${size}, and ${by} can read more than that. ` +
    'A repository not listed here can still be registered: type its owner/repo below.'
  )
}

function pickNote(r: Readable): string {
  const vis = (r.visibility ?? 'visibility not read') + (r.archived === true ? ' · archived' : '')
  return r.pushed_at === null ? `${vis} · last push not read` : `${vis} · pushed ${timeAgo(r.pushed_at)}`
}

function ScheduleStep({ picked, go, onBack }: { picked: Readable; go: (to: string) => void; onBack: () => void }) {
  const profiles = useUrRead(loadRunnerProfiles, 'profiles')
  const [chosen, setChosen] = useState<string[] | null>(null)
  const [every, setEvery] = useState<IntervalKey>('24')
  const [trigger, setTrigger] = useState<Trigger>('poll')
  const [firstIndex, setFirstIndex] = useState(true)
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<string | null>(null)
  const [registeredId, setRegisteredId] = useState<string | null>(null)
  const full = `${picked.owner}/${picked.repo}`
  const names = profiles.state.status === 'ok' || profiles.state.status === 'stale' ? profiles.state.data : null
  // Until the person touches a box, the default is the design's: claude-code, when the catalogue has it.
  const selected = chosen ?? (names !== null && names.includes(DEFAULT_PROFILE) ? [DEFAULT_PROFILE] : [])

  async function submit() {
    setBusy(true)
    setRefused(null)
    const body: RegisterRepositoryBody = {
      repository: full,
      ...(picked.default_branch !== null ? { default_branch: picked.default_branch } : {}),
      ...(names !== null ? { allowed_profiles: selected } : {}),
      index: { interval_hours: every === 'off' ? 'off' : Number(every), on_change: trigger },
    }
    const res = await registerRepository(body)
    if (res.status === 'error') {
      setBusy(false)
      setRefused(writeFailure(res.error))
      return
    }
    const data = res.status === 'ok' ? (res.data as Record<string, unknown> | null) : null
    const rec = normRepo(data !== null && typeof data.repository === 'object' ? data.repository : data)
    const id = rec?.repo_id ?? (typeof data?.repo_id === 'string' ? data.repo_id : null)
    if (id === null) {
      go(LIST)
      return
    }
    if (firstIndex) {
      const run = await runRepositoryIndex(id, 'full')
      if (run.status === 'error') {
        setBusy(false)
        setRegisteredId(id)
        setRefused(`Registered, but the first index run was refused: ${writeFailure(run.error)}`)
        return
      }
    }
    go(repoAddress(id))
  }

  return (
    <Card className="ur-form" title={full}>
      <div className="ur-fgrid">
        <div className="ur-fl">
          Runner profiles<small>which may work in it</small>
        </div>
        <div>
          <UrRegion state={profiles.state} route="GET /v1/runtimes" what="The runner profiles" onRetry={profiles.reload} lines={1}>
            {(list) => (
              <div className="ur-checks">
                {list.map((p) => (
                  <label key={p} className="ur-check">
                    <input
                      type="checkbox"
                      checked={selected.includes(p)}
                      onChange={(e) => setChosen(e.target.checked ? [...selected, p] : selected.filter((x) => x !== p))}
                    />
                    {p}
                  </label>
                ))}
              </div>
            )}
          </UrRegion>
          <p className="ur-hint">By name only. A registration narrows what runs here; it never changes what a profile is.</p>
        </div>
        <div className="ur-fl">Re-index every</div>
        <div>
          <UrRadio label="Re-index every" options={INTERVALS} value={every} onChange={setEvery} />
        </div>
        <div className="ur-fl">Change trigger</div>
        <div>
          <UrRadio<Trigger>
            label="Change trigger"
            options={[
              { key: 'poll', label: 'Poll the default branch (every 5 min)' },
              { key: 'webhook', label: 'Webhook · phase 3', disabled: true, title: 'Webhooks come in phase 3 (repo-index.md §3.3)' },
              { key: 'off', label: 'Off' },
            ]}
            value={trigger}
            onChange={setTrigger}
          />
          <p className="ur-hint">At most one change-triggered run every 30 min; ten pushes in a minute cost one run.</p>
        </div>
        <div className="ur-fl">First index</div>
        <div>
          <label className="ur-check">
            <input type="checkbox" checked={firstIndex} onChange={(e) => setFirstIndex(e.target.checked)} />
            Index now, after registering
          </label>
          <p className="ur-hint">
            A full run. It queues like any task and holds a slot of this tenant's capacity only while it runs.
          </p>
        </div>
      </div>
      {refused !== null && (
        <Banner
          tone="bad"
          title={registeredId === null ? "Not registered" : "Registered; the first index was not queued"}
          actions={
            registeredId !== null ? (
              <UrNavButton to={repoAddress(registeredId)} go={go} size="sm">
                Open {full}
              </UrNavButton>
            ) : undefined
          }
        >
          {refused}
        </Banner>
      )}
      <div className="ur-formacts">
        <Button kind="ghost" onClick={onBack}>
          Back
        </Button>
        <Button kind="primary" busy={busy} onClick={() => void submit()}>
          {`Register ${full}`}
        </Button>
      </div>
    </Card>
  )
}
