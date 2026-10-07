/**
 * U11a (owner QA re-run, 2026-10-04): the agent split, the Agents list and
 * the shell.
 *
 *   D14  the panel's recent workflow ids ellipsized with no title on the span.
 *   D21  the Checkpoints tab said `–` until opened, then `0`, beside Details'
 *        `1 written · 0 in bucket`.
 *   D28  at the 64px strip the list's head scrolled away and the breadcrumb
 *        was half off the top.
 *   D39  on a phone a lone `?` opened Overview's whole explanation, and the
 *        agent's tabs were cut (`Chi`) with no way to tell.
 *   +    the artifact viewer opened at y 763-971, under the log strip.
 *   N9   a row on the Recent tab opened `/agents/live/<id>` and lit `Live 0`.
 *   N10  the list head's `refresh` was clipped inside `.c-age`; the meta pill
 *        cut its words with no title. Since #138 the refresh is one button
 *        carrying its age (cut whole in its title) and the meta is the count
 *        note, whole in its title.
 *   N12  `/agents/waiting` headed a step waiting on an earlier step `No room`.
 *
 * MUTATIONS: drop the span's title; draw the Checkpoints pane only on its
 * tab, or say `in bucket` again; drop the strip's sticky head or its hidden
 * breadcrumb; drop the phone `?` rule or the tabs' wrap; scroll the viewer
 * with `scrollIntoView` inside the pane; drop `openRow`'s report; split the
 * refresh's age from its press, drop its title, let the title block shrink,
 * or drop the count note's title; file a park under
 * `No room`. Each turns a case red.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { CheckpointsPage, Task, TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { at, attempt, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTask: vi.fn(),
  loadTasks: vi.fn(),
  loadResourceClasses: vi.fn(),
  loadChildren: vi.fn(),
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadAgentRun: vi.fn(),
  loadCheckpoints: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgentSplit } = await import('../AgentSplit')
const { AgentsScreen, waitGroupOf } = await import('../Agents')
const { ArtifactViewer } = await import('../ArtifactViewer')
const { Screen } = await import('../Shell')
const { resetListSnap } = await import('../listSnap')

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const ID = 'task_0123456789abcdef0123'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

const hosts: HTMLElement[] = []
function tree(html: string): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = html
  document.body.appendChild(host)
  hosts.push(host)
  return host
}

beforeEach(() => {
  localStorage.clear()
  resetListSnap()
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadChildren.mockResolvedValue(ok({ tasks: [] } satisfies TaskPage))
  api.loadCheckpoints.mockReturnValue(new Promise(() => {}))
  api.loadResourceClasses.mockResolvedValue(ok({ resource_classes: {} }))
})

afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
  delete document.documentElement.dataset.agentList
})

describe('D14: a recent workflow’s span carries its whole name and id', () => {
  it('titles the ellipsized span itself', async () => {
    const { App } = await import('../App')
    const { RECENT_WORKFLOWS_KEY } = await import('../Spine')
    const id = 'wf_' + '8fa28bbc798e4b8a8e7e'.repeat(2)
    localStorage.setItem(RECENT_WORKFLOWS_KEY, JSON.stringify([{ id, state: 'RUNNING', name: 'nightly-review' }]))
    window.history.replaceState(null, '', '/workflows')
    try {
      const { container } = render(<App />)
      const span = await waitFor(() => {
        const s = container.querySelector<HTMLElement>(`span.${['sk', 'recent', 'id'].join('-')}`)
        expect(s).not.toBeNull()
        return s!
      })
      expect(span.textContent).toBe('nightly-review')
      expect(span.getAttribute('title')).toBe(`nightly-review · ${id}`)
    } finally {
      window.history.replaceState(null, '', '/')
    }
  })
})

function listing(records: CheckpointsPage['checkpoints']): CheckpointsPage {
  return {
    task_id: ID,
    tenant_id: 'eng',
    prefix: `tenants/eng/tasks/${ID}/checkpoints`,
    checkpoints: records,
    count: records.length,
    total_found: records.length,
    next_page_token: null,
    listed: true,
    truncated: false,
    latest_checkpoint: { pointer: null, status: records.length === 0 ? 'unset' : 'present', checkpoint_id: records[0]?.checkpoint_id ?? null },
  }
}

type Pane = 'detail' | 'logs' | 'children' | 'attempts' | 'artifacts' | 'checkpoints'
const PANES: readonly Pane[] = ['logs', 'children', 'attempts', 'artifacts', 'checkpoints']

function Routed({ start }: { start: Pane }) {
  const [pane, setPane] = useState<Pane>(start)
  const go = (to: string) => {
    const last = to.split('/').pop() as Pane
    setPane(PANES.includes(last) ? last : 'detail')
  }
  return (
    <div className="app has-inspector">
      <AgentSplit taskId={ID} pane={pane} artifact={null} closeTo="work/running/live" go={go} base={`work/task/${ID}`} />
    </div>
  )
}

function tab(name: string): HTMLElement {
  const t = within(screen.getByRole('tablist', { name: 'Agent panes' }))
    .getAllByRole('tab')
    .find((b) => b.querySelector('.c-tab-label')?.textContent === name)
  expect(t, `no ${name} tab`).toBeTruthy()
  return t!
}

describe('D21: the Checkpoints tab counts from the read its pane draws, from the start', () => {
  it('shows the count before the tab is opened, and says `1 written, 0 kept` in all three places', async () => {
    const finished = runTask({ id: ID, state: 'SUCCEEDED', runner_profile: 'claude-code', attempt_count: 1 })
    api.loadTask.mockResolvedValue(ok(finished))
    api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1, { attempt_id: 'att_1', checkpoints: ['ckpt-00001'] })] }))
    api.loadCheckpoints.mockResolvedValue(ok(listing([])))
    render(<Routed start="detail" />)
    // On Details, before Checkpoints has ever been opened: a count, not a dash.
    // Kept of written (G2-12): a bare `0` read as none ever written.
    await waitFor(() => expect(tab('Checkpoints').querySelector('em')?.textContent).toBe('0 of 1'))
    expect(tab('Details').getAttribute('aria-selected')).toBe('true')
    expect(tab('Checkpoints').querySelector('em')?.getAttribute('title')).toMatch(/^1 written, 0 kept/)
    // One read: the badge and the pane are the same answer.
    const reads = api.loadCheckpoints.mock.calls.length
    fireEvent.click(tab('Checkpoints'))
    const sum = await waitFor(() => {
      const s = document.querySelector<HTMLElement>('.ag-ckpts-sum')
      expect(s).not.toBeNull()
      return s!
    })
    expect(sum.textContent).toBe('1 written, 0 kept')
    expect(sum.closest('[hidden]')).toBeNull()
    expect(api.loadCheckpoints.mock.calls.length, 'opening the tab read the listing again').toBe(reads)
    expect(tab('Checkpoints').querySelector('em')?.textContent).toBe('0 of 1')
  })

  it('keeps a dash with its reason while the listing has not answered', async () => {
    api.loadTask.mockResolvedValue(ok(runTask({ id: ID, state: 'SUCCEEDED', attempt_count: 1 })))
    render(<Routed start="detail" />)
    await waitFor(() => expect(document.querySelector('.ag-head-facts')).not.toBeNull())
    const em = tab('Checkpoints').querySelector('em')!
    expect(em.textContent).toBe('—')
    expect(em.getAttribute('title')).toMatch(/not answered yet/)
  })
})

describe('D28: the 64px strip shows only its collapse control, cleanly', () => {
  it('keeps the list head at the top of the scrollport and steps the breadcrumb aside', () => {
    document.documentElement.dataset.agentList = 'strip'
    const host = tree(
      '<div class="app has-inspector"><main class="work"><div class="ctl-head"><nav class="ctl-crumb">Agents</nav></div>' +
        '<div class="ctl-toolbar ag-list-head"><button class="ag-collapse">» <kbd>[</kbd></button><label class="ag-find"></label></div></main></div>',
    )
    const head = host.querySelector('.ag-list-head')!
    expect(painted(head, 'position', WIDE)).toBe('sticky')
    expect(painted(head, 'top', WIDE)).toBe('0')
    expect(painted(head, ['background', 'background-color'], WIDE)).toMatch(/var\(--bg\)/)
    expect(painted(host.querySelector('.ag-find')!, 'display', WIDE)).toBe('none')
    expect(painted(host.querySelector('.ag-collapse > kbd')!, 'display', WIDE)).toBe('none')
    const crumb = host.querySelector('.ctl-head')!
    expect(painted(crumb, 'position', WIDE)).toBe('absolute')
    expect(painted(crumb, 'clip-path', WIDE)).toBe('inset(50%)')
    expect(painted(crumb, 'display', WIDE), 'removed from the outline too').not.toBe('none')
    // Only at the strip: at 380px the head scrolls with the list and the trail is drawn.
    document.documentElement.dataset.agentList = 'list'
    expect(painted(head, 'position', WIDE) ?? 'static').not.toBe('sticky')
    expect(painted(crumb, 'position', WIDE) ?? 'static').not.toBe('absolute')
  })
})

describe('D39: the phone frame', () => {
  it('never draws a `?` alone where Overview’s title steps aside', () => {
    const host = tree(
      '<div class="ov-page"><div class="c-phead"><div class="head"><h1>Overview</h1><span class="ctl-q">?</span></div><p class="sub"><span class="c-meta">tenant eng</span></p></div></div>',
    )
    const q = host.querySelector('.ctl-q')!
    expect(painted(q, 'display', PHONE)).toBe('none')
    expect(painted(q, 'display', WIDE) ?? 'inline-flex').not.toBe('none')
    // The title is still in the outline for a screen reader.
    expect(painted(host.querySelector('h1')!, 'display', PHONE) ?? 'block').not.toBe('none')
  })

  it('wraps the agent’s tabs onto as many lines as they take, so none is cut', () => {
    const host = tree('<div class="ag-split"><div class="ag-tabs-edge"><div class="c-tabs ag-split-tabs" role="tablist"></div></div></div>')
    const strip = host.querySelector('.ag-split-tabs')!
    expect(painted(strip, 'flex-wrap', PHONE)).toBe('wrap')
    expect(painted(strip, ['overflow-x', 'overflow'], PHONE)).toBe('visible')
    expect(painted(strip, 'flex-wrap', WIDE)).toBe('nowrap')
  })
})

describe('the artifact viewer opens in view, in its own pane', () => {
  it('scrolls only the agent’s pane to the viewer’s top, as it opens and when its read lands', async () => {
    const seen = vi.fn()
    const proto = Element.prototype as unknown as { scrollIntoView?: unknown }
    const had = proto.scrollIntoView
    proto.scrollIntoView = seen
    let land: (r: Result<unknown>) => void = () => {}
    try {
      const { container } = render(
        <div className="ag-split-pane">
          <ArtifactViewer
            taskId={ID}
            artifact={{ name: 'a.txt', bytes: 3, uri: '' }}
            onClose={() => {}}
            load={() => new Promise((r) => (land = r as typeof land))}
          />
        </div>,
      )
      const pane = container.querySelector<HTMLElement>('.ag-split-pane')!
      const viewer = container.querySelector<HTMLElement>('.art-viewer')!
      pane.getBoundingClientRect = () => ({ top: 100 }) as DOMRect
      viewer.getBoundingClientRect = () => ({ top: 763 }) as DOMRect
      pane.scrollTop = 0
      // The open read lands: the viewer is put at the pane's top once more.
      await act(async () => land({ status: 'error', error: { kind: 'unreachable', httpStatus: null, code: null, message: 'down' } }))
      expect(pane.scrollTop, 'the pane was not scrolled to the viewer').toBe(763 - 100 - 8)
      expect(seen, 'scrollIntoView moves every ancestor, the page included').not.toHaveBeenCalled()
    } finally {
      proto.scrollIntoView = had
    }
  })
})

function row(id: string, over: Partial<Task>): Task {
  return runTask({ id, started_at: null, completed_at: null, ...over })
}

describe('N9: a row opens under the tab it came from', () => {
  it('reports the Recent tab the list landed on before it opens the agent', async () => {
    const done = row('task_done0001', { state: 'SUCCEEDED', completed_at: at(10) })
    api.loadTasks.mockResolvedValue(ok({ tasks: [done], tenant_id: 'eng' }))
    const order: string[] = []
    const onList = vi.fn((l: { tab: string }) => order.push(`list:${l.tab}`))
    const onOpen = vi.fn((id: string) => order.push(`open:${id}`))
    render(<AgentsScreen onOpen={onOpen} onList={onList} />)
    // Nothing live: the list lands on Recent by itself, with no click.
    const r = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('[data-task-id="task_done0001"]')
      expect(el).not.toBeNull()
      return el!
    })
    expect(onList).not.toHaveBeenCalled()
    fireEvent.click(r)
    expect(order).toEqual(['list:recent', 'open:task_done0001'])
  })

  it('writes the agent’s path under that tab', async () => {
    const { addressToPath } = await import('../paths')
    expect(addressToPath('work/task/task_done0001', 'recent')).toBe('/agents/recent/task_done0001')
  })
})

describe('N10: the list head fits its control beside an open agent', () => {
  it('keeps the press and its age one button, whole in its title, and titles the count note', async () => {
    const { container } = render(
      <div className="app has-inspector">
        <main className="work">
          <Screen title="Agents" load={async () => ok({ n: 200 })} summary={(d) => <>{d.n} loaded · 0 live · eng</>}>
            {() => <p>rows</p>}
          </Screen>
        </main>
      </div>,
    )
    // #138: the head is title left, actions right; the refresh carries the
    // screen's age, so cutting its words can never cut the press off its age.
    const button = await waitFor(() => {
      const b = container.querySelector<HTMLButtonElement>('.c-phead > .c-acts > button.c-refresh')
      expect(b?.getAttribute('title')).toBeTruthy()
      return b!
    })
    const acts = button.parentElement!
    expect(acts.querySelectorAll('.c-refresh').length, 'the age and the press are split again').toBe(1)
    expect(container.querySelector('.c-age, .c-age-say, .c-meta'), 'the old age line or meta pill is back').toBeNull()
    // Whole in its title and its accessible name, as the words the reader sees.
    const said = button.getAttribute('title')!
    expect(said).toMatch(/^\d+ s$/)
    expect(button.textContent).toBe(`⟳ ${said}`)
    expect(button.getAttribute('aria-label')).toBe(`Refresh · read ${said} ago`)
    // It is what gives way, with an ellipsis; the title keeps its width.
    expect(painted(button, 'text-overflow', WIDE)).toBe('ellipsis')
    expect(painted(button, 'min-width', WIDE)).toBe('0')
    expect(painted(acts, ['overflow-x', 'overflow'], WIDE)).toBe('hidden')
    expect(painted(acts, 'min-width', WIDE)).toBe('0')
    const head = container.querySelector('.c-phead > .head')!
    expect(painted(head, ['flex', 'flex-shrink'], WIDE), 'the title shrinks for the actions').toMatch(/^(none|0)\b/)
    // The count is a note over the first card, out of the head, whole in its title.
    const note = container.querySelector('.c-count-note')!
    expect(note.closest('.c-phead'), 'the count is back in the head').toBeNull()
    expect(note.getAttribute('title')).toBe('200 loaded · 0 live · eng')
  })
})

describe('N12: a parked task is grouped by its own park reason', () => {
  it('heads a step waiting on an earlier step by that, not `No room`', async () => {
    const parked = row('task_wait0001', { state: 'PARKED', park_reason: 'DEPENDENCY_INCOMPLETE' })
    const full = row('task_full0001', { state: 'READY', blocked_by: [{ pool: 'global', reason: 'GLOBAL_LIMIT', limit: 4, active: 4 }] })
    api.loadTasks.mockResolvedValue(ok({ tasks: [parked, full], tenant_id: 'eng' }))
    render(<AgentsScreen onOpen={() => {}} list={{ tab: 'waiting', state: null }} />)
    const h = await screen.findByRole('heading', { name: /^Waiting on a step/ })
    const group = h.closest('section')!
    expect([...group.querySelectorAll('[data-task-id]')].map((r) => r.getAttribute('data-task-id'))).toEqual(['task_wait0001'])
    expect(group.textContent).toContain('Waiting on an earlier step in its workflow')
    const room = screen.getByRole('heading', { name: /^No room/ }).closest('section')!
    expect([...room.querySelectorAll('[data-task-id]')].map((r) => r.getAttribute('data-task-id'))).toEqual(['task_full0001'])
  })

  it('names an unknown reason verbatim and a missing one as not recorded', () => {
    expect(waitGroupOf(row('a', { state: 'PARKED', park_reason: 'SOMETHING_NEW' })).title).toBe('SOMETHING_NEW')
    expect(waitGroupOf(row('b', { state: 'PARKED', park_reason: null })).title).toBe('Parked, reason not recorded')
    expect(waitGroupOf(row('c', { state: 'PARKED', park_reason: 'PROVIDER_QUOTA_EXHAUSTED' })).title).toBe('Provider quota')
    expect(waitGroupOf(row('d', { state: 'READY' })).title).toBe('No room')
  })
})
