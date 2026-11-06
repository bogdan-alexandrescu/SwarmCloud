/**
 * ADMIN › SETUP, THE PAGE (DAYONE-SETUP-UI-2; part of #2688;
 * docs/design/day-one-plan.md §5.1, D26, frames B05-B09a, B11, B12b, B15).
 *
 * swarm install did the cloud part from a terminal and handed over to this
 * page for the people part. THE CONSOLE FINISH is a checklist beside a
 * working panel (the owner's pick, 2026-11-02; consolefinish/ChecklistFinish
 * .tsx): a status line saying what swarm install is doing now, eight steps
 * with who acts on each and since when, and the selected step's panel, which
 * holds today's step controls (`AdminSetupSteps`) unchanged. Everything is
 * read from `GET /v1/admin/setup` every 5 s (`useSetupState`); nothing here is
 * marked done by hand.
 *
 * THE ORDER IS sign-in client -> GitHub App -> first team -> Claude pool
 * accounts -> smoke agent -> verify -> Send to your team (BGD, 2026-10-29;
 * the sign-in client added by the owner's pick of 2026-11-01, issue #2964;
 * the smoke agent its own step in the owner's pick of 2026-11-02), after the
 * CLI's cloud part.
 *
 * WAITING FOR THE CLI (R8): when no CLI holds the install lease the status
 * line says swarm install is not running, when it was last seen, and to run
 * it again; every console step stays usable.
 *
 * DAY TWO (B15): once the first team exists, Add a team is the same team
 * panel as its own flow, with the `swarm team add` line printed beside it
 * (`AdminSetupTeam`).
 *
 * ONE PERSON (decision 7): the first-workspace step is the route's
 * `personal` row, the installer's personal workspace, named "Your
 * workspace"; there is no team step and no invites.
 *
 * THE FIXTURE BUILD draws the console finish's record moments
 * (consolefinish/moments.ts) at `?moment=<name>`; a live build never reads
 * the parameter.
 *
 * The Health page (`/admin/setup/health`) and the GitHub App callback
 * (`/admin/setup/github-app/callback`) are framed here too, so App.tsx routes
 * three screens from one module (SETUP-UI-3).
 */
import { useMemo, useRef, useState, type ReactNode } from 'react'
import { FIXTURE_CLOCK, normSetupState, type Result, type SetupState } from './api'
import { AdminSetupHealth } from './AdminSetupHealth'
import { GitHubAppCallback, useSetupState } from './AdminSetupSteps'
import { AdminSetupTeam } from './AdminSetupTeam'
import { Banner, Button, Card } from './components'
import { ChecklistFinish } from './consolefinish/ChecklistFinish'
import { fixtureMoment, momentSetupState } from './consolefinish/moments'
import { PageHead } from './Shell'
import './styles/config.css'
import './styles/onboarding.css'
import './styles/workspace-settings.css'
import './styles/admin-setup.css'

function SetupRead({ result, reload, children }: { result: Result<SetupState>; reload: () => void; children: (state: SetupState) => ReactNode }) {
  if (result.status === 'loading') {
    return (
      <Card title="Setup">
        <p className="ur-hint">Reading the install record…</p>
      </Card>
    )
  }
  if (result.status === 'error') {
    const admin = result.error.kind === 'admin_required'
    return (
      <Banner
        tone={admin ? 'warn' : 'bad'}
        title={admin ? 'Admin › Setup is for platform admins' : 'The install record could not be read'}
        actions={
          <Button size="sm" onClick={reload}>
            Read again
          </Button>
        }
      >
        {result.error.message}
      </Banner>
    )
  }
  if (result.status === 'empty') {
    return (
      <Banner tone="info" title="No install record yet">
        swarm install writes it when it starts. Run swarm install in a terminal to begin.
      </Banner>
    )
  }
  return <>{children(result.data)}</>
}

/**
 * The fixture build's moment (`?moment=<name>`, consolefinish/moments.ts) in
 * place of the fixture record, read at the fixture clock; a live build reads
 * only `GET /v1/admin/setup`.
 */
function useSetupRead(now: number | undefined): { result: Result<SetupState>; reload: () => void; now: number | undefined } {
  const read = useSetupState()
  const [moment] = useState(() => fixtureMoment())
  const fixed = useMemo(() => {
    if (moment === null) return null
    const at = Date.parse(FIXTURE_CLOCK)
    return { at, result: { status: 'ok', data: normSetupState(momentSetupState(moment, at)), fetchedAt: at } as Result<SetupState> }
  }, [moment])
  return fixed === null ? { ...read, now } : { result: fixed.result, reload: read.reload, now: fixed.at }
}

/** The page's body for one read of the record: exported so a test or a capture can draw a given state. */
export function AdminSetupPage({ state, now, onChanged }: { state: SetupState; now?: number; onChanged?: () => void }) {
  return (
    <>
      <ChecklistFinish state={state} now={now} onChanged={onChanged} />
      {state.teams.length > 0 && <AdminSetupTeam teams={state.teams} onChanged={onChanged} />}
    </>
  )
}

/**
 * `/admin/setup`, and its GitHub App callback page when `callback` holds the
 * query GitHub sent the person back with. `now` fixes the clock (a test, a
 * capture).
 */
export function AdminSetupScreen({ now, callback }: { now?: number; callback?: string }) {
  if (callback !== undefined) return <AdminSetupCallbackScreen search={callback} now={now} />
  return <SetupFirstRunScreen now={now} />
}

function SetupFirstRunScreen({ now }: { now?: number }) {
  // The Setup tab's page: the record read every 5 s, and drawn. (No doc
  // comment above it: test_nav_headings_agree.py follows AdminSetupScreen's
  // last return here only when that return ends its body.)
  const read = useSetupRead(now)
  return (
    <div className="ur-page asm-page cfc-page">
      <PageHead title="Setup" />
      <SetupRead result={read.result} reload={read.reload}>
        {(state) => <AdminSetupPage state={state} now={read.now} onChanged={read.reload} />}
      </SetupRead>
    </div>
  )
}

/** `/admin/setup/health`: the latest scheduled doctor run (B12b). */
export function AdminSetupHealthScreen({ now }: { now?: number }) {
  return (
    <div className="ur-page asm-page">
      <PageHead title="Health" />
      <p className="ur-sub">
        The hourly doctor’s checks, read-only, on the live deployment: the scheduled run’s findings, each with its repair. From a terminal, swarm doctor checks this machine and the deployment against the manifest. The install steps are on <a href="/admin/setup">Admin › Setup</a>.
      </p>
      <AdminSetupHealth now={now} />
    </div>
  )
}

/**
 * `/admin/setup/github-app/callback`: GitHub sends the person here with `code`
 * and `state` (`search`, read off the address on arrival). GitHub's page is
 * drawn as the GitHub step's panel beside the checklist. Which of the two
 * frames it gets is decided once, at the record's first answer, and never
 * changes: the callback posts GitHub's single-use code from its first mount,
 * so it is never mounted twice. Without a record (a refusal, no install yet)
 * it is drawn on its own, under the read's own words.
 */
export function AdminSetupCallbackScreen({ search, now }: { search: string; now?: number }) {
  const read = useSetupRead(now)
  const frame = useRef<'checklist' | 'alone' | null>(null)
  const last = useRef<SetupState | null>(null)
  if (read.result.status === 'ok' || read.result.status === 'stale') last.current = read.result.data
  if (frame.current === null && read.result.status !== 'loading') frame.current = last.current === null ? 'alone' : 'checklist'
  const callback = <GitHubAppCallback search={search} />
  return (
    <div className="ur-page asm-page cfc-page">
      <PageHead title="Setup" />
      {frame.current === 'checklist' && last.current !== null ? (
        <ChecklistFinish state={last.current} now={read.now} onChanged={read.reload} callback={callback} />
      ) : frame.current === 'alone' ? (
        <>
          <SetupRead result={read.result} reload={read.reload}>
            {() => null}
          </SetupRead>
          {callback}
        </>
      ) : (
        <SetupRead result={read.result} reload={read.reload}>
          {() => null}
        </SetupRead>
      )}
    </div>
  )
}
