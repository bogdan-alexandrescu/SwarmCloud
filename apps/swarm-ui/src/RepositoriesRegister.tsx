import { useState } from 'react'
import { loadReadableRepositories, loadRunnerProfiles, registerRepository, runRepositoryIndex, type RegisterRepositoryBody } from './api'
import { Banner, Button, Card, EmptyState } from './components'
import { normRepo, type Readable } from './RepositoriesData'
import { LIST, UrCrumb, UrNavButton, UrRadio, UrRefresh, UrRegion, repoAddress, useUrRead, writeFailure } from './RepositoriesParts'
import { PageHead } from './Shell'
import { timeAgo } from './types'

/**
 * REGISTER REPOSITORY, pick C (repositories.html screen 3; PICKS.md, owner
 * 2026-10-05): list what the tenant's git token can read, pick one, then set
 * the schedule and the change trigger.
 *
 * NOTHING IS TYPED AS owner/repo, so a registration can never name a
 * repository the platform cannot clone. NO CREDENTIAL IS ASKED FOR OR SHOWN:
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
  const [picked, setPicked] = useState<Readable | null>(null)
  const [step, setStep] = useState<1 | 2>(1)
  const name = picked === null ? null : `${picked.owner}/${picked.repo}`

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
              const shown = list.repositories.filter((r) => f === '' || `${r.owner}/${r.repo}`.toLowerCase().includes(f))
              return (
                <div className="c-card ur-picker">
                  <div className="ur-picker-h">
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
                      const on = name === full
                      return (
                        <button
                          key={full}
                          type="button"
                          role="radio"
                          className="ur-pick"
                          aria-checked={on}
                          aria-disabled={r.registered || undefined}
                          disabled={r.registered}
                          onClick={() => setPicked(r)}
                        >
                          <span className="ur-dot" aria-hidden />
                          <b>{full}</b>
                          <span className="ur-mu">{r.default_branch ?? 'default branch not read'}</span>
                          <small>{r.registered ? 'already registered' : pickNote(r)}</small>
                        </button>
                      )
                    })}
                    {shown.length === 0 && <p className="ur-none">No readable repository matches “{filter}”.</p>}
                  </div>
                </div>
              )
            }}
          </UrRegion>
          <div className="ur-formacts">
            <UrNavButton to={LIST} go={go} kind="ghost">
              Cancel
            </UrNavButton>
            <Button kind="primary" disabled={picked === null} onClick={() => setStep(2)}>
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

function pickNote(r: Readable): string {
  const vis = r.private === null ? 'visibility not read' : r.private ? 'private' : 'public'
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
