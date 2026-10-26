import { useEffect, useId, useState, type FormEvent } from 'react'
import {
  askToJoin, loadMyJoinRequests, lookupJoinCode, withdrawJoinRequest,
  type ApiError, type Result,
} from './api'
import { Banner, Button, Card } from './components'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { PageHead } from './Shell'
import { timeAgo, type JoinLookup, type JoinRequestMine } from './types'
import './styles/keys.css'
import './styles/workspace-settings.css'

/**
 * /JOIN, A PERSON ASKS TO JOIN A GROUP WORKSPACE (lane JOIN-UI-PERSON;
 * docs/design/join-requests.md §4.1, §4.2, §4.8; part of #2325).
 *
 * A workspace admin turns its join link on in Settings › People and roles and
 * shares `https://<console>/join#<code>`. This page is where the link lands.
 *
 * THE CODE NEVER LEAVES THE BROWSER EXCEPT IN A BODY (§4.2). It rides in the
 * URL's fragment, which a browser never sends, so it reaches no request log
 * and no load balancer's access log. `App` reads it off the bar once
 * (`joinCodeFrom`), before rewriting the bar to `/join` without it, and hands
 * it here; it is posted to `POST /v1/join/lookup` and `POST /v1/join-requests`
 * in their bodies and nowhere else -- never a path, a query, storage or a log.
 * A reload therefore lands on the page without a code, which still shows the
 * person's own requests: the request they sent is there.
 *
 * A CODE THAT DOES NOT WORK SAYS ONE THING. The API answers an unknown, a
 * rotated and a switched-off code with the same 404, so this page cannot tell
 * them apart either, and does not pretend to.
 *
 * WHAT A WORKING CODE SHOWS is the workspace's display name, and nothing
 * else: no id, no group, no member count, no admin's name.
 */

/** The page's path. Not a section and not a tab: only a shared link leads here. */
export const JOIN_PATH = '/join'

/** The code a join link carries in its fragment, on `/join` only; null anywhere else or with none. */
export function joinCodeFrom(pathname: string, hash: string): string | null {
  if (pathname !== JOIN_PATH && pathname !== `${JOIN_PATH}/`) return null
  const code = hash.replace(/^#/, '').trim()
  return code === '' ? null : code
}

const MINE_ROUTE = 'GET /v1/join-requests/mine'
/** The longest note the API keeps (`joinrequests.MESSAGE_MAX`). */
const MESSAGE_MAX = 200
const DAY_MS = 24 * 3600 * 1000

/** A refusal in the API's words: its message, and its code when it has one. */
function refusal(e: ApiError): string {
  return e.code === null ? e.message : `${e.message} (${e.code})`
}

/** How long an open request has left, from its `expires_at`. */
export function openFor(expiresAt: string, now: number = Date.now()): string {
  const t = new Date(expiresAt).getTime()
  if (!Number.isFinite(t)) return 'open until an admin answers'
  const days = Math.ceil((t - now) / DAY_MS)
  if (days <= 0) return 'closing now'
  if (days === 1) return 'open for less than a day more'
  return `open ${days} more days`
}

export function JoinWorkspaceScreen({ code }: { code: string | null }) {
  const mine = useUrRead(loadMyJoinRequests, 'join-requests-mine')
  return (
    <div className="pk-page jw-page">
      <PageHead title="Join a workspace" />
      {code === null ? <NoCode /> : <Ask code={code} onSent={mine.reload} />}
      <Card title="Your requests to join" action={<UrRefresh reads={[mine]} />}>
        <UrRegion state={mine.state} route={MINE_ROUTE} what="Your requests to join" plural onRetry={mine.reload}
          empty={<p className="ur-small">You have not asked to join a workspace.</p>}>
          {(rows) => <MyRequests rows={rows} onChange={mine.reload} />}
        </UrRegion>
      </Card>
    </div>
  )
}

function NoCode() {
  return (
    <Card title="Ask with a join link">
      <p className="pk-lead">
        To ask to join a team&rsquo;s workspace, open the join link one of its admins shared with you: it ends
        in <code>/join#</code> and a code.
      </p>
      <p className="ur-small">
        The code is read once, when the link is opened, and is not kept in the address: open the link again to ask
        again. An admin who has not shared one turns it on in Settings &rsaquo; People and roles.
      </p>
    </Card>
  )
}

function Ask({ code, onSent }: { code: string; onSent: () => void }) {
  const [look, setLook] = useState<Result<JoinLookup>>({ status: 'loading', since: Date.now() })
  const [nonce, setNonce] = useState(0)
  useEffect(() => {
    let live = true
    setLook({ status: 'loading', since: Date.now() })
    void lookupJoinCode(code).then((r) => {
      if (live) setLook(r)
    })
    return () => {
      live = false
    }
  }, [code, nonce])

  if (look.status === 'loading') {
    return (
      <Card title="Checking the link">
        <p className="ur-small" role="status">Checking which workspace this link is for…</p>
      </Card>
    )
  }
  if (look.status === 'error') {
    // Every code that does not work is the one 404 (§4.2): unknown, rotated or
    // switched off read alike, here as on the API.
    if (look.error.httpStatus === 404) {
      return (
        <Banner tone="bad" title="This join link does not work." role="alert">
          Its admin may have switched it off or made a new one, or only part of it was copied. Ask the workspace&rsquo;s
          admin for a new link.
        </Banner>
      )
    }
    return (
      <Banner tone="bad" title="The link could not be checked." role="alert"
        actions={<Button size="sm" onClick={() => setNonce((n) => n + 1)}>Retry</Button>}>
        {refusal(look.error)}
      </Banner>
    )
  }
  if (look.status === 'empty') return null
  return <AskForm code={code} name={look.data.display_name || 'this workspace'} onSent={onSent} />
}

function AskForm({ code, name, onSent }: { code: string; name: string; onSent: () => void }) {
  const id = useId()
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [sent, setSent] = useState<JoinRequestMine | null>(null)
  const [failed, setFailed] = useState<string | null>(null)

  async function ask(e: FormEvent) {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setFailed(null)
    const r = await askToJoin(code, message)
    setBusy(false)
    if (r.status === 'error') {
      setFailed(refusal(r.error))
      return
    }
    if (r.status === 'ok' || r.status === 'stale') setSent(r.data)
    onSent()
  }

  if (sent !== null) {
    return (
      <Card title={`Ask to join ${name}`}>
        <Banner tone="info" title={`Your request to join ${name} is waiting for its admins.`} role="status">
          It is {openFor(sent.expires_at)}. Their answer shows under Your requests to join, below.
        </Banner>
      </Card>
    )
  }

  return (
    <Card title={`Ask to join ${name}`}>
      <form className="pk-form" autoComplete="off" aria-labelledby={`${id}-h`} onSubmit={(e) => void ask(e)}>
        <p id={`${id}-h`} className="pk-lead">
          This link is {name}&rsquo;s. Ask to join it, and one of its admins approves or denies your request.
        </p>
        <p className="ur-small">
          They see your email and the note below. Until one approves it you cannot see anything in {name}.
        </p>
        <label htmlFor={`${id}-note`}>A note for its admins (optional)</label>
        <textarea id={`${id}-note`} className="c-inp" rows={2} maxLength={MESSAGE_MAX} value={message}
          onChange={(e) => setMessage(e.target.value)} />
        {failed !== null && <Banner tone="bad" title={`Your request was not sent: ${failed}`} role="alert" />}
        <div className="pk-form-row">
          <Button kind="primary" type="submit" busy={busy}>Ask to join</Button>
        </div>
      </form>
    </Card>
  )
}

/** A request's state, in words, and what it means for the person. */
export function requestWords(r: JoinRequestMine, now: number = Date.now()): { state: string; meaning: string } {
  switch (r.state) {
    case 'requested':
      return { state: 'Waiting for an admin', meaning: `Nobody has answered yet; it is ${openFor(r.expires_at, now)}.` }
    case 'approved':
      return {
        state: 'Approved',
        meaning: `Switch to ${r.display_name} from the workspace menu. If it is not there yet, its admin still has to add you to its group.`,
      }
    case 'denied':
      return { state: 'Denied', meaning: r.reason !== undefined ? `The admin said: “${r.reason}”` : 'The admin gave no reason that could be read.' }
    case 'withdrawn':
      return { state: 'Withdrawn', meaning: 'You withdrew it.' }
    case 'expired':
      return { state: 'Expired', meaning: 'Nobody answered in time. Open the join link again to ask again.' }
  }
}

/** When the row's state began: the decision, the expiry, or the ask. */
function whenOf(r: JoinRequestMine): string {
  if (r.decided_at !== undefined) return r.decided_at
  return r.state === 'expired' ? r.expires_at : r.requested_at
}

function MyRequests({ rows, onChange }: { rows: JoinRequestMine[]; onChange: () => void }) {
  const [busy, setBusy] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const [failed, setFailed] = useState<string | null>(null)

  async function withdraw(r: JoinRequestMine) {
    setBusy(r.ref)
    setDone(null)
    setFailed(null)
    const answer = await withdrawJoinRequest(r.ref)
    setBusy(null)
    if (answer.status === 'error') {
      setFailed(`Your request to join ${r.display_name} was not withdrawn: ${refusal(answer.error)}`)
      return
    }
    setDone(`Your request to join ${r.display_name} is withdrawn.`)
    onChange()
  }

  return (
    <>
      {done !== null && <p className="pk-saved" role="status">{done}</p>}
      {failed !== null && <Banner tone="bad" title={failed} role="alert" />}
      {/* keys.css's stacking table: below 720 px each request is a card. */}
      <table role="table" className="mcp-servers" aria-label="Your requests to join">
        <thead>
          <tr>
            <th scope="col">Workspace</th>
            <th scope="col">State</th>
            <th scope="col">When</th>
            <th scope="col">What it means</th>
            <th scope="col" aria-label="Actions" />
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const words = requestWords(r)
            const at = whenOf(r)
            return (
              <tr key={r.ref} data-state={r.state}>
                <th scope="row" data-label="Workspace">{r.display_name}</th>
                <td data-label="State"><b>{words.state}</b></td>
                <td data-label="When" title={at}>{at === '' ? '' : timeAgo(at)}</td>
                <td data-label="What it means">{words.meaning}</td>
                <td data-label="Actions">
                  {r.state === 'requested' && (
                    <Button size="sm" aria-label={`Withdraw your request to join ${r.display_name}`}
                      busy={busy === r.ref} disabled={busy !== null && busy !== r.ref}
                      onClick={() => void withdraw(r)}>
                      Withdraw
                    </Button>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </>
  )
}
