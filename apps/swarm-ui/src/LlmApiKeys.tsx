/**
 * CAPACITY › LLM PROVIDERS › API KEYS (lane LP-U3; docs/design/llm-providers.md
 * §4 and §6.3; the owner's ask of 11 November 2026: "lets implement the cards
 * mockup for llm providers instead of this.. it probably would look better.
 * Also the font here is super small and hard to read").
 *
 * ONE CARD A KEY, GROUPED BY VENDOR, the cards direction's look
 * (mockups/llm-providers-cards) built on real reads: its name, its state, live
 * holders over max agents, this month's spend over its cap with a bar, one
 * line per lend, and its controls. A key has no five-hour or seven-day window,
 * so none is drawn: spend is a key's measure.
 *
 * READABLE TYPE (the owner's first rule): nothing on a card is smaller than
 * the body step; the key's name is at the lead step and its spend at the
 * title step (styles/lp-cards.css, tokens only).
 *
 * READS AND WRITES ARE api.ts's: the list (`loadApiKeys`), Pause, Drain and
 * Resume (`setAccountState`, the plans' route), Delete (`removeAccount`, the
 * typed confirmation of §4.4), Add (`addApiKey`, the one-time `api-key` link
 * minted and redeemed here, or `mintSetupLink` to send that link to the key's
 * holder), Replace (`replaceApiKey`, §4.5: the same one step, its link minted
 * in `replace` mode for the key's own label and vendor) and Caps (§4.6: the
 * spend cap, each workspace's agents cap and each lend's two caps, through
 * `setKeySpendCap`, `setKeyPerWorkspaceMax` and `setKeyLends`; the owner's
 * picks on PR #3859). A write that succeeds re-reads the list; one that
 * fails says why on its card and leaves the card as the platform last said
 * it was.
 *
 * No explanatory prose on the surface: every definition sits behind an (i).
 */
import { useEffect, useId, useState, type FormEvent } from 'react'
import {
  API_KEY_PROVIDERS,
  addApiKey,
  loadApiKeys,
  loadMe,
  mintSetupLink,
  removeAccount,
  replaceApiKey,
  setAccountState,
  setKeyLends,
  setKeyPerWorkspaceMax,
  setKeySpendCap,
  type ApiKeyAccount,
  type ApiKeyProvider,
  type ApiKeysBoard,
  type SetupLink,
} from './api'
import { accountName } from './accountName'
import { Info } from './AccountsVerdict'
import { Button } from './components'
import type { ApiError, Result } from './fetch'
import './styles/lp-cards.css'

/** The vendors in the page's order, each with the page its keys are revoked on (§4.4). */
const VENDORS: readonly { provider: ApiKeyProvider; label: string; keysPage: string }[] = [
  { provider: 'anthropic-api', label: 'Anthropic', keysPage: 'https://console.anthropic.com/settings/keys' },
  { provider: 'openai-api', label: 'OpenAI', keysPage: 'https://platform.openai.com/api-keys' },
]

function vendorOf(provider: string) {
  return VENDORS.find((v) => v.provider === provider) ?? null
}

/** The keys grouped by vendor: the known vendors first, in order, then any other provider by name. */
export function groupKeys(keys: readonly ApiKeyAccount[]): { provider: string; keys: ApiKeyAccount[] }[] {
  const order = [...VENDORS.map((v) => v.provider as string), ...[...new Set(keys.map((k) => k.provider))].filter((p) => vendorOf(p) === null).sort()]
  return order
    .map((provider) => ({ provider, keys: keys.filter((k) => k.provider === provider).sort((a, b) => accountName(a).localeCompare(accountName(b))) }))
    .filter((g) => g.keys.length > 0)
}

function failed(r: Result<unknown>): ApiError | null {
  return r.status === 'error' ? r.error : null
}

/** US dollars as the broker meters them, to the cent. */
function usd(n: number): string {
  return `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

type Tone = 'ok' | 'warn' | 'bad' | 'park'

/** A key's state in a word and its tone. Out of credit (`exhausted_at`) outranks the rest: nothing is assigned until it is resumed. */
export function keyState(k: ApiKeyAccount): { word: string; tone: Tone } {
  if (typeof k.exhausted_at === 'string' && k.exhausted_at !== '') return { word: 'Out of credit', tone: 'bad' }
  if (k.state === 'PAUSED') return { word: 'Paused', tone: 'park' }
  if (k.state === 'DRAINING') return { word: 'Draining', tone: 'park' }
  if (k.state === 'REAUTH_REQUIRED') return { word: 'Key refused', tone: 'bad' }
  if (k.can_take_new_agent === false && typeof k.next_eligible_at === 'string') {
    const at = new Date(k.next_eligible_at)
    if (!Number.isNaN(at.getTime())) return { word: `Limited until ${at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`, tone: 'warn' }
  }
  return { word: 'Available', tone: 'ok' }
}

/** This month's spend over its cap, said as unknown when the broker served none, never as $0. */
function Spend({ k }: { k: ApiKeyAccount }) {
  const spent = typeof k.month_spend_usd === 'number' ? k.month_spend_usd : null
  const cap = typeof k.monthly_spend_cap_usd === 'number' && k.monthly_spend_cap_usd > 0 ? k.monthly_spend_cap_usd : null
  const share = spent !== null && cap !== null ? Math.min(1, spent / cap) : null
  const tone = share === null ? '' : share >= 1 ? ' is-bad' : share >= 0.8 ? ' is-warn' : ''
  return (
    <div className="lpc-fig">
      <span className="lpc-fl">
        This month
        <Info about="This month's spend" tip="This month's spend on the key, metered by the broker since the 1st (UTC), over its monthly spend cap. The key stops taking agents at its cap." />
      </span>
      <span className="lpc-figline">
        <span className={`lpc-big lpc-num${tone}`}>{spent === null ? 'unknown' : usd(spent)}</span>
        <span className="lpc-of lpc-num">{cap === null ? 'no cap' : `of ${usd(cap)}`}</span>
      </span>
      <span className={`lpc-bar${tone}${share === null ? ' is-off' : ''}`} role="img" aria-label={share === null ? 'no cap' : `${Math.round(share * 100)}% of the cap`}>
        {share !== null && <i style={{ width: `${Math.round(share * 100)}%` }} />}
      </span>
    </div>
  )
}

/** One line per lend: the borrowing workspace, its agents cap, and its month's spend over the lend's spend cap. */
function Lends({ k }: { k: ApiKeyAccount }) {
  const lends = k.lends ?? {}
  const workspaces = [...new Set([...Object.keys(lends), ...k.lend_to])].sort()
  if (workspaces.length === 0) return null
  return (
    <ul className="lpc-lends" aria-label={`Lends of ${accountName(k)}`}>
      {workspaces.map((ws) => {
        const lend = lends[ws]
        const spent = k.lend_spend_usd?.[ws]
        const cap = lend?.monthly_spend_cap_usd ?? null
        const capped = lend !== undefined && lend.cap_set !== false
        return (
          <li key={ws} className="lpc-lend">
            <span className="lpc-lend-ws">{ws}</span>
            <span className="lpc-num">{capped && typeof lend.max_agents === 'number' ? `≤ ${lend.max_agents} agents` : 'cap not set'}</span>
            <span className="lpc-num">
              {k.lend_spend_usd === undefined ? 'unknown' : usd(typeof spent === 'number' ? spent : 0)}
              {capped && cap !== null ? ` of ${usd(cap)}` : ''}
            </span>
          </li>
        )
      })}
    </ul>
  )
}

type Asking = 'PAUSED' | 'DRAINING' | 'delete' | 'replace' | 'caps' | null

/**
 * Replace key (§4.5): a new value for the same label and vendor, sent once in
 * the redeem body, as the add's is. The field is a password input and its
 * value leaves the page's state whatever the answer.
 */
function ReplaceKey({ k, busy, onCancel, onSend }: { k: ApiKeyAccount; busy: boolean; onCancel: () => void; onSend: (owner: string, value: string) => Promise<void> }) {
  const own = useOwnEmail()
  const [value, setValue] = useState('')
  const ready = value !== '' && own !== null && !busy
  return (
    <form
      className="lpc-panel"
      aria-label={`Replace key ${accountName(k)}`}
      autoComplete="off"
      onSubmit={(e) => {
        e.preventDefault()
        if (!ready || own === null) return
        const v = value
        setValue('')
        void onSend(own, v)
      }}
    >
      <span className="lpc-ptitle">
        New key
        <Info about="Replace key" tip="The new key is checked with its vendor and stored as the key's new value; the old one is expired once running agents have moved off it. Holders, caps, lends and sharing stay as they are." />
      </span>
      <input
        className="c-inp lpc-input lpc-mono"
        type="password"
        aria-label="New key"
        placeholder="New key"
        autoComplete="off"
        spellCheck={false}
        value={value}
        onChange={(e) => setValue(e.target.value.trim())}
      />
      <span className="lpc-prow">
        <Button kind="primary" type="submit" disabled={!ready} busy={busy ? 'Replacing' : false}>
          Replace key
        </Button>
        <Button kind="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </span>
    </form>
  )
}

/** A typed amount, or null for an empty field; NaN for anything else, which keeps Save disabled. */
function amount(text: string): number | null {
  const t = text.trim()
  if (t === '') return null
  const n = Number(t)
  return Number.isFinite(n) ? n : Number.NaN
}

function shown(n: number | null | undefined): string {
  return typeof n === 'number' ? String(n) : ''
}

/** What `KeyCaps` saves: only the parts that changed, each its own route. */
export type CapsChange = {
  spendCap?: number | null
  perWorkspaceMax?: Record<string, number>
  lends?: Record<string, { max_agents: number; monthly_spend_cap_usd: number }>
}

/**
 * Caps (§4.6): the key's monthly spend cap (empty: none), each workspace's
 * agents cap (the owner and every borrower; empty: none) and each lend's
 * agents and spend caps, both required on a key's lend (Q7).
 */
function KeyCaps({ k, busy, onCancel, onSave }: { k: ApiKeyAccount; busy: boolean; onCancel: () => void; onSave: (change: CapsChange) => void }) {
  const lendNames = [...new Set([...Object.keys(k.lends ?? {}), ...k.lend_to])].sort()
  const workspaces = [k.owner_tenant, ...lendNames.filter((w) => w !== k.owner_tenant)]
  const [spend, setSpend] = useState(shown(k.monthly_spend_cap_usd))
  const [perWs, setPerWs] = useState<Record<string, string>>(() => Object.fromEntries(workspaces.map((w) => [w, shown(k.per_workspace_max?.[w])])))
  const [lends, setLends] = useState<Record<string, { agents: string; usd: string }>>(() =>
    Object.fromEntries(lendNames.map((w) => [w, { agents: shown(k.lends?.[w]?.max_agents), usd: shown(k.lends?.[w]?.monthly_spend_cap_usd) }])),
  )

  const spendCap = amount(spend)
  const perWsCaps: Record<string, number> = {}
  let perWsOk = true
  for (const w of workspaces) {
    const n = amount(perWs[w] ?? '')
    if (n === null) continue
    if (!Number.isInteger(n) || n < 1) perWsOk = false
    else perWsCaps[w] = n
  }
  const lendCaps: Record<string, { max_agents: number; monthly_spend_cap_usd: number }> = {}
  let lendsOk = true
  for (const w of lendNames) {
    const a = amount(lends[w]?.agents ?? '')
    const u = amount(lends[w]?.usd ?? '')
    if (a === null || u === null || !Number.isInteger(a) || a < 1 || !(u > 0)) lendsOk = false
    else lendCaps[w] = { max_agents: a, monthly_spend_cap_usd: u }
  }
  const spendOk = spendCap === null || spendCap > 0

  const change: CapsChange = {}
  if (spendOk && spendCap !== (k.monthly_spend_cap_usd ?? null)) change.spendCap = spendCap
  const servedPerWs = k.per_workspace_max ?? {}
  if (perWsOk && JSON.stringify(Object.entries(perWsCaps).sort()) !== JSON.stringify(Object.entries(servedPerWs).sort())) change.perWorkspaceMax = perWsCaps
  if (
    lendsOk &&
    lendNames.length > 0 &&
    lendNames.some((w) => {
      const served = k.lends?.[w]
      const typed = lendCaps[w]
      return served === undefined || typed === undefined || served.cap_set === false || served.max_agents !== typed.max_agents || served.monthly_spend_cap_usd !== typed.monthly_spend_cap_usd
    })
  ) {
    change.lends = lendCaps
  }
  const ready = spendOk && perWsOk && lendsOk && Object.keys(change).length > 0 && !busy

  return (
    <form
      className="lpc-panel"
      aria-label={`Caps of ${accountName(k)}`}
      onSubmit={(e) => {
        e.preventDefault()
        if (ready) onSave(change)
      }}
    >
      <span className="lpc-ptitle">
        Caps
        <Info about="Caps" tip="The key's monthly spend cap in US dollars, each workspace's cap on the agents it holds on the key, and each lend's agents and spend caps. An empty cap is none, except on a lend, which always carries both. Lowering a cap stops no running agent: the next one over it is refused." />
      </span>
      <input className="c-inp lpc-input lpc-num" inputMode="decimal" aria-label="Spend cap, US dollars" placeholder="Spend cap $" value={spend} onChange={(e) => setSpend(e.target.value)} />
      {workspaces.map((w) => (
        <input
          key={`ws-${w}`}
          className="c-inp lpc-input lpc-num"
          inputMode="numeric"
          aria-label={`Agents cap, ${w}`}
          placeholder={`${w} agents`}
          value={perWs[w] ?? ''}
          onChange={(e) => setPerWs({ ...perWs, [w]: e.target.value })}
        />
      ))}
      {lendNames.map((w) => (
        <span key={`lend-${w}`} className="lpc-prow">
          <input
            className="c-inp lpc-input lpc-num"
            inputMode="numeric"
            aria-label={`Lend to ${w}, agents`}
            placeholder={`${w} lend agents`}
            value={lends[w]?.agents ?? ''}
            onChange={(e) => setLends({ ...lends, [w]: { usd: lends[w]?.usd ?? '', agents: e.target.value } })}
          />
          <input
            className="c-inp lpc-input lpc-num"
            inputMode="decimal"
            aria-label={`Lend to ${w}, spend cap, US dollars`}
            placeholder={`${w} lend $`}
            value={lends[w]?.usd ?? ''}
            onChange={(e) => setLends({ ...lends, [w]: { agents: lends[w]?.agents ?? '', usd: e.target.value } })}
          />
        </span>
      ))}
      <span className="lpc-prow">
        <Button kind="primary" type="submit" disabled={!ready} busy={busy ? 'Saving' : false}>
          Save caps
        </Button>
        <Button kind="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </span>
    </form>
  )
}

/** Each changed part of a caps edit, in order, stopping at the first refusal. */
async function saveCaps(accountId: string, change: CapsChange): Promise<Result<unknown>> {
  let last: Result<unknown> = { status: 'ok', fetchedAt: Date.now(), data: null }
  if (change.spendCap !== undefined) {
    last = await setKeySpendCap(accountId, change.spendCap)
    if (failed(last) !== null) return last
  }
  if (change.perWorkspaceMax !== undefined) {
    last = await setKeyPerWorkspaceMax(accountId, change.perWorkspaceMax)
    if (failed(last) !== null) return last
  }
  if (change.lends !== undefined) last = await setKeyLends(accountId, change.lends)
  return last
}

/** The typed confirmation of §4.4: the label typed back, then Delete key. */
function DeleteKey({ k, busy, onCancel, onDelete }: { k: ApiKeyAccount; busy: boolean; onCancel: () => void; onDelete: () => void }) {
  const [typed, setTyped] = useState('')
  const v = vendorOf(k.provider)
  const title = `Delete API key ${accountName(k)}`
  return (
    <div className="lpc-panel is-bad" role="dialog" aria-label={title}>
      <h4 className="lpc-ptitle">
        {title}
        <Info
          about="Delete key"
          tip="The stored key is destroyed now. Agents running on it keep the value they already read until their next turn, then move to the next tier or park. Deleting here does not revoke the key at the provider: a copy anywhere else keeps working until it is revoked there."
        />
      </h4>
      <span className="lpc-pline">
        Revoke it at{' '}
        {v !== null ? (
          <a href={v.keysPage} target="_blank" rel="noreferrer">
            {v.label}
          </a>
        ) : (
          k.provider
        )}{' '}
        too.
      </span>
      <input
        className="c-inp lpc-input"
        aria-label={`Type ${k.label} to delete`}
        placeholder={`Type ${k.label} to delete`}
        autoComplete="off"
        spellCheck={false}
        value={typed}
        onChange={(e) => setTyped(e.target.value)}
      />
      <span className="lpc-prow">
        <Button kind="danger-filled" disabled={typed !== k.label || busy} busy={busy ? 'Deleting' : false} onClick={onDelete}>
          Delete key
        </Button>
        <Button kind="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </span>
    </div>
  )
}

/** Pause or Drain: the reason is for the next person, so it is asked for first (the route requires one). */
function ReasonFor({ state, busy, onCancel, onSend }: { state: 'PAUSED' | 'DRAINING'; busy: boolean; onCancel: () => void; onSend: (reason: string) => void }) {
  const [reason, setReason] = useState('')
  const verb = state === 'PAUSED' ? 'Pause' : 'Drain'
  return (
    <form
      className="lpc-panel"
      aria-label={`${verb} key`}
      onSubmit={(e) => {
        e.preventDefault()
        if (reason.trim() !== '' && !busy) onSend(reason.trim())
      }}
    >
      <input className="c-inp lpc-input" aria-label="Reason" placeholder="Reason" autoComplete="off" value={reason} onChange={(e) => setReason(e.target.value)} />
      <span className="lpc-prow">
        <Button kind="primary" type="submit" disabled={reason.trim() === '' || busy} busy={busy ? `${verb === 'Pause' ? 'Pausing' : 'Draining'}` : false}>
          {verb} key
        </Button>
        <Button kind="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </span>
    </form>
  )
}

function KeyCard({ k, owned, reread }: { k: ApiKeyAccount; owned: boolean; reread: () => Promise<void> }) {
  const [asking, setAsking] = useState<Asking>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const st = keyState(k)
  const name = accountName(k)
  const exhausted = typeof k.exhausted_at === 'string' && k.exhausted_at !== ''
  const stopped = k.state === 'PAUSED' || k.state === 'DRAINING' || exhausted
  const voiceIt = Array.isArray(k.pools) && k.pools.includes('voice-it')

  async function run(call: () => Promise<Result<unknown>>) {
    setBusy(true)
    setError(null)
    const refused = failed(await call())
    setBusy(false)
    if (refused !== null) {
      setError(refused)
      return
    }
    setAsking(null)
    await reread()
  }

  return (
    <article className={`lpc-card is-${st.tone}`} aria-label={`API key ${name}`} data-account={k.account_id}>
      <header className="lpc-head">
        <span className="lpc-name">
          <h3 className="lpc-title">{name}</h3>
          {name !== k.label && <span className="lpc-mono">{k.label}</span>}
        </span>
        <span className={`lpc-state is-${st.tone}`}>
          <i aria-hidden="true" />
          {st.word}
        </span>
      </header>
      {(voiceIt || !owned) && (
        <span className="lpc-tags">
          {voiceIt && <span className="lpc-tag">Voice it</span>}
          {!owned && <span className="lpc-tag">Lent by {k.owner_tenant}</span>}
        </span>
      )}
      <div className="lpc-figs">
        <Spend k={k} />
        <div className="lpc-fig">
          <span className="lpc-fl">Agents</span>
          <span className="lpc-big lpc-num">
            {k.assigned}
            <span className="lpc-of">/{typeof k.max_agents === 'number' ? k.max_agents : '?'}</span>
          </span>
        </div>
      </div>
      <Lends k={k} />
      {error !== null && (
        <p className="lpc-err" role="alert">
          {error.message}
        </p>
      )}
      {owned && asking === 'delete' && <DeleteKey k={k} busy={busy} onCancel={() => setAsking(null)} onDelete={() => void run(() => removeAccount(k.account_id))} />}
      {owned && asking === 'replace' && (
        <ReplaceKey k={k} busy={busy} onCancel={() => setAsking(null)} onSend={(owner, value) => run(() => replaceApiKey({ key: k, owner, value }))} />
      )}
      {owned && asking === 'caps' && <KeyCaps k={k} busy={busy} onCancel={() => setAsking(null)} onSave={(change) => void run(() => saveCaps(k.account_id, change))} />}
      {owned && (asking === 'PAUSED' || asking === 'DRAINING') && (
        <ReasonFor state={asking} busy={busy} onCancel={() => setAsking(null)} onSend={(reason) => void run(() => setAccountState(k.account_id, asking, reason))} />
      )}
      {owned && asking === null && (
        <footer className="lpc-acts">
          {stopped ? (
            <Button busy={busy ? 'Resuming' : false} onClick={() => void run(() => setAccountState(k.account_id, 'AVAILABLE', ''))}>
              Resume
            </Button>
          ) : (
            <>
              <Button onClick={() => setAsking('PAUSED')}>Pause</Button>
              <Button onClick={() => setAsking('DRAINING')}>Drain</Button>
            </>
          )}
          <Button onClick={() => setAsking('caps')}>Caps</Button>
          <Button onClick={() => setAsking('replace')}>Replace</Button>
          <span className="lpc-grow" />
          <Button kind="danger" onClick={() => setAsking('delete')}>
            Delete
          </Button>
        </footer>
      )}
    </article>
  )
}

/** The reader's own verified email: the one-step add mints its link for them. null while unknown. */
function useOwnEmail(): string | null {
  const [email, setEmail] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    void loadMe({ frame: true }).then(
      (r) => {
        const e = r.status === 'ok' || r.status === 'stale' ? r.data.principal?.email : null
        if (live && typeof e === 'string' && e !== '') setEmail(e)
      },
      () => {},
    )
    return () => {
      live = false
    }
  }, [])
  return email
}

const LABEL_RE = /^[a-z0-9][a-z0-9-]{0,63}$/

/**
 * Add key (§4.1): the vendor and the label, then either the key itself (the
 * one-step form: a password field, sent once in the redeem body) or a
 * one-time link for the person who holds it.
 */
function AddKey({ close, done }: { close: () => void; done: () => Promise<void> }) {
  const id = useId()
  const own = useOwnEmail()
  const [provider, setProvider] = useState<ApiKeyProvider>(API_KEY_PROVIDERS[0])
  const [label, setLabel] = useState('')
  const [mode, setMode] = useState<'key' | 'link'>('key')
  const [value, setValue] = useState('')
  const [email, setEmail] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const [link, setLink] = useState<SetupLink | null>(null)
  const labelOk = LABEL_RE.test(label)
  const ready = labelOk && !busy && (mode === 'key' ? value !== '' && own !== null : email.includes('@'))

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!ready) return
    setBusy(true)
    setError(null)
    if (mode === 'key' && own !== null) {
      const r = await addApiKey({ provider, label, owner: own, value })
      // The value leaves the page's state whatever the answer: a retry types it again.
      setValue('')
      setBusy(false)
      const refused = failed(r)
      if (refused !== null) setError(refused)
      else await done()
      return
    }
    const r = await mintSetupLink({ kind: 'api-key', label, owner: email.trim(), provider })
    setBusy(false)
    if (r.status === 'error') setError(r.error)
    else if (r.status === 'ok' || r.status === 'stale') setLink(r.data)
  }

  return (
    <form className="lpc-card lpc-add" aria-label="Add key" autoComplete="off" onSubmit={(e) => void submit(e)}>
      <header className="lpc-head">
        <h3 className="lpc-title">Add key</h3>
        <Info about="Add key" tip="The key is checked with its vendor, then stored in Secret Manager alone; this page never shows it again. A link lets the key's holder add it themselves: single use, for their verified email, ten minutes." />
      </header>
      <div className="lpc-seg" role="group" aria-label="Vendor">
        {VENDORS.map((v) => (
          <button key={v.provider} type="button" className={provider === v.provider ? 'is-on' : ''} aria-pressed={provider === v.provider} onClick={() => setProvider(v.provider)}>
            {v.label}
          </button>
        ))}
      </div>
      <input
        id={`${id}-label`}
        className="c-inp lpc-input lpc-mono"
        aria-label="Label"
        placeholder="Label"
        spellCheck={false}
        value={label}
        onChange={(e) => setLabel(e.target.value.trim().toLowerCase())}
      />
      <div className="lpc-seg" role="group" aria-label="How">
        <button type="button" className={mode === 'key' ? 'is-on' : ''} aria-pressed={mode === 'key'} onClick={() => setMode('key')}>
          Paste key
        </button>
        <button type="button" className={mode === 'link' ? 'is-on' : ''} aria-pressed={mode === 'link'} onClick={() => setMode('link')}>
          Send a link
        </button>
      </div>
      {mode === 'key' ? (
        <input
          className="c-inp lpc-input lpc-mono"
          type="password"
          aria-label="API key"
          placeholder="API key"
          autoComplete="off"
          spellCheck={false}
          value={value}
          onChange={(e) => setValue(e.target.value.trim())}
        />
      ) : (
        <input className="c-inp lpc-input" type="email" aria-label="Holder's email" placeholder="Holder's email" value={email} onChange={(e) => setEmail(e.target.value)} />
      )}
      {link !== null && (
        <input className="c-inp lpc-input lpc-mono" readOnly aria-label="One-time link" value={link.url ?? link.path} onFocus={(e) => e.target.select()} />
      )}
      {error !== null && (
        <p className="lpc-err" role="alert">
          {error.message}
        </p>
      )}
      <footer className="lpc-acts">
        <Button kind="primary" type="submit" disabled={!ready} busy={busy ? (mode === 'key' ? 'Adding' : 'Making link') : false}>
          {mode === 'key' ? 'Add key' : 'Make link'}
        </Button>
        <Button kind="ghost" onClick={close}>
          {link !== null ? 'Done' : 'Cancel'}
        </Button>
      </footer>
    </form>
  )
}

function Keys({ board, reread }: { board: ApiKeysBoard; reread: () => Promise<void> }) {
  const [adding, setAdding] = useState(false)
  const groups = groupKeys(board.keys)
  const add = adding ? (
    <AddKey
      close={() => setAdding(false)}
      done={async () => {
        setAdding(false)
        await reread()
      }}
    />
  ) : null
  if (groups.length === 0) {
    return (
      <div className="lpc-page">
        <section className="lpc-empty" aria-label="No API keys">
          <h2 className="lpc-title">
            No API keys
            <Info about="No API keys" tip="Agents fall back to keys only after the plans, so a page with none is valid." />
          </h2>
          {!adding && (
            <Button kind="primary" onClick={() => setAdding(true)}>
              Add key
            </Button>
          )}
        </section>
        {add}
      </div>
    )
  }
  return (
    <div className="lpc-page">
      <div className="lpc-bar-row">
        <span className="lpc-grow" />
        {!adding && (
          <Button kind="primary" onClick={() => setAdding(true)}>
            Add key
          </Button>
        )}
      </div>
      {add}
      {groups.map((g) => {
        const v = vendorOf(g.provider)
        return (
          <section key={g.provider} className="lpc-group" aria-label={v?.label ?? g.provider}>
            <h2 className="lpc-group-h">
              {v?.label ?? g.provider}
              <span className="lpc-dim lpc-num">{g.keys.length}</span>
            </h2>
            <div className="lpc-grid">
              {g.keys.map((k) => (
                <KeyCard key={k.account_id} k={k} owned={board.tenant === null || k.owner_tenant === board.tenant} reread={reread} />
              ))}
            </div>
          </section>
        )
      })}
    </div>
  )
}

/**
 * The API Keys sub-page's body, once the key list has been read: the page's
 * `Screen` (its one head, the tab's (i) and the Plans | API Keys strip) is
 * LlmProviders.tsx's, and hands each read in as `board`.
 */
export function LlmApiKeysBody({ board }: { board: ApiKeysBoard }) {
  // A WRITE RE-READS THE LIST, and the newer of the two reads is drawn: the
  // screen's own (its first read, its poll and its head's refresh) or this one.
  const [fresh, setFresh] = useState<ApiKeysBoard | null>(null)
  const reread = async () => {
    const r = await loadApiKeys()
    if (r.status === 'ok' || r.status === 'stale') setFresh(r.data)
  }
  return <Keys board={fresh !== null && fresh.readAt >= board.readAt ? fresh : board} reread={reread} />
}
