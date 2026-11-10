// MOCK-HOOK FILE
// LLM Providers, direction "cards" (docs/design/llm-providers.md §6.6, LP-M1/M2/M3).
// Drawn on invented fixtures at ?lp=cards. Not wired to any API.
import { Fragment, useEffect, useMemo, useState, type ReactNode } from 'react'
import './styles/lp-cards.css'

export const LP_CARDS = typeof window !== 'undefined' && new URLSearchParams(window.location.search).get('lp') === 'cards'

type Vendor = 'anthropic' | 'openai'
type Kind = 'plan' | 'key'
type State = 'available' | 'limited' | 'exhausted' | 'paused' | 'draining' | 'signin'

interface Lend {
  ws: string
  agents: number
  maxAgents: number
  spend?: number
  spendCap?: number
}

interface Cred {
  id: string
  kind: Kind
  vendor: Vendor
  label: string
  plan?: string
  state: State
  until?: string
  holders: number
  maxAgents: number
  five?: number
  seven?: number
  fiveReset?: string
  sevenReset?: string
  spend?: number
  spendCap?: number | null
  today: number
  classes: string[]
  voice: boolean
  by: string
  at: string
  lends: Lend[]
  perWs: { ws: string; max: number; now: number }[]
  probe?: { at: string; result: string; next: string; since: string; log: { at: string; result: string }[] }
  isNew?: boolean
  replaced?: string
}

const VENDOR: Record<Vendor, string> = { anthropic: 'Anthropic', openai: 'OpenAI' }
const CLASSES: Record<Vendor, string[]> = { anthropic: ['Opus', 'Sonnet', 'Haiku'], openai: ['GPT-5', 'GPT-5 mini', 'Live'] }
const OWN = 'core'

const PLANS: Cred[] = [
  {
    id: 'p-build', kind: 'plan', vendor: 'anthropic', label: 'build-team', plan: 'Claude Max', state: 'available', holders: 4, maxAgents: 6,
    five: 41, seven: 63, fiveReset: '04:40', sevenReset: 'Nov 14', today: 48, classes: ['Opus', 'Sonnet', 'Haiku'], voice: false, by: 'Dana Reyes', at: 'Oct 12',
    lends: [{ ws: 'harbor-lab', agents: 1, maxAgents: 2 }, { ws: 'north-wing', agents: 1, maxAgents: 1 }], perWs: [{ ws: OWN, max: 4, now: 2 }],
  },
  {
    id: 'p-research', kind: 'plan', vendor: 'anthropic', label: 'research-desk', plan: 'Claude Team', state: 'limited', until: '04:12', holders: 0, maxAgents: 4,
    five: 100, seven: 71, fiveReset: '04:12', sevenReset: 'Nov 13', today: 22, classes: ['Sonnet', 'Haiku'], voice: false, by: 'Lee Park', at: 'Oct 20',
    lends: [], perWs: [],
  },
  {
    id: 'p-ops', kind: 'plan', vendor: 'openai', label: 'ops-desk', plan: 'ChatGPT Pro', state: 'available', holders: 2, maxAgents: 3,
    five: 18, seven: 44, fiveReset: '05:55', sevenReset: 'Nov 15', today: 17, classes: ['GPT-5', 'GPT-5 mini', 'Live'], voice: true, by: 'Sam Okafor', at: 'Oct 28',
    lends: [{ ws: 'harbor-lab', agents: 0, maxAgents: 1 }], perWs: [{ ws: OWN, max: 2, now: 2 }],
  },
  {
    id: 'p-spare', kind: 'plan', vendor: 'openai', label: 'spare-seat', plan: 'ChatGPT Plus', state: 'paused', holders: 0, maxAgents: 2,
    five: 0, seven: 9, fiveReset: '—', sevenReset: 'Nov 16', today: 0, classes: ['GPT-5 mini'], voice: false, by: 'Sam Okafor', at: 'Nov 1',
    lends: [], perWs: [],
  },
]

const KEYS: Cred[] = [
  {
    id: 'k-ci', kind: 'key', vendor: 'anthropic', label: 'ci-overflow', state: 'available', holders: 5, maxAgents: 8, spend: 212.4, spendCap: 400, today: 31,
    classes: ['Opus', 'Sonnet', 'Haiku'], voice: false, by: 'Dana Reyes', at: 'Nov 2',
    lends: [{ ws: 'harbor-lab', agents: 1, maxAgents: 2, spend: 38.1, spendCap: 100 }, { ws: 'north-wing', agents: 3, maxAgents: 2, spend: 96.2, spendCap: 100 }],
    perWs: [{ ws: OWN, max: 5, now: 1 }],
  },
  {
    id: 'k-browser', kind: 'key', vendor: 'anthropic', label: 'browser-steps', state: 'draining', holders: 2, maxAgents: 4, spend: 403.1, spendCap: 400, today: 14,
    classes: ['Sonnet'], voice: false, by: 'Lee Park', at: 'Oct 30', lends: [], perWs: [],
  },
  {
    id: 'k-backup', kind: 'key', vendor: 'anthropic', label: 'research-backup', state: 'exhausted', holders: 0, maxAgents: 4, spend: 318.75, spendCap: null, today: 6,
    classes: ['Opus', 'Sonnet'], voice: false, by: 'Dana Reyes', at: 'Oct 18', lends: [], perWs: [],
    probe: {
      at: '01:06', result: 'Credit still gone · 402', next: '02:06', since: 'Nov 9, 21:06',
      log: [{ at: '01:06', result: '402' }, { at: '00:06', result: '402' }, { at: '23:06', result: '402' }, { at: '22:06', result: '402' }],
    },
  },
  {
    id: 'k-voice', kind: 'key', vendor: 'openai', label: 'voice-and-codex', state: 'limited', until: '02:14', holders: 1, maxAgents: 4, spend: 91.3, spendCap: 250, today: 9,
    classes: ['GPT-5', 'Live'], voice: true, by: 'Sam Okafor', at: 'Nov 3', lends: [{ ws: 'harbor-lab', agents: 1, maxAgents: 1, spend: 12.4, spendCap: 40 }], perWs: [],
  },
  {
    id: 'k-nightly', kind: 'key', vendor: 'openai', label: 'codex-nightly', state: 'paused', holders: 0, maxAgents: 2, spend: 44, spendCap: 150, today: 0,
    classes: ['GPT-5 mini'], voice: false, by: 'Lee Park', at: 'Nov 6', lends: [], perWs: [],
  },
]

const WS_POOL = ['harbor-lab', 'north-wing', 'delta-studio', 'quarry-ops', 'lantern-research']
const LONG_A = ['integration-overflow-for-the-weekend-batch', 'browser-and-visual-checks-long-running', 'research-team-shared-backup']
function manyFixtures(): { plans: Cred[]; keys: Cred[] } {
  const states: State[] = ['available', 'limited', 'available', 'draining', 'paused', 'available', 'exhausted']
  const plans: Cred[] = []
  for (let i = 0; i < 9; i++) {
    const vendor: Vendor = i < 5 ? 'anthropic' : 'openai'
    const base = PLANS[i % PLANS.length]!
    plans.push({ ...base, id: `mp${i}`, vendor, state: (['available', 'limited', 'paused', 'draining', 'available'] as State[])[i % 5]!, voice: vendor === 'openai' && i % 2 === 0, classes: CLASSES[vendor], label: i === 1 ? 'nightly-regression-and-release-candidate-seat' : i === 6 ? 'design-review-and-browser-checks-shared-seat' : `${base.label}-${i + 1}`, lends: i === 0 ? WS_POOL.map((ws, j) => ({ ws, agents: [1, 1, 0, 2, 1][j]!, maxAgents: [2, 1, 1, 1, 1][j]! })) : base.lends, holders: i === 0 ? 6 : base.holders })
  }
  const keys: Cred[] = []
  for (let i = 0; i < 14; i++) {
    const vendor: Vendor = i < 8 ? 'anthropic' : 'openai'
    const base = KEYS[i % KEYS.length]!
    const st = states[i % states.length]!
    keys.push({
      ...base, id: `mk${i}`, vendor, state: st, label: i < 3 ? LONG_A[i]! : `${vendor === 'anthropic' ? 'claude' : 'codex'}-pool-${String(i + 1).padStart(2, '0')}`,
      classes: CLASSES[vendor], voice: vendor === 'openai' && i % 3 === 0, until: st === 'limited' ? '02:40' : undefined,
      probe: st === 'exhausted' ? KEYS[2]!.probe : undefined,
      lends: i === 0 ? WS_POOL.map((ws, j) => ({ ws, agents: [1, 2, 0, 1, 3][j]!, maxAgents: [2, 2, 1, 1, 2][j]!, spend: [38.1, 61.5, 0, 22.9, 104.2][j]!, spendCap: [100, 80, 25, 50, 100][j]! })) : i % 4 === 1 ? base.lends : [],
    })
  }
  return { plans, keys }
}

const usd = (n: number) => (n >= 1000 ? `$${Math.round(n).toLocaleString('en-US')}` : `$${n.toFixed(2)}`)

/* ---------------------------------------------------------------- bits */

function Info({ say }: { say: string }) {
  return (
    <button type="button" className="lp-i" title={say} aria-label={say} onClick={(e) => e.stopPropagation()}>
      i
    </button>
  )
}

function Bar({ pct, tone }: { pct: number; tone?: 'warn' | 'bad' | 'off' }) {
  return (
    <span className={`lp-bar${tone ? ` is-${tone}` : ''}`} aria-hidden="true">
      <i style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
    </span>
  )
}

const toneOf = (n: number, cap: number): 'warn' | 'bad' | undefined => (n > cap ? 'bad' : n >= cap * 0.9 ? 'warn' : undefined)

function VendorGlyph({ v }: { v: Vendor }) {
  return <span className={`lp-vg is-${v}`} aria-hidden="true">{v === 'anthropic' ? 'A' : 'O'}</span>
}

/** The state mark: a dot always, its word only where it asks something. */
function StateMark({ c }: { c: Cred }) {
  const map: Record<State, { tone: string; word: string | null; say: string }> = {
    available: { tone: 'ok', word: null, say: 'Available' },
    limited: { tone: 'warn', word: `Limited until ${c.until}`, say: 'Rate limited by the provider (429); retried after its retry-after' },
    exhausted: { tone: 'bad', word: 'Credit gone', say: 'The provider refused for credit. Probed hourly with a one-token request; cleared by a probe that succeeds or by Resume' },
    paused: { tone: 'park', word: 'Paused', say: 'Takes no new agent; resumes on Resume' },
    draining: { tone: 'park', word: 'Draining', say: 'Running agents finish; no new agent is given it' },
    signin: { tone: 'bad', word: 'Sign in again', say: 'The plan’s sign-in expired' },
  }
  const m = map[c.state]
  return (
    <span className={`lp-state is-${m.tone}`} title={m.say}>
      <i aria-hidden="true" />
      {m.word ?? <span className="lp-sr">Available</span>}
    </span>
  )
}

function Metric({ label, info, children, wide }: { label: string; info?: string; children: ReactNode; wide?: boolean }) {
  return (
    <div className={`lp-m${wide ? ' is-wide' : ''}`}>
      <span className="lp-ml">
        {label}
        {info && <Info say={info} />}
      </span>
      {children}
    </div>
  )
}

function LendLine({ l, kind, div }: { l: Lend; kind: Kind; div?: boolean }) {
  const over = l.agents > l.maxAgents
  const sp = l.spend !== undefined && l.spendCap !== undefined
  const Tag = div ? 'div' : 'li'
  return (
    <Tag className="lp-lend">
      <span className="lp-lend-ws" title={`Lent to ${l.ws}`}>
        <svg viewBox="0 0 16 16" className="lp-ic" aria-hidden="true"><path d="M3 8h8M8 5l3 3-3 3" /><path d="M13 3v10" /></svg>
        {l.ws}
      </span>
      <span className={`lp-lend-n${over ? ' is-bad' : l.agents === l.maxAgents ? ' is-warn' : ''}`} title={over ? 'Over its cap: running agents finish, no new one until under it' : 'Agents now / the lend’s cap'}>
        {l.agents}/{l.maxAgents} agents
      </span>
      {kind === 'key' && sp && (
        <span className="lp-lend-sp">
          <Bar pct={(l.spend! / l.spendCap!) * 100} tone={toneOf(l.spend!, l.spendCap!)} />
          <span className={`lp-num${toneOf(l.spend!, l.spendCap!) ? ` is-${toneOf(l.spend!, l.spendCap!)}` : ''}`} title={l.spend! > l.spendCap! ? 'Over its spend cap: no new agent on this lend until the 1st' : 'Spend this month / the lend’s spend cap'}>{usd(l.spend!)} / {usd(l.spendCap!)}</span>
        </span>
      )}
    </Tag>
  )
}

/* ---------------------------------------------------------------- card */

type Panel = null | 'lend' | 'caps' | 'replace' | 'delete' | 'rename'

function CredCard({ c, open, onOpen, panel, setPanel, onDelete }: { c: Cred; open: boolean; onOpen: () => void; panel: Panel; setPanel: (p: Panel) => void; onDelete: () => void }) {
  const isKey = c.kind === 'key'
  const agentsTone = c.holders > c.maxAgents ? 'bad' : c.holders === c.maxAgents ? 'warn' : undefined
  return (
    <article className={`lp-card is-${c.state}${open ? ' is-open' : ''}${c.isNew ? ' is-new' : ''}`} data-cred={c.id}>
      <div className="lp-card-hit" onClick={onOpen}>
        <span className="lp-card-head">
          <span className="lp-card-name">
            <span className="lp-label" title={c.label}>{c.label}</span>
            {c.plan && <span className="lp-sub">{c.plan}</span>}
          </span>
          <StateMark c={c} />
          {c.voice && <span className="lp-voice" title="Shared with Voice it">Voice it</span>}
          <button type="button" className="lp-chev-btn" aria-expanded={open} aria-label={`${open ? 'Close' : 'Open'} ${c.label}`} onClick={(e) => { e.stopPropagation(); onOpen() }}>
            <svg viewBox="0 0 16 16" className="lp-ic lp-chev" aria-hidden="true"><path d="M4 6l4 4 4-4" /></svg>
          </button>
        </span>
        <div className="lp-metrics">
          <Metric label="Agents">
            <Bar pct={(c.holders / c.maxAgents) * 100} tone={agentsTone} />
            <span className={`lp-num${agentsTone ? ` is-${agentsTone}` : ''}`}>{c.holders} / {c.maxAgents}</span>
          </Metric>
          {!isKey && (
            <>
              <Metric label="5-hour">
                <Bar pct={c.five!} tone={c.five! >= 100 ? 'bad' : c.five! >= 90 ? 'warn' : undefined} />
                <span className="lp-mv"><span className="lp-num">{c.five}%</span><span className="lp-faint lp-num" title="Resets">{c.fiveReset}</span></span>
              </Metric>
              <Metric label="7-day">
                <Bar pct={c.seven!} tone={c.seven! >= 90 ? 'warn' : undefined} />
                <span className="lp-mv"><span className="lp-num">{c.seven}%</span><span className="lp-faint lp-num" title="Resets">{c.sevenReset}</span></span>
              </Metric>
            </>
          )}
          {isKey && (
            <Metric label="Spend · Nov" info="The platform’s own estimate from each attempt’s token count and list price, never the provider’s figure. Resets on the 1st (UTC)." wide>
              {c.spendCap ? <Bar pct={(c.spend! / c.spendCap) * 100} tone={toneOf(c.spend!, c.spendCap)} /> : <Bar pct={0} tone="off" />}
              <span className={`lp-num${c.spendCap ? ` ${toneOf(c.spend!, c.spendCap) ? `is-${toneOf(c.spend!, c.spendCap)}` : ''}` : ''}`}>
                {usd(c.spend!)}
                <span className="lp-faint"> / {c.spendCap ? usd(c.spendCap) : 'no cap'}</span>
              </span>
            </Metric>
          )}
          <Metric label="Today" info={isKey ? 'Attempts that ran on this key today because the plans had no room' : 'Attempts that ran on this plan today'}>
            <span className="lp-num lp-big">{c.today}</span>
          </Metric>
        </div>
      </div>

      {c.state === 'exhausted' && c.probe && (
        <div className="lp-ask is-bad">
          <span className="lp-num" title={`Credit gone since ${c.probe.since}`}>Probe {c.probe.at} · {c.probe.result}</span>
          <span className="lp-faint lp-num">next {c.probe.next}</span>
          <button type="button" className="c-btn is-sm is-primary">Resume</button>
        </div>
      )}

      {c.lends.length > 0 && !(open && panel === 'lend') && <ul className="lp-lends">{c.lends.map((l) => <LendLine key={l.ws} l={l} kind={c.kind} />)}</ul>}

      <div className="lp-foot">
        <span title={c.replaced ? `Key replaced by ${c.replaced}` : `Added by ${c.by}`}>{c.replaced ? `Replaced · ${c.replaced}` : c.by} · {c.at}</span>
        <span className="lp-classes">{c.classes.join(' · ')}</span>
      </div>

      {open && <Expanded c={c} panel={panel} setPanel={setPanel} onDelete={onDelete} />}
    </article>
  )
}

function Expanded({ c, panel, setPanel, onDelete }: { c: Cred; panel: Panel; setPanel: (p: Panel) => void; onDelete: () => void }) {
  const isKey = c.kind === 'key'
  const paused = c.state === 'paused' || c.state === 'draining'
  return (
    <div className="lp-x">
      <div className="lp-acts" role="toolbar" aria-label={`${c.label} actions`}>
        {c.state === 'exhausted' ? null : paused ? (
          <button type="button" className="c-btn is-sm is-primary">Resume</button>
        ) : (
          <>
            <button type="button" className="c-btn is-sm">Pause</button>
            <button type="button" className="c-btn is-sm">Drain</button>
          </>
        )}
        <button type="button" className="c-btn is-sm" aria-pressed={panel === 'caps'} onClick={() => setPanel(panel === 'caps' ? null : 'caps')}>Limits</button>
        <button type="button" className="c-btn is-sm" aria-pressed={panel === 'lend'} onClick={() => setPanel(panel === 'lend' ? null : 'lend')}>Lend</button>
        <button type="button" className="c-btn is-sm" aria-pressed={panel === 'rename'} onClick={() => setPanel(panel === 'rename' ? null : 'rename')}>Rename</button>
        {!isKey && <button type="button" className="c-btn is-sm">Refresh</button>}
        {isKey && <button type="button" className="c-btn is-sm" aria-pressed={panel === 'replace'} onClick={() => setPanel(panel === 'replace' ? null : 'replace')}>Replace key</button>}
        <span className="lp-acts-gap" />
        <label className="lp-switch" title={c.vendor === 'anthropic' ? 'Voice it runs on OpenAI only' : 'Serves Voice it after the plans and before the dedicated Voice it key'}>
          <input type="checkbox" defaultChecked={c.voice} disabled={c.vendor === 'anthropic'} />
          <span className="lp-switch-track" aria-hidden="true" />
          Voice it
        </label>
        <button type="button" className="c-btn is-sm is-danger" aria-pressed={panel === 'delete'} onClick={() => setPanel(panel === 'delete' ? null : 'delete')}>
          {isKey ? 'Delete' : 'Remove'}
        </button>
      </div>

      {panel === 'caps' && <CapsPanel c={c} onClose={() => setPanel(null)} />}
      {panel === 'lend' && <LendPanel c={c} onClose={() => setPanel(null)} />}
      {panel === 'rename' && (
        <div className="lp-panel">
          <h3>Rename</h3>
          <div className="lp-form-row">
            <input className="lp-input" defaultValue={c.label} aria-label="Label" />
            <button type="button" className="c-btn is-sm is-primary">Save</button>
            <button type="button" className="c-btn is-sm is-ghost" onClick={() => setPanel(null)}>Cancel</button>
          </div>
        </div>
      )}
      {panel === 'replace' && <ReplacePanel c={c} onClose={() => setPanel(null)} />}
      {panel === 'delete' && <DeletePanel c={c} onClose={() => setPanel(null)} onDelete={onDelete} />}

      {panel === null && (c.perWs.length > 0 || (c.state === 'exhausted' && c.probe)) && (
        <div className="lp-x-grid">
          {c.perWs.length > 0 && (
            <section className="lp-x-sec">
              <h3>Per workspace <Info say="Agents at once for one workspace; edited under Limits" /></h3>
              <dl className="lp-dl">
                {c.perWs.map((w) => (<Fragment key={w.ws}><dt>{w.ws}</dt><dd className={`lp-num${w.now > w.max ? ' is-bad' : ''}`}>{w.now} / {w.max} agents</dd></Fragment>))}
              </dl>
            </section>
          )}
          {c.state === 'exhausted' && c.probe && (
            <section className="lp-x-sec">
              <h3>Earlier probes <Info say={`A one-token request each hour while the credit is gone, since ${c.probe.since}. A success clears the state; so does Resume after a top-up.`} /></h3>
              <ol className="lp-probes">
                {c.probe.log.slice(1).map((p) => (
                  <li key={p.at}><span className="lp-num">{p.at}</span><span className="lp-state is-bad"><i aria-hidden="true" />{p.result}</span></li>
                ))}
              </ol>
            </section>
          )}
        </div>
      )}
    </div>
  )
}

function Stepper({ label, value, max, info }: { label: string; value: number; max?: number; info?: string }) {
  const [n, setN] = useState(value)
  return (
    <div className="lp-field">
      <span className="lp-flabel">{label}{info && <Info say={info} />}</span>
      <span className="lp-step">
        <button type="button" className="c-btn is-sm is-icon" aria-label={`Fewer: ${label}`} onClick={() => setN(Math.max(0, n - 1))}>−</button>
        <input className="lp-input lp-num" value={n} onChange={(e) => setN(Number(e.target.value) || 0)} aria-label={label} inputMode="numeric" />
        <button type="button" className="c-btn is-sm is-icon" aria-label={`More: ${label}`} onClick={() => setN(max ? Math.min(max, n + 1) : n + 1)}>+</button>
      </span>
    </div>
  )
}

function Money({ label, value, info }: { label: string; value: number | null; info?: string }) {
  return (
    <div className="lp-field">
      <span className="lp-flabel">{label}{info && <Info say={info} />}</span>
      <span className="lp-money"><span aria-hidden="true">$</span><input className="lp-input lp-num" defaultValue={value ?? ''} placeholder="no cap" aria-label={label} inputMode="decimal" /></span>
    </div>
  )
}

function CapsPanel({ c, onClose }: { c: Cred; onClose: () => void }) {
  const [classes, setClasses] = useState<string[]>(c.classes)
  const rows = [c.perWs.find((w) => w.ws === OWN) ?? { ws: OWN, max: 5, now: 1 }, ...c.lends.map((l) => ({ ws: l.ws, max: l.maxAgents, now: l.agents }))]
  return (
    <div className="lp-panel">
      <h3>Limits</h3>
      <div className="lp-panel-grid">
        <Stepper label="Max agents" value={c.maxAgents} info="Agents at once across every workspace, counted from the lease" />
        {c.kind === 'key' && <Money label="Spend cap · month" value={c.spendCap ?? null} info="Past it the key takes no new agent until the 1st; metered from the platform’s estimate" />}
        <div className="lp-field is-wide">
          <span className="lp-flabel">Model classes</span>
          <span className="lp-chips" role="group" aria-label="Model classes">
            {CLASSES[c.vendor].map((k) => (
              <button key={k} type="button" className="c-btn is-sm" aria-pressed={classes.includes(k)} onClick={() => setClasses(classes.includes(k) ? classes.filter((x) => x !== k) : [...classes, k])}>
                {k}
              </button>
            ))}
          </span>
        </div>
      </div>
      <h4>Per workspace <Info say="Agents at once for one workspace. Narrowing below use stops no agent; the next one waits until under the cap." /></h4>
      <ul className="lp-ws-caps">
        {rows.map((r) => (
          <li key={r.ws}>
            <span className="lp-ws">{r.ws}{r.ws === OWN && <span className="lp-faint"> · owner</span>}</span>
            <span className={`lp-num${r.now > r.max ? ' is-bad' : ''}`}>{r.now} now</span>
            <Stepper label={`${r.ws} max agents`} value={r.max} />
          </li>
        ))}
      </ul>
      <div className="lp-panel-acts">
        <button type="button" className="c-btn is-sm is-primary">Save limits</button>
        <button type="button" className="c-btn is-sm is-ghost" onClick={onClose}>Cancel</button>
      </div>
    </div>
  )
}

function LendPanel({ c, onClose }: { c: Cred; onClose: () => void }) {
  const isKey = c.kind === 'key'
  const free = WS_POOL.filter((w) => !c.lends.some((l) => l.ws === w))
  const [ws, setWs] = useState(free[0] ?? '')
  return (
    <div className="lp-panel">
      <h3>Lend <Info say={`Every lend carries a cap: agents at once${isKey ? ' and a monthly spend cap' : ''}, enforced when an agent is granted the ${isKey ? 'key' : 'plan'}.`} /></h3>
      <div className="lp-panel-grid">
        <div className="lp-field">
          <label className="lp-flabel" htmlFor={`lend-ws-${c.id}`}>Workspace</label>
          <select id={`lend-ws-${c.id}`} className="lp-input lp-select" value={ws} onChange={(e) => setWs(e.target.value)}>
            {free.map((w) => <option key={w}>{w}</option>)}
          </select>
        </div>
        <Stepper label="Agents at once" value={2} max={c.maxAgents} />
        {isKey && <Money label="Spend cap · month" value={75} info="Required on a key. That workspace’s estimated spend on this key, reset on the 1st (UTC)." />}
      </div>
      {c.lends.length > 0 && (
        <>
          <h4>Lent now</h4>
          <ul className="lp-lend-edit">
            {c.lends.map((l) => (
              <li key={l.ws}>
                <LendLine l={l} kind={c.kind} div />
                <button type="button" className="c-btn is-sm">Edit caps</button>
                <button type="button" className="c-btn is-sm is-danger">End lend</button>
              </li>
            ))}
          </ul>
        </>
      )}
      <div className="lp-panel-acts">
        <button type="button" className="c-btn is-sm is-primary">Lend to {ws}</button>
        <button type="button" className="c-btn is-sm is-ghost" onClick={onClose}>Cancel</button>
      </div>
    </div>
  )
}

function KeyField({ id }: { id: string }) {
  return <input id={id} className="lp-input lp-secret" type="password" autoComplete="off" spellCheck={false} placeholder="Paste the key" aria-label="API key" />
}

function ReplacePanel({ c, onClose }: { c: Cred; onClose: () => void }) {
  const [how, setHow] = useState<'paste' | 'link'>('paste')
  return (
    <div className="lp-panel">
      <h3>Replace key <Info say="Same label, caps, lends and spend. Running agents keep the old value until their next turn; the old version is destroyed after the hold time." /></h3>
      <Seg value={how} onPick={setHow} options={[{ k: 'paste', l: 'Paste key' }, { k: 'link', l: 'Send a link' }]} label="How" />
      {how === 'paste' ? (
        <div className="lp-form-row">
          <KeyField id={`rep-${c.id}`} />
          <button type="button" className="c-btn is-sm is-primary">Replace</button>
          <button type="button" className="c-btn is-sm is-ghost" onClick={onClose}>Cancel</button>
        </div>
      ) : (
        <div className="lp-form-row">
          <input className="lp-input" type="email" placeholder="Their email" aria-label="Email of the key's owner" />
          <button type="button" className="c-btn is-sm is-primary">Send link</button>
          <button type="button" className="c-btn is-sm is-ghost" onClick={onClose}>Cancel</button>
        </div>
      )}
    </div>
  )
}

function DeletePanel({ c, onClose, onDelete }: { c: Cred; onClose: () => void; onDelete: () => void }) {
  const [typed, setTyped] = useState(c.label.slice(0, Math.max(0, c.label.length - 3)))
  const v = VENDOR[c.vendor]
  const isKey = c.kind === 'key'
  return (
    <div className="lp-panel is-danger" role="group" aria-labelledby={`del-${c.id}`}>
      <h3 id={`del-${c.id}`}>
        {isKey ? 'Delete API key' : 'Remove plan'} {c.label}
        <Info
          say={isKey
            ? `The stored key is destroyed now. Agents running on it keep the value they already read until their next turn, then move to the next tier or park. Deleting here does not revoke the key at ${v}: a copy anywhere else keeps working until it is revoked there.`
            : 'Live holds close now; the sign-in is kept so the plan can be added back.'}
        />
      </h3>
      <div className="lp-form-row">
        <label className="lp-flabel" htmlFor={`del-in-${c.id}`}>Type <b className="lp-mono">{c.label}</b> to delete</label>
        <input id={`del-in-${c.id}`} className="lp-input lp-mono" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
        <button type="button" className="c-btn is-sm is-danger-filled" disabled={typed !== c.label} onClick={onDelete}>{isKey ? 'Delete key' : 'Remove plan'}</button>
        <button type="button" className="c-btn is-sm is-ghost" onClick={onClose}>Cancel</button>
      </div>
    </div>
  )
}

function Seg<T extends string>({ value, onPick, options, label }: { value: T; onPick: (v: T) => void; options: { k: T; l: ReactNode }[]; label: string }) {
  return (
    <span className="lp-seg" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button key={o.k} type="button" role="radio" aria-checked={value === o.k} className={value === o.k ? 'is-on' : ''} onClick={() => onPick(o.k)}>
          {o.l}
        </button>
      ))}
    </span>
  )
}

/* ---------------------------------------------------------------- add */

function AddCard({ kind, step0, onCancel, onDone }: { kind: Kind; step0: string | null; onCancel: () => void; onDone: (c: Cred) => void }) {
  const [vendor, setVendor] = useState<Vendor | null>(step0 === 'vendor' || step0 === null ? null : 'anthropic')
  const [how, setHow] = useState<'here' | 'link'>('here')
  const [label, setLabel] = useState(step0 === 'form' ? 'eval-runs' : '')
  return (
    <article className="lp-card is-open lp-add" aria-label={kind === 'key' ? 'Add key' : 'Add plan'}>
      <div className="lp-add-head">
        <h3>{kind === 'key' ? 'Add key' : 'Add plan'}</h3>
        <button type="button" className="c-btn is-sm is-ghost" onClick={onCancel}>Cancel</button>
      </div>
      <div className="lp-vendors" role="radiogroup" aria-label="Vendor">
        {(['anthropic', 'openai'] as Vendor[]).map((v) => (
          <button key={v} type="button" role="radio" aria-checked={vendor === v} className={`lp-vendor${vendor === v ? ' is-on' : ''}`} onClick={() => setVendor(v)}>
            <VendorGlyph v={v} />
            <span>
              <b>{VENDOR[v]}</b>
              <span className="lp-faint">{kind === 'plan' ? (v === 'anthropic' ? 'Claude plan' : 'ChatGPT plan') : `${VENDOR[v]} API key`}</span>
            </span>
          </button>
        ))}
      </div>
      {vendor && kind === 'plan' && (
        <div className="lp-add-body">
          <Seg value={how} onPick={setHow} options={[{ k: 'here', l: 'Sign in here' }, { k: 'link', l: 'Send a link' }]} label="How" />
          {how === 'here' ? (
            <ol className="lp-signin">
              <li className="is-done"><span className="lp-stepn">1</span>{vendor === 'anthropic' ? 'Claude sign-in opened' : 'Codex sign-in opened'}<span className="lp-faint lp-num">new tab · 01:38</span></li>
              <li className="is-now">
                <span className="lp-stepn">2</span>
                <span className="lp-signin-f">
                  <label className="lp-flabel" htmlFor="signin-code">Code from the sign-in page<Info say={vendor === 'anthropic' ? 'Sign in with the Claude account that holds the plan, then paste the code it shows.' : 'Sign in with the ChatGPT account that holds the plan (the Codex sign-in), then paste the code it shows.'} /></label>
                  <span className="lp-form-row">
                    <input id="signin-code" className="lp-input lp-mono" autoComplete="off" placeholder="code" />
                    <button type="button" className="c-btn is-sm is-primary">Add plan</button>
                    <button type="button" className="c-btn is-sm">Open sign-in again</button>
                  </span>
                </span>
              </li>
              <li><span className="lp-stepn">3</span>Label, max agents, lends</li>
            </ol>
          ) : (
            <div className="lp-form-row">
              <input className="lp-input" type="email" placeholder="Plan owner’s email" aria-label="Plan owner's email" />
              <button type="button" className="c-btn is-sm is-primary">Send link</button>
              <Info say="A one-time link, bound to that email, single use, 10 minutes." />
            </div>
          )}
        </div>
      )}
      {vendor && kind === 'key' && (
        <div className="lp-add-body">
          <div className="lp-panel-grid">
            <div className="lp-field">
              <label className="lp-flabel" htmlFor="add-label">Label</label>
              <input id="add-label" className="lp-input lp-mono" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="ci-overflow" />
            </div>
            <div className="lp-field is-wide">
              <span className="lp-flabel">Key<Info say="Sent once to the broker and kept only in Secret Manager. Never shown back, not even masked." /></span>
              <Seg value={how} onPick={setHow} options={[{ k: 'here', l: 'Paste key' }, { k: 'link', l: 'Send a link' }]} label="How" />
            </div>
          </div>
          {how === 'here' ? (
            <div className="lp-form-row">
              <KeyField id="add-key" />
              <button type="button" className="c-btn is-sm is-primary" disabled={!label} onClick={() => onDone(newKey(vendor, label))}>Add key</button>
            </div>
          ) : (
            <div className="lp-form-row">
              <input className="lp-input" type="email" placeholder="Key owner’s email" aria-label="Key owner's email" />
              <button type="button" className="c-btn is-sm is-primary" disabled={!label}>Send link</button>
              <Info say="A one-time link, bound to that email, single use, 10 minutes. The key is only ever in their form’s POST." />
            </div>
          )}
        </div>
      )}
    </article>
  )
}

function newKey(vendor: Vendor, label: string): Cred {
  return {
    id: `k-new-${label}`, kind: 'key', vendor, label, state: 'available', holders: 0, maxAgents: 4, spend: 0, spendCap: null, today: 0,
    classes: CLASSES[vendor], voice: false, by: 'You', at: 'just now', lends: [], perWs: [], isNew: true,
  }
}

/* ---------------------------------------------------------------- page */

function readParams() {
  const q = new URLSearchParams(window.location.search)
  return { tab: q.get('tab') === 'api-keys' ? 'keys' : 'plans', open: q.get('open'), do: q.get('do'), fx: q.get('fx'), filter: q.get('vendor') }
}

export function LlmProvidersCards({ go }: { go?: (to: string) => void }) {
  const p = useMemo(readParams, [])
  const many = p.fx === 'many' ? manyFixtures() : null
  const [tab, setTab] = useState<'plans' | 'keys'>(p.tab as 'plans' | 'keys')
  const [plans, setPlans] = useState<Cred[]>(many ? many.plans : PLANS)
  const [keys, setKeys] = useState<Cred[]>(p.fx === 'empty' ? [] : many ? many.keys : p.do === 'add-done' ? [newKey('anthropic', 'eval-runs'), ...KEYS] : KEYS)
  const [open, setOpen] = useState<string | null>(p.open)
  const [panel, setPanel] = useState<Panel>(['lend', 'caps', 'replace', 'delete', 'rename'].includes(p.do ?? '') ? (p.do as Panel) : null)
  const [adding, setAdding] = useState<string | null>(p.do?.startsWith('add-') && p.do !== 'add-done' ? p.do.slice(4) : null)
  const [filter, setFilter] = useState<string>(p.filter ?? 'all')

  useEffect(() => {
    const q = new URLSearchParams(window.location.search)
    q.set('lp', 'cards')
    q.set('tab', tab === 'keys' ? 'api-keys' : 'plans')
    window.history.replaceState(window.history.state, '', `${window.location.pathname}?${q.toString()}`)
  }, [tab])
  void go

  const list = tab === 'plans' ? plans : keys
  const setList = tab === 'plans' ? setPlans : setKeys
  const kind: Kind = tab === 'plans' ? 'plan' : 'key'
  const shown = list.filter((c) => filter === 'all' || (filter === 'voice' ? c.voice : c.vendor === filter))
  const live = list.reduce((n, c) => n + c.holders, 0)
  const cap = list.reduce((n, c) => n + c.maxAgents, 0)
  const free = list.filter((c) => c.state === 'available' && c.holders < c.maxAgents).length
  const today = list.reduce((n, c) => n + c.today, 0)
  const asks = list.filter((c) => c.state === 'exhausted' || c.state === 'signin').length
  const monthSpend = keys.reduce((n, c) => n + (c.spend ?? 0), 0)

  const toggle = (id: string) => {
    setOpen(open === id ? null : id)
    setPanel(null)
  }

  return (
    <div className="lp-page">
      <div className="c-phead lp-phead">
        <div className="head">
          <h1>LLM Providers</h1>
          <Info say="Plans serve first; then the shared API keys; Voice it last uses its own key. Within one vendor only: Anthropic for Claude steps, OpenAI for Codex and Voice it." />
        </div>
        <div className="c-acts">
          <Seg
            value={tab}
            onPick={(t) => { setTab(t); setOpen(null); setPanel(null); setAdding(null); setFilter('all') }}
            options={[
              { k: 'plans', l: <>Plans <span className="lp-count">{plans.length}</span></> },
              { k: 'keys', l: <>API Keys <span className="lp-count">{keys.length}</span></> },
            ]}
            label="LLM Providers pages"
          />
        </div>
      </div>

      {(list.length > 0 || adding) && (
        <div className="lp-bar-row">
          <span className="lp-facts lp-num">
            <span><b>{free}</b> of {list.length} can take an agent</span>
            <span><b>{live}</b> / {cap} agents</span>
            <span title={tab === 'keys' ? 'Attempts that fell through to a key today' : 'Attempts on plans today'}><b>{today}</b> today</span>
            {tab === 'keys' && <span title="The platform’s estimate this month"><b>{usd(monthSpend)}</b> Nov est.</span>}
            {asks > 0 && <span className="is-bad"><b>{asks}</b> needs you</span>}
          </span>
          <span className="lp-filters" role="radiogroup" aria-label="Show">
            {[['all', 'All'], ['anthropic', 'Anthropic'], ['openai', 'OpenAI'], ...(tab === 'plans' || list.some((c) => c.voice) ? [['voice', 'Voice it']] : [])].map(([k, l]) => (
              <button key={k} type="button" role="radio" aria-checked={filter === k} className={`lp-chip${filter === k ? ' is-on' : ''}`} onClick={() => setFilter(k!)}>{l}</button>
            ))}
          </span>
          <button type="button" className="c-btn is-primary lp-add-btn" onClick={() => { setAdding('vendor'); setOpen(null) }}>
            <svg viewBox="0 0 16 16" className="c-ic" aria-hidden="true"><path d="M8 3v10M3 8h10" /></svg>
            {tab === 'plans' ? 'Add plan' : 'Add key'}
          </button>
        </div>
      )}

      {list.length === 0 && !adding && (
        <div className="lp-empty">
          <span className="lp-empty-t">No API keys<Info say="Agents use a key only after the plans have no room, so a page with no key is valid. Read just now." /></span>
          <button type="button" className="c-btn is-primary" onClick={() => setAdding('vendor')}>
            <svg viewBox="0 0 16 16" className="c-ic" aria-hidden="true"><path d="M8 3v10M3 8h10" /></svg>
            Add key
          </button>
        </div>
      )}

      {adding && (
        <AddCard
          kind={kind}
          step0={adding}
          onCancel={() => setAdding(null)}
          onDone={(c) => { setList([c, ...list]); setAdding(null) }}
        />
      )}

      {(['anthropic', 'openai'] as Vendor[]).map((v) => {
        const rows = shown.filter((c) => c.vendor === v)
        if (rows.length === 0) return null
        const hold = rows.reduce((n, c) => n + c.holders, 0)
        const mx = rows.reduce((n, c) => n + c.maxAgents, 0)
        return (
          <section key={v} className="lp-group" aria-label={VENDOR[v]}>
            <h2 className="lp-group-h">
              <VendorGlyph v={v} />
              {VENDOR[v]}
              <span className="lp-faint lp-num">{rows.length} {kind === 'plan' ? (rows.length === 1 ? 'plan' : 'plans') : rows.length === 1 ? 'key' : 'keys'} · {hold}/{mx} agents</span>
            </h2>
            <div className="lp-grid">
              {rows.map((c) => (
                <CredCard
                  key={c.id}
                  c={c}
                  open={open === c.id}
                  onOpen={() => toggle(c.id)}
                  panel={open === c.id ? panel : null}
                  setPanel={setPanel}
                  onDelete={() => { setList(list.filter((x) => x.id !== c.id)); setOpen(null); setPanel(null) }}
                />
              ))}
            </div>
          </section>
        )
      })}
    </div>
  )
}

/* ---------------------------------------------------------------- Voice it */

/** Voice it › Settings: the dedicated OpenAI key block and the chain line. */
export function LpVoiceKeyCard() {
  const [panel, setPanel] = useState<null | 'replace' | 'delete'>(null)
  const [typed, setTyped] = useState('')
  return (
    <section className="vset-card lp-vkey" aria-labelledby="vset-lpkey" data-card="lp-key">
      <header className="vset-card-head">
        <h2 id="vset-lpkey">Dedicated OpenAI key</h2>
        <Info say="An OpenAI API key for Voice it alone, never lent. Used last: after the plans shared with Voice it and the shared API keys." />
      </header>
      <div className="vset-card-body">
        <ol className="lp-chain" aria-label="Voice it’s order">
          <li className="is-ok" title="ops-desk · ChatGPT Pro: 1 of 3 agents free">
            <span className="lp-chain-n">1</span>
            <span><b>Plans</b><span className="lp-state is-ok"><i aria-hidden="true" />1 free</span></span>
          </li>
          <li className="is-warn" title="voice-and-codex: rate limited until 02:14">
            <span className="lp-chain-n">2</span>
            <span><b>Shared keys</b><span className="lp-state is-warn"><i aria-hidden="true" />Limited until 02:14</span></span>
          </li>
          <li className="is-ok is-this" title="This key: ready">
            <span className="lp-chain-n">3</span>
            <span><b>This key</b><span className="lp-state is-ok"><i aria-hidden="true" />Ready</span></span>
          </li>
        </ol>
        <dl className="lp-dl lp-vkey-dl">
          <dt>Set by</dt><dd>Sam Okafor</dd>
          <dt>When</dt><dd className="lp-num">Nov 3, 14:20</dd>
          <dt>Spend · Nov</dt><dd className="lp-num">$27.80 <Info say="The platform’s estimate from Voice it’s own metering, never the provider’s figure" /></dd>
        </dl>
        <div className="lp-acts">
          <button type="button" className="c-btn is-sm" aria-pressed={panel === 'replace'} onClick={() => setPanel(panel === 'replace' ? null : 'replace')}>Replace</button>
          <button type="button" className="c-btn is-sm is-danger" aria-pressed={panel === 'delete'} onClick={() => setPanel(panel === 'delete' ? null : 'delete')}>Delete</button>
        </div>
        {panel === 'replace' && (
          <div className="lp-form-row">
            <KeyField id="vkey-rep" />
            <button type="button" className="c-btn is-sm is-primary">Replace</button>
          </div>
        )}
        {panel === 'delete' && (
          <div className="lp-panel is-danger">
            <div className="lp-form-row">
              <label className="lp-flabel" htmlFor="vkey-del">Type <b className="lp-mono">voice-it</b> to delete<Info say="The stored key is destroyed now; Voice it then has no last tier. Deleting here does not revoke the key at OpenAI: a copy anywhere else keeps working until it is revoked there." /></label>
              <input id="vkey-del" className="lp-input lp-mono" value={typed} onChange={(e) => setTyped(e.target.value)} />
              <button type="button" className="c-btn is-sm is-danger-filled" disabled={typed !== 'voice-it'}>Delete key</button>
            </div>
          </div>
        )}
      </div>
    </section>
  )
}

/** A Voice it session's header: the serving tier as a small mark, and one caption for a mid-session change. */
export function LpVoiceTierHeader() {
  return (
    <div className="lp-vhead">
      <div className="lp-vhead-row">
        <span className="lp-vhead-dot" aria-hidden="true" />
        <span className="lp-vhead-title">Session · 01:42</span>
        <span className="lp-num lp-faint">12 min</span>
        <span className="lp-tier is-shared" title="Serving on the shared API key voice-and-codex (OpenAI). Plans shared with Voice it had no room.">Shared key</span>
        <span className="lp-vhead-gap" />
        <button type="button" className="c-btn is-sm">Mute</button>
        <button type="button" className="c-btn is-sm is-danger">End</button>
      </div>
      <p className="lp-caption" title="ops-desk reached its five-hour limit; the session reopened on the next tier at its renew">
        <span className="lp-num">01:51</span> Plan → Shared key
      </p>
    </div>
  )
}

/** An agent attempt's card: the credential tier chip and the moved-to-API-key line. */
export function LpAttemptTier() {
  return (
    <section className="lp-attempt" aria-label="Attempt 2">
      <header className="lp-attempt-h">
        <span className="lp-attempt-n">Attempt 2</span>
        <span className="lp-state is-ok"><i aria-hidden="true" />Running</span>
        <span className="lp-tier is-key" title="ci-overflow · Anthropic API key">API key</span>
        <span className="lp-num lp-faint">gen 7 · 00:41 · 18 min</span>
      </header>
      <ol className="lp-tl">
        <li><span className="lp-num">00:41</span><span>Leased</span><span className="lp-tier is-plan" title="build-team · Claude Max">Plan</span></li>
        <li><span className="lp-num">00:42</span><span>Running</span></li>
        <li><span className="lp-num">00:47</span><span>Checkpoint 3</span></li>
        <li className="is-move" title="build-team reached its five-hour limit (resets 04:40); swapped at a turn boundary">
          <span className="lp-num">00:53</span><span>Moved to API key</span>
        </li>
        <li><span className="lp-num">00:58</span><span>Checkpoint 4</span></li>
      </ol>
    </section>
  )
}

/** The live session's hud in Talk: the tier mark beside the status, and the caption of a mid-session change. */
export function LpVoiceTierHud() {
  return (
    <>
      <p className="vt-status lp-vstatus" role="status">
        <span className="lp-vhead-dot" aria-hidden="true" />
        Live
        <span className="lp-num lp-faint">12 min</span>
        <span className="lp-tier is-shared" title="Serving on the shared API key voice-and-codex (OpenAI): the plans shared with Voice it had no room">Shared key</span>
      </p>
      <span className="vt-hud-actions lp-vacts">
        <button type="button" className="c-btn is-sm">Mute</button>
        <button type="button" className="c-btn is-sm">End</button>
      </span>
      <p className="lp-caption lp-vcap" title="ops-desk reached its five-hour limit; the session reopened on the next tier at its renew">
        <span className="lp-num">01:51</span>
        <span className="lp-tier is-plan">Plan</span>→<span className="lp-tier is-shared">Shared key</span>
      </p>
    </>
  )
}

/** An attempt's credential tier, read from its own record. */
export function LpTierChip({ latest }: { latest: boolean }) {
  return latest
    ? <span className="lp-tier is-key" title="ci-overflow · Anthropic API key">API key</span>
    : <span className="lp-tier is-plan" title="build-team · Claude Max">Plan</span>
}

/** The tier change on the attempt's timeline, one line. */
export function LpMovedLine() {
  return (
    <p className="lp-moved">
      <span className="lp-num lp-faint">00:53</span>
      <b>Moved to API key</b>
      <span className="lp-faint">ci-overflow</span>
      <Info say="build-team reached its five-hour limit (resets 04:40); swapped to the next tier at a turn boundary" />
    </p>
  )
}
