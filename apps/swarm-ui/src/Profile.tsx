import { useEffect, useId, useState, type FormEvent, type MouseEvent, type ReactNode } from 'react'
import { loadProfile, putPreferences, NOTIFICATION_TOPICS, REMINDER_CADENCES } from './api'
import { Banner, Button, Card, CardLink, acknowledge } from './components'
import { chooseTenant, type ApiError } from './fetch'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { PageHead } from './Shell'
import { noteOwnName } from './Spine'
import { humaniseUntil, timeAgo } from './types'
import './styles/keys.css'
import type {
  NotificationTopic,
  Preferences,
  Profile,
  ProfileClaudeAccount,
  ProfileGitHub,
  ProfileKeyStatus,
  ProfileQuotaWindow,
  ProfileWorkspace,
  ReminderCadence,
} from './types'

/**
 * SETTINGS › PROFILE (lane PROFILE-PAGE, part of #1505;
 * docs/design/user-profile.md §4.1). Everything that is about the signed-in
 * person, in four cards, from ONE read (`GET /v1/profile`):
 *
 *   1. you and your workspaces -- email, display name, platform role once,
 *      each workspace with your role in it, and your default workspace;
 *   2. your connections -- exactly GitHub and the TypeSafe key (Q6: there are
 *      no SwarmCloud API keys, so the page never offers a row for them);
 *   3. what reaches your bell, and how often reminders repeat;
 *   4. your Claude accounts.
 *
 * THE PAGES THAT OWN A THING STAY ITS SOURCE. Each summary links to its owning
 * page: Work › Setup, Work › Access, Settings › API keys, Capacity › Accounts.
 * A GitHub row for a workspace other than the acting one switches workspace
 * first (`chooseTenant`, with the panel's own toast), because Access is
 * scoped to the acting workspace and the address never selects one.
 *
 * A SECTION THE API COULD NOT READ SAYS SO. It is absent from the response
 * and named in `unavailable`; it is drawn as "could not be read", never as an
 * empty list, which would tell a person their token or account is gone. With
 * `preferences` unavailable every editor is disabled, because a save could
 * only overwrite what the page did not see.
 *
 * THE ROLE IS DRAWN AS GIVEN (Q7). `role.name` comes from
 * `workspace_role.py::role_in`, and the roles build (#1518) may serve any
 * name; nothing here branches on it, so a new role needs no page change.
 *
 * A SAVE REPLACES THE WHOLE SET AT THE REVISION READ. Each editor sends its
 * own change with the other fields as last read, and `revision`; a 409 means
 * another tab saved first, and the page reads again rather than guessing.
 */

const PROFILE_ROUTE = 'GET /v1/profile'

const TOPIC_WORDS: Readonly<Record<NotificationTopic, string>> = {
  approval: 'Approvals waiting for you',
  waiting_step: 'Workflow steps waiting for you',
  question: 'Agents asking you something',
  failed: 'Failed runs',
  finished: 'Finished runs',
  account: 'Your accounts and connections',
  admin: 'Admin requests (admins only)',
}

const REMINDER_WORDS: Readonly<Record<ReminderCadence, string>> = {
  as_set: 'As each item sets it',
  every_4h: 'Every 4 hours',
  daily: 'Once a day',
  never: 'Never',
}

const GITHUB_STATE: Readonly<Record<string, string>> = {
  active: 'Connected',
  refresh_failed: 'Renewal failed',
  revoked: 'Revoked',
  not_connected: 'Not connected',
}

const IGNORED_WORDS: Readonly<Record<string, string>> = {
  not_a_member: 'you are no longer a member of it',
  not_ready: 'your personal workspace is not ready',
  unknown: 'it is not one of your workspaces',
}

const USES_WORDS = { user: 'your key', tenant: 'the workspace key', none: 'no key' } as const

/** "Section could not be read", said in place of the section. */
function Unreadable({ what }: { what: string }) {
  return (
    <p className="ur-small" role="status" data-unreadable="">
      {`${what} could not be read just now. Refresh to try again.`}
    </p>
  )
}

/** The words a save's refusal is shown in: the API's own message, verbatim. */
function refusal(e: ApiError): string {
  return e.message
}

/** A link into a workspace-scoped page: switch to `ws` first when it is not the acting one. */
function switchFirst(ws: ProfileWorkspace | undefined) {
  return (e: MouseEvent<HTMLAnchorElement>) => {
    if (ws === undefined || ws.is_acting || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
    chooseTenant(ws.tenant_id)
    acknowledge(
      <>
        Now acting as <b>{ws.display_name}</b>.
      </>,
    )
  }
}

export function ProfileScreen() {
  const profile = useUrRead(loadProfile, 'profile')
  return (
    <div className="pf-page">
      <PageHead title="Profile">
        <UrRefresh reads={[profile]} />
      </PageHead>
      <UrRegion state={profile.state} route={PROFILE_ROUTE} what="Your profile" onRetry={profile.reload}>
        {(d) => <ProfileCards profile={d} onReload={profile.reload} />}
      </UrRegion>
    </div>
  )
}

/** The four cards for one read. Exported for tests that draw a fixture directly. */
export function ProfileCards({ profile, onReload }: { profile: Profile; onReload: () => void }) {
  // The last read, updated in place by a save's answer so a second save in a
  // row carries the revision the first one produced.
  const [p, setP] = useState(profile)
  useEffect(() => setP(profile), [profile])
  // The panel's signed-in line shows this name's first word (Spine.tsx
  // `noteOwnName`): this page's read and save tell it, so it needs no read.
  useEffect(() => {
    if (p.identity.email !== '') noteOwnName(p.identity.email, p.identity.display_name)
  }, [p.identity.email, p.identity.display_name])
  const [changed, setChanged] = useState<string | null>(null)

  const prefsUnreadable = p.unavailable.includes('preferences') || p.revision === null || p.notifications === undefined
  const stored: Preferences = p.notifications?.preferences ?? { muted: [], reminder: 'as_set' }
  const storedDefault = p.default_workspace.source === 'profile' ? p.default_workspace.tenant_id : null

  /** Save one change with everything else as last read. */
  async function save(change: Change): Promise<SaveOutcome> {
    if (prefsUnreadable || p.revision === null) return { refused: 'Your preferences could not be read; retry shortly.' }
    const r = await putPreferences({
      display_name: change.display_name !== undefined ? change.display_name : p.identity.display_name,
      default_workspace: change.default_workspace !== undefined ? change.default_workspace : storedDefault,
      notifications: change.notifications ?? stored,
      revision: p.revision,
    })
    if (r.status === 'error') {
      if (r.error.httpStatus === 409) {
        setChanged('Your preferences were changed in another tab or window, so nothing was saved. The page has read them again: make your change once more.')
        onReload()
        return 'reloaded'
      }
      return { refused: refusal(r.error) }
    }
    if (r.status === 'ok' || r.status === 'stale') {
      const saved = r.data
      setChanged(null)
      setP((was) => ({
        ...was,
        identity: { ...was.identity, ...saved.identity },
        default_workspace: saved.default_workspace,
        notifications: saved.notifications,
        revision: saved.revision,
        workspaces: was.workspaces.map((w) => ({ ...w, is_default: w.tenant_id === saved.default_workspace.tenant_id })),
      }))
    }
    return 'saved'
  }

  return (
    <>
      {prefsUnreadable && (
        <Banner tone="warn" title="Your preferences could not be read; retry shortly.">
          You can still see who you are and your workspaces. Your name, default workspace and notifications cannot be changed
          until the preferences can be read, so nothing you did not see is overwritten.
        </Banner>
      )}
      {changed !== null && <Banner tone="info" title={changed} role="alert" />}
      <div role="region" aria-label="You and your workspaces" data-profile-section="you">
        <YouCard p={p} disabled={prefsUnreadable} save={save} />
      </div>
      <div role="region" aria-label="Your connections" data-profile-section="connections">
        <GitHubCard p={p} />
        <TypeSafeCard p={p} />
      </div>
      <div role="region" aria-label="Notifications" data-profile-section="notifications">
        <NotificationsCard p={p} stored={stored} disabled={prefsUnreadable} save={save} />
      </div>
      <div role="region" aria-label="Claude accounts" data-profile-section="claude-accounts">
        <ClaudeAccountsCard p={p} />
      </div>
    </>
  )
}

interface Change {
  display_name?: string | null
  default_workspace?: string | null
  notifications?: Preferences
}

/** A save stored the change, was refused with the API's words, or met a newer revision and read again. */
type SaveOutcome = 'saved' | 'reloaded' | { refused: string }

type Save = (change: Change) => Promise<SaveOutcome>

/** Run one editor's save and say what happened to it. */
function useSave(save: Save) {
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  async function run(change: Change) {
    if (busy) return
    setBusy(true)
    setFailed(null)
    setSaved(false)
    const out = await save(change)
    setBusy(false)
    if (out === 'saved') setSaved(true)
    else if (out !== 'reloaded') setFailed(out.refused)
  }
  return { busy, failed, saved, setSaved, run }
}

/** "Saved" and its reach, shown under an editor after its save. */
function Saved() {
  return (
    <p className="pk-saved" role="status">
      Saved. It applies to new sessions within a minute.
    </p>
  )
}

// -- 1. You and your workspaces -----------------------------------------------------

/** What the name editor says as it is typed: the rules of §4.4, in words. */
function nameHint(value: string): string | null {
  const v = value.trim().replace(/\s+/g, ' ')
  if (v === '') return null
  if (v.length > 64) return 'At most 64 characters.'
  if (/[0-9]/.test(v)) return 'No digits.'
  if (v.includes('@')) return 'No @: a name cannot hold an email address.'
  if (!/^[\p{L}\p{M} .'-]+$/u.test(v)) return "Only letters, spaces and . ' - are allowed."
  return null
}

function YouCard({ p, disabled, save }: { p: Profile; disabled: boolean; save: Save }) {
  const id = useId()
  const initial = p.identity.display_name ?? p.identity.display_name_suggested ?? ''
  const [name, setName] = useState(initial)
  useEffect(() => setName(initial), [initial])
  const { busy, failed, saved, setSaved, run } = useSave(save)
  const hint = nameHint(name)

  function submit(e: FormEvent | MouseEvent, value: string | null) {
    e.preventDefault()
    if (!disabled) void run({ display_name: value })
  }

  const needsSetup = p.workspaces.every((w) => !(w.kind === 'user' && w.selectable))
  return (
    <Card title="You" action={<CardLink href="/setup">Work › Setup</CardLink>}>
      <dl className="pf-facts">
        <dt>Email</dt>
        <dd>{p.identity.email}</dd>
        <dt>Platform role</dt>
        <dd>{p.identity.platform_role ?? (p.identity.is_admin_unresolved ? 'could not be read' : 'none')}</dd>
      </dl>
      <form className="pk-form" aria-label="Change your display name" onSubmit={(e) => submit(e, name.trim() === '' ? null : name)}>
        <label htmlFor={`${id}-n`}>Display name</label>
        <p className="ur-small">
          Shown to other people beside your email, and used to name your agents&rsquo; commits. Letters, spaces and . &rsquo; -
          only; up to 64 characters.
        </p>
        <div className="pk-form-row">
          <input
            id={`${id}-n`}
            className="ur-search"
            value={name}
            maxLength={128}
            autoComplete="name"
            disabled={disabled}
            onChange={(e) => {
              setName(e.target.value)
              setSaved(false)
            }}
          />
          <Button kind="primary" size="sm" type="submit" busy={busy ? 'Saving…' : false} disabled={disabled || hint !== null}>
            Save name
          </Button>
          {p.identity.display_name !== null && (
            <Button size="sm" disabled={disabled || busy} onClick={(e) => submit(e, null)}>
              Show my email only
            </Button>
          )}
        </div>
        {hint !== null && <p className="ur-small">{hint}</p>}
        {p.identity.display_name === null && p.identity.display_name_suggested !== null && (
          <p className="ur-small">Suggested from your sign-in. Others see your email alone until you save a name.</p>
        )}
      </form>
      {saved && <Saved />}
      {failed !== null && <Banner tone="bad" title={failed} role="alert" />}
      <h3>Workspaces</h3>
      <ul className="pf-list" aria-label="Your workspaces">
        {p.workspaces.map((w) => (
          <li key={w.tenant_id} data-tenant={w.tenant_id}>
            <b>{w.display_name}</b>
            {' · '}
            <span className="pf-role">{w.role.name}</span>
            {w.role.via !== null && <span className="ur-small">{` via ${w.role.via}`}</span>}
            {w.kind === 'user' && w.state !== null && w.state !== 'ready' && <span className="ur-small">{` · ${w.state}`}</span>}
            {w.is_acting && <span className="ur-small"> · you are here</span>}
            {w.is_default && <span className="ur-small"> · default</span>}
          </li>
        ))}
      </ul>
      {needsSetup && <p className="ur-small">You have no ready personal workspace. Request one on Work › Setup.</p>}
      {p.workspaces.length === 0 && <p className="ur-small">You are not in any workspace yet.</p>}
      <DefaultEditor p={p} disabled={disabled} save={save} />
    </Card>
  )
}

function DefaultEditor({ p, disabled, save }: { p: Profile; disabled: boolean; save: Save }) {
  const id = useId()
  const current = p.default_workspace.source === 'profile' ? (p.default_workspace.tenant_id ?? '') : ''
  const [pick, setPick] = useState(current)
  useEffect(() => setPick(current), [current])
  const { busy, failed, saved, setSaved, run } = useSave(save)
  const choices = p.workspaces.filter((w) => w.selectable)

  function submit(e: FormEvent) {
    e.preventDefault()
    if (!disabled) void run({ default_workspace: pick === '' ? null : pick })
  }

  const ignored = p.default_workspace.ignored
  return (
    <form className="pk-form" aria-label="Change your default workspace" onSubmit={(e) => submit(e)}>
      <label htmlFor={`${id}-d`}>Default workspace</label>
      <p className="ur-small">
        Where the console and /sc open when you have not picked a workspace. A switch in the panel lasts for that tab only.
      </p>
      <div className="pk-form-row">
        <select id={`${id}-d`} className="ur-search" value={pick} disabled={disabled} onChange={(e) => {
            setSaved(false)
            setPick(e.target.value)
          }}>
          <option value="">Your personal workspace, else your first group</option>
          {choices.map((w) => (
            <option key={w.tenant_id} value={w.tenant_id}>
              {w.display_name}
            </option>
          ))}
        </select>
        <Button kind="primary" size="sm" type="submit" busy={busy ? 'Saving…' : false} disabled={disabled || pick === current}>
          Save default
        </Button>
      </div>
      {ignored !== null && (
        <p className="ur-small">{`Your saved default is not in use because ${IGNORED_WORDS[ignored] ?? ignored}.`}</p>
      )}
      {saved && <Saved />}
      {failed !== null && <Banner tone="bad" title={failed} role="alert" />}
    </form>
  )
}

// -- 2. Connections: exactly GitHub and TypeSafe (Q6) ------------------------------

function GitHubRow({ row, ws }: { row: ProfileGitHub; ws: ProfileWorkspace | undefined }) {
  const state = row.state ?? 'not_connected'
  const facts: string[] = [GITHUB_STATE[state] ?? state]
  if (row.forge_login !== null) facts.push(`@${row.forge_login}`)
  if (row.method !== null) facts.push(row.method)
  if (row.access_expires_at !== null) {
    const left = new Date(row.access_expires_at).getTime() - Date.now()
    facts.push(left > 0 ? `token expires in ${humaniseUntil(left)}` : 'token expired')
  }
  if (row.refreshed_at !== null) facts.push(`renewed ${timeAgo(row.refreshed_at)}`)
  return (
    <li data-tenant={row.tenant_id}>
      <b>{ws?.display_name ?? row.tenant_id}</b>
      {' · '}
      {facts.join(' · ')}
      {row.failure?.recovery != null && <span className="ur-small">{` ${row.failure.recovery}`}</span>}{' '}
      <a className="c-link" href="/access" onClick={switchFirst(ws)}>
        {ws === undefined || ws.is_acting ? 'Open Work › Access' : `Open Work › Access in ${ws.display_name}`}
      </a>
    </li>
  )
}

function GitHubCard({ p }: { p: Profile }) {
  const rows = p.connections.github
  return (
    <Card title="GitHub" action={<CardLink href="/access">Work › Access</CardLink>}>
      {rows === undefined || p.unavailable.includes('github') ? (
        <Unreadable what="Your GitHub connections" />
      ) : (
        <ul className="pf-list" aria-label="GitHub, one row per workspace">
          {rows.map((row) => (
            <GitHubRow key={row.tenant_id} row={row} ws={p.workspaces.find((w) => w.tenant_id === row.tenant_id)} />
          ))}
        </ul>
      )}
    </Card>
  )
}

function keyWords(s: ProfileKeyStatus): string {
  if (!s.set) return 'Not set'
  const parts = ['Set']
  if (s.set_at !== null) parts.push(timeAgo(s.set_at))
  if (s.set_by !== null) parts.push(`by ${s.set_by}`)
  const head = parts.join(' ')
  return s.version === null ? head : `${head} · version ${s.version}`
}

function TypeSafeCard({ p }: { p: Profile }) {
  const ts = p.connections.typesafe
  return (
    <Card title="TypeSafe key" action={<CardLink href="/settings/keys">Settings › API keys</CardLink>}>
      {ts === undefined || p.unavailable.includes('typesafe') ? (
        <Unreadable what="Your TypeSafe key status" />
      ) : (
        <dl className="pf-facts">
          <dt>Your key</dt>
          <dd>{ts.personal === null ? 'No ready personal workspace to keep it in' : keyWords(ts.personal)}</dd>
          <dt>{`The key for ${ts.acting_workspace.tenant_id}`}</dt>
          <dd>{ts.acting_workspace.set ? 'Set' : 'Not set'}</dd>
          <dt>{`Your tasks in ${ts.acting_workspace.tenant_id} would use`}</dt>
          <dd>{USES_WORDS[ts.uses]}</dd>
        </dl>
      )}
    </Card>
  )
}

// -- 3. Notifications ---------------------------------------------------------------

function NotificationsCard({ p, stored, disabled, save }: { p: Profile; stored: Preferences; disabled: boolean; save: Save }) {
  const id = useId()
  const [muted, setMuted] = useState<NotificationTopic[]>(stored.muted)
  const [reminder, setReminder] = useState<ReminderCadence>(stored.reminder)
  const storedKey = `${stored.muted.join(',')}|${stored.reminder}`
  useEffect(() => {
    setMuted(stored.muted)
    setReminder(stored.reminder)
    // `storedKey` names the stored set; the array is new on every render.
  }, [storedKey])
  const { busy, failed, saved, setSaved, run } = useSave(save)
  const topics = p.notifications?.topics ?? [...NOTIFICATION_TOPICS]
  const dirty = `${NOTIFICATION_TOPICS.filter((t) => muted.includes(t)).join(',')}|${reminder}` !== storedKey

  function submit(e: FormEvent) {
    e.preventDefault()
    if (!disabled) void run({ notifications: { muted: NOTIFICATION_TOPICS.filter((t) => muted.includes(t)), reminder } })
  }

  return (
    <Card title="Notifications">
      <form className="pk-form" aria-label="Change your notifications" onSubmit={(e) => submit(e)}>
        <fieldset disabled={disabled}>
          <legend>What reaches your bell</legend>
          {topics.map((t) => (
            <label key={t} className="pf-topic">
              <input
                type="checkbox"
                checked={!muted.includes(t)}
                disabled={disabled}
                onChange={(e) => {
                  setSaved(false)
                  setMuted((was) => (e.target.checked ? was.filter((x) => x !== t) : [...was, t]))
                }}
              />{' '}
              {TOPIC_WORDS[t]}
            </label>
          ))}
          <p className="ur-small">
            Anything may be muted. A muted item that needs you still shows as a quiet count under the bell, and still waits in
            Approvals.
          </p>
        </fieldset>
        <label htmlFor={`${id}-r`}>Remind me again about something I have read</label>
        <div className="pk-form-row">
          <select
            id={`${id}-r`}
            className="ur-search"
            value={reminder}
            disabled={disabled}
            onChange={(e) => {
              setSaved(false)
              setReminder(REMINDER_CADENCES.find((c) => c === e.target.value) ?? 'as_set')
            }}
          >
            {REMINDER_CADENCES.map((c) => (
              <option key={c} value={c}>
                {REMINDER_WORDS[c]}
              </option>
            ))}
          </select>
          <Button kind="primary" size="sm" type="submit" busy={busy ? 'Saving…' : false} disabled={disabled || !dirty}>
            Save notifications
          </Button>
        </div>
      </form>
      {saved && <Saved />}
      {failed !== null && <Banner tone="bad" title={failed} role="alert" />}
    </Card>
  )
}

// -- 4. Claude accounts ------------------------------------------------------------

function windowWords(label: string, w: ProfileQuotaWindow | undefined): string | null {
  if (w === undefined || w.utilization === null) return null
  const pct = `${label} ${Math.round(w.utilization * 100)}%`
  if (w.resets_at === null) return pct
  const left = new Date(w.resets_at).getTime() - Date.now()
  return left > 0 ? `${pct}, resets in ${humaniseUntil(left)}` : pct
}

function AccountRow({ a }: { a: ProfileClaudeAccount }) {
  const facts: (string | null)[] = [
    a.relation === 'own' ? 'yours' : 'lent to you',
    a.state,
    windowWords('5-hour', a.windows.five_hour),
    windowWords('7-day', a.windows.seven_day),
    a.lent_to_count !== null && a.lent_to_count > 0 ? `you lend it to ${a.lent_to_count}` : null,
    a.stale ? 'figures may be out of date' : null,
  ]
  return (
    <li>
      <b>{a.label ?? a.account_id ?? 'an account'}</b>
      {' · '}
      {facts.filter((f): f is string => f !== null).join(' · ')}
    </li>
  )
}

function ClaudeAccountsCard({ p }: { p: Profile }) {
  const c = p.claude_accounts
  const actingGroup = p.workspaces.some((w) => w.is_acting && w.kind === 'group')
  let body: ReactNode
  if (c === undefined || p.unavailable.includes('claude_accounts')) {
    body = <Unreadable what="Your Claude accounts" />
  } else if (c.personal_workspace === null) {
    body = (
      <p className="ur-small">
        You have no personal workspace yet, which is where your own accounts live. <a className="c-link" href="/setup">Request one on Work › Setup</a>.
      </p>
    )
  } else {
    body = (
      <>
        {c.accounts.length === 0 ? (
          <p className="ur-small">{`${c.personal_workspace} has no Claude accounts of its own and none lent to it.`}</p>
        ) : (
          <ul className="pf-list" aria-label="Your Claude accounts">
            {c.accounts.map((a, i) => (
              <AccountRow key={a.account_id ?? `account-${i}`} a={a} />
            ))}
          </ul>
        )}
        {c.unreadable_document_count > 0 && (
          <p className="ur-small">{`${c.unreadable_document_count} account record${c.unreadable_document_count === 1 ? '' : 's'} could not be read, so this list may be short.`}</p>
        )}
      </>
    )
  }
  return (
    <Card title="Claude accounts" action={<CardLink href="/capacity/accounts">Capacity › Accounts</CardLink>}>
      {body}
      {actingGroup && <p className="ur-small">This workspace&rsquo;s shared accounts are on Capacity › Accounts.</p>}
    </Card>
  )
}
