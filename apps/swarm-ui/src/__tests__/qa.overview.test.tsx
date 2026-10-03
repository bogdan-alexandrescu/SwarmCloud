/**
 * VISUAL QA Q5 (owner, 2026-10-02): Overview › Needs a look. The check card's
 * first line was the whole headline in bold, wrapping whole workflow ids ("21
 * failed tasks among the 200 most recent · newest 42m ago · 1 in workflow
 * wf_…, 1 in wf_… +6 workflows"). Per overview.html O1 a check card is a short
 * title, two lines of regular text and a link, with the ids in the second
 * line, cut. MUTATION: draw `p.headline` as the `<b>` again, or let the title
 * or the ids line wrap.
 */
import { render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import * as Overview from '../Overview'
import type { Problem } from '../checks'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WIDE: CascadeEnv = { width: 1440 }
const HEADLINE = '21 failed tasks among the 200 most recent · newest 42m ago · 1 in workflow wf_422ecd715f19465c8283, 1 in wf_618742792c2443a3afaf +6 workflows'

afterEach(() => {
  document.body.innerHTML = ''
})

describe('Q5: a check card is a short title, two lines and a link', () => {
  it('splits the headline at its first clause', () => {
    expect(Overview.splitHeadline(HEADLINE)).toEqual({
      title: '21 failed tasks among the 200 most recent',
      ids: 'newest 42m ago · 1 in workflow wf_422ecd715f19465c8283, 1 in wf_618742792c2443a3afaf +6 workflows',
    })
    expect(Overview.splitHeadline('2 workers silent past the grace period: tsk_a, tsk_b').title).toBe('2 workers silent past the grace period')
    expect(Overview.splitHeadline('Dispatch is paused platform-wide')).toEqual({ title: 'Dispatch is paused platform-wide', ids: null })
  })

  it('draws the title on one line, the ids on the next, cut, and keeps the whole headline as its name', () => {
    const p: Problem = { severity: 'bad', n: 21, headline: HEADLINE, detail: 'All still have attempts left and may retry.', href: '#work/running/recent/failed', linkLabel: 'failed agents' }
    const { container } = render(<Overview.CheckCard problem={p} />)
    const b = container.querySelector('.ov-att-t b')!
    expect(b.textContent).toBe('21 failed tasks among the 200 most recent')
    expect(b.textContent).not.toMatch(/wf_/)
    const ids = container.querySelector<HTMLElement>('.ov-att-ids')!
    expect(ids.textContent).toMatch(/wf_422ecd715f19465c8283/)
    expect(ids.getAttribute('title')).toBe(ids.textContent)
    expect(container.querySelector('a')!.getAttribute('aria-label')).toBe(`${HEADLINE}. ${p.detail}`)
    for (const el of [b, ids]) {
      expect(painted(el, 'white-space', WIDE), el.className || el.tagName).toBe('nowrap')
      expect(painted(el, 'text-overflow', WIDE), el.className || el.tagName).toBe('ellipsis')
    }
    // Regular-size title, not a heading-sized run of bold.
    expect(painted(b, ['font-size', 'font'], WIDE)).toMatch(/--t-meta/)
  })
})
