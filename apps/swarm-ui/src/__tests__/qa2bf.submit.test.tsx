// QA2B-F (part of #2694, the console visual QA pass 2 epic, and #2391): the
// pass-2 fix lanes' patches whose target is Submit.tsx, SubmitWorkflow.tsx,
// Dispatch.tsx and styles.css, landed here.
//
//   runs2-01  Start one agent in the fixture build answered 404, and a 4xx
//             refusal's card also said "Check Agents before submitting again".
//   runs2-02  Submit a workflow in the fixture build answered 404.
//   runs2-30  each merge option printed its raw key in mono beside its label:
//             "Collect collect default".

import { createElement } from 'react'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

// The fixture build's answer to the submit is on by default, as the dev build
// runs; `answerWrite` turns it off for a case that hands the submit a real
// response.
const fixtures = vi.hoisted(() => ({ on: true }))
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return Object.defineProperty({ ...actual }, 'USE_FIXTURES', { get: () => fixtures.on, enumerable: true })
})

import { DispatchChoice } from '../Dispatch'
import { SubmitScreen, postTask } from '../Submit'
import { postWorkflow } from '../SubmitWorkflow'
import type { CascadeEnv } from './cssgate'
import { familyOf } from './faces'
import { painted } from './marks'

const WAIT = { timeout: 4000 }
const BOTH: CascadeEnv[] = [{ width: 400 }, { width: 1440 }]
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
const WARN = 'Check Agents before submitting again.'

const realFetch = globalThis.fetch

/** Answer the one write with `reply`; every read goes to the development fixture as before. */
function answerWrite(path: string, reply: { status: number; body: unknown }) {
  fixtures.on = false
  const prior = globalThis.fetch
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if ((init?.method ?? 'GET') === 'POST' && String(input).startsWith(path)) {
      return new Response(JSON.stringify(reply.body), { status: reply.status, headers: JSON_HEADERS })
    }
    return prior(input, init)
  }) as unknown as typeof fetch
}

afterEach(() => {
  fixtures.on = true
  globalThis.fetch = realFetch
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

async function taskForm() {
  const r = render(<SubmitScreen />)
  await screen.findByRole('button', { name: 'Start one agent' }, WAIT)
  fireEvent.click(r.container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="mock"]')!)
  return r.container
}

const outcome = (c: HTMLElement) => c.querySelector<HTMLElement>('.sbf-send .sbf-outcome')

describe('runs2-01 and runs2-02: the fixture build answers the submit as the 201 does', () => {
  it('postTask creates the agent without a request', async () => {
    const f = vi.fn()
    globalThis.fetch = f as unknown as typeof fetch
    const r = await postTask({ runner_profile: 'claude-code', input: { prompt: 'Draw the cost column' } })
    // MUTATION: drop the USE_FIXTURES branch in postTask -> it fetches, and jsdom's 404 is 'failed'.
    expect(r.kind).toBe('created')
    expect(f).not.toHaveBeenCalled()
  })

  it('postWorkflow creates the workflow without a request', async () => {
    const f = vi.fn()
    globalThis.fetch = f as unknown as typeof fetch
    const r = await postWorkflow({ steps: [{ step_id: 'generic', runner_profile: 'generic', depends_on: [] }], priority: 5 })
    // MUTATION: drop the USE_FIXTURES branch in postWorkflow -> it fetches and is not 'created'.
    expect(r.kind).toBe('created')
    expect(f).not.toHaveBeenCalled()
  })

  it('the task form lands on what it created', async () => {
    const c = await taskForm()
    fireEvent.click(screen.getByRole('button', { name: 'Start one agent' }))
    await within(c.querySelector<HTMLElement>('.sbf-send')!).findByRole('button', { name: 'Submit another' }, WAIT)
    expect(visible(outcome(c))).toContain('Created at')
    expect(visible(outcome(c))).not.toContain('Not found')
  })
})

describe('runs2-01: a definite refusal does not also say the agent may exist', () => {
  for (const reply of [
    { status: 404, body: { code: 'not_found', message: 'This deployment has no route that takes a task.' } },
    { status: 422, body: { code: 'refused', message: 'The tenant may not submit this today.' } },
  ]) {
    it(`a ${reply.status} draws its refusal and no "${WARN}"`, async () => {
      const c = await taskForm()
      answerWrite('/v1/tasks', reply)
      fireEvent.click(screen.getByRole('button', { name: 'Start one agent' }))
      await waitFor(() => expect(visible(outcome(c))).toContain(reply.body.message), WAIT)
      // MUTATION: drop `!writeRefused(outcome.error) &&` -> the refusal also says the agent may exist.
      expect(visible(outcome(c))).not.toContain(WARN)
    })
  }

  it(`a 500 still says "${WARN}": that write may have landed`, async () => {
    const c = await taskForm()
    answerWrite('/v1/tasks', { status: 500, body: { code: 'internal', message: 'The write failed on purpose.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start one agent' }))
    await waitFor(() => expect(visible(outcome(c))).toContain('The write failed on purpose.'), WAIT)
    expect(visible(outcome(c))).toContain(WARN)
  })
})

describe('runs2-30: a merge option is named by its label alone', () => {
  function options(scale: 'task' | 'workflow') {
    const { container } = render(
      createElement(DispatchChoice, {
        draft: { strategy: 'collect', carrier: 'checkpoints', repositoryUrl: '' },
        onChange: () => {},
        steps: scale === 'task' ? 1 : 2,
        scale,
        terminals: ['report'],
      }),
    )
    return [...container.querySelectorAll<HTMLElement>('.dsp-option-head')]
  }

  for (const scale of ['task', 'workflow'] as const) {
    it(`the ${scale} form's options print no API key beside their label`, () => {
      const heads = options(scale)
      expect(heads.length).toBeGreaterThan(1)
      for (const head of heads) {
        // MUTATION: put `<code className="dsp-code">{s}</code>` back -> each head carries its key.
        expect(head.querySelector('code')).toBeNull()
        expect(visible(head)).not.toMatch(/\b(collect|direct-pr|integrate)\b/)
      }
      // The head is the label and, on Collect, the "default" tag: nothing between.
      const collect = heads.find((h) => h.querySelector('.dsp-default') !== null)!
      expect([...collect.children].map((el) => el.className || el.tagName)).toEqual(['B', 'dsp-default'])
    })
  }

  it('"default" is set in the sans face, as words, at both widths', () => {
    const tag = options('task').map((h) => h.querySelector('.dsp-default')).find((t) => t !== null)!
    // MUTATION: `.dsp-default` back on `var(--mono)` -> 'mono'.
    for (const env of BOTH) expect(familyOf(tag, env)).toBe('sans')
    expect(painted(tag, ['font-size', 'font'], BOTH[1]!)).toMatch(/^var\(--t-meta\)/)
  })
})
