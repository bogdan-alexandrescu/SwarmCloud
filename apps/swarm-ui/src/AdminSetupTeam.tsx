/**
 * ADMIN › SETUP, ADD A TEAM (DAYONE-SETUP-UI-2; part of #2688; plan §5.1
 * "Second team (day two)", frame B15, GATE 1 DX2 B).
 *
 * A second team is added from the console with the same team panel the first
 * team's step draws, as a flow of its own, and the `swarm team add` line that
 * does the same from a terminal printed beside it. The line follows the form
 * as it is typed, so what the console sends and what the terminal would send
 * are visibly one call: both are `POST /v1/admin/setup/teams`
 * (`createSetupTeam`), then `swarm-workspace-apply` (design §5.7, one writer).
 *
 * Nothing here takes a credential: the team's git slot is filled through a
 * one-time link its owner opens (CLAUDE.md forge-token rule), never this form.
 */
import { useState } from 'react'
import { createSetupTeam, type ApiError, type SetupTeam } from './api'
import { Facts, type Fact } from './AdminSetupSteps'
import { Button, Card, CodeBlock } from './components'

/** One value of the line, quoted for a POSIX shell when it holds anything but an address's characters. */
function arg(v: string): string {
  return /^[\w@.+-]+$/.test(v) ? v : `'${v.replace(/'/g, `'\\''`)}'`
}

/** The form's fields as the setup API takes them: empty fields left out, invites split on commas and spaces. */
export function teamBody(f: { group: string; name: string; admin: string; invites: string }): { group: string; display_name?: string; first_admin?: string; invites: string[] } {
  const name = f.name.trim()
  const admin = f.admin.trim()
  return {
    group: f.group.trim(),
    ...(name === '' ? {} : { display_name: name }),
    ...(admin === '' ? {} : { first_admin: admin }),
    invites: f.invites.split(/[\s,]+/).filter(Boolean),
  }
}

/** The `swarm team add` line for the form as it stands; `<group>` until the group is named. */
export function teamAddLine(f: { group: string; name: string; admin: string; invites: string }): string {
  const b = teamBody(f)
  return [
    'swarm team add',
    b.group === '' ? '<group>' : arg(b.group),
    ...(b.display_name === undefined ? [] : [`--name ${arg(b.display_name)}`]),
    ...(b.first_admin === undefined ? [] : [`--admin ${arg(b.first_admin)}`]),
    ...b.invites.map((i) => `--invite ${arg(i)}`),
  ].join(' ')
}

/** A team's state in a word, from its workspace record and its smoke agent. */
function teamFact(t: SetupTeam): Fact {
  const ws = typeof t.progress.workspace_state === 'string' ? t.progress.workspace_state : null
  const smoke = t.smoke
  const failed = (ws !== null && (ws === 'failed' || ws === 'denied')) || smoke?.state === 'failed'
  const done = ws === 'ready' && smoke?.state === 'passed'
  const parts = [
    t.group,
    t.first_admin === null ? null : `admin ${t.first_admin}`,
    ws === null ? 'workspace apply not reported yet' : `workspace ${ws}`,
    smoke === null ? 'smoke agent not started' : `smoke ${smoke.state}${smoke.detail === null ? '' : `, ${smoke.detail}`}`,
  ].filter((p): p is string => p !== null)
  return { state: failed ? 'failed' : done ? 'done' : 'in_progress', name: t.workspace, text: parts.join(' · ') }
}

/** Add a team: the form, the line beside it, and the teams the deployment has. */
export function AdminSetupTeam({ teams, onChanged }: { teams: SetupTeam[]; onChanged?: () => void }) {
  const [group, setGroup] = useState('')
  const [name, setName] = useState('')
  const [admin, setAdmin] = useState('')
  const [invites, setInvites] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const [asked, setAsked] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const form = { group, name, admin, invites }
  const line = teamAddLine(form)
  const field = (label: string, value: string, set: (v: string) => void, hint: string, placeholder: string) => (
    <label className="c-field">
      <span className="c-lbl">{label}</span>
      <input
        className="c-inp"
        value={value}
        placeholder={placeholder}
        onChange={(e) => {
          set(e.target.value)
          setCopied(false)
        }}
      />
      <p className="c-hint">{hint}</p>
    </label>
  )
  const submit = () => {
    const body = teamBody(form)
    setBusy(true)
    setError(null)
    void createSetupTeam(body).then((r) => {
      setBusy(false)
      if (r.status === 'error') {
        setError(r.error)
        return
      }
      setAsked(r.status === 'ok' || r.status === 'stale' ? (r.data.team ?? body.group) : body.group)
      setGroup('')
      setName('')
      setAdmin('')
      setInvites('')
      onChanged?.()
    })
  }
  return (
    <section className="ws-grid is-setup asm-team" role="region" aria-label="Add a team">
      <Card title="Add a team" foot="The same call swarm team add makes: the team’s record, then swarm-workspace-apply makes its namespace, quota, verify-keys and worker identity.">
        <form
          className="asm-form"
          onSubmit={(e) => {
            e.preventDefault()
            submit()
          }}
        >
          <div className="asm-fields">
            {field('Workspace group', group, setGroup, 'A Google Workspace group; its members are the team', 'research@your-domain')}
            {field('Workspace name', name, setName, 'Shown in the console; the group’s name when left empty', 'research')}
            {field('First workspace admin', admin, setAdmin, 'Administers the team’s workspace', 'name@your-domain')}
            {field('Invite', invites, setInvites, 'Each gets a sign-in link; nobody pastes anything', 'one or more emails')}
          </div>
          <span className="ob-acts">
            <Button size="sm" kind="primary" type="submit" busy={busy ? 'Adding' : false} disabledReason={group.trim().length < 3 ? 'Name the Workspace group first' : undefined}>
              Add the team
            </Button>
          </span>
          {error !== null && (
            <div className="ob-issue" role="alert">
              <b>{error.message}</b>
              {error.kind === 'admin_required' && <span className="ur-hint">Only a platform admin can add a team.</span>}
            </div>
          )}
          {asked !== null && error === null && (
            <p className="ur-hint" role="status">
              {asked} was asked for: its progress shows under Teams as swarm-workspace-apply reports it.
            </p>
          )}
        </form>
      </Card>
      <div className="ws-col">
        <Card title="From a terminal">
          <p className="ur-hint">The same steps, from the machine swarm install runs on. It follows the form as you type.</p>
          <div role="group" aria-label="From a terminal">
            <CodeBlock
              title="Same steps"
              lang="shell"
              text={line}
              action={
                <Button size="sm" kind="ghost" onClick={() => void navigator.clipboard?.writeText(line).then(() => setCopied(true))}>
                  {copied ? 'Copied' : 'Copy'}
                </Button>
              }
            />
          </div>
        </Card>
        <div role="region" aria-label="Teams">
          <Card title="Teams">
            <Facts items={teams.map(teamFact)} label="Teams" />
          </Card>
        </div>
      </div>
    </section>
  )
}
