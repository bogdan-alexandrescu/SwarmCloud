// THE PLAN REVIEW CHAT, THE COMPONENT ALONE (docs/design/plans-pipeline.md
// §7, §7.8, §2.5 `DC:D8a`-`DC:D8c`, `DC:D7c`; lane U9a PLAN-CHAT-UI). Red
// first: PlanChat.tsx and styles/plan-chat.css do not exist yet. Its mount on
// the decision page (the page-head button, the folded margin, the phone's
// sheet) is lane U9b and its own test, plan.chat-column.test.tsx.
//
// WHAT EACH CASE HOLDS:
//   * the transcript is read on mount and drawn oldest first: each turn's ask,
//     its answer as TEXT (markup in an answer is shown, never parsed), its cost
//     and the model it ran (`model.model_id`), or the class with the reason
//     when the attempt recorded none;
//   * the head says "$6.40 of $25" from `plan_review_usage`, "at least" when a
//     cost was unreported, amber from 80%, and at a cap the refusal's sentence
//     with the ask disabled; the head keeps a slot for the model control (U13);
//   * an ask posts the shown digest and the text, the turn streams: "starting"
//     before the agent, each delta drawn as text, then the final answer and its
//     cost swap in; the stream reconnects by the turn's id when told to, and a
//     turn already in flight on load is streamed;
//   * a busy chat (409) and a cap (429) answer in the server's words, the cap
//     disabling the ask;
//   * a viewer reads the transcript and has no composer; a decided run's chat
//     is read-only;
//   * Pin to the thread posts the turn's anchor and the turn says pinned;
//   * a chat diagram offers Pin to the plan at the edit level only, posting the
//     CURRENT digest and the turn's diagram, and hands back revision N+1;
//   * a proposal shows its diff against the plan on screen with Request
//     changes (the answer pinned to the thread, then the re-plan asked on the
//     shown digest) and Edit with this (the editor opened with its plan);
//   * at 390 pixels the column fills the screen and its text wraps; amber is
//     the console's warning token in both themes.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { painted } from './marks'
import type { CascadeEnv } from './cssgate'
import { D1, D2, PLAN, RUN, WAIT, at, sent, type Handler, type Json } from './planthreadfixture'

vi.mock('../DiagramView', () => ({
  default: ({ title }: { title: string }) => <figure data-testid="dv" aria-label={title} />,
}))

const LIST = `/v1/runs/${RUN}/plan-chat`
const ADD = `/v1/runs/${RUN}/plan:add-diagram`
const REVISE = `/v1/runs/${RUN}/plan:revise`
const pinOf = (turn: string) => `/v1/runs/${RUN}/plan-chat/${turn}:pin`
const streamOf = (turn: string) => `/v1/runs/${RUN}/plan-chat/${turn}/stream`

const EDITOR = { approve: true, reject: true, why: null, deciders: ['members'], edit: true, comment: true }
const COMMENTER = { approve: false, reject: false, why: 'approvers only', deciders: ['admins'], edit: false, comment: true }
const VIEWER = { approve: false, reject: false, why: 'viewer', deciders: ['members'], edit: false, comment: false }

const SONNET = { class: 'sonnet', from: 'workspace', fell_through: null, model_id: 'claude-sonnet-x-1', why_none: null }

const DRAWN = {
  diagram_id: 'd-cache-state',
  kind: 'state',
  title: 'Overlap cache per digest',
  language: 'mermaid',
  source: 'stateDiagram-v2\n  [*] --> cold\n  cold --> warm',
  why: 'Shows when the cached overlaps are read again.',
  explains: { part: 'plan' },
}

const PROPOSED = { ...PLAN, summary: 'Serve overlaps on the plan read, cached per plan digest.' }

function turn(over: Json = {}): Json {
  return {
    turn_id: 'pct_aa01',
    run_id: RUN,
    asked_by: 'ana@example.com',
    ask: 'Why does step 2 need the train bucket?',
    anchor: null,
    on_digest: D2,
    on_revision: 2,
    state: 'answered',
    task_id: 'task_chat1',
    answer: { text: 'The planner lists the train to find overlaps.', diagrams: [], proposal: null, dropped: [] },
    error: null,
    cost: { usd: 0.42, task_id: 'task_chat1', model_class: 'sonnet', why_none: null },
    first_token_ms: 41000,
    plan_settings: null,
    model: SONNET,
    caps: null,
    no_draw: null,
    pinned_as: null,
    at: at(-10),
    answered_at: at(-9),
    ...over,
  }
}

function usage(spent: number, over: Json = {}): Json {
  return {
    chat_turns: { used: 3, cap: 40 },
    diagram_asks: { used: 1, cap: 20 },
    review_usd: { spent, cap: 25, at_least: false },
    ...over,
  }
}

function runOf(over: Json = {}): Json {
  return {
    id: RUN,
    state: 'PLANNED',
    plan: PLAN,
    plan_digest: D2,
    plan_revision: 2,
    may_decide: EDITOR,
    plan_review_usage: { replans: { used: 0, cap: 5 }, ...usage(6.4) },
    ...over,
  }
}

/** A fake EventSource: the test sends each server-sent event by hand. */
class FakeSource {
  static made: FakeSource[] = []
  url: string
  closed = false
  onmessage: ((ev: MessageEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  constructor(url: string) {
    this.url = url
    FakeSource.made.push(this)
  }
  send(data: Json) {
    act(() => this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent))
  }
  close() {
    this.closed = true
  }
}

const open = () => FakeSource.made.filter((s) => !s.closed)

beforeEach(() => {
  FakeSource.made = []
  vi.stubGlobal('EventSource', FakeSource)
})
afterEach(() => {
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
})

async function chat(o: { turns?: Json[]; run?: Json; listUsage?: Json; extra?: Handler; onEdit?: (plan: Json, replaces: string) => void; onRun?: (run: Json) => void; slot?: boolean; openChat?: boolean } = {}) {
  const run = runOf(o.run ?? {})
  const calls = serve((m, url, body) => {
    const hit = o.extra?.(m, url, body) ?? null
    if (hit !== null) return hit
    const path = url.split('?')[0]!
    if (m === 'GET' && path === LIST) return { status: 200, body: { run_id: RUN, turns: o.turns ?? [turn()], open: o.openChat ?? run.state === 'PLANNED', usage: o.listUsage ?? usage(6.4) } }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { PlanChat } = await import('../PlanChat')
  const onEdit = o.onEdit ?? vi.fn()
  const onRun = o.onRun ?? vi.fn()
  const utils = render(
    <PlanChat
      runId={RUN}
      run={run}
      plan={run.plan as Json}
      digest={String(run.plan_digest)}
      onEdit={onEdit}
      onRun={onRun}
      modelControl={o.slot ? <span data-testid="model-slot">sonnet</span> : undefined}
    />,
  )
  const column = await screen.findByRole('complementary', { name: 'Review chat' }, WAIT)
  return { ...utils, calls, column, onEdit, onRun }
}

const turnsOf = (column: HTMLElement) => within(column).queryAllByTestId('pc-turn')
const askBox = () => screen.getByRole('textbox', { name: 'Ask the review agent' })
const askButton = () => screen.getByRole('button', { name: 'Ask' })

describe('the transcript', () => {
  it('draws each turn oldest first with its ask, its answer as text, its cost and its model', async () => {
    const second = turn({ turn_id: 'pct_aa02', ask: 'And step 1?', answer: { text: '<img src=x onerror="alert(1)"> **bold**', diagrams: [], proposal: null, dropped: [] }, at: at(-5) })
    const { column } = await chat({ turns: [turn(), second] })
    await waitFor(() => expect(turnsOf(column)).toHaveLength(2), WAIT)
    const [one, two] = turnsOf(column)
    expect(visible(one!)).toContain('Why does step 2 need the train bucket?')
    expect(visible(one!)).toContain('The planner lists the train to find overlaps.')
    expect(visible(one!)).toContain('$0.42')
    expect(visible(one!)).toContain('claude-sonnet-x-1')
    // Text from the agent is drawn as text, never as markup.
    expect(visible(two!)).toContain('<img src=x onerror="alert(1)"> **bold**')
    expect(two!.querySelector('img')).toBeNull()
    expect(two!.querySelector('strong')).toBeNull()
  })

  it('names the class and the reason when the attempt recorded no model', async () => {
    const t = turn({ model: { ...SONNET, model_id: null, why_none: 'the attempt recorded no model' } })
    const { column } = await chat({ turns: [t] })
    await waitFor(() => expect(turnsOf(column)).toHaveLength(1), WAIT)
    const model = within(turnsOf(column)[0]!).getByTestId('pc-model')
    expect(visible(model)).toContain('sonnet')
    expect(model.getAttribute('title')).toContain('the attempt recorded no model')
  })

  it('says why a diagram or a proposal was dropped', async () => {
    const t = turn({ answer: { text: 'Here.', diagrams: [], proposal: null, dropped: ['diagram #1 dropped: a diagram is asked for by someone who may edit this plan'] } })
    const { column } = await chat({ turns: [t] })
    await waitFor(() => expect(turnsOf(column)).toHaveLength(1), WAIT)
    expect(visible(turnsOf(column)[0]!)).toContain('a diagram is asked for by someone who may edit this plan')
  })
})

describe('the head: the review spend against its cap', () => {
  it('says "$6.40 of $25" from plan_review_usage and keeps a slot for the model control', async () => {
    const { column } = await chat({ slot: true })
    const head = within(column).getByTestId('pc-spend')
    expect(visible(head)).toContain('$6.40 of $25')
    expect(head.getAttribute('data-level')).toBe('ok')
    expect(within(column).getByTestId('model-slot')).toBeTruthy()
  })

  it('says "at least" when a cost was unreported', async () => {
    const u = usage(6.4, { review_usd: { spent: 6.4, cap: 25, at_least: true } })
    const { column } = await chat({ run: { plan_review_usage: u }, listUsage: u })
    expect(visible(within(column).getByTestId('pc-spend'))).toContain('at least')
  })

  it('turns amber from 80%', async () => {
    const u = usage(20)
    const { column } = await chat({ run: { plan_review_usage: u }, listUsage: u })
    expect(within(column).getByTestId('pc-spend').getAttribute('data-level')).toBe('near')
    expect((askButton() as HTMLButtonElement).disabled).toBe(true) // empty box
    fireEvent.change(askBox(), { target: { value: 'One more?' } })
    expect((askButton() as HTMLButtonElement).disabled).toBe(false)
  })

  it('at the dollar cap shows the refusal sentence and disables the ask', async () => {
    const u = usage(25)
    const { column } = await chat({ run: { plan_review_usage: u }, listUsage: u })
    const spend = within(column).getByTestId('pc-spend')
    expect(spend.getAttribute('data-level')).toBe('cap')
    const alert = within(column).getByRole('alert')
    expect(visible(alert)).toContain('This run has spent $25.00 of its $25.00 of review agents')
    expect(visible(alert)).toContain('Admin › Settings')
    fireEvent.change(askBox(), { target: { value: 'One more?' } })
    expect((askButton() as HTMLButtonElement).disabled).toBe(true)
  })

  it('at the turn cap names that cap', async () => {
    const u = usage(3, { chat_turns: { used: 40, cap: 40 } })
    const { column } = await chat({ run: { plan_review_usage: u }, listUsage: u })
    expect(visible(within(column).getByRole('alert'))).toContain('This run has had its 40 chat turns')
    fireEvent.change(askBox(), { target: { value: 'One more?' } })
    expect((askButton() as HTMLButtonElement).disabled).toBe(true)
  })
})

describe('asking and streaming', () => {
  it('posts the shown digest and the text, streams the deltas as text, then swaps in the final answer', async () => {
    const pending = turn({ turn_id: 'pct_bb01', ask: 'Is the cache keyed by digest?', state: 'starting', answer: null, cost: null, model: { ...SONNET, model_id: null } })
    const { column, calls } = await chat({
      extra: (m, url) => (m === 'POST' && url.split('?')[0] === LIST ? { status: 202, body: { turn: pending } } : null),
    })
    fireEvent.change(askBox(), { target: { value: 'Is the cache keyed by digest?' } })
    fireEvent.click(askButton())
    await waitFor(() => expect(sent(calls, 'POST', LIST)).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', LIST)[0]!.body).toEqual({ plan_digest: D2, ask: 'Is the cache keyed by digest?' })
    await waitFor(() => expect(open()).toHaveLength(1), WAIT)
    const source = open()[0]!
    expect(source.url.split('?')[0]).toBe(streamOf('pct_bb01'))
    source.send({ state: 'starting', position: 'QUEUED' })
    const live = within(column).getAllByTestId('pc-turn').at(-1)!
    expect(visible(live)).toContain('starting')
    source.send({ delta: 'Yes, by the <b>plan</b> ' })
    source.send({ delta: 'digest.' })
    expect(visible(live)).toContain('Yes, by the <b>plan</b> digest.')
    expect(live.querySelector('b')).toBeNull()
    expect((askButton() as HTMLButtonElement).disabled).toBe(true) // one turn in flight
    source.send({ state: 'answered', turn: turn({ turn_id: 'pct_bb01', ask: 'Is the cache keyed by digest?', answer: { text: 'Yes: by the plan digest.', diagrams: [], proposal: null, dropped: [] }, cost: { usd: 0.37, task_id: 'task_chat2', model_class: 'sonnet', why_none: null } }) })
    await waitFor(() => expect(visible(within(column).getAllByTestId('pc-turn').at(-1)!)).toContain('Yes: by the plan digest.'), WAIT)
    expect(visible(within(column).getAllByTestId('pc-turn').at(-1)!)).toContain('$0.37')
    expect(source.closed).toBe(true)
  })

  it('reconnects by the turn id when the stream says so', async () => {
    const live = turn({ turn_id: 'pct_cc01', state: 'answering', answer: null, cost: null })
    await chat({ turns: [live] })
    await waitFor(() => expect(open()).toHaveLength(1), WAIT)
    const first = open()[0]!
    expect(first.url.split('?')[0]).toBe(streamOf('pct_cc01'))
    first.send({ state: 'answering', reconnect: true })
    await waitFor(() => expect(first.closed).toBe(true), WAIT)
    await waitFor(() => expect(open()).toHaveLength(1), WAIT)
    expect(open()[0]!.url.split('?')[0]).toBe(streamOf('pct_cc01'))
  })

  it('shows a busy chat in the server\'s words', async () => {
    const { column } = await chat({
      extra: (m, url) => (m === 'POST' && url.split('?')[0] === LIST ? { status: 409, body: { code: 'chat_busy', detail: 'run already has a chat turn in flight: pct_zz09' } } : null),
    })
    fireEvent.change(askBox(), { target: { value: 'Again?' } })
    fireEvent.click(askButton())
    const alert = await within(column).findByRole('alert', {}, WAIT)
    expect(visible(alert)).toContain('a chat turn in flight')
  })

  it('a cap refused by the server disables the ask with its sentence', async () => {
    const sentence = 'This run has had its 40 chat turns. The cap is a platform setting (Admin › Settings); comment, ask for changes, or edit the plan by hand.'
    const { column } = await chat({
      extra: (m, url) => (m === 'POST' && url.split('?')[0] === LIST ? { status: 429, body: { code: 'chat_cap', cap: 'chat_turns', used: 40, limit: 40, detail: sentence } } : null),
    })
    fireEvent.change(askBox(), { target: { value: 'Again?' } })
    fireEvent.click(askButton())
    const alert = await within(column).findByRole('alert', {}, WAIT)
    expect(visible(alert)).toContain(sentence)
    expect((askButton() as HTMLButtonElement).disabled).toBe(true)
  })
})

describe('who may ask', () => {
  it('a viewer reads the transcript and has no composer', async () => {
    const { column } = await chat({ run: { may_decide: VIEWER } })
    await waitFor(() => expect(turnsOf(column)).toHaveLength(1), WAIT)
    expect(screen.queryByRole('textbox', { name: 'Ask the review agent' })).toBeNull()
    expect(within(column).queryByRole('button', { name: 'Pin to the thread' })).toBeNull()
  })

  it('a decided run\'s chat is read-only', async () => {
    const { column } = await chat({ run: { state: 'APPROVED', may_decide: undefined }, openChat: false })
    await waitFor(() => expect(turnsOf(column)).toHaveLength(1), WAIT)
    expect(screen.queryByRole('textbox', { name: 'Ask the review agent' })).toBeNull()
  })
})

describe('pinning', () => {
  it('Pin to the thread posts the turn\'s anchor and the turn says pinned', async () => {
    const anchored = turn({ anchor: { part: 'step', ref: 'iam-binding' } })
    const { column, calls } = await chat({
      turns: [anchored],
      extra: (m, url) => (m === 'POST' && url.split('?')[0] === pinOf('pct_aa01') ? { status: 201, body: { entry: { entry_id: 'pth_pin1', kind: 'chat_pin' } } } : null),
    })
    await waitFor(() => expect(turnsOf(column)).toHaveLength(1), WAIT)
    fireEvent.click(within(turnsOf(column)[0]!).getByRole('button', { name: 'Pin to the thread' }))
    await waitFor(() => expect(sent(calls, 'POST', pinOf('pct_aa01'))).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', pinOf('pct_aa01'))[0]!.body).toEqual({ anchor: { part: 'step', ref: 'iam-binding' } })
    await waitFor(() => expect(within(turnsOf(column)[0]!).getByTestId('pc-pinned')).toBeTruthy(), WAIT)
    expect(within(turnsOf(column)[0]!).queryByRole('button', { name: 'Pin to the thread' })).toBeNull()
  })

  it('a chat diagram offers Pin to the plan, posting the current digest and the turn\'s diagram', async () => {
    const t = turn({ answer: { text: 'Drawn.', diagrams: [DRAWN], proposal: null, dropped: [] } })
    const next = runOf({ plan: { ...PLAN, diagrams: [DRAWN] }, plan_digest: 'sha256:' + 'ee'.repeat(32), plan_revision: 3 })
    const { column, calls, onRun } = await chat({
      turns: [t],
      extra: (m, url) => (m === 'POST' && url.split('?')[0] === ADD ? { status: 200, body: { run: next } } : null),
    })
    await waitFor(() => expect(within(column).getByTestId('dv')).toBeTruthy(), WAIT)
    fireEvent.click(within(column).getByRole('button', { name: 'Pin to the plan' }))
    await waitFor(() => expect(sent(calls, 'POST', ADD)).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', ADD)[0]!.body).toEqual({ plan_digest: D2, from: { chat_turn_id: 'pct_aa01', diagram_id: 'd-cache-state' } })
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(next), WAIT)
  })

  it('a member below the edit level gets no Pin to the plan', async () => {
    const t = turn({ answer: { text: 'Drawn.', diagrams: [DRAWN], proposal: null, dropped: [] } })
    const { column } = await chat({ turns: [t], run: { may_decide: COMMENTER } })
    await waitFor(() => expect(within(column).getByTestId('dv')).toBeTruthy(), WAIT)
    expect(within(column).queryByRole('button', { name: 'Pin to the plan' })).toBeNull()
    expect(within(column).getByRole('button', { name: 'Pin to the thread' })).toBeTruthy()
  })
})

describe('a proposal', () => {
  const proposing = () =>
    turn({ answer: { text: 'Key the cache by digest.', diagrams: [], proposal: { plan: PROPOSED, digest: D1, why: 'A cache keyed by run misses a new revision.', changed: [{ path: 'summary', op: 'replace' }], changed_truncated: false }, dropped: [] } })

  it('shows its diff against the plan on screen', async () => {
    const { column } = await chat({ turns: [proposing()] })
    const diff = await within(column).findByTestId('pr-diff', {}, WAIT)
    const row = diff.querySelector('[data-path="summary"]')
    expect(row).not.toBeNull()
    expect(visible(row as Element)).toContain(PROPOSED.summary)
    expect(visible(within(column).getByTestId('pc-proposal'))).toContain('A cache keyed by run misses a new revision.')
  })

  it('Request changes pins the answer to the thread, then asks the re-plan on the shown digest', async () => {
    const { column, calls } = await chat({
      turns: [proposing()],
      extra: (m, url) => {
        const path = url.split('?')[0]
        if (m === 'POST' && path === pinOf('pct_aa01')) return { status: 201, body: { entry: { entry_id: 'pth_pin2', kind: 'chat_pin' } } }
        if (m === 'POST' && path === REVISE) return { status: 202, body: { replan: { replan_id: 'prp_1', state: 'starting' } } }
        return null
      },
    })
    fireEvent.click(await within(column).findByRole('button', { name: 'Request changes with this' }, WAIT))
    await waitFor(() => expect(sent(calls, 'POST', REVISE)).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', pinOf('pct_aa01'))[0]!.body).toEqual({ anchor: { part: 'plan', ref: null } })
    expect(sent(calls, 'POST', REVISE)[0]!.body).toEqual({ plan_digest: D2 })
    const pinAt = calls.findIndex((c) => c.url.split('?')[0] === pinOf('pct_aa01'))
    const reviseAt = calls.findIndex((c) => c.url.split('?')[0] === REVISE)
    expect(pinAt).toBeLessThan(reviseAt)
  })

  it('Edit with this opens the editor with the proposed plan and the digest it replaces', async () => {
    const { column, onEdit } = await chat({ turns: [proposing()] })
    fireEvent.click(await within(column).findByRole('button', { name: 'Edit with this' }, WAIT))
    expect(onEdit).toHaveBeenCalledWith(PROPOSED, D2)
  })

  it('a member below the edit level sees the diff without the two buttons', async () => {
    const { column } = await chat({ turns: [proposing()], run: { may_decide: COMMENTER } })
    await within(column).findByTestId('pr-diff', {}, WAIT)
    expect(within(column).queryByRole('button', { name: 'Request changes with this' })).toBeNull()
    expect(within(column).queryByRole('button', { name: 'Edit with this' })).toBeNull()
  })
})

describe('the column on every screen', () => {
  const PHONE: CascadeEnv = { width: 390 }
  it('at 390 pixels fills the screen and wraps its text', async () => {
    const { column } = await chat()
    await waitFor(() => expect(turnsOf(column)).toHaveLength(1), WAIT)
    expect(painted(column, 'max-width', PHONE)).toBe('100%')
    expect(painted(column, 'min-width', PHONE)).toBe('0')
    const text = within(turnsOf(column)[0]!).getByText('The planner lists the train to find overlaps.')
    expect(painted(text, 'overflow-wrap', PHONE)).toBe('anywhere')
    expect(painted(text, 'white-space', PHONE)).toBe('pre-wrap')
  })

  it('draws amber with the console\'s warning token in both themes', async () => {
    const u = usage(21)
    const { column } = await chat({ run: { plan_review_usage: u }, listUsage: u })
    const spend = within(column).getByTestId('pc-spend')
    for (const theme of ['dark', 'light'] as const) {
      expect(painted(spend, 'color', { width: 1280, theme })).toContain('warn')
    }
  })
})
