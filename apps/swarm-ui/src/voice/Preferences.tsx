/**
 * VOICE IT › PREFERENCES: THE STANDING PREFERENCES (docs/design/voice-it.md
 * §3.11) AND WHICH CHATGPT PLAN THE NEXT SESSION USES.
 *
 * THE STANDING PREFERENCES are swarm-api's `voice_preferences` document for
 * the person (routes/voice.py, voice/preferences.py): the voice, the default
 * mic mode, how brief to be, preferred stacks, hosting and repositories, how
 * many options to propose, the grace window and the per-size reservations.
 * Each is edited on its own, inline, and sent with the revision the pane read,
 * so swarm-api refuses (409) an edit made over a change this page never
 * showed -- a "remember I prefer Postgres" said in a session after the page
 * opened -- and the pane reads again and says so. Undo is swarm-api's: it
 * swaps back the values the last change replaced, whichever way that change
 * was made, by voice included; the pane says what it would put back before
 * the person presses it. A voice change takes effect at the next session
 * (the voice is fixed per GPT-Live session).
 *
 * THE VOICE IT POOL (the owner, 2026-11-02). Voice it no longer talks on a
 * plan the person signed in to here: there is no sign-in on this page and no
 * call to the old personal sign-in routes. A session leases a ChatGPT plan
 * account from the Voice it pool, which a platform admin keeps on Capacity ›
 * Accounts. This pane says, in one line, which account the person's NEXT
 * session would use (`GET /v1/voice/pool`, which asks the broker without
 * leasing anything), or why there is none, and links to Capacity. It never
 * shows a credential: the route serves the account's label, name and provider
 * and nothing else.
 */
import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from 'react'

import { Banner, Button, Card, CardLink, LoadingState, Table, ToneMark, type Column } from '../components'
import { read, route, write } from '../fetch'

/** Which Voice it pool account the caller's next session would lease, without leasing it. */
export const VOICE_POOL = '/v1/voice/pool'

/** Where the Voice it pool is kept. */
export const CAPACITY_ACCOUNTS_HREF = '/capacity/llm-providers/plans'

export type VoicePoolReason = '' | 'not_in_voice_pool' | 'no_voice_account_registered' | 'no_voice_account_available'

export interface VoicePoolAnswer {
  account: { label: string; display_name: string | null; provider: string | null } | null
  reason: VoicePoolReason | string
}

function asPoolAnswer(data: unknown): VoicePoolAnswer | null {
  const d = data as { account?: unknown; reason?: unknown } | null
  if (d === null || typeof d !== 'object') return null
  const reason = typeof d.reason === 'string' ? d.reason : ''
  const a = d.account as { label?: unknown; display_name?: unknown; provider?: unknown } | null | undefined
  if (a === null || a === undefined) return { account: null, reason }
  if (typeof a !== 'object' || typeof a.label !== 'string' || a.label === '') return null
  return {
    account: {
      label: a.label,
      display_name: typeof a.display_name === 'string' && a.display_name.trim() !== '' ? a.display_name : null,
      provider: typeof a.provider === 'string' ? a.provider : null,
    },
    reason,
  }
}

/** The one line: the account, or why there is none. */
export function poolLine(answer: VoicePoolAnswer): string {
  if (answer.account !== null) {
    return `Your next session will use the Voice it pool's ChatGPT plan: ${answer.account.display_name ?? answer.account.label}.`
  }
  if (answer.reason === 'not_in_voice_pool') return 'You are not in the Voice it pool.'
  if (answer.reason === 'no_voice_account_registered') return 'No ChatGPT plan account is in the Voice it pool yet.'
  return 'No Voice it pool account is free right now.'
}

/** Voice it › Preferences: which Voice it pool account the next session uses. */
export function VoicePreferences() {
  const [answer, setAnswer] = useState<VoicePoolAnswer | null>(null)
  const [readError, setReadError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    void read<VoicePoolAnswer>(route(VOICE_POOL), () => false).then((r) => {
      if (!live) return
      if (r.status === 'ok' || r.status === 'stale') {
        const a = asPoolAnswer(r.data)
        if (a === null) setReadError("swarm-api's answer did not say which Voice it pool account is next.")
        else setAnswer(a)
      } else if (r.status === 'error') {
        setReadError(r.error.message)
      }
    })
    return () => {
      live = false
    }
  }, [])

  return (
    <Card title="ChatGPT plan" action={<CardLink href={CAPACITY_ACCOUNTS_HREF}>LLM Providers</CardLink>}>
      {readError !== null ? (
        <Banner tone="bad" title="Could not read the Voice it pool" role="alert">
          {readError}
        </Banner>
      ) : answer === null ? (
        <LoadingState lines={1} label="Reading the Voice it pool" />
      ) : (
        <p data-voice-pool={answer.account !== null ? 'account' : answer.reason || 'none'}>
          {answer.account !== null ? <ToneMark tone="ok">Ready</ToneMark> : <ToneMark tone="warn">No account</ToneMark>}{' '}
          {poolLine(answer)}
        </p>
      )}
    </Card>
  )
}

// --------------------------------------------------------------------------
// The standing preferences (§3.11)
// --------------------------------------------------------------------------

/** Read them; PATCH {changes, revision} edits them. */
export const VOICE_PREFERENCES = '/v1/voice/preferences'
/** POST {revision}: swap back the values the last change replaced. */
export const VOICE_PREFERENCES_UNDO = '/v1/voice/preferences/undo'

/** swarm-api's own choices and bounds (voice/preferences.py), which it checks again. */
const VOICES = ['alloy', 'ash', 'ballad', 'cedar', 'coral', 'echo', 'marin', 'sage', 'shimmer', 'verse'] as const
const MIC_MODES: Readonly<Record<string, string>> = { push_to_talk: 'Push to talk', open_mic: 'Open mic' }
const BREVITY: Readonly<Record<string, string>> = { terse: 'Terse', normal: 'Normal', detailed: 'Detailed' }
const SIZES = ['fix', 'small', 'medium', 'large', 'project'] as const
const RESERVATION_MAX_USD = 500

export interface StandingPreferences {
  voice: string
  mic_mode: string
  brevity: string
  stacks: string[]
  hosting: string[]
  repositories: string[]
  options: number
  grace_seconds: number
  reservations: Record<string, number>
}

type PrefKey = keyof StandingPreferences

interface PrefsView {
  preferences: StandingPreferences
  revision: number
  can_undo: boolean
  previous: StandingPreferences | null
}

function strings(v: unknown): string[] | null {
  return Array.isArray(v) && v.every((x) => typeof x === 'string') ? (v as string[]) : null
}

function asPreferences(data: unknown): StandingPreferences | null {
  const d = data as Partial<Record<PrefKey, unknown>> | null
  if (d === null || typeof d !== 'object') return null
  const stacks = strings(d.stacks)
  const hosting = strings(d.hosting)
  const repositories = strings(d.repositories)
  const res = d.reservations
  if (
    typeof d.voice !== 'string' ||
    typeof d.mic_mode !== 'string' ||
    typeof d.brevity !== 'string' ||
    stacks === null ||
    hosting === null ||
    repositories === null ||
    typeof d.options !== 'number' ||
    typeof d.grace_seconds !== 'number' ||
    res === null ||
    typeof res !== 'object' ||
    Array.isArray(res)
  ) {
    return null
  }
  const reservations: Record<string, number> = {}
  for (const [size, usd] of Object.entries(res as Record<string, unknown>)) if (typeof usd === 'number') reservations[size] = usd
  return {
    voice: d.voice,
    mic_mode: d.mic_mode,
    brevity: d.brevity,
    stacks,
    hosting,
    repositories,
    options: d.options,
    grace_seconds: d.grace_seconds,
    reservations,
  }
}

function asPrefsView(data: unknown): PrefsView | null {
  const d = data as { preferences?: unknown; revision?: unknown; can_undo?: unknown; previous?: unknown } | null
  if (d === null || typeof d !== 'object') return null
  const preferences = asPreferences(d.preferences)
  if (preferences === null || typeof d.revision !== 'number') return null
  const previous = d.previous === null || d.previous === undefined ? null : asPreferences(d.previous)
  return { preferences, revision: d.revision, can_undo: d.can_undo === true && previous !== null, previous }
}

const capital = (s: string): string => (s === '' ? s : s[0]!.toUpperCase() + s.slice(1))
const names = (v: string[]): string => (v.length === 0 ? 'None' : v.join(', '))
const fromNames = (text: string): string[] => text.split(',').map((x) => x.trim()).filter((x) => x !== '')

/** A whole number from `low` to `high`, or null. */
function wholeIn(text: string, low: number, high: number): number | null {
  if (!/^\d+$/.test(text.trim())) return null
  const n = Number(text.trim())
  return n >= low && n <= high ? n : null
}

/**
 * One standing preference: its label, how its value reads, and its editor.
 * The editor's draft is text (or one text per size); `parse` turns it into
 * the value sent, or null while it is not one swarm-api would take.
 */
interface PrefSpec {
  key: PrefKey
  label: string
  show: (p: StandingPreferences) => string
  draft: (p: StandingPreferences) => Record<string, string>
  parse: (d: Record<string, string>) => StandingPreferences[PrefKey] | null
  editor: (d: Record<string, string>, set: (k: string, v: string) => void) => ReactNode
}

function choiceSpec(key: 'voice' | 'mic_mode' | 'brevity', label: string, choices: Readonly<Record<string, string>>): PrefSpec {
  return {
    key,
    label,
    show: (p) => choices[p[key]] ?? p[key],
    draft: (p) => ({ v: p[key] }),
    parse: (d) => (d.v !== undefined && d.v in choices ? d.v : null),
    editor: (d, set) => (
      <select className="c-inp" aria-label={label} value={d.v} onChange={(e) => set('v', e.target.value)}>
        {Object.entries(choices).map(([k, l]) => (
          <option key={k} value={k}>
            {l}
          </option>
        ))}
      </select>
    ),
  }
}

function listSpec(key: 'stacks' | 'hosting' | 'repositories', label: string, example: string): PrefSpec {
  return {
    key,
    label,
    show: (p) => names(p[key]),
    draft: (p) => ({ v: p[key].join(', ') }),
    parse: (d) => fromNames(d.v ?? ''),
    editor: (d, set) => (
      <input className="c-inp" aria-label={label} value={d.v} placeholder={example} autoComplete="off" onChange={(e) => set('v', e.target.value)} />
    ),
  }
}

function wholeSpec(key: 'options' | 'grace_seconds', label: string, low: number, high: number, unit: (n: number) => string): PrefSpec {
  return {
    key,
    label,
    show: (p) => unit(p[key]),
    draft: (p) => ({ v: String(p[key]) }),
    parse: (d) => wholeIn(d.v ?? '', low, high),
    editor: (d, set) => (
      <input className="c-inp" type="number" min={low} max={high} step={1} aria-label={label} value={d.v} onChange={(e) => set('v', e.target.value)} />
    ),
  }
}

const reservationsShown = (r: Record<string, number>): string => {
  const set = SIZES.filter((s) => r[s] !== undefined).map((s) => `${s} $${r[s]}`)
  return set.length === 0 ? 'None' : set.join(' · ')
}

const SPECS: readonly PrefSpec[] = [
  choiceSpec('voice', 'Voice', Object.fromEntries(VOICES.map((v) => [v, capital(v)]))),
  choiceSpec('mic_mode', 'Microphone', MIC_MODES),
  choiceSpec('brevity', 'How brief', BREVITY),
  listSpec('stacks', 'Preferred stacks', 'Postgres, FastAPI'),
  listSpec('hosting', 'Preferred hosting', 'Cloud Run'),
  listSpec('repositories', 'Preferred repositories', 'org/repo'),
  wholeSpec('options', 'Options to propose', 1, 5, String),
  wholeSpec('grace_seconds', 'Grace window', 0, 600, (n) => (n === 1 ? '1 second' : `${n} seconds`)),
  {
    key: 'reservations',
    label: 'Reservations',
    show: (p) => reservationsShown(p.reservations),
    draft: (p) => Object.fromEntries(SIZES.map((s) => [s, p.reservations[s] === undefined ? '' : String(p.reservations[s])])),
    parse: (d) => {
      const out: Record<string, number> = {}
      for (const s of SIZES) {
        const text = (d[s] ?? '').trim()
        if (text === '') continue
        const n = wholeIn(text, 0, RESERVATION_MAX_USD)
        if (n === null) return null
        out[s] = n
      }
      return out
    },
    editor: (d, set) => (
      <span className="ob-acts">
        {SIZES.map((s) => (
          <label key={s} className="c-field">
            <span className="c-lbl">{capital(s)} (US$)</span>
            <input className="c-inp" type="number" min={0} max={RESERVATION_MAX_USD} step={1} value={d[s]} onChange={(e) => set(s, e.target.value)} />
          </label>
        ))}
      </span>
    ),
  },
]

const same = (a: unknown, b: unknown): boolean => JSON.stringify(a) === JSON.stringify(b)

/** What Undo would put back, in words: "Voice back to Marin, Preferred stacks back to None". */
function undoWords(v: PrefsView): string | null {
  if (!v.can_undo || v.previous === null) return null
  const prev = v.previous
  const changed = SPECS.filter((s) => !same(v.preferences[s.key], prev[s.key])).map((s) => `${s.label} back to ${s.show(prev)}`)
  return changed.length === 0 ? null : changed.join(', ')
}

/** One preference's inline editor, in its row. */
function PrefEditor({ spec, from, busy, onSave, onCancel }: { spec: PrefSpec; from: StandingPreferences; busy: boolean; onSave: (v: StandingPreferences[PrefKey]) => void; onCancel: () => void }) {
  const [draft, setDraft] = useState<Record<string, string>>(() => spec.draft(from))
  const value = spec.parse(draft)
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (value !== null) onSave(value)
  }
  return (
    <form className="ob-acts" onSubmit={submit} aria-label={`Edit ${spec.label}`}>
      {spec.editor(draft, (k, v) => setDraft((d) => ({ ...d, [k]: v })))}
      <Button kind="primary" size="sm" type="submit" busy={busy ? 'Saving…' : false} disabled={value === null}>
        Save
      </Button>
      <Button kind="ghost" size="sm" onClick={onCancel} disabled={busy}>
        Cancel
      </Button>
    </form>
  )
}

/** Voice it › Preferences: the standing preferences card, each editable, with Undo. */
export function VoiceStandingPreferences() {
  const [view, setView] = useState<PrefsView | null>(null)
  const [readError, setReadError] = useState<string | null>(null)
  const [editing, setEditing] = useState<PrefKey | null>(null)
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(async (): Promise<void> => {
    try {
      const r = await read<unknown>(route(VOICE_PREFERENCES), () => false)
      if (r.status === 'ok' || r.status === 'stale') {
        const v = asPrefsView(r.data)
        if (v === null) setReadError("swarm-api's answer did not carry your preferences.")
        else {
          setReadError(null)
          setView(v)
        }
      } else if (r.status === 'error') {
        setReadError(r.error.message)
      } else if (r.status === 'empty') {
        setReadError("swarm-api's answer did not carry your preferences.")
      }
    } catch (e) {
      setReadError(e instanceof Error ? e.message : 'The read did not finish.')
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  /** A write's answer: the new view, or a refusal said in place (and a stale one read again). */
  const settle = useCallback(
    async (r: Awaited<ReturnType<typeof write>>, done: string): Promise<boolean> => {
      if (r.status === 'error' || r.status === 'stale') {
        setFailure(r.error.message)
        if (r.error.kind === 'conflict') await load()
        return false
      }
      const v = r.status === 'ok' ? asPrefsView(r.data) : null
      if (v === null) {
        setFailure("swarm-api's answer did not carry your preferences; read them again to see what was kept.")
        await load()
        return false
      }
      setView(v)
      setNotice(done)
      return true
    },
    [load],
  )

  const save = useCallback(
    async (key: PrefKey, value: StandingPreferences[PrefKey]) => {
      if (view === null) return
      setBusy(true)
      setFailure(null)
      setNotice(null)
      try {
        const r = await write(route(VOICE_PREFERENCES), 'PATCH', { changes: { [key]: value }, revision: view.revision })
        const label = SPECS.find((s) => s.key === key)?.label ?? key
        await settle(r, key === 'voice' ? 'Saved. A new voice takes effect at your next session.' : `Saved ${label}.`)
      } finally {
        setEditing(null)
        setBusy(false)
      }
    },
    [view, settle],
  )

  const undo = useCallback(async () => {
    if (view === null) return
    setBusy(true)
    setFailure(null)
    setNotice(null)
    try {
      const r = await write(route(VOICE_PREFERENCES_UNDO), 'POST', { revision: view.revision })
      await settle(r, 'Undone: the previous values are back. Undo again to redo.')
    } finally {
      setEditing(null)
      setBusy(false)
    }
  }, [view, settle])

  if (readError !== null && view === null) {
    return (
      <Card title="Standing preferences">
        <Banner tone="bad" title="Could not read your Voice it preferences" role="alert">
          {readError}
        </Banner>
      </Card>
    )
  }
  if (view === null) {
    return (
      <Card title="Standing preferences">
        <LoadingState lines={4} label="Reading your Voice it preferences" />
      </Card>
    )
  }

  const back = undoWords(view)
  const columns: Column<PrefSpec>[] = [
    { key: 'label', head: 'Preference', cell: (s) => <b>{s.label}</b> },
    {
      key: 'value',
      head: 'Value',
      cell: (s) =>
        editing === s.key ? (
          <PrefEditor spec={s} from={view.preferences} busy={busy} onSave={(v) => void save(s.key, v)} onCancel={() => setEditing(null)} />
        ) : (
          <span>{s.show(view.preferences)}</span>
        ),
    },
    {
      key: 'edit',
      head: 'Edit',
      cell: (s) =>
        editing === s.key ? null : (
          <Button size="sm" aria-label="Edit" title={`Edit ${s.label}`} disabled={busy || editing !== null} onClick={() => setEditing(s.key)}>
            Edit
          </Button>
        ),
    },
  ]

  return (
    <Card
      title="Standing preferences"
      action={
        <Button kind="secondary" size="sm" disabled={busy || back === null} onClick={() => void undo()}>
          Undo the last change
        </Button>
      }
      foot={back === null ? 'Nothing to undo yet.' : `Undo puts ${back}.`}
    >
      <p>What Voice it remembers about how you like to work, whether you set it here or said it in a session. Each change can be undone.</p>
      {failure !== null && (
        <Banner tone="bad" title="Your preferences were not changed" role="alert">
          {failure}
        </Banner>
      )}
      {notice !== null && (
        <p role="status">
          <ToneMark tone="ok">{notice}</ToneMark>
        </p>
      )}
      <Table caption="Your Voice it preferences" columns={columns} rows={SPECS} rowKey={(s) => s.key} />
    </Card>
  )
}
