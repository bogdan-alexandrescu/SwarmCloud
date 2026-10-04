/**
 * WALKTHROUGH G (owner, 2026-10-03, live console at 1440x900): human names.
 *
 * Measured: an agent with no step name showed only a hash ("02e9705a ·
 * browser · swarm-verify"); the Timeline's lane labels led with the task id
 * and the "cancel asked 21:16" tag overlapped its bar.
 *
 * Now an agent is named from what it is -- its step, else "browser check ·
 * 02e9705a" or "<profile> task · <short id>" -- by one rule (`agentName`) in
 * the agent list, Overview, the Timeline and the breadcrumb. A lane label
 * leads with the name, the state mark and the duration ("implement ✓ · 1h
 * 18m"), the task id is its tooltip, and the cancel tag sits above the bar.
 *
 * MUTATIONS: return `task.id` from `agentName` again, put `lane.taskId`
 * back at the head of the note, or put the tag back at `top: -15px` -- each
 * turns a case red.
 */
import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { App } from '../App'
import { agentName } from '../agentlist'
import type { CascadeEnv } from './cssgate'
import { painted, TABLES } from './marks'
import { resolveVars } from './spaceprobe'
import { task } from './runfixture'

const WIDE: CascadeEnv = { width: 1440 }
const WAIT = { timeout: 8000 }

afterEach(() => {
  window.location.hash = ''
  window.history.replaceState(null, '', '/')
})

describe('G: an agent is named from what it is', () => {
  it('names a step by its step, a lone browser task a browser check, and any other lone task by its profile', () => {
    expect(agentName(task({ id: 'task_02e9705a1c2d', runner_profile: 'browser', step_id: null }))).toBe('browser check · 02e9705a')
    expect(agentName(task({ id: 'task_a073aff5', runner_profile: 'claude-code', step_id: null }))).toBe('claude-code task · a073aff5')
    expect(agentName(task({ id: 'task_a073aff5', runner_profile: 'codex', step_id: 'implement' }))).toBe('implement')
  })

  it('draws that name, not the hash, on the agent list', async () => {
    window.location.hash = '#work/running/recent'
    render(<App />)
    const row = await waitFor(() => {
      const r = document.querySelector<HTMLElement>('.row.is-compact[data-task-id="task_108ef29c"]')
      expect(r).not.toBeNull()
      return r!
    }, WAIT)
    expect(row.querySelector('.cr-name b')?.textContent).toBe('mock task · 108ef29c')
    expect(row.querySelector('.cr-name b')?.getAttribute('title')).toBe('task_108ef29c')
  })

  it('draws that name as the open agent’s breadcrumb', async () => {
    window.location.hash = '#work/task/task_a073aff5'
    render(<App />)
    await waitFor(() => {
      expect(document.querySelector('.ctl-crumb [aria-current="page"]')?.textContent).toBe('claude-code task · a073aff5')
    }, WAIT)
  })
})

describe('G: a Timeline lane leads with its name, state and duration', () => {
  // The label itself -- name, mark, duration, the id in the tooltip -- is
  // asserted against the lanes screen's own fixture in
  // timeline.lanes.screen.test.tsx ("G: a lane label leads with …"): the
  // development fixture behind <App /> serves no attempts page.

  it('sets the cancel tag above the bar, inside the track', () => {
    const host = document.createElement('div')
    host.innerHTML =
      '<div class="tl-row"><div class="tl-lab"></div><div class="tl-trk"><span class="tl-seg is-hold"></span>' +
      '<span class="tl-mkr is-warn"><span class="tl-tag">cancel asked 21:16</span></span></div></div>'
    document.body.appendChild(host)
    try {
      const px = (v: string | null) => parseFloat(v ?? 'NaN')
      const trk = px(painted(host.querySelector('.tl-trk')!, 'height', WIDE))
      const bar = px(painted(host.querySelector('.tl-seg')!, 'height', WIDE))
      const mkr = px(painted(host.querySelector('.tl-mkr')!, 'height', WIDE))
      const tag = host.querySelector('.tl-tag')!
      // The bar and the mark are centred on the track.
      const barTop = (trk - bar) / 2
      const mkrTop = (trk - mkr) / 2
      expect(painted(tag, 'top', WIDE) ?? 'auto', 'the tag is hung from the mark’s top').toBe('auto')
      // `bottom: calc(100% + Kpx)` hangs the tag's foot K px above the mark's top.
      const k = /^calc\(100% \+ ([\d.]+)px\)$/.exec(painted(tag, 'bottom', WIDE) ?? '')
      expect(k, 'the tag is not hung above the mark').not.toBeNull()
      const tagBottom = mkrTop - Number(k![1])
      const lh = px(resolveVars(painted(tag, 'line-height', WIDE) ?? 'NaN', TABLES.dark))
      const size = 12
      const tagTop = tagBottom - (lh > 3 ? lh : lh * size)
      expect(tagBottom, 'the tag overlaps the bar').toBeLessThanOrEqual(barTop)
      expect(tagTop, 'the tag is cut by the top of the track').toBeGreaterThanOrEqual(0)
    } finally {
      host.remove()
    }
  })
})
