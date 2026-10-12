// LANE LB-UX2b (#1030): the workflow graph's live browser card, as the chosen
// mockup "Browser Tab and Theater" draws it (docs/design/live-browser.md §6).
//
// WHAT EACH CASE HOLDS:
//   * the card is INLINE in the RUNNING browser step's node -- a sibling of
//     the node's button inside `.node-slot`, never inside the button -- and the
//     layout reserves its height (`LIVE_CARD_H`), so it overlaps nothing;
//   * the step details panel no longer carries it: one live view per step;
//   * Open Browser tab is /agents/live/<task>/browser and ⤢ Theater is the
//     same path with ?theater=1, plus `wf=<workflow>` so the theater's prev and
//     next walk this workflow's live steps (`workflowLiveAgents`);
//   * a session read with NO session says "no live view", not "reconnecting",
//     and spends no viewer ticket; a session that exists is retried quietly.
//
// MUTATIONS: drop `live` from `layoutOf`'s height (the reserved-height case
// reads the old height and turns red); mount the card in `StepInspector` again
// (the panel case turns red); let `ticketedSocket` fail a null session as an
// ordinary close (the "no live view" case reads "reconnecting" and turns red).

import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { WorkflowCard } from '../Workflows'
import { LIVE_CARD_H, heightOf, layoutOf } from '../dag'
import {
  LiveThumb,
  NO_LIVE_SESSION,
  THUMB_RECONNECT_MS,
  THUMB_SESSION_RECHECK_MS,
  browserTabPath,
  theaterPath,
  workflowLiveAgents,
  type LiveSocket,
} from '../LiveThumb'
import type { Task, Workflow, WorkflowStep } from '../types'
import { bigWorkflow, running } from './dagfixture'

const BROWSER = 'claude-code-browser'

function step(step_id: string, depends_on: string[], runner_profile = 'claude-code'): WorkflowStep {
  return { step_id, runner_profile, resource_class: 'standard', depends_on, input_from: {}, task_id: null }
}

/** plan -> (browse, shop, code): two browser steps and one that is not. */
function liveWorkflow(): { workflow: Workflow; taskById: Map<string, Task> } {
  const steps = [step('plan', []), step('browse', ['plan'], BROWSER), step('shop', ['plan'], BROWSER), step('code', ['plan'])]
  const made = bigWorkflow(steps, (s) => (s.step_id === 'plan' ? { state: 'SUCCEEDED', started_at: null, completed_at: null } : running(60)))
  for (const t of made.taskById.values()) {
    const s = steps.find((x) => x.step_id === t.step_id)!
    t.runner_profile = s.runner_profile
  }
  // `shop` is waiting for a person: needs-you, so the theater walks it first.
  const shop = made.taskById.get('task_shop')!
  shop.human_wait = { reason: 'sign in', expect: 'shop.example', requested_at: null, deadline: null, controller: null }
  return made
}

function draw(workflow: Workflow, taskById: Map<string, Task>) {
  return render(
    <WorkflowCard
      workflow={workflow}
      taskById={taskById}
      usage={{ kind: 'ready', usage: null }}
      openStages={{}}
      onToggleStage={() => undefined}
      reload={() => undefined}
    />,
  )
}

afterEach(() => {
  vi.useRealTimers()
})

describe('the layout reserves the inline card', () => {
  it('a live node is LIVE_CARD_H taller, at every tier', () => {
    const s = step('browse', ['plan'], BROWSER)
    for (const tier of ['figures', 'details', 'names'] as const) {
      expect(heightOf(s, tier, 255, false, false, true) - heightOf(s, tier, 255, false, false, false)).toBe(LIVE_CARD_H)
    }
  })

  it('layoutOf gives the live step’s level the room, and only that level', () => {
    const steps = [step('plan', []), step('browse', ['plan'], BROWSER)]
    const without = layoutOf(steps)
    const withCard = layoutOf(steps, undefined, 'figures', undefined, undefined, undefined, new Set(['browse']))
    const h = (l: typeof without, id: string) => l.nodes.find((n) => n.step.step_id === id)!.h
    expect(h(withCard, 'browse') - h(without, 'browse')).toBe(LIVE_CARD_H)
    expect(h(withCard, 'plan')).toBe(h(without, 'plan'))
    expect(withCard.height - without.height).toBe(LIVE_CARD_H)
  })
})

describe('the card is inline in the running browser step’s node', () => {
  it('draws in the node’s slot, outside its button, on running browser steps only', () => {
    const { workflow, taskById } = liveWorkflow()
    const { container } = draw(workflow, taskById)
    const card = (id: string) => {
      const button = container.querySelector(`.node[data-step="${id}"]`)!
      return { button, live: button.parentElement!.querySelector('.node-live') }
    }
    for (const id of ['browse', 'shop']) {
      const { button, live } = card(id)
      expect(live, id).not.toBeNull()
      expect(button.contains(live)).toBe(false)
      expect(live!.querySelector('[aria-label="Live browser"]')).not.toBeNull()
      expect(button.parentElement!.classList.contains('has-live')).toBe(true)
    }
    expect(card('code').live).toBeNull()
    expect(card('plan').live).toBeNull()
  })

  it('links the tab and the theater by the routed paths, with the workflow for prev and next', () => {
    const { workflow, taskById } = liveWorkflow()
    const { container } = draw(workflow, taskById)
    const live = container.querySelector('.node[data-step="browse"]')!.parentElement!.querySelector('.node-live')!
    const links = [...live.querySelectorAll('a')].map((a) => a.getAttribute('href'))
    expect(links).toEqual([
      '/agents/live/task_browse/browser',
      `/agents/live/task_browse/browser?theater=1&wf=${workflow.workflow_id}`,
    ])
  })

  it('the step details panel carries no second live view', () => {
    const { workflow, taskById } = liveWorkflow()
    const { container } = render(
      <WorkflowCard
        workflow={workflow}
        taskById={taskById}
        usage={{ kind: 'ready', usage: null }}
        openStages={{}}
        onToggleStage={() => undefined}
        reload={() => undefined}
        picked="browse"
        onPick={() => undefined}
      />,
    )
    const panel = container.querySelector('.wf-inspect')
    expect(panel).not.toBeNull()
    expect(panel!.querySelector('[aria-label="Live browser"]')).toBeNull()
    expect(container.querySelectorAll('[aria-label="Live browser"]')).toHaveLength(2)
  })
})

describe('the theater’s walk over this workflow', () => {
  it('workflowLiveAgents lists the running browser steps, labelled by step, needs-you marked', () => {
    const { workflow, taskById } = liveWorkflow()
    expect(workflowLiveAgents(workflow, taskById)).toEqual([
      { taskId: 'task_browse', label: 'browse', needsYou: false },
      { taskId: 'task_shop', label: 'shop', needsYou: true },
    ])
  })

  it('the paths are the ones LB-UX1 routes', () => {
    expect(browserTabPath('task_x')).toBe('/agents/live/task_x/browser')
    expect(theaterPath('task_x', null)).toBe('/agents/live/task_x/browser?theater=1')
    expect(theaterPath('task_x', 'wf_1')).toBe('/agents/live/task_x/browser?theater=1&wf=wf_1')
  })
})

/** A socket that closes the way `ticketedSocket` does, with `why`, once the card has wired it. */
function closing(why: unknown): { open: (id: string) => LiveSocket; opened: () => number } {
  let n = 0
  const open = () => {
    n += 1
    const sock: LiveSocket = {
      binaryType: 'arraybuffer',
      onmessage: null,
      onclose: null,
      onerror: null,
      send() {},
      close() {},
    }
    queueMicrotask(() => sock.onclose?.(why))
    return sock
  }
  return { open, opened: () => n }
}

describe('no session reads "no live view"', () => {
  it('a null session is "no live view", not "reconnecting", and spends no reconnect', async () => {
    vi.useFakeTimers()
    const s = closing(NO_LIVE_SESSION)
    render(<LiveThumb taskId="task_a" workflowId="wf_1" open={s.open} />)
    await act(async () => {
      await Promise.resolve()
    })
    expect(screen.getByText('no live view')).toBeTruthy()
    expect(screen.queryByText('reconnecting')).toBeNull()
    await act(async () => {
      vi.advanceTimersByTime(THUMB_RECONNECT_MS * 2)
    })
    expect(s.opened()).toBe(1)
    // The session is re-read slowly, so a browser that starts later is shown.
    await act(async () => {
      vi.advanceTimersByTime(THUMB_SESSION_RECHECK_MS)
    })
    expect(s.opened()).toBe(2)
    expect(screen.getByText('no live view')).toBeTruthy()
  })

  it('a session that exists and drops is retried quietly', async () => {
    vi.useFakeTimers()
    const s = closing(null)
    render(<LiveThumb taskId="task_a" workflowId="wf_1" open={s.open} />)
    await act(async () => {
      await Promise.resolve()
    })
    expect(screen.queryByText('no live view')).toBeNull()
    await act(async () => {
      vi.advanceTimersByTime(THUMB_RECONNECT_MS)
    })
    expect(s.opened()).toBe(2)
  })

  it('the ticketed socket reports a null session as NO_LIVE_SESSION and mints no ticket', async () => {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ live_browser: true, session: null }), { status: 200, headers: { 'content-type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    const mod = await import('../LiveThumb')
    const sock = mod.openLiveSocket('task_a')!
    const why = await new Promise((resolve) => (sock.onclose = resolve))
    expect(why).toBe(mod.NO_LIVE_SESSION)
    expect(fetchMock.mock.calls.map((c) => String((c as unknown[])[0]))).not.toContainEqual(expect.stringContaining('/live/ticket'))
    vi.unstubAllEnvs()
    vi.unstubAllGlobals()
    await waitFor(() => undefined)
  })
})
