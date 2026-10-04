/**
 * BROWSER QA U10b (owner, 2026-10-04; live console at 1440x900 and 390):
 * Submit.
 *
 *   D23  /submit/issue after Read: the summary card wrapped the reference at
 *        the owner's hyphen ("bogdan- / alexandrescu/…"), and the body preview
 *        printed raw markdown ("### What are you trying to do?"). The
 *        reference is one line, cut, whole in its title; the body is the
 *        console's own safe markdown (React elements, never HTML).
 *   D34  "+ Add setting": each item laid its key and its note out as two
 *        squeezed columns, and Escape did not close the menu while focus was
 *        on its button. An item is the key over its note; Escape closes from
 *        the button or the list and gives focus back to the button.
 *   D35  The summary card with the send button scrolled out of view: its
 *        column was only as tall as the card, so `sticky` had nowhere to go.
 *        The column spans the form's row on a wide screen.
 *
 * MUTATIONS: drop the ellipsis from `.in-send code`, render the body as text
 * again, put the item back to `justify-content: space-between`, move the
 * Escape handler back onto the list, or drop `align-self: stretch` from the
 * side column -- each turns a case red.
 */
import { fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { AddSetting } from '../Submit'
import { IssuePreviewCard } from '../IssueSubmit'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WIDE: CascadeEnv = { width: 1440 }

afterEach(() => {
  document.body.innerHTML = ''
})

const BODY = '### What are you trying to do?\nSee what a finished workflow cost.\n\n- one\n- two\n\n```\nmake test\n```'

function read() {
  return {
    issue: {
      ref: 'bogdan-alexandrescu/SwarmCloud#454', owner: 'bogdan-alexandrescu', repo: 'SwarmCloud', number: 454,
      url: 'https://github.com/bogdan-alexandrescu/SwarmCloud/issues/454',
      repository_url: 'https://github.com/bogdan-alexandrescu/SwarmCloud',
      title: 'The workflow list shows no cost column', body: BODY,
      body_truncated: false, body_redacted: false, labels: [], state: 'open', comments: 0,
    },
    tenant_id: 'eng',
  }
}

describe('D23: the issue as read', () => {
  it('renders the body as safe markdown, never its markers', () => {
    const { container } = render(<IssuePreviewCard read={read() as never} at={Date.now()} closedOk={false} onPlanAnyway={() => {}} />)
    const body = container.querySelector('.in-body')!
    expect(body.textContent).not.toContain('###')
    expect(body.textContent).not.toContain('```')
    expect(body.querySelector('.art-h')?.textContent).toBe('What are you trying to do?')
    expect(body.querySelectorAll('li').length).toBe(2)
    expect(body.querySelector('pre, code')?.textContent).toContain('make test')
    expect(body.innerHTML).not.toMatch(/<script/)
    // The preview is not a pre-wrap block any more: markdown lays out its own lines.
    expect(painted(body, 'white-space', WIDE) ?? 'normal').not.toBe('pre-wrap')
  })

  it('keeps the reference in the summary card on one line, cut, with its title', () => {
    document.body.innerHTML =
      '<aside class="sbf-side"><div class="sbf-send in-send"><ul class="ctl-facts"><li class="ctl-fact"><b>issue</b>' +
      '<span><code class="in-ref-code" title="bogdan-alexandrescu/SwarmCloud#454">bogdan-alexandrescu/SwarmCloud#454</code> · open</span></li></ul></div></aside>'
    const code = document.querySelector('code')!
    expect(painted(code, 'white-space', WIDE)).toBe('nowrap')
    expect(painted(code, 'text-overflow', WIDE)).toBe('ellipsis')
    expect(painted(code, 'overflow', WIDE)).toBe('hidden')
  })
})

describe('D34: + Add setting', () => {
  const offers = [{ name: 'model', note: 'which model the runner asks the provider for; the runner default when unset' }] as never

  it('draws each item as the key over its note, not two squeezed columns', () => {
    const { container } = render(<AddSetting idPrefix="t" offers={offers} onAdd={() => {}} onOwn={() => {}} />)
    fireEvent.click(container.querySelector('.sbf-addset-btn')!)
    const item = container.querySelector('.sbf-addset-item')!
    expect(painted(item, 'flex-direction', WIDE)).toBe('column')
    expect(painted(item.querySelector('em')!, 'text-align', WIDE)).toBe('left')
  })

  it('closes on Escape from the button, and gives focus back to it', () => {
    const { container } = render(<AddSetting idPrefix="t" offers={offers} onAdd={() => {}} onOwn={() => {}} />)
    const button = container.querySelector<HTMLButtonElement>('.sbf-addset-btn')!
    fireEvent.click(button)
    expect(container.querySelector('.sbf-addset-list')).not.toBeNull()
    fireEvent.keyDown(button, { key: 'Escape' })
    expect(container.querySelector('.sbf-addset-list')).toBeNull()
    expect(document.activeElement).toBe(button)
    // And from inside the list.
    fireEvent.click(button)
    fireEvent.keyDown(container.querySelector('.sbf-addset-item')!, { key: 'Escape' })
    expect(container.querySelector('.sbf-addset-list')).toBeNull()
  })
})

describe('D35: the send card stays in view on a wide screen', () => {
  it('stretches the side column to the form’s row, so the sticky card has room to stick', () => {
    document.body.innerHTML = '<form class="sbf"><div class="sbf-build"></div><aside class="sbf-side"><div class="sbf-send"></div></aside></form>'
    const side = document.querySelector('.sbf-side')!
    const send = document.querySelector('.sbf-send')!
    expect(painted(send, 'position', WIDE)).toBe('sticky')
    expect(painted(side, 'align-self', WIDE)).toBe('stretch')
    // A phone stacks the two and the card is in the flow.
    expect(painted(send, 'position', { width: 390 }) ?? 'static').toBe('static')
  })
})
