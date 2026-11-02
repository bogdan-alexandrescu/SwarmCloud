/**
 * VOICE IT › PREFERENCES: THE CHATGPT SIGN-IN (the owner's pick, 2026-11-02:
 * "Sign-in button, then go live").
 *
 * Voice it talks on the person's own ChatGPT plan. swarm-voice reads that
 * plan's credential from the person's own personal secret; this pane is how
 * the person puts it there. It shows whether a sign-in is stored and since
 * when -- never a value: swarm-api's status route serves `stored` and
 * `stored_at` and nothing else -- and runs the sign-in:
 *
 *   1. "Sign in with ChatGPT" (or "Sign in again") asks swarm-api to start
 *      one, and opens ChatGPT's sign-in page in a new tab.
 *   2. ChatGPT's sign-in ends on an address on the person's own machine
 *      (`http://localhost:1455/auth/callback?...`), the one place the plan's
 *      client may return to, and the tab says the page cannot be reached.
 *      That is expected: the address carries the sign-in's code. The pane
 *      says so, in swarm-api's words, and asks for that address.
 *   3. The pasted address goes to swarm-api, which redeems it and stores the
 *      credential; the pane shows the stored state. The field is emptied
 *      either way and nothing pasted is kept or shown again.
 */
import { useCallback, useEffect, useState, type FormEvent } from 'react'

import { Banner, Button, Card, LoadingState, ToneMark } from '../components'
import { read, route, write } from '../fetch'
import { formatDateTime } from '../timezone'

/** Whether a sign-in is stored, and when. */
export const VOICE_CHATGPT = '/v1/voice/chatgpt'
/** Start one: the authorize URL, its state and what to tell the person. */
export const VOICE_CHATGPT_AUTHORIZE = '/v1/voice/chatgpt/authorize'
/** Finish it from the address the sign-in ended on. */
export const VOICE_CHATGPT_EXCHANGE = '/v1/voice/chatgpt/exchange'

export interface SignInStatus {
  stored: boolean
  stored_at: string | null
}

interface Started {
  authorize_url: string
  state: string
  instructions: string
}

function asStatus(data: unknown): SignInStatus | null {
  const d = data as Partial<SignInStatus> | null
  if (d === null || typeof d !== 'object' || typeof d.stored !== 'boolean') return null
  return { stored: d.stored, stored_at: typeof d.stored_at === 'string' ? d.stored_at : null }
}

function asStarted(data: unknown): Started | null {
  const d = data as Partial<Started> | null
  if (d === null || typeof d !== 'object') return null
  if (typeof d.authorize_url !== 'string' || !d.authorize_url.startsWith('https://')) return null
  if (typeof d.state !== 'string' || typeof d.instructions !== 'string') return null
  return { authorize_url: d.authorize_url, state: d.state, instructions: d.instructions }
}

/** The stored or not-stored line, with the one control that fits it. */
function StatusLine({ status, busy, onStart }: { status: SignInStatus; busy: boolean; onStart: () => void }) {
  return (
    <>
      <p>
        {status.stored ? (
          <ToneMark tone="ok">Signed in</ToneMark>
        ) : (
          <ToneMark tone="warn">Not signed in</ToneMark>
        )}{' '}
        {status.stored
          ? status.stored_at === null
            ? 'A ChatGPT sign-in is stored.'
            : `A ChatGPT sign-in is stored, since ${formatDateTime(status.stored_at)}.`
          : 'Voice it talks on your own ChatGPT plan. Sign in once and it is kept for you.'}
      </p>
      <span className="ob-acts">
        <Button kind={status.stored ? 'secondary' : 'primary'} busy={busy ? 'Starting…' : false} onClick={onStart}>
          {status.stored ? 'Sign in again' : 'Sign in with ChatGPT'}
        </Button>
      </span>
    </>
  )
}

/** Step two: where the sign-in ends, and the field for its address. */
function Finish({
  started,
  busy,
  onFinish,
  onCancel,
}: {
  started: Started
  busy: boolean
  onFinish: (address: string) => void
  onCancel: () => void
}) {
  const [address, setAddress] = useState('')
  const submit = (e: FormEvent) => {
    e.preventDefault()
    const pasted = address.trim()
    setAddress('')
    if (pasted !== '') onFinish(pasted)
  }
  return (
    <form onSubmit={submit} aria-label="Finish the ChatGPT sign-in">
      <p>{started.instructions}</p>
      <p>
        <a href={started.authorize_url} target="_blank" rel="noopener noreferrer">
          Open the ChatGPT sign-in
        </a>{' '}
        if the tab did not open.
      </p>
      <label className="c-field">
        <span className="c-lbl">The address the sign-in tab ended on</span>
        <input
          className="c-inp"
          value={address}
          autoComplete="off"
          spellCheck={false}
          placeholder="http://localhost:1455/auth/callback?code=…"
          onChange={(e) => setAddress(e.target.value)}
        />
      </label>
      <span className="ob-acts">
        <Button onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
        <Button kind="primary" type="submit" busy={busy ? 'Saving…' : false} disabled={address.trim() === ''}>
          Save sign-in
        </Button>
      </span>
    </form>
  )
}

/** Voice it › Preferences: the ChatGPT sign-in card. */
export function VoicePreferences() {
  const [status, setStatus] = useState<SignInStatus | null>(null)
  const [readError, setReadError] = useState<string | null>(null)
  const [started, setStarted] = useState<Started | null>(null)
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    void read<SignInStatus>(route(VOICE_CHATGPT), () => false).then((r) => {
      if (!live) return
      if (r.status === 'ok' || r.status === 'stale') {
        const s = asStatus(r.data)
        if (s === null) setReadError("swarm-api's answer did not say whether a sign-in is stored.")
        else setStatus(s)
      } else if (r.status === 'error') {
        setReadError(r.error.message)
      }
    })
    return () => {
      live = false
    }
  }, [])

  const start = useCallback(async () => {
    setBusy(true)
    setFailure(null)
    try {
      const r = await write(route(VOICE_CHATGPT_AUTHORIZE), 'POST')
      if (r.status === 'error' || r.status === 'stale') {
        setFailure(r.error.message)
        return
      }
      const s = r.status === 'ok' ? asStarted(r.data) : null
      if (s === null) {
        setFailure("swarm-api's answer carried no ChatGPT sign-in page.")
        return
      }
      setStarted(s)
      window.open(s.authorize_url, '_blank', 'noopener,noreferrer')
    } finally {
      setBusy(false)
    }
  }, [])

  const finish = useCallback(
    async (address: string) => {
      if (started === null) return
      setBusy(true)
      setFailure(null)
      try {
        const r = await write(route(VOICE_CHATGPT_EXCHANGE), 'POST', { state: started.state, callback: address })
        if (r.status === 'error' || r.status === 'stale') {
          setFailure(r.error.message)
          return
        }
        const s = r.status === 'ok' ? asStatus(r.data) : null
        setStatus(s ?? { stored: true, stored_at: null })
        setStarted(null)
      } finally {
        setBusy(false)
      }
    },
    [started],
  )

  return (
    <Card title="ChatGPT sign-in">
      {readError !== null ? (
        <Banner tone="bad" title="Could not read whether a ChatGPT sign-in is stored" role="alert">
          {readError}
        </Banner>
      ) : status === null ? (
        <LoadingState lines={2} label="Reading the ChatGPT sign-in" />
      ) : (
        <>
          {failure !== null && (
            <Banner tone="bad" title="The ChatGPT sign-in did not finish" role="alert">
              {failure}
            </Banner>
          )}
          {started === null ? (
            <StatusLine status={status} busy={busy} onStart={() => void start()} />
          ) : (
            <Finish started={started} busy={busy} onFinish={(a) => void finish(a)} onCancel={() => setStarted(null)} />
          )}
        </>
      )}
    </Card>
  )
}
