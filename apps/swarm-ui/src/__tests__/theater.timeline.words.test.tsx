// The theater's timeline says what people and the agent did (lane LB-V3, part of #1030).
//
// MEASURED 2026-10-12 05:48 PDT (visual QA pass 5): a person took control,
// switched to iPhone 15, rotated to landscape and returned control, and the
// Timeline read only "05:48:09 A person took control" and "05:48:09 refused".
// swarm-api now serves every step (tests/unit/control_plane/test_live_timeline.py);
// this file holds what the column SAYS for each: who ("You" for the reader),
// the device and orientation, the toggle, the return, the hand-off, and for a
// refusal what was refused and why, in words -- never a bare event name.

import { render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { TheaterTimeline, actionWords, type LiveAction, type LiveActions } from '../TheaterTimeline'

const FIXTURES = join(__dirname, '..', '..', '..', '..', 'tests', 'fixtures', 'live')
const ACTIONS = JSON.parse(readFileSync(join(FIXTURES, 'actions.json'), 'utf8')) as LiveActions

function serve(body: unknown): void {
  globalThis.fetch = vi.fn(
    async () => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } }),
  ) as unknown as typeof fetch
}

afterEach(() => {
  vi.restoreAllMocks()
})

const AT = '2026-10-12T12:48:09+00:00'
const line = (over: Partial<LiveAction>): LiveAction => ({ at: AT, kind: 'control', ...over })

describe('the timeline in words', () => {
  it('draws the B6 sequence, each step on its own line, with the time', async () => {
    serve(ACTIONS)
    render(<TheaterTimeline taskId="task_live" live={false} />)
    const list = await screen.findByRole('list', { name: 'Agent actions' })
    const items = [...list.querySelectorAll('li')].map((li) => li.querySelector('.th-act-what')!.textContent)
    expect(items).toEqual([
      'The agent asked for help',
      'The agent went to',
      'Agent input: 12 characters typed',
      'A navigation was refused: the live gate does not let the agent do that',
      'You took control',
      'Taking control was refused: the page is not on a secure https connection',
      'You switched the device to iPhone 15, portrait, touch on',
      'You rotated the iPhone 15 to landscape',
      'You returned control to the agent',
      'Hand-off resolved: control is back with the agent',
    ])
    for (const li of list.querySelectorAll('li')) expect(li.querySelector('time')!.getAttribute('dateTime')).toBeTruthy()
    // A bare event name is what B6 measured; none is drawn.
    expect(items).not.toContain('refused')
    expect(list.querySelectorAll('.is-refused')).toHaveLength(2)
  })

  it('names another person, and says "You" only for the reader', () => {
    expect(actionWords(line({ event: 'control_taken', by: 'dave@example.com', you: false }))).toBe('dave@example.com took control')
    expect(actionWords(line({ event: 'control_taken', by: 'dave@example.com', you: true }))).toBe('You took control')
    expect(actionWords(line({ event: 'control_taken' }))).toBe('A person took control')
  })

  it('says what changed on the device', () => {
    expect(actionWords(line({ event: 'touch_toggled', by: 'a@b', you: true, preset: 'iphone-15', touch: false }))).toBe('You turned touch off')
    expect(actionWords(line({ event: 'device_changed', by: 'a@b', you: true, preset: 'desktop', orientation: 'landscape', touch: false }))).toBe(
      'You switched the device to Desktop, landscape',
    )
    expect(actionWords(line({ event: 'device_changed', preset: 'pixel-8', orientation: 'portrait', touch: false }))).toBe(
      'A person switched the device to Pixel 8, portrait, touch off',
    )
    expect(actionWords(line({ event: 'device_refused', cause: 'no_touch' }))).toBe('A device change was refused: that device has no touch screen')
  })

  it('says why control ended, and how a hand-off ended', () => {
    expect(actionWords(line({ event: 'control_ended', by: 'a@b', you: true, cause: 'idle' }))).toBe(
      'Your control ended: nobody used the browser for too long',
    )
    expect(actionWords(line({ event: 'ended', cause: 'page_closed' }))).toBe('The browser ended control: the page closed')
    // LB-RETURN: an expired hand-off PARKS, and a person's I'm ready resumes it -- it is not over.
    expect(actionWords(line({ event: 'handoff_resolved', outcome: 'expired' }))).toBe(
      'Hand-off parked: nobody took control in time · press I’m ready to resume',
    )
    expect(actionWords(line({ event: 'control_opened_to_members', by: 'a@b', you: true }))).toBe('You let any member take control')
  })

  it('a refusal with a cause this build does not know still says what and why', () => {
    expect(actionWords(line({ event: 'refused', cause: 'some_new_reason' }))).toBe('Taking control was refused: some new reason')
    expect(actionWords(line({ event: 'refused' }))).toBe('Taking control was refused: no reason was recorded')
    expect(actionWords({ at: AT, kind: 'cdp', method: 'Runtime.evaluate', outcome: 'refused', cause: 'not_its_session' })).toBe(
      'The agent’s Runtime.evaluate was refused: it named a browser session the agent does not own',
    )
    // LB-R2: a navigation says what it is; its URL is the line under it.
    expect(actionWords({ at: AT, kind: 'cdp', method: 'Page.navigate' })).toBe('The agent went to')
  })
})
