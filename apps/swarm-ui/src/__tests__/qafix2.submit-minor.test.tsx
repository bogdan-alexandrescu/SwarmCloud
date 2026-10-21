/**
 * VISUAL QA RUN 2, LANE QAFIX2-SUBMIT-MINOR (2026-10-20; part of #1680).
 *
 *   work-b-007  In light mode the workflow form's "repository url (optional)"
 *               field had a tinted fill (`--surface-2`) and the browser's own
 *               placeholder ink, so the example URL read as an entered value.
 *               The same field on Submit a task looked white with a grey
 *               placeholder only because that step is dimmed while no runner
 *               is chosen; the workflow form never dims it, so it showed the
 *               field as drawn. It is drawn as every other text field now: the
 *               `--surface` fill of `.c-inp`, and a placeholder in a muted
 *               token that is not the value's ink.
 */
import { render } from '@testing-library/react'
import { createElement } from 'react'
import { describe, expect, it } from 'vitest'

import { DispatchChoice } from '../Dispatch'
import type { CascadeEnv } from './cssgate'
import { THEMES, painted, resolveColour, sameColour } from './marks'
import { contrast } from './spaceprobe'

function workflowRepoField(): HTMLInputElement {
  const { container } = render(
    createElement(DispatchChoice, {
      draft: { strategy: 'collect', carrier: 'checkpoints', repositoryUrl: '' },
      onChange: () => {},
      steps: 2,
      scale: 'workflow',
      terminals: ['report'],
    }),
  )
  return container.querySelector<HTMLInputElement>('#dsp-repo')!
}

describe('work-b-007: the workflow form\'s repository field reads as an empty field', () => {
  it('is an empty, optional field with an example in its placeholder', () => {
    const field = workflowRepoField()
    expect(field.value).toBe('')
    expect(field.placeholder).toBe('https://github.com/owner/repo.git')
  })

  for (const theme of THEMES) {
    const env: CascadeEnv = { width: 1440, theme }

    it(`fills it as every other text field is filled, in ${theme}`, () => {
      const field = workflowRepoField()
      const fill = painted(field, ['background', 'background-color'], env)
      expect(fill).not.toBeNull()
      // MUTATION: put `background: var(--surface-2)` back on `.dsp-repo` and
      // this is the tinted #eef4fa in light, not the field white.
      expect(sameColour(resolveColour(fill!, theme), resolveColour('var(--surface)', theme))).toBe(true)
    })

    it(`draws its placeholder in a muted ink, not the value's, and legibly, in ${theme}`, () => {
      const field = workflowRepoField()
      const ink = painted(field, 'color', env, 'placeholder')
      // MUTATION: drop the `.dsp-repo::placeholder` rule and this is null --
      // the browser's own ink, which the fixture build drew near-black.
      expect(ink).not.toBeNull()
      const placeholder = resolveColour(ink!, theme)
      // MUTATION: paint the placeholder `var(--text)` and the example reads
      // as an entered value again.
      expect(sameColour(placeholder, resolveColour('var(--text)', theme))).toBe(false)
      // MUTATION: paint it `var(--line)` and the instruction is unreadable.
      expect(contrast(placeholder, resolveColour('var(--surface)', theme))).toBeGreaterThanOrEqual(4.5)
    })
  }
})
