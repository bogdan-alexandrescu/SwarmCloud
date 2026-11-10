// MOCK-HOOK FILE
// Voice it › Settings' dedicated OpenAI key block, a Voice it session's tier mark, and an agent
// attempt's tier chip (docs/design/llm-providers.md §5.4, §6.4), direction "table".
import { useState } from 'react'
import { Info } from '../AccountsVerdict'
import type { CaptionLine } from '../voice/captions'
import { lpFirst } from './lp'
import './lp.css'

type Tier = 'plan' | 'key' | 'voice'
const TIER_WORD: Record<Tier, string> = { plan: 'Plan', key: 'Shared key', voice: 'Voice it key' }

function TierChip({ tier, title, word }: { tier: Tier; title: string; word?: string }) {
  return (
    <span className={`lp-chip is-${tier}`} title={title}>
      {word ?? TIER_WORD[tier]}
    </span>
  )
}

/** The card, in Voice it › Settings' own card markup (vset-card). */
export function LpVoiceKeyCard({ canEdit }: { canEdit: boolean }) {
  const [sheet, setSheet] = useState<null | 'replace' | 'delete'>((lpFirst('vsheet') as 'replace' | 'delete' | null) ?? null)
  const [typed, setTyped] = useState(lpFirst('typed') === '1' ? 'voice-it' : '')
  const [val, setVal] = useState(lpFirst('typed') === '1' ? 'x'.repeat(48) : '')
  return (
    <section className="vset-card" aria-labelledby="vset-lpkey" data-card="lpkey">
      <header className="vset-card-head">
        <h2 id="vset-lpkey">Dedicated OpenAI key</h2>
        <Info about="Dedicated OpenAI key" tip="Voice it's own OpenAI key, used last: after the plans and the keys shared with Voice it. It serves GPT-Live, which no ChatGPT plan can. Never lent; never shown, not even masked." />
      </header>
      <div className="vset-card-body lp-vkey">
        <div className="lp-vkey-row">
          <span className="lp-st is-ok" title="Set and answering">
            <i aria-hidden="true" />
            <span className="lp-vh">Set</span>
          </span>
          <span>
            Rowan Vale <span className="lp-dim">· Nov 3, 16:20</span>
          </span>
          <span className="lp-grow" />
          {canEdit && (
            <>
              <button type="button" className="c-btn is-sm" aria-pressed={sheet === 'replace'} onClick={() => setSheet('replace')}>
                Replace
              </button>
              <button type="button" className="c-btn is-sm is-danger" aria-pressed={sheet === 'delete'} onClick={() => setSheet('delete')}>
                Delete
              </button>
            </>
          )}
        </div>
        <div className="lp-chain" aria-label="Voice it's order">
          <span className="lp-chain-step is-serving" title="Serving now: 2 of 2 plans shared with Voice it have room">
            <span className="lp-st is-ok"><i aria-hidden="true" /></span>Plans <span className="lp-dim">2 free</span>
          </span>
          <span className="lp-chain-arrow" aria-hidden="true">→</span>
          <span className="lp-chain-step is-none" title="1 key is shared with Voice it: Shared voice, draining and over its $120 cap">
            <span className="lp-st is-draining"><i aria-hidden="true" /></span>Shared keys <span className="lp-dim">0 of 1</span>
          </span>
          <span className="lp-chain-arrow" aria-hidden="true">→</span>
          <span className="lp-chain-step" title="Ready">
            <span className="lp-st is-ok"><i aria-hidden="true" /></span>This key
          </span>
          <Info about="Order" tip="Each session takes the first tier with room and moves down at a turn boundary when it runs out. GPT-Live starts at the shared keys: a ChatGPT plan cannot serve it." />
        </div>
        {canEdit && sheet === 'replace' && (
          <div className="lp-ss">
            <label className="lp-f">
              <span className="lp-f-l">
                New key
                <Info about="New key" tip="Checked with one read-only call, then stored as a new version. A live session keeps the old value until its next renew; the old version is then destroyed." />
              </span>
              <input className="lp-in mono" type="password" autoComplete="off" spellCheck={false} value={val} onChange={(e) => setVal(e.target.value)} placeholder="sk-…" />
            </label>
            <div className="lp-sheet-acts">
              <button type="button" className="c-btn is-primary is-sm" disabled={val.length < 20}>Replace key</button>
              <button type="button" className="c-btn is-ghost is-sm" onClick={() => setSheet(null)}>Cancel</button>
            </div>
          </div>
        )}
        {canEdit && sheet === 'delete' && (
          <div className="lp-ss">
            <p className="lp-del-line">
              Revoke it at <a href="https://platform.openai.com/api-keys" target="_blank" rel="noreferrer">OpenAI</a> too.
              <Info about="Delete" tip="Every version of the stored key is destroyed now. A session on it moves to the next tier at its next renew or ends with the reason. The key is not revoked at OpenAI." />
            </p>
            <label className="lp-f">
              <span className="lp-f-l">Type voice-it to delete</span>
              <input className="lp-in mono" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
            </label>
            <div className="lp-sheet-acts">
              <button type="button" className="c-btn is-danger-filled is-sm" disabled={typed !== 'voice-it'}>Delete key</button>
              <button type="button" className="c-btn is-ghost is-sm" onClick={() => setSheet(null)}>Cancel</button>
            </div>
          </div>
        )}
      </div>
    </section>
  )
}

/** The real Talk pane's mid-session captions (MOCK-HOOK fixture lines). */
export const LP_TIER_LINES: CaptionLine[] = [
  { id: 'lp1', side: 'you', text: 'Start the docs refresh on the atlas-lab repo.', final: true },
  { id: 'lp2', side: 'agent', text: 'Started. It is planning the first pass now.', final: true },
  { id: 'lp3', side: 'you', text: 'How far is it?', final: true },
  { id: 'lp4', side: 'agent', text: 'Three of eight pages are rewritten.', final: true },
]

/** The tier mark in the Talk pane's status line. */
export function LpTierMark() {
  return <TierChip tier="key" title="Served by Shared voice, an OpenAI key shared with Voice it, since 12:31" />
}

/** The one caption line a mid-session tier change adds to the transcript. */
export function LpTierCaption() {
  return (
    <p className="vt-cap lp-cap-sys" title="Every plan shared with Voice it ran out of its five-hour window; the session moved at a turn boundary">
      <span className="vt-cap-who mono">12:31</span>
      <span className="vt-cap-text">
        <TierChip tier="plan" title="Team primary" /> → <TierChip tier="key" title="Shared voice" />
      </span>
    </p>
  )
}

/** The attempt card's tier chip: the credential tier its record carries (credential_tier, account_id). */
export function LpAttemptChip() {
  return <TierChip tier="key" word="API key" title="Build pool (build-pool), an Anthropic API key, since 11:47" />
}

/** The tier lines from the holds log, at the top of the attempt card. */
export function LpAttemptMoves() {
  return (
    <ol className="lp-att-ev" aria-label="Credential">
      <li>
        <span className="lp-dim mono">11:02:14</span>
        <span>
          <TierChip tier="plan" title="Team primary (team-primary), a Claude plan" /> Team primary
        </span>
      </li>
      <li className="is-move">
        <span className="lp-dim mono">11:47:09</span>
        <span title="Team primary reached its five-hour window (resets 14:22) and no other Anthropic plan had room; the hold moved at the turn boundary">
          Moved to API key · Build pool
        </span>
      </li>
    </ol>
  )
}
