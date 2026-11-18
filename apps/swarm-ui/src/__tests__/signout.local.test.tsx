/**
 * SIGN OUT, NOT BEHIND IAP: jsdom's default host is `localhost`, which is
 * `npm run dev` -- there is no IAP session to end, and a link to IAP's clear
 * URL would only reload the page. The control is not drawn, expanded or
 * collapsed. The IAP case is `signout.test.tsx`.
 *
 * MUTATION: drop the `iapFronted` check in Spine.tsx and both cases go red.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import { SkyShell } from '../Spine'

afterEach(() => window.localStorage.clear())

async function signedIn(): Promise<HTMLElement> {
  const { container } = render(
    <SkyShell section="overview" tab="now" title="Overview" go={() => {}} foot={null}>
      <p>page</p>
    </SkyShell>,
  )
  await waitFor(() => expect(container.querySelector('.sk-spine .sk-av')?.getAttribute('title')).toBe('alice@example.com'))
  return container
}

describe('Sign out on a local host', () => {
  it('is not drawn in the expanded panel', async () => {
    expect(window.location.hostname).toBe('localhost')
    const c = await signedIn()
    expect(c.querySelector('.sk-pfoot .sk-pfrow')).not.toBeNull()
    expect(c.querySelector('button.sk-out')).toBeNull()
  })

  it('is not drawn on the collapsed spine', async () => {
    window.localStorage.setItem('swarm.shell.collapsed', '1')
    const c = await signedIn()
    expect(c.querySelector('.sk-app.is-collapsed')).not.toBeNull()
    expect(c.querySelector('button.sk-out')).toBeNull()
  })
})
