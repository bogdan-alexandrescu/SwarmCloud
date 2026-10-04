/**
 * U10a (owner QA, 2026-10-04): the agent split's list, tabs and panes.
 *
 *   D6   beside an open agent the page title wrapped `Agent / s`.
 *   D28  at the 64px strip the page head's fragments stayed, `?` over meta.
 *   D36  Children did not change the URL, and its empty state said
 *        `waiting on 0 · 0 of — not measured` beside `no children`.
 *   D21  the Checkpoints tab said `–` beside `found 1 of 1`; Size and Age
 *        were full-width rows and `2 objects` sat alone on a row.
 *   D39  on a phone the tabs were cut (`Che…`) with nothing to say so.
 *   D33  the second input block had no label and touched the prompt.
 *   D15  the attempt phases axis printed `-3h 54m-2h 46m`.
 *   +    Escape closes the artifact viewer, which scrolls into view.
 *
 * MUTATIONS: drop each rule or attribute named in the cases below.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { CheckpointRecord, CheckpointsPage, Task, TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { at, attempt, ev, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTask: vi.fn(),
  loadChildren: vi.fn(),
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadAgentRun: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadTaskInputOnce: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgentSplit } = await import('../AgentSplit')
const { resetListSnap } = await import('../listSnap')
const { Prompt } = await import('../Artifacts')
const { ArtifactViewer } = await import('../ArtifactViewer')
const { AttemptDurations, axisGapPx, AXIS_CHAR_PX } = await import('../charts/AttemptPhases')
const { addressToPath, pathToAddress } = await import('../paths')
const { fromHash } = await import('../App')

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const COLUMN: CascadeEnv = { width: 1440, container: 520 }
const ID = 'task_0123456789abcdef0123'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function agent(over: Partial<Task> = {}): Task {
  return runTask({ id: ID, state: 'RUNNING', runner_profile: 'claude-code', attempt_count: 1, completed_at: null, ...over })
}

type Pane = 'detail' | 'children' | 'attempts' | 'artifacts' | 'checkpoints'
const PANES: readonly Pane[] = ['children', 'attempts', 'artifacts', 'checkpoints']

function Routed({ start, onGo }: { start: Pane; onGo: (to: string) => void }) {
  const [pane, setPane] = useState<Pane>(start)
  const go = (to: string) => {
    onGo(to)
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
  const list = screen.getByRole('tablist', { name: 'Agent panes' })
  const t = within(list)
    .getAllByRole('tab')
    .find((b) => b.querySelector('.c-tab-label')?.textContent === name)
  expect(t, `no ${name} tab`).toBeTruthy()
  return t!
}

/** Build `.a > .b > …` from a selector of classes and tags, for the cascade. */
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
})

afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
  delete document.documentElement.dataset.agentList
})

describe('D6: beside an open agent the page title keeps its word', () => {
  it('does not shrink the title; the meta and then the freshness give way', () => {
    const host = tree(
      '<div class="app has-inspector"><main class="work"><div class="c-phead"><div class="head"><h1>Agents</h1></div>' +
        '<p class="sub"><span class="c-meta">2 read</span><span class="c-age"><span class="c-age-say">read just now</span><button>refresh</button></span></p></div></main></div>',
    )
    const head = host.querySelector('.c-phead > .head')!
    const h1 = host.querySelector('h1')!
    expect(painted(head, ['flex', 'flex-shrink'], WIDE)).toMatch(/^(none|0)\b/)
    expect(painted(h1, 'white-space', WIDE)).toBe('nowrap')
    expect(painted(h1, 'overflow-wrap', WIDE), 'the title may break mid-word').toBe('normal')
    expect(painted(h1, 'text-overflow', WIDE)).toBe('ellipsis')
    const sub = host.querySelector('.sub')!
    expect(painted(sub, 'min-width', WIDE)).toBe('0')
    expect(painted(sub, 'overflow', WIDE)).toBe('hidden')
    expect(painted(host.querySelector('.c-meta')!, 'text-overflow', WIDE)).toBe('ellipsis')
    // U11a N10: the freshness words give way, not the refresh control after them.
    expect(painted(host.querySelector('.c-age-say')!, 'text-overflow', WIDE)).toBe('ellipsis')
  })
})

describe('D28: at the 64px strip the page head is not drawn', () => {
  it('hides the head visually and keeps it for a screen reader', () => {
    document.documentElement.dataset.agentList = 'strip'
    const host = tree('<div class="app has-inspector"><main class="work"><div class="c-phead"><div class="head"><h1>Agents</h1></div></div></main></div>')
    const ph = host.querySelector('.c-phead')!
    expect(painted(ph, 'position', WIDE)).toBe('absolute')
    expect(painted(ph, 'width', WIDE)).toBe('1px')
    expect(painted(ph, 'clip-path', WIDE)).toBe('inset(50%)')
    expect(painted(ph, 'display', WIDE), 'removed from the outline too').not.toBe('none')
    // And only at the strip: at 380px the head is drawn.
    delete document.documentElement.dataset.agentList
    expect(painted(ph, 'position', WIDE)).not.toBe('absolute')
  })
})

describe('D36: Children has an address and one honest empty state', () => {
  it('routes the tab to <agent>/children, and the address opens it', async () => {
    api.loadTask.mockResolvedValue(ok(agent({ parent_task_id: null })))
    const go = vi.fn()
    render(<Routed start="detail" onGo={go} />)
    await waitFor(() => expect(tab('Children').querySelector('em')?.textContent).toBe('0'))
    fireEvent.click(tab('Children'))
    expect(go).toHaveBeenCalledWith(`work/task/${ID}/children`)
    expect(tab('Children').getAttribute('aria-selected')).toBe('true')
    // The address round-trips as a pane, not as part of the id.
    expect(pathToAddress(addressToPath(`work/task/${ID}/children`))?.address).toBe(`work/task/${ID}/children`)
    window.history.replaceState(null, '', `/#work/task/${ID}/children`)
    expect(fromHash()).toMatchObject({ taskId: ID, taskPane: 'children' })
    window.history.replaceState(null, '', '/')
  })

  it('says `no children` once, with nothing waited on and no cap beside it', async () => {
    api.loadTask.mockResolvedValue(ok(agent({ parent_task_id: null })))
    render(<Routed start="children" onGo={() => {}} />)
    const pane = await screen.findByRole('region', { name: 'Children' })
    await waitFor(() => expect(pane.textContent).toMatch(/no children/))
    expect(pane.textContent).not.toMatch(/waiting on/)
    expect(pane.textContent).not.toMatch(/of —/)
    expect(pane.querySelector('.ag-children-facts')).toBeNull()
    expect(pane.querySelectorAll('.att-none')).toHaveLength(1)
  })
})

function record(id: string): CheckpointRecord {
  const prefix = `tenants/eng/tasks/${ID}/checkpoints/${id}`
  return {
    checkpoint_id: id,
    attempt_id: 'att_1',
    attempt_known: true,
    attempt_created_at: at(0),
    attempt_completed_at: null,
    prefix,
    uri: `gs://swarm-workspaces/${prefix}`,
    is_latest_pointer: true,
    objects: [
      { name: 'manifest.json', key: `${prefix}/manifest.json`, bytes: 412 },
      { name: 'workspace.tar.zst', key: `${prefix}/workspace.tar.zst`, bytes: 18_442_240 },
    ],
    stored_bytes: 18_442_652,
    manifest: 'present',
    manifest_detail: null,
    created_at: at(4),
    seq: 1,
    generation: 1,
    label: null,
    archive_bytes: 18_442_240,
    archive_sha256: null,
    file_count: 12,
    resumable: true,
    resumable_detail: null,
  }
}

function listing(over: Partial<CheckpointsPage> = {}): CheckpointsPage {
  return {
    task_id: ID,
    tenant_id: 'eng',
    prefix: `tenants/eng/tasks/${ID}/checkpoints`,
    checkpoints: [record('ckpt-00001')],
    count: 1,
    total_found: 1,
    next_page_token: null,
    listed: true,
    truncated: false,
    latest_checkpoint: { pointer: null, status: 'present', checkpoint_id: 'ckpt-00001' },
    ...over,
  }
}

describe('D21: the Checkpoints tab counts what its pane found', () => {
  it('shows the listing’s count, not a dash, once the pane has read it', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1, { attempt_id: 'att_1' })] }))
    api.loadCheckpoints.mockResolvedValue(ok(listing()))
    render(<Routed start="checkpoints" onGo={() => {}} />)
    await waitFor(() => expect(document.querySelector('.ag-ckpts')?.textContent).toMatch(/1 of 1/))
    await waitFor(() => expect(tab('Checkpoints').querySelector('em')?.textContent).toBe('1'))
  })

  it('keeps the dash, with its reason, for a listing that was cut', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    api.loadCheckpoints.mockResolvedValue(ok(listing({ truncated: true })))
    render(<Routed start="checkpoints" onGo={() => {}} />)
    await waitFor(() => expect(document.querySelector('.ag-ckpts')?.textContent).toMatch(/1 of 1/))
    expect(tab('Checkpoints').querySelector('em')?.textContent).toBe('—')
  })

  it('sets Size and Age side by side, the objects button beside the size', () => {
    const host = tree(
      '<div class="ag-ckpts"><div class="ctl-table is-stacked"><table><tbody><tr><th scope="row">ckpt</th>' +
        '<td data-label="Size">17.6 MiB<span class="ctl-sub"><button>2 objects</button></span></td><td data-label="Age">4m</td></tr></tbody></table></div></div>',
    )
    const tr = host.querySelector('tr')!
    expect(painted(tr, 'display', COLUMN)).toBe('flex')
    expect(painted(tr, 'flex-wrap', COLUMN)).toBe('wrap')
    const [size] = [...host.querySelectorAll('td')]
    expect(painted(size!, 'display', COLUMN), 'a cell is still a full-width grid row').toBe('inline-flex')
    expect(painted(host.querySelector('td > .ctl-sub')!, 'display', COLUMN)).toBe('inline')
    expect(painted(host.querySelector('th')!, ['flex', 'flex-basis'], COLUMN)).toMatch(/100%/)
  })
})

describe('D39: on a phone the tabs say there are more of them', () => {
  it('marks the end with more beyond it, and the start once scrolled', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    render(<Routed start="detail" onGo={() => {}} />)
    const strip = screen.getByRole('tablist', { name: 'Agent panes' })
    const edge = strip.closest('.ag-tabs-edge') as HTMLElement
    expect(edge, 'the tabs have no edge marker').not.toBeNull()
    Object.defineProperty(strip, 'scrollWidth', { configurable: true, value: 600 })
    Object.defineProperty(strip, 'clientWidth', { configurable: true, value: 300 })
    strip.scrollLeft = 0
    fireEvent.scroll(strip)
    expect(edge.dataset.more).toBe('end')
    strip.scrollLeft = 150
    fireEvent.scroll(strip)
    expect(edge.dataset.more).toBe('start end')
    strip.scrollLeft = 300
    fireEvent.scroll(strip)
    expect(edge.dataset.more).toBe('start')
    // The sheet draws it: a chevron over a fade, never a pointer target.
    edge.dataset.more = 'end'
    expect(painted(edge, 'content', PHONE, 'after')).toMatch(/›/)
    expect(painted(edge, 'pointer-events', PHONE, 'after')).toBe('none')
  })
})

describe('D33: the rest of the input is labelled and apart from the prompt', () => {
  it('labels the second block and spaces it from the prompt', async () => {
    api.loadTaskInputOnce.mockResolvedValue(
      ok({
        task_id: ID,
        tenant_id: 'eng',
        read_at: new Date().toISOString(),
        prompt_key: 'string',
        prompt: { text: 'Fix the heartbeat.', redaction_count: 0 },
        rest: { text: '{\n  "issue": 454\n}', redaction_count: 0 },
        full: { text: '{}', redaction_count: 0 },
        redacted: false,
        redaction_count: 0,
        redaction: { applied_at_read_time: true, rules: 12 },
      }),
    )
    const { container } = render(<Prompt taskId={ID} readAt={1} />)
    const rest = await waitFor(() => {
      const r = container.querySelector('.arts-rest')
      expect(r, 'the rest of the input is not its own labelled block').not.toBeNull()
      return r!
    })
    expect(rest.querySelector('.ctl-eyebrow')?.textContent).toBe('rest of the input')
    expect(rest.querySelector('pre')?.textContent).toContain('"issue": 454')
    expect(painted(rest, ['margin-top', 'margin'], WIDE) ?? '0').not.toMatch(/^0(px)?$/)
  })
})

describe('the artifact viewer: in view when it opens, and Escape closes it', () => {
  it('scrolls itself into view, and Escape closes it without closing the agent', async () => {
    const seen = vi.fn()
    const proto = Element.prototype as unknown as { scrollIntoView?: unknown }
    const had = proto.scrollIntoView
    proto.scrollIntoView = seen
    try {
      const close = vi.fn()
      const closeAgent = vi.fn()
      render(
        <div onKeyDown={(e) => e.key === 'Escape' && closeAgent()}>
          <button type="button">a.txt</button>
          <ArtifactViewer taskId={ID} artifact={{ name: 'a.txt', bytes: 3, uri: '' }} onClose={close} load={() => new Promise(() => {})} />
        </div>,
      )
      expect(seen).toHaveBeenCalled()
      fireEvent.keyDown(screen.getByRole('button', { name: 'a.txt' }), { key: 'Escape' })
      expect(close).toHaveBeenCalledTimes(1)
      expect(closeAgent, 'Escape on the viewer closed the agent').not.toHaveBeenCalled()
    } finally {
      proto.scrollIntoView = had
    }
  })
})

describe('D15: the attempt phases axis never runs two labels together', () => {
  it('spaces label centres by the widest label, not a fixed 52px', () => {
    // The pair the owner saw drawn as one run.
    const gap = axisGapPx(['-3h 54m', '-2h 46m'])
    expect(gap).toBeGreaterThanOrEqual(7 * AXIS_CHAR_PX + 8)
    expect(axisGapPx(['admitted'])).toBeGreaterThan(52)
  })

  it('draws no two tick labels closer than their half-widths and some air', () => {
    // Submitted four hours before admission: a long negative queue.
    const t = runTask({ created_at: at(0) })
    const a = attempt(1, { created_at: at(242), started_at: at(242), completed_at: at(250), exit_code: 0 })
    const { container } = render(
      <div style={{ width: '480px' }}>
        <AttemptDurations task={t} attempts={[a]} events={[ev('lease_acquired', at(240), 'att_1')]} />
      </div>,
    )
    // The chart draws one axis per drawing (wide and narrow); each is checked.
    const svgs = [...container.querySelectorAll('svg[role="group"]')]
    expect(svgs.length, 'the chart drew no axis to check').toBeGreaterThan(0)
    for (const svg of svgs) {
      const ticks = [...svg.querySelectorAll('.ctl-chart-tick')]
        .map((g) => ({ label: g.querySelector('text')?.textContent ?? '', x: Number(g.querySelector('line')?.getAttribute('x1')) }))
        .filter((t) => t.label !== '' && Number.isFinite(t.x))
        .sort((p, q) => p.x - q.x)
      expect(ticks.length, 'an axis drew fewer than two labels').toBeGreaterThanOrEqual(2)
      for (let i = 1; i < ticks.length; i++) {
        const p = ticks[i - 1]!
        const q = ticks[i]!
        const need = ((p.label.length + q.label.length) / 2) * AXIS_CHAR_PX + 8
        expect(q.x - p.x, `"${p.label}" and "${q.label}" overprint`).toBeGreaterThanOrEqual(need)
      }
    }
  })
})

