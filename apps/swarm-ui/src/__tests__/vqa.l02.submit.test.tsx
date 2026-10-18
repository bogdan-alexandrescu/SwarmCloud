// VISUAL QA LANE VQA-L02 (part of #1038): THE THREE SUBMIT FORMS.
//
// One case per finding, each asserting the property the finding is about --
// where the outcome is drawn, what the button is after a 201, whether a step
// is marked, what a grid track or a `white-space` resolves to in the real
// sheets -- rather than the markup's shape:
//
//   V002  the outcome (a 201, a failure) is drawn inside the send panel, under
//         the button, on the task and workflow forms, as the issue form does;
//   V048  after a 201 the button is "Submit another" until the form changes;
//   V049  the outcome's parts are 12px apart, never flush;
//   V051  a workflow 422 is read: the step it names is marked, no JSON dump;
//   V052  direct-pr with no repository is "Not ready to send", button off;
//   V139  the workflow panel names the repository whenever the strategy pushes;
//   V130  a long repository address is one cut line, whole in its title;
//   V131  "N steps not ready" never opens a line on a comma;
//   V127  a step's runner facts never open a line on a separator;
//   V125  runner names are drawn at a DM Mono weight main.tsx bundles;
//   V128  "Other runners (…)" has a disclosure marker;
//   V003  the issue preview's track is the card's width, its text wraps;
//   V132  an invalid fix-rounds field has the warn edge, and nothing says
//         "not sent" before a click;
//   V133  "Show the whole body" only when the cut hides something;
//   V137  "+ Add setting" brings its opened list into view.
//
// MUTATIONS, each of which turns a case red: put `<Outcome>` back at the top of
// `.sbf-build`; drop `justSent` from either button; drop `(pushes && repo ===
// '')` from `blocked`; put `<pre>{JSON.stringify(error.detail)}</pre>` back;
// set `.in-preview` back to an `auto` track; set the runner name back to 600;
// drop the `::before` marker on `.sbf-runners-more > summary`.
//
// Nothing here is credential-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import { AddSetting, SubmitScreen } from '../Submit'
import { SubmitWorkflowScreen, refusalOf } from '../SubmitWorkflow'
import { IssuePreviewCard } from '../IssueSubmit'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import MAIN from '../main.tsx?raw'

const WAIT = { timeout: 4000 }
const PHONE: CascadeEnv = { width: 400 }
const WIDE: CascadeEnv = { width: 1440 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

const realFetch = globalThis.fetch

/** Answer the one write with `reply`; every read goes to the development fixture as before. */
function answerWrite(path: string, reply: { status: number; body: unknown }) {
  const posts: unknown[] = []
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
  globalThis.fetch = realFetch
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllEnvs()
})

async function taskForm() {
  const r = render(<SubmitScreen />)
  await screen.findByRole('button', { name: 'Submit one task' }, WAIT)
  fireEvent.click(r.container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="mock"]')!)
  return r.container
}

async function workflowForm() {
  const r = render(<SubmitWorkflowScreen />)
  await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
  return r.container
}

function pickStepRunner(container: HTMLElement, index: number, profile: string) {
  const step = container.querySelectorAll<HTMLElement>('.wfb-step')[index]!
  fireEvent.change(step.querySelector<HTMLSelectElement>('select.sb-runner-select')!, { target: { value: profile } })
}

function strategy(container: HTMLElement, s: string) {
  fireEvent.click(container.querySelector<HTMLInputElement>(`input[name="dispatch-strategy"][value="${s}"]`)!)
}

const panel = (c: HTMLElement) => c.querySelector<HTMLElement>('.sbf-send')!

describe('V002 / V048 / V049: the outcome is drawn beside the button, and a 201 is not one click from a duplicate', () => {
  it('the task form: the 201 is in the send panel, and the button is "Submit another" until the form changes', async () => {
    const c = await taskForm()
    const posts = answerWrite('/v1/tasks', { status: 201, body: { task: { id: 't_vqa_1', state: 'READY' }, scheduler_woken: true } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit one task' }))
    const another = await within(panel(c)).findByRole('button', { name: 'Submit another' }, WAIT)

    // Drawn in the panel, under the button; nothing is drawn above step 1.
    const card = panel(c).querySelector('.sbf-outcome .state[role="status"]')
    expect(visible(card)).toContain('Created at READY')
    expect(c.querySelector('.sbf-build .state'), 'the outcome is still drawn in the build column').toBeNull()
    expect(visible(panel(c).querySelector('h2'))).toBe('Sent')
    expect(screen.queryByRole('button', { name: 'Submit one task' }), 'the submit button survived the 201').toBeNull()
    expect(another.getAttribute('type')).toBe('button')

    // V049: the outcome's parts keep the steps' 12px, at both widths.
    const slot = panel(c).querySelector('.sbf-outcome')!
    expect(painted(slot, 'gap', PHONE)).toBe('12px')
    expect(painted(slot, 'gap', WIDE)).toBe('12px')

    // A change to the form is a new request, and the button says so again.
    fireEvent.change(c.querySelector<HTMLInputElement>('#dsp-repo')!, { target: { value: 'https://github.com/example-org/infra.git' } })
    expect(screen.getByRole('button', { name: 'Submit one task' })).toBeTruthy()
    expect(within(panel(c)).queryByRole('button', { name: 'Submit another' })).toBeNull()
    expect(posts).toHaveLength(1)
  })

  it('the task form: "Submit another" starts a blank form', async () => {
    const c = await taskForm()
    answerWrite('/v1/tasks', { status: 201, body: { task: { id: 't_vqa_2', state: 'READY' }, scheduler_woken: false } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit one task' }))
    fireEvent.click(await within(panel(c)).findByRole('button', { name: 'Submit another' }, WAIT))
    expect(c.querySelectorAll('input[name="runner-profile"]:checked')).toHaveLength(0)
    expect(visible(panel(c).querySelector('h2'))).toBe('Not ready to send')
    expect(c.querySelector('.sbf-outcome')).toBeNull()
  })

  it('the task form: a failed write is drawn in the panel too', async () => {
    const c = await taskForm()
    answerWrite('/v1/tasks', { status: 500, body: { code: 'internal', message: 'The write failed on purpose.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit one task' }))
    await waitFor(() => expect(visible(panel(c).querySelector('.sbf-outcome'))).toContain('The write failed on purpose.'), WAIT)
    expect(visible(panel(c).querySelector('.sbf-outcome'))).toContain('Check Agents before submitting again.')
    expect(visible(c.querySelector('.sbf-build'))).not.toContain('The write failed on purpose.')
  })

  it('the workflow form: the 201 is in the send panel, and the button is "Submit another" until the form changes', async () => {
    const c = await workflowForm()
    pickStepRunner(c, 0, 'mock')
    const posts = answerWrite('/v1/workflows', { status: 201, body: {
      workflow: { workflow_id: 'wf_vqa_1', steps: [{ step_id: 'mock-1' }], created_at: '2026-10-11T00:00:00Z' }, dispatch: null,
    } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit this workflow' }))
    await within(panel(c)).findByRole('button', { name: 'Submit another' }, WAIT)
    expect(visible(panel(c).querySelector('.sbf-outcome'))).toContain('Workflow submitted')
    expect(visible(panel(c).querySelector('.sbf-outcome'))).toContain('wf_vqa_1')
    expect(c.querySelector('.sbf-build .state')).toBeNull()
    expect(visible(panel(c).querySelector('h2'))).toBe('Sent')
    expect(screen.queryByRole('button', { name: 'Submit this workflow' })).toBeNull()

    fireEvent.change(c.querySelector<HTMLInputElement>('#wf-priority')!, { target: { value: '5' } })
    expect(screen.getByRole('button', { name: 'Submit this workflow' })).toBeTruthy()
    expect(posts).toHaveLength(1)

    // And "Submit another" from an unchanged form starts the plan over.
    fireEvent.change(c.querySelector<HTMLInputElement>('#wf-priority')!, { target: { value: '0' } })
    fireEvent.click(within(panel(c)).getByRole('button', { name: 'Submit another' }))
    expect(c.querySelector<HTMLSelectElement>('.wfb-step select.sb-runner-select')!.value).toBe('')
    expect(c.querySelector('.sbf-outcome')).toBeNull()
  })
})

describe('V051: a workflow 422 is read, and the step it names is marked', () => {
  it('a validator error on steps.0 marks that step and prints no JSON', async () => {
    const c = await workflowForm()
    pickStepRunner(c, 0, 'mock')
    answerWrite('/v1/workflows', { status: 422, body: {
      code: 'validation_failed', message: 'request body failed validation',
      detail: { errors: [{ loc: ['body', 'steps', 0, 'input'], msg: 'Input should be a valid dictionary' }, { loc: ['body', 'priority'], msg: 'too high' }] },
    } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit this workflow' }))
    await waitFor(() => expect(panel(c).querySelector('.sbf-outcome')).not.toBeNull(), WAIT)
    const out = panel(c).querySelector<HTMLElement>('.sbf-outcome')!
    expect(out.querySelector('pre'), 'the detail is dumped as JSON').toBeNull()
    expect(visible(out)).not.toMatch(/[{}"]/)
    expect(visible(out.querySelector('.sbf-refused'))).toBe('mock-1 — input: Input should be a valid dictionary')
    const fact = [...out.querySelectorAll('.ctl-fact')].find((li) => visible(li.querySelector('b')) === 'priority')
    expect(visible(fact?.querySelector('span') ?? null)).toBe('too high')
    const step = c.querySelector<HTMLElement>('.wfb-step')!
    expect(step.classList.contains('is-bad')).toBe(true)
    expect(visible(step)).toContain('The API refused this step: input: Input should be a valid dictionary')
  })

  it('a service refusal naming steps by id marks each of them; what names no step is a fact', () => {
    const error = { kind: 'invalid' as const, httpStatus: 422, code: 'invalid_dag', message: 'the plan has a cycle',
      detail: { cycle: ['a-1', 'b-1'], filename: 'notes.md', accepted: { min: 1, max: 5 } } }
    const r = refusalOf(error, ['a-1', 'b-1', 'c-1'])
    expect(r.byStep).toEqual({ 'a-1': 'the plan has a cycle', 'b-1': 'the plan has a cycle' })
    expect(r.facts).toEqual([{ key: 'filename', value: 'notes.md' }, { key: 'accepted', value: 'min 1; max 5' }])
  })
})

describe('V052 / V139 / V130: a push with nowhere to push, and the repository it pushes to', () => {
  it('V052: the task form with direct-pr and no repository is not ready, and its button is off', async () => {
    const c = await taskForm()
    strategy(c, 'direct-pr')
    expect(visible(panel(c).querySelector('h2'))).toBe('Not ready to send')
    expect((screen.getByRole('button', { name: 'Submit one task' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(c.querySelector<HTMLInputElement>('#dsp-repo')!, { target: { value: 'https://github.com/example-org/infra.git' } })
    expect(visible(panel(c).querySelector('h2'))).toBe('Ready to send')
    expect((screen.getByRole('button', { name: 'Submit one task' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('V139: the workflow panel names the repository whenever the strategy pushes, and holds the button without one', async () => {
    const c = await workflowForm()
    pickStepRunner(c, 0, 'mock')
    const fact = () => [...panel(c).querySelectorAll('.ctl-fact')].find((li) => visible(li.querySelector('b')) === 'repository') ?? null
    expect(fact(), 'collect pushes nothing, yet a repository is listed').toBeNull()
    strategy(c, 'direct-pr')
    expect(visible(fact()?.querySelector('i') ?? null)).toBe('none — nowhere to push')
    expect((screen.getByRole('button', { name: 'Submit this workflow' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(c.querySelector<HTMLInputElement>('#dsp-repo')!, { target: { value: 'https://github.com/example-org/infra.git' } })
    expect(visible(fact()?.querySelector('code') ?? null)).toBe('example-org/infra')
    expect((screen.getByRole('button', { name: 'Submit this workflow' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('V130: an address the slug cannot shorten is one cut line, whole in its title', async () => {
    const c = await taskForm()
    strategy(c, 'direct-pr')
    const long = 'https://git.internal.example.com/platform/teams/infrastructure/very-long-repository-name-for-the-panel'
    fireEvent.change(c.querySelector<HTMLInputElement>('#dsp-repo')!, { target: { value: long } })
    const code = panel(c).querySelector<HTMLElement>('code.sbf-cut')!
    expect(code.getAttribute('title')).toBe(long)
    for (const env of [PHONE, WIDE]) {
      expect(painted(code, 'white-space', env)).toBe('nowrap')
      expect(painted(code, 'text-overflow', env)).toBe('ellipsis')
      expect(painted(code, 'overflow', env)).toBe('hidden')
    }
  })
})

describe('V131 / V127: no line opens on a comma or a separator', () => {
  it('V131: each not-ready name carries its own comma, in one unbroken run', async () => {
    const c = await workflowForm()
    fireEvent.click(c.querySelector<HTMLButtonElement>('button.wfb-add.is-stage')!)
    fireEvent.click(c.querySelector<HTMLButtonElement>('button.wfb-add.is-stage')!)
    const say = [...panel(c).querySelectorAll('.warn-text')].find((p) => visible(p).includes('not ready'))!
    expect(visible(say)).toBe('3 steps not ready: step-1, step-2, step-3')
    const items = [...say.querySelectorAll('.sbf-gap-i')]
    expect(items.map(visible)).toEqual(['step-1,', 'step-2,', 'step-3'])
    // No text node between the names opens with a comma.
    for (const n of say.childNodes) if (n.nodeType === Node.TEXT_NODE) expect(n.textContent ?? '').not.toMatch(/^\s*,/)
    expect(painted(items[0]!, 'white-space', PHONE)).toBe('nowrap')
  })

  it('V127: a step\'s runner facts hold each separator to the word before it', async () => {
    const c = await workflowForm()
    pickStepRunner(c, 0, 'mock')
    const facts = c.querySelector<HTMLElement>('.wfb-step .wfb-cost')!
    const seps = [...facts.childNodes].filter((n) => n.nodeType === Node.TEXT_NODE).map((n) => n.textContent ?? '')
    expect(seps.length).toBe(2)
    for (const s of seps) expect(s).toBe(' · ')
    expect(painted(facts.querySelector('.sb-runner-size')!, 'white-space', PHONE)).toBe('nowrap')
  })
})

describe('V125 / V128: the runner list', () => {
  it('V125: a runner name is drawn at a DM Mono weight main.tsx bundles', async () => {
    const c = await taskForm()
    const name = c.querySelector('.sbf-runners.is-cards .sbf-runner-name')!
    const font = painted(name, 'font', WIDE) ?? ''
    const weight = /^(\d{3})\s/.exec(font)?.[1]
    expect(weight, `no weight in ${font}`).toBeDefined()
    expect(MAIN).toContain(`@fontsource/dm-mono/${weight}.css`)
  })

  it('V128: "Other runners (…)" draws a marker that turns on open', () => {
    document.body.innerHTML =
      '<div class="sbf-picker"><details class="sbf-runners-more"><summary>Other runners (4 unavailable)</summary><ul></ul></details></div>'
    const summary = document.querySelector('summary')!
    expect(painted(summary, 'display', WIDE)).toBe('inline-flex')
    expect(painted(summary, 'content', WIDE, 'before')).toBe("'\\25B8'")
    document.querySelector('details')!.setAttribute('open', '')
    expect(painted(summary, 'content', WIDE, 'before')).toBe("'\\25BE'")
  })
})

const PREVIEW = {
  issue: {
    ref: 'an-organisation-with-a-long-name/a-repository-with-an-even-longer-name#123456',
    owner: 'an-organisation-with-a-long-name', repo: 'a-repository-with-an-even-longer-name', number: 123456,
    url: 'https://github.com/an-organisation-with-a-long-name/a-repository-with-an-even-longer-name/issues/123456',
    repository_url: 'https://github.com/an-organisation-with-a-long-name/a-repository-with-an-even-longer-name',
    title: 'apps/swarm-ui/src/components/AVeryLongComponentNameWithoutAnyBreakOpportunity.tsx', body: 'short',
    body_truncated: false, body_redacted: false, labels: ['a-label-with-no-break-opportunity-at-all'], state: 'closed', comments: 2,
  },
  tenant_id: 'eng',
}

describe('V003 / V133: the issue as read', () => {
  it('V003: one track of the card\'s width, and its long words wrap', () => {
    const { container } = render(<IssuePreviewCard read={PREVIEW as never} at={Date.now()} closedOk={false} onPlanAnyway={() => {}} />)
    const card = container.querySelector('.in-preview')!
    for (const env of [PHONE, WIDE]) {
      expect(painted(card, 'grid-template-columns', env)).toBe('minmax(0, 1fr)')
      expect(painted(card.querySelector('h3')!, 'overflow-wrap', env)).toBe('anywhere')
      expect(painted(card.querySelector('.in-meta')!, 'overflow-wrap', env)).toBe('anywhere')
      expect(painted(card.querySelector('.c-tag')!, 'white-space', env)).toBe('normal')
    }
  })

  it('V133: a body the cut does not hide offers no "Show the whole body"', () => {
    render(<IssuePreviewCard read={PREVIEW as never} at={Date.now()} closedOk={false} onPlanAnyway={() => {}} />)
    expect(screen.queryByRole('button', { name: /Show the whole body/ })).toBeNull()
  })

  it('V133: a body the cut hides offers it, and it closes again', () => {
    vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(function (this: HTMLElement) {
      return this.classList.contains('in-body') ? 600 : 0
    })
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(function (this: HTMLElement) {
      return this.classList.contains('in-body') ? 120 : 0
    })
    const long = { ...PREVIEW, issue: { ...PREVIEW.issue, body: 'line\n\n'.repeat(80) } }
    render(<IssuePreviewCard read={long as never} at={Date.now()} closedOk={false} onPlanAnyway={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: /Show the whole body/ }))
    expect(screen.getByRole('button', { name: 'Show less of the body' })).toBeTruthy()
  })
})

// The issue screen reads through the live client, as submit.workspace.refusal.test.tsx does.
const admission = { units: 1, headroom: 4, basis: 'measured', blockers: [], binding: [], counterfactual: [], complete: true, unread: [], uncapped: [] }
const pool = (name: string) => ({
  name, hard_limit: 10, adaptive_target: null, quota_derived_limit: null, effective_limit: 10,
  active: 2, available: 8, enabled: true, updated_at: '2026-10-02T10:00:00Z',
})
const CAPACITY = {
  pools: [pool('global'), pool('tenant:eng'), pool('resource:standard')],
  runner_profiles: {
    'claude-code': { resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: 'anthropic', units: 1, pools: ['global', 'tenant:eng', 'resource:standard'], admission },
  },
  tenant_id: 'eng',
  generated_at: '2026-10-02T10:00:00Z',
}

describe('V132: an invalid fix-round count', () => {
  it('has the warn edge, and nothing says "not sent" before a click', async () => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const reply = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: JSON_HEADERS })
      if (url.startsWith('/v1/capacity')) return reply(200, CAPACITY)
      if (url.startsWith('/v1/providers')) return reply(200, { tenant_id: 'eng', providers: [], generated_at: '2026-10-02T10:00:00Z' })
      return reply(404, { code: 'not_found', message: `no stub for ${url}` })
    }) as unknown as typeof fetch
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { IssueSubmitScreen } = await import('../IssueSubmit')
    const { container } = render(<IssueSubmitScreen go={vi.fn()} />)
    const field = await screen.findByLabelText('Fix rounds when checks go red', {}, WAIT)
    fireEvent.change(field, { target: { value: '9' } })
    expect(field.getAttribute('aria-invalid')).toBe('true')
    expect(field.getAttribute('aria-describedby')).toBe('in-fix-rounds-say')
    for (const env of [PHONE, WIDE]) {
      expect(painted(field, 'border-color', env)).toBe('var(--warn)')
      expect(painted(field, 'box-shadow', env)).toBe('inset 0 0 0 1px var(--warn)')
    }
    expect(visible(container)).not.toMatch(/not sent/i)
    expect(visible(container.querySelector('#in-fix-rounds-say'))).toBe('Must be a whole number from 1 to 5.')
  })
})

describe('V137: "+ Add setting" brings its list into view', () => {
  it('scrolls the opened list to the nearest edge, clear of the dock', () => {
    const into = vi.fn()
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { value: into, configurable: true, writable: true })
    try {
      render(<AddSetting idPrefix="t" offers={[{ name: 'prompt', note: 'what to do' } as never]} onAdd={() => {}} onOwn={() => {}} />)
      fireEvent.click(screen.getByRole('button', { name: /Add setting/ }))
      const list = document.getElementById('t-add-setting')!
      expect(into).toHaveBeenCalledWith({ block: 'nearest' })
      expect(into.mock.contexts.at(-1)).toBe(list)
      expect(painted(list, 'scroll-margin-bottom', PHONE)).toBe('12px')
    } finally {
      delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView
    }
  })
})
