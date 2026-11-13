// MOCK-HOOK FILE
// Direction "margin" of the plans-pipeline mock-ups (docs/design/plans-pipeline.md §2.3, §2.5):
// the built one-page brief stays the document on the left; a margin beside each part carries the
// planner's reason and that part's thread; the pinned diagrams sit as a strip above the brief; the
// review chat opens as a right-hand column that folds the margin under each part while it is open.
// Reached at /automate/approvals/run:run_mock_rerun01?plans=margin&mock=<state>, on invented data.
import { useState, type ReactNode } from 'react'
import { PlanBriefPage, type Decision } from './ApprovalDecision'
import { Button } from './components'
import { InfoTip } from './People'
import { InApp } from './Schedules'
import { Screen } from './Shell'
import { FlowDiagram, SeqDiagram } from './MarginDiagrams'
import {
  AFTER_APPROVAL, ASKED, CHAT, DIGEST, EXPECTED_COST, MOCK_ISSUE, MOCK_RUN, PEOPLE, PINNED, REVISIONS, REVISION_3, THREAD,
  ago, changeWord, mockNow, mockPlan, shortDigest, type MockDiagram, type MockEntry, type MockRevision,
} from './marginFixture'
import './styles/margin-mock.css'

export const MOCK_PLAN_ITEM = `run:${MOCK_RUN}`
/** The mock's query: the item's address drops its query on load, so the first read keeps it for the page. */
const q = () => {
  if (typeof location === 'undefined') return new URLSearchParams('')
  if (location.search.includes('plans=')) sessionStorage.setItem('mg-query', location.search)
  else if (!location.pathname.includes(MOCK_RUN)) return new URLSearchParams(location.search)
  return new URLSearchParams(sessionStorage.getItem('mg-query') ?? '')
}
/** The mock-ups' switch: `?plans=margin`. */
export const marginMock = () => q().get('plans') === 'margin'

type State = 'waiting' | 'viewer' | 'superseded' | 'replanning' | 'revised' | 'edit' | 'running' | 'merged' | 'unmet'
const STATES: State[] = ['waiting', 'viewer', 'superseded', 'replanning', 'revised', 'edit', 'running', 'merged', 'unmet']

const name = (by: string) => PEOPLE[by] ?? by
const mins = (iso: string) => {
  const m = Math.max(0, Math.round((mockNow - Date.parse(iso)) / 60_000))
  return m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`
}
const usd = (n: number) => `$${n.toFixed(2)}`

/** Which part of the brief each changed path of a revision sits beside. */
const PART_OF = (path: string) =>
  path.startsWith('steps.s1') ? 'ab-steps:s1' : path.startsWith('steps.s2') ? 'ab-steps:s2' : path.startsWith('estimate') ? 'ab-head' : 'ab-pair'

interface View {
  state: State
  chat: boolean
  ask: 'none' | 'answer' | 'pinned'
  canDecide: boolean
  canComment: boolean
  revision: number
  approved: boolean
}

function viewOf(): View {
  const p = q()
  const raw = p.get('mock') ?? 'waiting'
  const state = (STATES as string[]).includes(raw) ? (raw as State) : 'waiting'
  const ask = p.get('ask') === 'answer' ? 'answer' : p.get('ask') === 'pinned' ? 'pinned' : 'none'
  const approved = state === 'running' || state === 'merged' || state === 'unmet'
  return { state, chat: p.get('chat') === '1', ask, canDecide: state !== 'viewer', canComment: true, revision: ask === 'pinned' ? 3 : 2, approved }
}

function decisionOf(v: View): Decision {
  return {
    approval: {
      approval_id: MOCK_PLAN_ITEM,
      kind: 'plan',
      subject: { run_id: MOCK_RUN, issue: MOCK_ISSUE },
      digest: DIGEST[v.revision],
      summary: `${MOCK_ISSUE}: Re-run only the checks a rebase can change`,
      approvers: 'owner_only',
      state: v.approved ? 'approved' : 'pending',
      requested_at: ago(42),
      expires_at: new Date(mockNow + 23 * 3_600_000).toISOString(),
      may_decide: v.canDecide
        ? { approve: true, reject: true, why: null, deciders: ['owner_only'] }
        : { approve: false, reject: false, why: 'Only the platform owner decides this plan.', deciders: ['owner_only'] },
      decided_by: v.approved ? 'kit@example.com' : null,
      decided_by_name: v.approved ? 'Kit Arden' : null,
      decided_at: v.approved ? ago(30) : null,
    },
    run: { id: MOCK_RUN, plan: mockPlan(v.revision) },
  } as unknown as Decision
}

export function MarginPlanScreen({ go }: { go: (to: string) => void }) {
  return (
    <Screen title="Approvals" load={async () => ({ status: 'ok' as const, data: 1, fetchedAt: Date.now() })}>
      {() => <MarginPlan go={go} />}
    </Screen>
  )
}

function MarginPlan({ go }: { go: (to: string) => void }) {
  const init = viewOf()
  const [chat, setChat] = useState(init.chat)
  const v = { ...init, chat }
  const d = decisionOf(v)
  const plan = mockPlan(v.revision)
  const back = new URLSearchParams({ plans: 'margin' })
  for (const k of ['source', 'state', 'waits']) if (q().get(k)) back.set(k, q().get(k)!)
  return (
    <div className={`mg${chat ? ' is-chat' : ''}`} data-mock-state={v.state} data-testid="mg">
      <div className="mg-top">
        <p className="ad-back mg-back">
          <InApp go={go} to={`automate/pipeline?${back.toString()}`}>
            ‹ Pipeline
          </InApp>
        </p>
        <Revisions v={v} />
        <Button kind={chat ? 'primary' : 'secondary'} size="sm" aria-pressed={chat} onClick={() => setChat(!chat)} data-testid="mg-chat-toggle">
          {chat ? 'Close review chat' : 'Review chat'}
        </Button>
      </div>
      {v.state === 'superseded' && <Superseded />}
      <div className="mg-body">
        <div className="mg-doc">
          {v.state === 'edit' ? (
            <Editor />
          ) : (
            <>
              <Strip v={v} />
              <PlanBriefPage d={d} plan={plan} docs={null} pulls={[]} kind="plan" go={go} onDecided={() => undefined} bar={<Bar v={v} />} part={(id) => <Margin id={id} v={v} />} />
            </>
          )}
        </div>
        {chat && <Chat onClose={() => setChat(false)} />}
      </div>
    </div>
  )
}

// ---- the revision row in the page head --------------------------------------------------------------
function Revisions({ v }: { v: View }) {
  const revs: MockRevision[] = [...REVISIONS, ...(v.ask === 'pinned' ? [{ ...REVISION_3, author: { kind: 'person' as const, by: 'kit@example.com' }, made_by: 'add_diagram' as const, changed: [{ path: 'diagrams.2', op: 'add' as const }] }] : [])]
  return (
    <ol className="mg-revs" aria-label="Revisions">
      {revs.map((r) => (
        <li key={r.revision} className={r.revision === v.revision ? 'is-on' : ''} aria-current={r.revision === v.revision ? 'true' : undefined}>
          <b>r{r.revision}</b>
          <span className="mono">{shortDigest(r.digest)}</span>
          <span className="ab-dim">
            {r.made_by === 'plan' ? 'planner' : r.made_by === 'revise' ? 're-plan' : r.made_by === 'add_diagram' ? 'diagrams only' : `edit · ${name(r.author.by)}`}
          </span>
        </li>
      ))}
      <InfoTip about="revisions" tip="Every revision keeps its digest. Approve acts on the revision and digest on screen; a comment never changes either." />
    </ol>
  )
}

function Superseded() {
  return (
    <div className="mg-super" role="status" data-testid="mg-superseded">
      <div>
        <b>Revision 3 arrived</b>
        <span className="ab-dim">
          {name(REVISION_3.author.by)} · {mins(REVISION_3.at)} · {REVISION_3.changed.map((c) => `${changeWord(c.path)} ${c.was} → ${c.now}`).join('; ')}
        </span>
      </div>
    </div>
  )
}

// ---- the strip: pinned to the plan -------------------------------------------------------------------------
function Strip({ v }: { v: View }) {
  const diagrams: MockDiagram[] = v.ask === 'pinned' ? [...PINNED, ASKED] : PINNED
  const [tab, setTab] = useState(v.ask === 'pinned' ? ASKED.diagram_id : PINNED[0]!.diagram_id)
  const phone = typeof matchMedia !== 'undefined' && matchMedia('(max-width: 720px)').matches
  // Folded to its tabs with the chat open, on a phone, while a re-plan runs and once approved, so the bar and the thread's
  // latest entries stay above the fold.
  const [open, setOpen] = useState(!v.chat && !phone && v.state !== 'replanning' && !v.approved)
  const on = diagrams.find((x) => x.diagram_id === tab) ?? diagrams[0]!
  const folded = !open
  return (
    <section className={`mg-strip${folded ? ' is-folded' : ''}`} aria-label="Pinned to the plan" data-testid="mg-strip">
      <header className="mg-strip-h">
        <h3>Pinned to the plan</h3>
        <span className="ab-dim">
          {diagrams.length} in r{v.revision}
        </span>
        <InfoTip about="pinned diagrams" tip="These diagrams are inside the revision's digest: approving the plan approves them. A diagram in the thread or chat is not, until it is pinned." />
        <div className="mg-tabs" role="tablist">
          {diagrams.map((x) => (
            <button
              key={x.diagram_id}
              type="button"
              role="tab"
              aria-selected={x.diagram_id === on.diagram_id && !folded}
              className="mg-tab"
              onClick={() => {
                setTab(x.diagram_id)
                setOpen(true)
              }}
            >
              {x.title}
              <span className="ab-dim">{x.explains.part === 'plan' ? 'the plan' : `step ${x.explains.ref === 's1' ? 1 : 2}`}</span>
            </button>
          ))}
        </div>
        {!folded && (
          <Button kind="ghost" size="sm">
            Open large
          </Button>
        )}
      </header>
      {!folded && (
        <div className="mg-strip-b" role="tabpanel">
          <div className="mg-wide">{on.kind === 'flow' ? <FlowDiagram /> : <div className="mg-seqwrap"><SeqDiagram /></div>}</div>
          <div className="mg-tall">{on.kind === 'flow' ? <FlowDiagram tall /> : <SeqDiagram />}</div>
        </div>
      )}
    </section>
  )
}

// ---- the decision bar -------------------------------------------------------------------------------------------
function Bar({ v }: { v: View }) {
  const [reason, setReason] = useState('')
  const dig = shortDigest(DIGEST[v.revision]!)
  const open = THREAD.filter((e) => e.kind === 'comment' && e.answered_by_revision === undefined).length
  if (v.approved)
    return (
      <div className={`ad-bar mg-bar ${v.state === 'unmet' ? 'is-warn' : 'is-ok'}`} data-testid="mg-bar">
        <div className="ad-bar-l">
          <b className="ad-bar-h">
            Approved r2 by Kit Arden · <span className="mono">{shortDigest(DIGEST[2]!)}</span>
          </b>
          <span className="ad-sub">{v.state === 'running' ? 'pull request #4133 · CI round 2 passed' : v.state === 'merged' ? 'merged #4133 · Closes #4127 · review: 3 of 3 requirements met' : 'merged #4133 · part of #4127 · review: 2 of 3 requirements met'}</span>
          {v.state === 'unmet' && <span className="ad-sub mg-warnline">R3 unmet: {UNMET_WHY}</span>}
        </div>
        <div className="ad-bar-acts">
          <Button size="sm">Open the run</Button>
        </div>
      </div>
    )
  if (v.state === 'superseded')
    return (
      <div className="ad-bar mg-bar is-warn" data-testid="mg-bar">
        <div className="ad-bar-l">
          <b className="ad-bar-h">
            r2 · <span className="mono">{dig}</span> is no longer the plan
          </b>
          <span className="ad-sub">Approve left with it</span>
        </div>
        <div className="ad-bar-acts">
          <Button kind="primary">Read revision 3</Button>
        </div>
      </div>
    )
  if (!v.canDecide)
    return (
      <div className="ad-bar mg-bar" data-testid="mg-bar">
        <div className="ad-bar-l">
          <b className="ad-bar-h">The platform owner decides this plan</b>
          <span className="ad-sub">
            r2 · <span className="mono">{dig}</span> · expires in 23 h
          </span>
        </div>
        <div className="ad-bar-acts">
          <InfoTip about="what you may do" tip="Comment in the margin and ask the review chat: a comment never changes the plan, only a revision does." end />
        </div>
      </div>
    )
  const replanning = v.state === 'replanning'
  return (
    <div className="ad-bar is-decide mg-bar" data-testid="mg-bar">
      <div className="ad-bar-l">
        <b className="ad-bar-h">
          Decide revision {v.revision} · <span className="mono">{shortDigest(DIGEST[v.revision]!)}</span>
        </b>
        <span className="ad-sub">expires in 23 h · expected {usd(EXPECTED_COST.low)}–{usd(EXPECTED_COST.high)}</span>
        {replanning && <span className="ad-sub mg-warnline">a re-plan is running for 1 comment; approving revision 2 cancels it</span>}
      </div>
      <label className="c-field ad-why mg-why">
        <span className="c-lbl">Reason to reject</span>
        <textarea className="c-inp" rows={1} value={reason} placeholder="Say why it is rejected" onChange={(e) => setReason(e.target.value)} />
      </label>
      <div className="ad-bar-acts">
        <Button kind="danger" disabledReason={reason.trim() === '' ? 'Say why it is rejected.' : undefined}>
          Reject
        </Button>
        {replanning ? (
          <Button>Cancel the re-plan</Button>
        ) : (
          <Button disabledReason={open === 0 ? 'No open comment on this revision.' : undefined}>Request changes ({open})</Button>
        )}
        <Button disabledReason={replanning ? 'A re-plan is running.' : undefined}>Edit</Button>
        <Button kind="primary">Approve r{v.revision}</Button>
      </div>
    </div>
  )
}

// ---- the margin, beside (or, with the chat open, under) each part ----------------------------------------------
const REASONS: Record<string, { label: string; text: string }[]> = {
  'ab-head': [{ label: 'Why this plan', text: String(mockPlan(2).rationale) }],
  'ab-map': [{ label: 'Why these files', text: 'mergestep.py owns the rule; the cache is a new module so the rule stays one function and the cache can be switched off alone.' }],
  'ab-pair': [{ label: 'Why medium', text: 'Every merge passes through the rule; a wrong key merges on a check that never ran. The age bound and the off switch cap that.' }],
  'ab-over': [{ label: 'Why it is not blocked', text: '#4090 changes the retry count in the same function, not the re-run rule; whichever merges second rebases.' }],
}

function Margin({ id, v }: { id: string; v: View }) {
  if (id === 'ab-steps') {
    const plan = mockPlan(v.revision) as { steps: { step_id: string; rationale: string; title: string }[] }
    return (
      <>
      <aside className={`mg-note is-steps${v.ask === 'answer' ? ' has-asked' : ''}`} aria-label="Margin: steps" data-part={id}>
        {plan.steps.map((s, i) => (
          <div key={s.step_id} className="mg-sub">
            <p className="mg-sub-h">
              <b>Step {i + 1}</b>
              <span className="ab-dim">{s.title}</span>
            </p>
            <Reason label="Why this step" text={s.rationale} stale={v.state === 'running' ? null : null} />
            <Entries anchor={`ab-steps:${s.step_id}`} v={v} />
            {s.step_id === 's1' && v.ask !== 'none' && <Ask v={v} />}
            <Composer v={v} anchor={`step ${i + 1}`} ask={!v.approved} />
          </div>
        ))}
      </aside>
      {v.ask === 'answer' && <AskedDiagram />}
      </>
    )
  }
  const reasons = REASONS[id] ?? []
  return (
    <aside className="mg-note" aria-label={`Margin: ${id.replace('ab-', '')}`} data-part={id}>
      {reasons.map((r) => (
        <Reason key={r.label} label={r.label} text={r.text} />
      ))}
      {id === 'ab-head' && <Opening v={v} />}
      {id === 'ab-head' && (v.state === 'revised' || v.state === 'replanning') && <RevisionNote v={v} />}
      {id === 'ab-head' && <Cost v={v} />}
      <Entries anchor={id} v={v} />
      {id === 'ab-head' && v.state === 'replanning' && <Replanning />}
      {id === 'ab-pair' && (v.state === 'merged' || v.state === 'unmet') && <Verdicts unmet={v.state === 'unmet'} />}
      {id !== 'ab-map' && <Composer v={v} anchor={id === 'ab-head' ? 'the plan' : id === 'ab-pair' ? 'risk' : 'overlaps'} ask={id === 'ab-head' && !v.approved} />}
    </aside>
  )
}

function Reason({ label, text, stale = null }: { label: string; text: string; stale?: number | null }) {
  return (
    <div className="mg-reason">
      <p className="mg-who">
        <span className="mg-av is-planner" aria-hidden>
          P
        </span>
        <b>{label}</b>
        {stale !== null && <span className="ab-dim">written for r{stale}</span>}
      </p>
      <p className="mg-text">{text}</p>
    </div>
  )
}

function Opening({ v }: { v: View }) {
  return (
    <div className="mg-entry is-planner">
      <p className="mg-who">
        <span className="mg-av is-planner" aria-hidden>
          P
        </span>
        <b>Planner</b>
        <span className="ab-dim">r1 · {mins(ago(300))}</span>
      </p>
      <p className="mg-text">
        Drew 1 diagram:{' '}
        <a className="mg-link" href="#mg-strip">
          {PINNED[0]!.title}
        </a>
        <span className="ab-dim"> · {PINNED[0]!.why}</span>
      </p>
      {v.ask === 'pinned' && (
        <p className="mg-text">
          <span className="mg-chip">r3 · diagrams only</span> added{' '}
          <a className="mg-link" href="#mg-strip">
            {ASKED.title}
          </a>
        </p>
      )}
    </div>
  )
}

function Cost({ v }: { v: View }) {
  return (
    <p className="mg-cost">
      <span className="ab-fk">Expected cost</span>
      <b>
        {usd(EXPECTED_COST.low)}–{usd(EXPECTED_COST.high)}
      </b>
      <InfoTip about="expected cost" tip={`${EXPECTED_COST.basis}. Prices read ${mins(EXPECTED_COST.prices_at)}; outside the digest.`} end />
      <span className="ab-fk">Planning so far</span>
      <b>{v.state === 'replanning' ? usd(1.2 + 1.65 + 0.84) : usd(1.2 + 1.65)}</b>
    </p>
  )
}

function RevisionNote({ v }: { v: View }) {
  const r = REVISIONS[1]!
  return (
    <div className="mg-entry is-planner is-rev">
      <p className="mg-who">
        <span className="mg-av is-planner" aria-hidden>
          P
        </span>
        <b>Revision 2</b>
        <span className="ab-dim">
          re-plan asked by {name(r.requested_by!)} · {mins(r.at)} · {usd(r.cost!.usd!)} {r.cost!.model_class}
        </span>
      </p>
      <p className="mg-text">
        {r.changed.length} changes, answering 2 comments · <span className="mono">{shortDigest(DIGEST[1]!)}</span> → <span className="mono">{shortDigest(DIGEST[2]!)}</span>
      </p>
      {v.state === 'revised' && (
        <Button kind="ghost" size="sm">
          Compare r1 with r2
        </Button>
      )}
    </div>
  )
}

function Replanning() {
  return (
    <div className="mg-entry is-planner is-busy" role="status">
      <p className="mg-who">
        <span className="mg-av is-planner" aria-hidden>
          P
        </span>
        <b>Re-planning r2</b>
        <span className="ab-dim">asked by Kit Arden · 3 min · {usd(0.84)} so far · opus</span>
      </p>
      {THREAD.filter((e) => e.kind === 'comment' && e.answered_by_revision === undefined).map((e) => (
        <div key={e.id} className="mg-quote">
          <p className="mg-who">
            <b>{name(e.by)}</b>
            <span className="ab-dim">on r{e.on_revision} · overlaps</span>
          </p>
          <p className="mg-text">{e.text}</p>
          <span className="mg-chip is-warn">sent to the planner with r2's digest</span>
        </div>
      ))}
    </div>
  )
}

function Entries({ anchor, v }: { anchor: string; v: View }) {
  const after = v.approved ? AFTER_APPROVAL.filter((e) => e.anchor === anchor && (v.state !== 'running' || true)) : []
  const list: MockEntry[] = [...THREAD.filter((e) => e.anchor === anchor && e.kind !== 'opening'), ...after]
  const diff = v.state === 'revised' ? REVISIONS[1]!.changed.filter((c) => PART_OF(c.path) === anchor) : []
  if (list.length === 0 && diff.length === 0) return null
  return (
    <>
      {diff.length > 0 && (
        <div className="mg-diff" aria-label="Changed in revision 2">
          <p className="mg-who">
            <span className="mg-chip">changed in r2</span>
          </p>
          {diff.map((c) => (
            <p key={c.path} className="mg-dl">
              <span className="mg-dk">
                {changeWord(c.path)} · {c.op === 'add' ? 'added' : c.op === 'remove' ? 'removed' : 'was, now'}
              </span>
              {c.was !== undefined && <del>{c.was}</del>}
              {c.now !== undefined && <ins>{c.now}</ins>}
            </p>
          ))}
        </div>
      )}
      {list.map((e) => (
        <Entry key={e.id} e={e} v={v} />
      ))}
      {anchor === 'ab-head' && v.approved && v.state !== 'running' && (
        <>
          <Event text="Review: 3 requirements read" at={ago(5)} tone={v.state === 'unmet' ? 'warn' : 'ok'} />
          <Event text={v.state === 'unmet' ? 'Merged #4133 · part of #4127' : 'Merged #4133 · Closes #4127'} at={ago(2)} tone={v.state === 'unmet' ? 'warn' : 'ok'} />
        </>
      )}
    </>
  )
}

function Event({ text, at, tone }: { text: string; at: string; tone?: string }) {
  return (
    <p className={`mg-event${tone ? ` is-${tone}` : ''}`}>
      <span className="mg-dot" aria-hidden />
      <span>{text}</span>
      <span className="ab-dim">{mins(at)}</span>
    </p>
  )
}

function Entry({ e, v }: { e: MockEntry; v: View }) {
  if (e.kind === 'event') return <Event text={e.text} at={e.at} tone={e.tone} />
  const answered = e.answered_by_revision !== undefined
  const sent = v.state === 'replanning' && !answered
  return (
    <div className={`mg-entry${answered ? ' is-answered' : ''}`}>
      <p className="mg-who">
        <span className="mg-av" aria-hidden>
          {name(e.by).slice(0, 1)}
        </span>
        <b>{name(e.by)}</b>
        <span className="ab-dim">
          on r{e.on_revision} · {mins(e.at)}
        </span>
      </p>
      <p className="mg-text">{e.text}</p>
      {answered && <span className="mg-chip is-ok">answered by r{e.answered_by_revision}</span>}
      {sent && <span className="mg-chip is-warn">sent to the planner</span>}
    </div>
  )
}

function Ask({ v }: { v: View }) {
  return (
    <>
      <div className="mg-entry">
        <p className="mg-who">
          <span className="mg-av" aria-hidden>
            K
          </span>
          <b>Kit Arden</b>
          <span className="ab-dim">diagram ask · r2 · 9 min ago</span>
        </p>
        <p className="mg-text">Draw who reads the cache and when CI is called.</p>
      </div>
      <div className="mg-entry is-planner is-diagram">
        <p className="mg-who">
          <span className="mg-av is-planner" aria-hidden>
            R
          </span>
          <b>Review agent</b>
          <span className="ab-dim">sonnet · 2 min · {usd(0.38)}</span>
        </p>
        <p className="mg-text">
          <a className="mg-link" href={v.ask === 'pinned' ? '#mg-strip' : '#mg-asked'}>
            {ASKED.title}
          </a>
        </p>
        {v.ask === 'pinned' ? <span className="mg-chip is-ok">pinned in r3 · on the strip</span> : <span className="mg-chip">not in the plan yet</span>}
      </div>
    </>
  )
}

/** The asked diagram, before it is pinned: drawn under the steps it explains, so the margin stays as long as its part. */
function AskedDiagram() {
  return (
    <section className="ab-card mg-asked" id="mg-asked" aria-label={`Asked diagram: ${ASKED.title}`} data-testid="mg-ask-answer">
      <header className="ab-card-h">
        <h3>{ASKED.title}</h3>
        <span className="ab-dim">asked on step 1 · review agent · sonnet · {usd(0.38)} · not in the plan</span>
        <InfoTip about="pinning" tip="Pin to the plan makes revision 3, whose digest covers the new diagram set. Until then the diagram is part of the conversation, not of what Approve approves." end />
      </header>
      <div className="mg-inline-d">
        <SeqDiagram />
      </div>
      <div className="mg-acts">
        <Button kind="primary" size="sm">
          Pin to the plan
        </Button>
        <Button kind="ghost" size="sm">
          Open large
        </Button>
      </div>
    </section>
  )
}

function Composer({ v, anchor, ask }: { v: View; anchor: string; ask: boolean }) {
  if (!v.canComment) return null
  return (
    <div className="mg-compose">
      <Button kind="ghost" size="sm" aria-label={`Comment on ${anchor}`}>
        Comment
      </Button>
      {ask && v.canDecide && (
        <Button kind="ghost" size="sm" aria-label={`Ask for a diagram of ${anchor}`}>
          Ask for a diagram
        </Button>
      )}
    </div>
  )
}

const UNMET_WHY = "the age bound reads the result's start, not its end; a 23 h check finishing late is kept"

function Verdicts({ unmet }: { unmet: boolean }) {
  const reqs = (mockPlan(2) as { requirements: string[] }).requirements
  return (
    <div className="mg-entry is-planner">
      <p className="mg-who">
        <span className="mg-av is-planner" aria-hidden>
          R
        </span>
        <b>Review against the requirements</b>
      </p>
      <ul className="mg-verdicts">
        {reqs.map((r, i) => {
          const bad = unmet && i === 2
          return (
            <li key={r} className={bad ? 'is-bad' : 'is-ok'}>
              <b>{bad ? 'unmet' : 'met'}</b>
              <span>
                R{i + 1} {r}
                {bad && <span className="ab-dim"> · {UNMET_WHY}</span>}
              </span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

// ---- edit by hand ------------------------------------------------------------------------------------------------
function Editor() {
  const plan = mockPlan(2) as { summary: string; estimate: string; requirements: string[]; steps: { step_id: string; title: string; prompt: string; files: string[]; estimate: string; rationale: string }[] }
  const [steps, setSteps] = useState(plan.steps.map((s) => ({ ...s, estimate: s.step_id === 's2' ? '1 agent-hour' : s.estimate })))
  const field = (label: string, value: string, rows = 1, changed = false): ReactNode => (
    <label className={`c-field mg-ef${changed ? ' is-changed' : ''}`}>
      <span className="c-lbl">{label}</span>
      {rows === 1 ? <input className="c-inp" defaultValue={value} /> : <textarea className="c-inp" rows={rows} defaultValue={value} />}
    </label>
  )
  return (
    <div className="mg-edit" data-testid="mg-edit">
      <section className="ab-card mg-editor" aria-label="Edit revision 2">
        <header className="ab-card-h">
          <h3>Edit r2 by hand</h3>
          <span className="ab-dim mono">replaces {shortDigest(DIGEST[2]!)}</span>
        </header>
        {field('Summary', plan.summary)}
        {field('Estimate', '2 agent-hours', 1, true)}
        {field('Requirements', plan.requirements.join('\n'), 3)}
        {steps.map((s, i) => (
          <fieldset key={s.step_id} className="mg-estep">
            <legend>
              Step {i + 1}
              <Button kind="ghost" size="sm" disabledReason={steps.length === 1 ? 'A plan keeps at least one step.' : undefined} onClick={() => setSteps(steps.filter((x) => x.step_id !== s.step_id))}>
                Remove step
              </Button>
            </legend>
            {field('Title', s.title)}
            {field('Prompt', s.prompt, 3)}
            {field('Files', s.files.join('\n'), 2)}
            {field('Estimate', s.estimate, 1, s.step_id === 's2')}
            {field('Reason', s.rationale, 2)}
          </fieldset>
        ))}
        <fieldset className="mg-estep">
          <legend>Diagrams</legend>
          <p className="mg-drow">
            <span>{PINNED[0]!.title}</span>
            <span className="ab-dim">the plan</span>
            <Button kind="ghost" size="sm" disabledReason="Only one diagram to order.">
              Move up
            </Button>
            <Button kind="ghost" size="sm">
              Remove
            </Button>
          </p>
        </fieldset>
      </section>
      <aside className="mg-note mg-changes" aria-label="The change list">
        <p className="mg-who">
          <b>Changes</b>
          <span className="ab-dim">2 · becomes r3</span>
        </p>
        {REVISION_3.changed.map((c) => (
          <p key={c.path} className="mg-dl">
            <span className="mg-dk">{changeWord(c.path)} · was, now</span>
            <del>{c.was}</del>
            <ins>{c.now}</ins>
          </p>
        ))}
        <p className="mg-text ab-dim">
          Reason for step 2 unchanged: marked written for r2
        </p>
        <div className="mg-acts">
          <Button kind="primary">Save as r3</Button>
          <Button kind="ghost">Discard</Button>
        </div>
      </aside>
    </div>
  )
}

// ---- the review chat ----------------------------------------------------------------------------------------------
function Chat({ onClose }: { onClose: () => void }) {
  const [draft, setDraft] = useState('')
  const u = CHAT.usage
  const pct = Math.round((u.review_usd.spent / u.review_usd.cap) * 100)
  return (
    <aside className="mg-chat" aria-label="Review chat" data-testid="mg-chat">
      <header className="mg-chat-h">
        <div>
          <b>Review chat</b>
          <span className="ab-dim">
            review agent · {CHAT.model} · next turn {CHAT.next}
          </span>
        </div>
        <InfoTip about="the review chat" tip="Each question starts one review agent that reads the plan on screen, its thread and this transcript. It never approves or changes the plan; its answers can be pinned to the thread or, for a diagram, to the plan." end />
        <Button kind="ghost" size="sm" onClick={onClose}>
          Close
        </Button>
      </header>
      <ol className="mg-turns">
        {CHAT.turns.map((t, i) => (
          <li key={i} className={t.who === 'review-agent' ? 'is-agent' : 'is-me'}>
            <p className="mg-who">
              <span className={`mg-av${t.who === 'review-agent' ? ' is-planner' : ''}`} aria-hidden>
                {name(t.who).slice(0, 1)}
              </span>
              <b>{name(t.who)}</b>
              <span className="ab-dim">
                {mins(t.at)}
                {'cost' in t && t.cost !== undefined ? ` · ${usd(t.cost)}` : ''}
              </span>
            </p>
            <p className="mg-text">{t.text}</p>
            {t.who === 'review-agent' && (
              <div className="mg-acts">
                {'proposal' in t && t.proposal ? (
                  <>
                    <Button size="sm">Request changes with this</Button>
                    <Button size="sm">Edit with this</Button>
                  </>
                ) : null}
                <Button kind="ghost" size="sm">
                  Pin to the thread
                </Button>
              </div>
            )}
          </li>
        ))}
      </ol>
      <div className="mg-chat-f">
        <label className="c-field">
          <span className="c-lbl">Ask about revision 2</span>
          <textarea className="c-inp" rows={2} value={draft} placeholder="Ask, or ask for a diagram" onChange={(e) => setDraft(e.target.value)} />
        </label>
        <div className="mg-acts">
          <Button kind="primary" size="sm" disabled={draft.trim() === ''}>
            Ask
          </Button>
        </div>
        <div className="mg-meter" aria-label="This run's review spend">
          <span className="ab-fk">Review spend</span>
          <b>
            {usd(u.review_usd.spent)} of {usd(u.review_usd.cap)}
          </b>
          <span className="mg-track" aria-hidden>
            <span style={{ width: `${pct}%` }} />
          </span>
          <span className="ab-dim">
            {u.chat_turns.used} of {u.chat_turns.cap} turns · {u.diagram_asks.used} of {u.diagram_asks.cap} diagram asks
          </span>
          <InfoTip about="review spend" tip="The review chat and on-demand diagrams of this run, against the platform's per-run cap. Re-plans count against the run's day budget instead." end />
        </div>
      </div>
    </aside>
  )
}

