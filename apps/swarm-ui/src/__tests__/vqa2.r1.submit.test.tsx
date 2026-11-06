// VISUAL QA RE-RUN, LANE VQA2-R1 (part of #1427): THE THREE SUBMIT FORMS.
//
// One case per finding, each asserting the property the finding is about --
// where a refusal is drawn and what the panel then says, where focus lands,
// what a cascade resolves to in the real sheets -- not the markup's shape:
//
//   V002  a 422 is drawn in the send panel's outcome slot, attributed or not;
//         the panel says "Refused" and holds the button until the form changes;
//         each field it names is a way to that field;
//   V255  after a submit, of any outcome, focus is on the outcome, not <body>,
//         on the task, workflow and issue forms;
//   V175  the issue form's POST failure is drawn in that slot and scrolled to;
//   V127  a step's runner facts are inline text, so a dot stays with its word;
//   V174  "accepted as" is one value, the row does not wrap, the card's facts
//         are at its size, and its two facts lists do not touch;
//   V250  the Created card's id is 8px from its neighbours and split evenly,
//         and the carrier and its mark are one value;
//   V251  a failed preview read is 12px under the input row, as a 404 is;
//   V252  the reference's dot is inline text after it, held by a no-break space;
//   V253  both radio groups' descriptions are the meta size;
//   V254  a workflow 422's facts are 8px from the sentence under them;
//   V257  the pools in "Room for 0 more" are DM Mono at the meta step.
//
// MUTATIONS, each of which turns a case red: draw `outcome.kind === 'refused'`
// outside `OutcomeSlot` again, or drop `refusedAsIs` from the heading or the
// button; drop the `focus()` from `OutcomeSlot`; move the issue form's failure
// out of its slot; drop `.wfb-step > .wfb-cost { display: block }`; put the
// three parts of "accepted as" back as siblings, or `flex-wrap: wrap` back on
// `.sbf-outcome .ctl-fact`; drop the `* + *` margin; drop `.in-read-failed`;
// make `.in-meta-i.is-ref` a flex row again; put `.dsp-option-say` back at
// `--t-body`; drop the `.sbf-room code` rule.
//
// Nothing here is credential-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'

// `answerWrite` hands the submit a real response, so it turns the fixture
// build's answer to the submit (QA2-FIX-07, runs2-01 and runs2-02) off for that
// case alone; the issue form's cases keep the fixture reads they measure.
const fixtures = vi.hoisted(() => ({ on: true }))
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return Object.defineProperty({ ...actual }, 'USE_FIXTURES', { get: () => fixtures.on, enumerable: true })
})

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import { SubmitScreen } from '../Submit'
import { SubmitWorkflowScreen } from '../SubmitWorkflow'
import { IssuePreviewCard } from '../IssueSubmit'
import type { CascadeEnv } from './cssgate'
import { familyOf } from './faces'
import { painted } from './marks'

const WAIT = { timeout: 4000 }
const PHONE: CascadeEnv = { width: 400 }
const WIDE: CascadeEnv = { width: 1440 }
const BOTH = [PHONE, WIDE]
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
const marginTop = (el: Element, env: CascadeEnv) => {
  const v = painted(el, ['margin-top', 'margin'], env)
  // A shorthand's first value is its top.
  return v === null ? null : v.trim().split(/\s+/)[0]
}

const realFetch = globalThis.fetch

/** Answer the one write with `reply`; every read goes to the development fixture as before. */
function answerWrite(path: string, reply: { status: number; body: unknown }) {
  const posts: unknown[] = []
  fixtures.on = false
  const prior = globalThis.fetch
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if ((init?.method ?? 'GET') === 'POST' && url.startsWith(path)) {
      posts.push(JSON.parse(String(init?.body ?? 'null')))
      return new Response(JSON.stringify(reply.body), { status: reply.status, headers: JSON_HEADERS })
    }
    return prior(input, init)
  }) as unknown as typeof fetch
  return posts
}

afterEach(() => {
  fixtures.on = true
  globalThis.fetch = realFetch
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllEnvs()
})

async function taskForm(profile = 'mock') {
  const r = render(<SubmitScreen />)
  await screen.findByRole('button', { name: 'Start one agent' }, WAIT)
  fireEvent.click(r.container.querySelector<HTMLInputElement>(`input[name="runner-profile"][value="${profile}"]`)!)
  return r.container
}

async function workflowForm() {
  const r = render(<SubmitWorkflowScreen />)
  await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
  const step = r.container.querySelector<HTMLElement>('.wfb-step')!
  fireEvent.change(step.querySelector<HTMLSelectElement>('select.sb-runner-select')!, { target: { value: 'mock' } })
  return r.container
}

const panel = (c: HTMLElement) => c.querySelector<HTMLElement>('.sbf-send')!
const submitButton = () => screen.getByRole('button', { name: 'Start one agent' }) as HTMLButtonElement

describe('V002: a 422 is drawn where the button is, and the panel stops saying "Ready to send"', () => {
  it('a refusal naming a field: in the slot, with a way to the field; "Refused" and the button held until an edit', async () => {
    const c = await taskForm()
    const posts = answerWrite('/v1/tasks', { status: 422, body: {
      code: 'validation_failed', message: 'request body failed validation',
      detail: { errors: [{ loc: ['body', 'input'], msg: 'input.prompt must be a non-empty string' }] },
    } })
    fireEvent.click(submitButton())
    await waitFor(() => expect(panel(c).querySelector('.sbf-outcome')).not.toBeNull(), WAIT)
    const out = panel(c).querySelector<HTMLElement>('.sbf-outcome')!
    expect(visible(out.querySelector('h3'))).toBe('The API refused this agent')
    expect(visible(out.querySelector('.sbf-refused'))).toBe('input — input.prompt must be a non-empty string')
    expect(visible(out)).toContain('Nothing was created.')
    // The field keeps its own sentence.
    expect(visible(c.querySelector('#refused-input'))).toBe('input.prompt must be a non-empty string')

    expect(visible(panel(c).querySelector('h2'))).toBe('Refused')
    expect(submitButton().disabled, 'the same click would be refused the same way').toBe(true)

    // The field's name is a way to it.
    fireEvent.click(within(out).getByRole('button', { name: 'input' }))
    expect(document.activeElement).toBe(c.querySelector('#refused-input'))

    // Any edit makes it a new request.
    fireEvent.change(c.querySelector<HTMLInputElement>('#dsp-repo')!, { target: { value: 'https://github.com/example-org/infra.git' } })
    expect(visible(panel(c).querySelector('h2'))).toBe('Ready to send')
    expect(submitButton().disabled).toBe(false)
    expect(posts).toHaveLength(1)
  })

  it('a refusal naming no field: in the slot too, never under the panel note', async () => {
    const c = await taskForm()
    answerWrite('/v1/tasks', { status: 422, body: { code: 'refused', message: 'The tenant may not submit this today.' } })
    fireEvent.click(submitButton())
    await waitFor(() => expect(visible(panel(c).querySelector('.sbf-outcome'))).toContain('The tenant may not submit this today.'), WAIT)
    expect(visible(panel(c).querySelector('.sbf-outcome'))).toContain('It did not say which field')
    for (const child of panel(c).children) {
      if (child.classList.contains('sbf-outcome')) continue
      expect(visible(child), 'the refusal is drawn outside the slot').not.toContain('The tenant may not submit this today.')
    }
    expect(visible(panel(c).querySelector('h2'))).toBe('Refused')
  })

  it('a refusal on the repository: its name focuses the repository field', async () => {
    const c = await taskForm()
    answerWrite('/v1/tasks', { status: 422, body: { code: 'invalid_dispatch', message: 'direct-pr needs a repository', detail: { missing: 'repository_url' } } })
    fireEvent.click(submitButton())
    const out = await waitFor(() => {
      const o = panel(c).querySelector<HTMLElement>('.sbf-outcome')
      expect(o).not.toBeNull()
      return o!
    }, WAIT)
    fireEvent.click(within(out).getByRole('button', { name: 'repository' }))
    expect(document.activeElement).toBe(c.querySelector('#dsp-repo'))
  })
})

describe('V255: after a submit, focus is on what it did', () => {
  it('the task form: a 201, a 422 and a 500 each leave focus on the outcome', async () => {
    for (const reply of [
      { status: 201, body: { task: { id: 't_vqa2_1', state: 'READY' }, scheduler_woken: true } },
      { status: 422, body: { code: 'refused', message: 'no' } },
      { status: 500, body: { code: 'internal', message: 'The write failed on purpose.' } },
    ]) {
      const c = await taskForm()
      answerWrite('/v1/tasks', reply)
      submitButton().focus()
      fireEvent.click(submitButton())
      await waitFor(() => expect(panel(c).querySelector('.sbf-outcome')).not.toBeNull(), WAIT)
      const out = panel(c).querySelector<HTMLElement>('.sbf-outcome')!
      await waitFor(() => expect(document.activeElement, `HTTP ${reply.status}`).toBe(out))
      expect(out.tabIndex).toBe(-1)
      globalThis.fetch = realFetch
      document.body.innerHTML = ''
    }
  })

  it('the workflow form: a 201 leaves focus on the outcome', async () => {
    const c = await workflowForm()
    answerWrite('/v1/workflows', { status: 201, body: {
      workflow: { workflow_id: 'wf_vqa2_1', steps: [{ step_id: 'mock-1' }], created_at: '2026-10-16T00:00:00Z' }, dispatch: null,
    } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit this workflow' }))
    await within(panel(c)).findByRole('button', { name: 'Submit another' }, WAIT)
    await waitFor(() => expect(document.activeElement).toBe(panel(c).querySelector('.sbf-outcome')))
  })
})

// The issue screen reads through the live client, as vqa.l02.submit.test.tsx does.
const admission = { units: 1, headroom: 4, basis: 'measured', blockers: [], binding: [], counterfactual: [], complete: true, unread: [], uncapped: [] }
const pool = (name: string) => ({
  name, hard_limit: 10, adaptive_target: null, quota_derived_limit: null, effective_limit: 10,
  active: 2, available: 8, enabled: true, updated_at: '2026-10-16T10:00:00Z',
})
const CAPACITY = {
  pools: [pool('global'), pool('tenant:eng'), pool('resource:standard')],
  runner_profiles: {
    'claude-code': { resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: 'anthropic', units: 1, pools: ['global', 'tenant:eng', 'resource:standard'], admission },
  },
  tenant_id: 'eng',
  generated_at: '2026-10-16T10:00:00Z',
}
const PREVIEW = {
  tenant_id: 'eng',
  issue: {
    ref: 'example-org/infra#42', owner: 'example-org', repo: 'infra', number: 42,
    url: 'https://github.com/example-org/infra/issues/42', repository_url: 'https://github.com/example-org/infra',
    title: 'A title', body: 'short', body_truncated: false, body_redacted: false, labels: [], state: 'open', comments: 1,
  },
}

/** The issue form in live mode, every `/v1` call answered here. */
async function issueForm(answers: { preview: { status: number; body: unknown }; run?: { status: number; body: unknown } }) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const reply = (r: { status: number; body: unknown }) => new Response(JSON.stringify(r.body), { status: r.status, headers: JSON_HEADERS })
    if (url.startsWith('/v1/capacity')) return reply({ status: 200, body: CAPACITY })
    if (url.startsWith('/v1/providers')) return reply({ status: 200, body: { tenant_id: 'eng', providers: [], generated_at: '2026-10-16T10:00:00Z' } })
    if (url.startsWith('/v1/issues/preview')) return reply(answers.preview)
    if (url.startsWith('/v1/runs') && init?.method === 'POST' && answers.run) return reply(answers.run)
    return reply({ status: 404, body: { code: 'not_found', message: `no stub for ${url}` } })
  }) as unknown as typeof fetch
  vi.stubEnv('VITE_LIVE', '1')
  // A mock outlives resetModules: the live api is the real module, read afresh.
  vi.doUnmock('../api')
  vi.resetModules()
  const { IssueSubmitScreen } = await import('../IssueSubmit')
  const r = render(<IssueSubmitScreen go={vi.fn()} />)
  const ref = await screen.findByLabelText('Issue reference', {}, WAIT)
  fireEvent.change(ref, { target: { value: 'example-org/infra#42' } })
  fireEvent.keyDown(ref, { key: 'Enter' })
  return r.container
}

describe('V175 / V255: the issue form\'s failed POST is drawn in the slot, in view, with focus', () => {
  it('a 500 lands inside the send panel\'s outcome slot, which is scrolled to and focused', async () => {
    const into = vi.fn()
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { value: into, configurable: true, writable: true })
    try {
      const c = await issueForm({ preview: { status: 200, body: PREVIEW }, run: { status: 500, body: { code: 'internal', message: 'The run write failed on purpose.' } } })
      await waitFor(() => expect(c.querySelector('.in-preview')).not.toBeNull(), WAIT)
      fireEvent.click(screen.getByRole('button', { name: 'Plan this issue' }))
      await waitFor(() => expect(c.querySelector('.in-send .sbf-outcome')).not.toBeNull(), WAIT)
      const out = c.querySelector<HTMLElement>('.in-send .sbf-outcome')!
      expect(visible(out)).toContain('The run write failed on purpose.')
      expect(visible(out)).toContain('Check Runs before submitting again.')
      expect(into.mock.contexts).toContain(out)
      expect(document.activeElement).toBe(out)
    } finally {
      delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView
    }
  })
})

describe('V251: a failed preview read is spaced from the input row as a 404 is', () => {
  it('a 500 read sits 12px under the row, at both widths', async () => {
    const c = await issueForm({ preview: { status: 500, body: { code: 'internal', message: 'The preview read failed on purpose.' } } })
    await waitFor(() => expect(visible(c)).toContain('The preview read failed on purpose.'), WAIT)
    const ask = c.querySelector('.in-ask')!
    const next = ask.nextElementSibling!
    expect(next.classList.contains('in-read-failed')).toBe(true)
    for (const env of BOTH) expect(marginTop(next, env)).toBe('12px')
  })
})

describe('V127: a step\'s runner facts are text, so a dot stays with its word', () => {
  it('the facts line is not a flex row at either width', async () => {
    const c = await workflowForm()
    const facts = c.querySelector<HTMLElement>('.wfb-step > .wfb-cost')!
    for (const env of BOTH) expect(painted(facts, 'display', env)).toBe('block')
    // The no-break space before each dot is still there to hold it.
    const seps = [...facts.childNodes].filter((n) => n.nodeType === Node.TEXT_NODE).map((n) => n.textContent ?? '')
    for (const s of seps) expect(s).toBe(' · ')
  })
})

describe('V174 / V254: the workflow outcome', () => {
  it('V174: "accepted as" is one value on an unwrapping row, at the card\'s size, and the two lists do not touch', async () => {
    const c = await workflowForm()
    answerWrite('/v1/workflows', { status: 201, body: {
      workflow: { workflow_id: 'wf_vqa2_2', steps: [{ step_id: 'mock-1' }], created_at: '2026-10-16T00:00:00Z' },
      dispatch: { strategy: 'direct-pr', carrier: 'checkpoints', integrator_step_id: null },
    } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit this workflow' }))
    await within(panel(c)).findByRole('button', { name: 'Submit another' }, WAIT)
    const out = panel(c).querySelector<HTMLElement>('.sbf-outcome')!
    const fact = [...out.querySelectorAll('.ctl-fact')].find((li) => visible(li.querySelector('b')) === 'accepted as')!
    const parts = [...fact.childNodes].filter((n) => !(n instanceof HTMLElement && n.tagName === 'B'))
    expect(parts, 'the value is not one element').toHaveLength(1)
    expect((parts[0]!.textContent ?? '')).toBe('direct-pr / checkpoints')
    for (const env of BOTH) {
      expect(painted(fact, 'flex-wrap', env)).toBe('nowrap')
      expect(painted(fact, 'font-size', env)).toBe('var(--t-meta)')
      expect(painted(fact.querySelector('code')!, 'font-size', env)).toBe('var(--t-micro)')
    }
    const lists = [...out.querySelectorAll('.state > .ctl-facts')]
    expect(lists).toHaveLength(2)
    for (const env of BOTH) expect(marginTop(lists[1]!, env)).toBe('8px')
  })

  it('V254: a 422\'s last fact is 8px from "Nothing was created…"', async () => {
    const c = await workflowForm()
    answerWrite('/v1/workflows', { status: 422, body: {
      code: 'validation_failed', message: 'request body failed validation',
      detail: { errors: [{ loc: ['body', 'priority'], msg: 'Input should be less than or equal to 100' }] },
    } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit this workflow' }))
    await waitFor(() => expect(panel(c).querySelector('.sbf-outcome .ctl-facts')).not.toBeNull(), WAIT)
    const facts = panel(c).querySelector('.sbf-outcome .state > .ctl-facts')!
    const next = facts.nextElementSibling!
    expect(visible(next)).toMatch(/^Nothing was created/)
    for (const env of BOTH) expect(marginTop(next, env)).toBe('8px')
  })
})

describe('V250: the Created card', () => {
  it('its id is 8px from what is above and below it and splits evenly; the carrier and its mark are one value', async () => {
    const c = await taskForm()
    answerWrite('/v1/tasks', { status: 201, body: {
      task: { id: 'task_3f1e6c0a9b2d4e7f8a1c5b6d9e0f12d4', state: 'READY', dispatch: { strategy: 'collect', carrier: 'checkpoints', role: null, integrates: [] } },
      scheduler_woken: true,
    } })
    fireEvent.click(submitButton())
    await within(panel(c)).findByRole('button', { name: 'Submit another' }, WAIT)
    const id = panel(c).querySelector<HTMLElement>('.sbf-outcome p.sbf-id')!
    const below = id.nextElementSibling!
    expect(below.classList.contains('ctl-facts')).toBe(true)
    for (const env of BOTH) {
      expect(marginTop(id, env)).toBe('8px')
      expect(marginTop(below, env)).toBe('8px')
      expect(painted(id, 'word-break', env)).toBe('break-all')
      expect(painted(id, 'text-wrap', env)).toBe('balance')
    }
    const carrier = [...below.querySelectorAll('.ctl-fact')].find((li) => visible(li.querySelector('b')) === 'carrier')!
    const parts = [...carrier.children].filter((e) => e.tagName !== 'B')
    expect(parts).toHaveLength(1)
    expect(visible(parts[0]!)).toBe('checkpoints not acted on')
  })
})

describe('V252: the dot after a long reference follows its last line', () => {
  it('the reference item is inline text, and its dot is held by a no-break space', () => {
    const long = 'an-organisation-with-a-long-name/a-repository-with-an-even-longer-name#123456'
    const read = { ...PREVIEW, issue: { ...PREVIEW.issue, ref: long, owner: 'an-organisation-with-a-long-name', repo: 'a-repository-with-an-even-longer-name', number: 123456 } }
    const { container } = render(<IssuePreviewCard read={read as never} at={Date.now()} closedOk={false} onPlanAnyway={() => {}} />)
    const item = container.querySelector<HTMLElement>('.in-meta > .in-meta-i')!
    expect(visible(item.querySelector('a'))).toBe(long)
    for (const env of BOTH) expect(painted(item, 'display', env)).toBe('block')
    expect(item.lastChild?.textContent).toBe(' ·')
  })
})

describe('V253: one description size in the workflow form\'s two radio groups', () => {
  it('"If a step fails" describes at the merge options\' size', async () => {
    const c = await workflowForm()
    const say = c.querySelector('.dsp-option-say')!
    const count = c.querySelector('.dsp-count')!
    for (const env of BOTH) {
      expect(painted(say, 'font-size', env)).toBe('var(--t-meta)')
      expect(painted(count, ['font-size', 'font'], env)).toContain('var(--t-meta)')
    }
  })
})

describe('V257: the pools in "Room for 0 more" are DM Mono', () => {
  it('each pool named in the room box is the mono face at the meta step', async () => {
    const c = await taskForm('claude-code')
    const room = c.querySelector<HTMLElement>('.sbf-room')!
    expect(visible(room)).toMatch(/Room for 0 more/)
    const codes = [...room.querySelectorAll('code')]
    expect(codes.length).toBeGreaterThan(0)
    for (const code of codes) {
      for (const env of BOTH) {
        expect(familyOf(code, env)).toBe('mono')
        expect(painted(code, ['font-size', 'font'], env)).toBe('var(--t-meta)')
      }
    }
  })
})
