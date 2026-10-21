/**
 * LANE QAFIX3-AGENT-HEAD (part of #1680, visual QA run 3,
 * wf_d598fa7bffdb41fc9d76, 2026-10-21, the fixture build of main at 51d99c53).
 * The agent pane's head and its tab strip:
 *
 *   task-010  after a 48px scroll the trail bar (`.ctl-bar`, sticky at the
 *             scroller's top, 48px, z-index 20) covered Copy link, Stop and
 *             Close: the column stuck 16px down, under the bar.
 *   ff-007    the `account not read` fact said nothing about what was not
 *             read or why.
 *   ff-009    `Changes —` and `Artifacts —`: a bare dash for a count not known.
 *   axe       the head was a `<header>`, a second banner landmark beside the
 *             trail bar's (landmark-no-duplicate-banner, landmark-unique), and
 *             one inside the phone's region (landmark-banner-is-top-level).
 */
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { task as runTask } from './runfixture'
import type { Task } from '../types'

const api = vi.hoisted(() => ({
  loadResourceClasses: vi.fn(() => new Promise(() => {})),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgHead, AgHeadMeta } = await import('../agent-split/head')
const { paneTabs } = await import('../agent-split/tabs')
const { Tabs } = await import('../components')

const WIDE: CascadeEnv = { width: 1440 }
const ID = 'task_0123456789abcdef0123'

function row(over: Partial<Task> = {}): Task {
  return runTask({ id: ID, state: 'RUNNING', completed_at: null, ...over })
}

const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
})

describe('task-010: the agent column sticks under the trail bar, not under the scroller’s top', () => {
  it('sticks the bar’s height plus 16px down and is that much shorter', () => {
    const host = document.createElement('div')
    host.innerHTML =
      '<div class="ctl-frame has-ctl-bar"><div class="sk-main"><div class="ctl-scroll">' +
      '<header class="ctl-bar"></header><div class="app has-inspector"><main class="work"></main>' +
      '<div class="drawer ctl-drawer ag-split"></div></div></div></div></div>'
    document.body.appendChild(host)
    hosts.push(host)
    const bar = host.querySelector('.ctl-bar')!
    const col = host.querySelector('.ag-split')!
    // The bar the column has to clear: sticky at the scroller's top, over the agent.
    expect(painted(bar, 'position', WIDE)).toBe('sticky')
    expect(painted(bar, 'top', WIDE)).toBe('0')
    // The bar's height, read off the sheet: `--ctl-bar-h` under `.has-ctl-bar`.
    const barH = painted(bar, 'height', WIDE)
    expect(barH).toBe('48px')
    const top = (painted(col, 'top', WIDE) ?? '').replace(/\s+/g, ' ')
    // MUTATION: set the column's `top` back to `16px` and the bar draws over Copy link, Stop and ✕ after a 48px scroll.
    expect(top).toBe(`calc(${barH} + 16px)`)
    const h = (painted(col, 'height', WIDE) ?? '').replace(/\s+/g, ' ')
    // MUTATION: drop `- var(--ctl-bar-h, 0px)` from the height and the column's foot runs under the API-reads line.
    expect(h).toMatch(new RegExp(`^calc\\(100vh - (var\\(--dock-h, 28px\\)|28px) - ${barH} - 16px\\)$`))
    // MUTATION: leave max-height at the old figure and the column can still grow under the dock.
    expect((painted(col, 'max-height', WIDE) ?? '').replace(/\s+/g, ' ')).toBe(h)
  })
})

describe('axe: the agent head is not a second banner landmark', () => {
  it('draws no banner, and the head and its title are still there', () => {
    const { container } = render(
      <AgHead
        taskId={ID}
        task={row()}
        runs={1}
        read="ok"
        reload={() => {}}
        onClose={() => {}}
        live={{ status: 'idle' }}
        reloadLive={() => {}}
      />,
    )
    // MUTATION: make `.ag-head` a `<header>` again and it is a banner (a second one beside the trail bar's).
    expect(screen.queryAllByRole('banner')).toHaveLength(0)
    // MUTATION: drop the head's class and the sheet's header rules match nothing.
    expect(container.querySelector('.ag-head > .ag-head-row')).not.toBeNull()
    // MUTATION: drop the title and the head names nothing.
    expect(screen.getByRole('heading', { level: 2 }).textContent).not.toBe('')
  })
})

describe('ff-007: an account not read says what was not read, and why', () => {
  const accountLi = (container: HTMLElement) =>
    [...container.querySelectorAll<HTMLElement>('.ag-head-facts > li')].find((l) => l.textContent?.startsWith('account'))!

  it('the API sent no account: says which account and that this read did not carry it', () => {
    const { container } = render(<AgHeadMeta task={row({ account: null })} />)
    const li = accountLi(container)
    // MUTATION: drop the head's own sentence and the title is `The account was not read on this request.`, no reason.
    expect(li.title).toMatch(/subscription account/)
    // MUTATION: say only what was not read and the reader still does not know why.
    expect(li.title).toMatch(/did not send it/)
    // MUTATION: drop the class and nothing on screen says the fact has an explanation.
    expect(li.classList.contains('ag-head-why')).toBe(true)
    // MUTATION: drop the sheet's dotted underline and the title is found only by chance.
    expect(painted(li, ['text-decoration-style', 'text-decoration'], WIDE)).toMatch(/dotted/)
    // MUTATION: drop `cursor: help` and the pointer does not say there is more to read.
    expect(painted(li, 'cursor', WIDE)).toBe('help')
  })

  it('the account read failed or was cut short: says so', () => {
    const { container } = render(
      <AgHeadMeta
        task={row({
          account: { status: 'unread', account_id: null, provider: null, attempt_id: null, generation: null, swapped_from: null, swaps: [] },
        })}
      />,
    )
    const li = accountLi(container)
    // MUTATION: fall back to `accountText`'s title and it reads `not read.` -- the chip's words again.
    expect(li.title).not.toBe('not read.')
    // MUTATION: drop the `unread` sentence and the read's failure is not named.
    expect(li.title).toMatch(/failed or stopped short/)
  })

  it('a real account keeps its id title and no hint', () => {
    const { container } = render(
      <AgHeadMeta
        task={row({
          account: { status: 'assigned', account_id: 'claude-max-2', provider: 'anthropic', attempt_id: 'att_1', generation: 1, swapped_from: null, swaps: [] },
        })}
      />,
    )
    const li = accountLi(container)
    // MUTATION: give every account the not-read sentence and a real one is described as missing.
    expect(li.title).toMatch(/^Running on claude-max-2/)
    // MUTATION: mark every account and a known one looks as if it needed explaining.
    expect(li.classList.contains('ag-head-why')).toBe(false)
  })
})

describe('ff-009: Changes and Artifacts say what an unknown count means, not a bare dash', () => {
  const draw = (t: Task | null) => {
    const tabs = paneTabs({ task: t, base: '/agents/live/x', hasBrowser: false, sessionOpen: false, childCount: null, runs: 1, ckpt: null, ckptCount: 0 })
    render(
      <Tabs
        label="Agent panes"
        current="detail"
        onSelect={() => {}}
        tabs={tabs.map((x) => ({ key: x.id, label: x.label, count: x.count, why: x.say ?? undefined }))}
      />,
    )
    const em = (name: string) => screen.getByRole('tab', { name: new RegExp(`^${name}`) }).querySelector('em')!
    return { changes: em('Changes'), artifacts: em('Artifacts') }
  }

  it('a running agent: `not yet`, with the reason as the title', () => {
    const { changes, artifacts } = draw(row())
    // MUTATION: leave the count null and the tab reads `Changes —`.
    expect(changes.textContent).toBe('not yet')
    // MUTATION: leave the count null and the tab reads `Artifacts —`.
    expect(artifacts.textContent).toBe('not yet')
    // MUTATION: drop the reason and the words have no explanation on hover.
    expect(changes.title).toMatch(/when an attempt ends/)
    expect(artifacts.title).toMatch(/when an attempt ends/)
  })

  it('an ended agent with nothing recorded: `not counted`', () => {
    const { changes, artifacts } = draw(row({ state: 'FAILED', completed_at: new Date().toISOString() }))
    // MUTATION: say `not yet` for an ended agent and it promises a count that will never come.
    expect(changes.textContent).toBe('not counted')
    expect(artifacts.textContent).toBe('not counted')
  })

  it('a known count is still a number', () => {
    const { artifacts } = draw(row({ state: 'SUCCEEDED', result_summary: { artifacts: [{ name: 'a.md', bytes: 1, uri: 'gs://x/a.md' }] } }))
    // MUTATION: replace every count with words and a measured count is lost.
    expect(artifacts.textContent).toBe('1')
  })
})
