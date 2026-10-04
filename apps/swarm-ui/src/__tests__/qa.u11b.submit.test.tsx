/**
 * BROWSER QA U11b (owner, 2026-10-04; live console at main 69416faf, 1440x900
 * light and dark and 390px): Submit › From a GitHub issue, and the task
 * form's "+ Add setting".
 *
 *   N2   After Read at 1440 the summary card spilled past the viewport: the
 *        326px "Plan this issue" button ended at x1447 in a 300px card. The
 *        card's grid track grew to the unbroken mono issue reference. The
 *        track is the card's width, each value is cut with its title, and the
 *        button is the card's width, never more.
 *   N13  With Plan approval = Auto the summary said "plan: runs straight on"
 *        AND "lands as PLANNING, then PLANNED" AND "nothing else runs until
 *        the plan is approved". The summary follows the choice.
 *   N14  Fix rounds = 9 read "not a number from 1 to 5": it is "a whole
 *        number from 1 to 5".
 *   N18  The preview printed an issue title's backticks.
 *   N20  The preview's meta line started with "·" where it wrapped, and said
 *        "11:45 PM" while the console uses 24-hour time everywhere.
 *   D34  "+ Add setting": with focus outside the control (the field a click
 *        left it in), Escape did not close the list.
 *
 * MUTATIONS: drop the send card's track rule or the value's cut, say "runs
 * straight on" beside "until the plan is approved" again, drop "whole", put
 * a separator back at the start of a meta item, use the locale's 12-hour
 * clock, or handle Escape on the control alone -- each turns a case red.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WIDE: CascadeEnv = { width: 1440 }
const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

const admission = { units: 1, headroom: 4, basis: 'measured', blockers: [], binding: [], counterfactual: [], complete: true, unread: [], uncapped: [] }
const pool = (name: string) => ({ name, hard_limit: 10, adaptive_target: null, quota_derived_limit: null, effective_limit: 10, active: 2, available: 8, enabled: true, updated_at: '2026-10-02T10:00:00Z' })
const CAPACITY = {
  pools: [pool('global'), pool('tenant:eng')],
  runner_profiles: {
    'claude-code': { resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: 'anthropic', units: 1, pools: ['global', 'tenant:eng'], admission },
  },
  tenant_id: 'eng',
  generated_at: '2026-10-02T10:00:00Z',
}
const REF = 'bogdan-alexandrescu/SwarmCloud-with-a-much-longer-repository-name#454'
const PREVIEW = {
  issue: {
    ref: REF, owner: 'bogdan-alexandrescu', repo: 'SwarmCloud-with-a-much-longer-repository-name', number: 454,
    url: 'https://github.com/bogdan-alexandrescu/SwarmCloud/issues/454',
    repository_url: 'https://github.com/bogdan-alexandrescu/SwarmCloud',
    title: 'Wire `Closes #N` into the PR body from `apps/swarm-api`',
    body: 'Body.', body_truncated: false, body_redacted: false, labels: [], state: 'open', comments: 2,
  },
  tenant_id: 'eng',
}

function serve() {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    const reply = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: JSON_HEADERS })
    if (url.startsWith('/v1/capacity')) return reply(200, CAPACITY)
    if (url.startsWith('/v1/providers')) return reply(200, { tenant_id: 'eng', providers: [{ provider: 'anthropic', credential_registered: true }], generated_at: '2026-10-02T10:00:00Z' })
    if (url.startsWith('/v1/issues/preview')) return reply(200, PREVIEW)
    return reply(404, { code: 'not_found', message: url })
  }) as unknown as typeof fetch
}

async function mountRead() {
  serve()
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { IssueSubmitScreen } = await import('../IssueSubmit')
  const utils = render(<IssueSubmitScreen go={vi.fn()} />)
  await screen.findByRole('radiogroup', { name: 'runner' }, WAIT)
  fireEvent.change(screen.getByLabelText('Issue reference'), { target: { value: REF } })
  fireEvent.click(screen.getByRole('button', { name: 'Read' }))
  await waitFor(() => expect(utils.container.querySelector('.in-preview')).not.toBeNull(), WAIT)
  return utils
}

afterEach(() => {
  vi.unstubAllEnvs()
  document.body.innerHTML = ''
})

const fact = (send: Element, key: string) => [...send.querySelectorAll('.ctl-fact')].find((li) => visible(li.querySelector('b')) === key)!

describe('N2: the send card never runs past its column', () => {
  it('sizes the card\'s track to the card, cuts the reference with its title and holds the button to the card', async () => {
    const { container } = await mountRead()
    const send = container.querySelector('.sbf-send.in-send')!
    expect(painted(send, 'grid-template-columns', WIDE)).toBe('minmax(0, 1fr)')
    const issue = fact(send, 'issue')
    expect(painted(issue, 'max-width', WIDE)).toBe('100%')
    const value = issue.querySelector(':scope > span')!
    expect(painted(value, 'min-width', WIDE)).toBe('0')
    expect(painted(value, 'overflow', WIDE)).toBe('hidden')
    const code = value.querySelector('code')!
    expect(code.getAttribute('title')).toBe(REF)
    expect(painted(code, 'text-overflow', WIDE)).toBe('ellipsis')
    const button = screen.getByRole('button', { name: 'Plan this issue' })
    expect(painted(button, 'width', WIDE)).toBe('100%')
    expect(painted(button, 'max-width', WIDE)).toBe('100%')
    expect(painted(button, 'box-sizing', WIDE) ?? painted(document.documentElement, 'box-sizing', WIDE) ?? 'border-box').toBe('border-box')
  })
})

describe('N13: the summary follows Plan approval', () => {
  it('says what Required and what Auto do, and never both', async () => {
    const { container } = await mountRead()
    const send = container.querySelector('.sbf-send.in-send')!
    expect(visible(fact(send, 'plan'))).toMatch(/waits for approval/)
    expect(visible(fact(send, 'lands as'))).toMatch(/PLANNED/)
    expect(visible(send)).toMatch(/until the plan is approved/)
    fireEvent.click(screen.getByRole('radio', { name: 'Auto' }))
    const auto = visible(send)
    expect(visible(fact(send, 'plan'))).toMatch(/approved as soon as it is written/)
    expect(auto).not.toMatch(/until the plan is approved/)
    expect(auto).not.toMatch(/waits for approval/)
    expect(visible(fact(send, 'lands as'))).toMatch(/then RUNNING/)
    // Planning leases capacity (the planner is an ordinary task); only PLANNED is free.
    expect(visible(fact(send, 'lands as'))).not.toMatch(/planning holds no capacity/i)
    expect(visible(fact(send, 'lands as'))).toMatch(/only the planner and the workflow's steps hold capacity/)
  })
})

describe('N14: fix rounds names the rule', () => {
  it('says "a whole number from 1 to 5" for 9', async () => {
    const { container } = await mountRead()
    fireEvent.change(container.querySelector('#in-fix-rounds')!, { target: { value: '9' } })
    const send = container.querySelector('.sbf-send.in-send')!
    const said = visible(fact(send, 'fix rounds'))
    expect(said).toMatch(/a whole number from 1 to 5$/)
    expect(said).not.toMatch(/not a number/)
  })
})

describe('N18 / N20: the preview\'s title and meta line', () => {
  it('draws the title\'s inline code as code', async () => {
    const { container } = await mountRead()
    const h3 = container.querySelector('.in-preview > h3')!
    expect(visible(h3)).not.toContain('`')
    expect([...h3.querySelectorAll('code')].map((c) => c.textContent)).toEqual(['Closes #N', 'apps/swarm-api'])
  })

  it('never opens a line on a separator, and prints the read time on the 24-hour clock', async () => {
    const { container } = await mountRead()
    const meta = container.querySelector('.in-meta')!
    for (const item of meta.children) {
      expect(visible(item).startsWith('·'), visible(item)).toBe(false)
    }
    expect(visible(meta)).not.toMatch(/\b(AM|PM)\b/i)
    expect(visible(meta)).toMatch(/read \d{2}:\d{2}:\d{2} /)
  })
})

describe('D34: Escape closes "+ Add setting" from anywhere', () => {
  it('closes the list when Escape is pressed in a field outside it', async () => {
    const { AddSetting } = await import('../Submit')
    const { container } = render(
      <div>
        <input aria-label="elsewhere" />
        <AddSetting idPrefix="t" offers={[{ name: 'model', note: 'the model', kind: 'text' } as never]} onAdd={() => {}} onOwn={() => {}} />
      </div>,
    )
    fireEvent.click(screen.getByRole('button', { name: /Add setting/ }))
    expect(container.querySelector('.sbf-addset-list')).not.toBeNull()
    const input = screen.getByLabelText('elsewhere')
    input.focus()
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(container.querySelector('.sbf-addset-list'), 'Escape outside the control left the list open').toBeNull()
  })
})
