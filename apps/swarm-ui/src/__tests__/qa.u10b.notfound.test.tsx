/**
 * BROWSER QA U10b (owner, 2026-10-04): an unknown route, e.g.
 * /capacity/quota, silently redirected to /overview -- a stale link landed on
 * a page that looked right and answered nothing. It is a not-found state now,
 * the address bar keeps what was asked for, and the page offers the nearest
 * route that exists (/capacity/accounts/quota) with Overview beside it.
 *
 * MUTATIONS: send a null `pathToAddress` back to `fromAddress('')`, or have
 * `nearestPath` return '/overview' always -- each turns a case red.
 */
import { render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { App } from '../App'
import { nearestPath } from '../NotFound'


afterEach(() => {
  window.history.replaceState(null, '', '/')
})

describe('nearestPath', () => {
  it('finds the route an unknown path most likely meant', () => {
    expect(nearestPath('/capacity/quota')).toBe('/capacity/accounts/quota')
    expect(nearestPath('/agnets')).toBe('/agents')
    expect(nearestPath('/capacity/holder')).toBe('/capacity/holders')
    expect(nearestPath('/admin/tenant')).toBe('/admin/tenants')
    expect(nearestPath('/workflow')).toBe('/workflows')
  })

  it('falls back to Overview when nothing is near', () => {
    expect(nearestPath('/zzzzzzzzzz')).toBe('/overview')
  })
})

describe('an unknown route is a not-found state, not a redirect', () => {
  it('says which address has no page, keeps it in the bar, and links the nearest route', async () => {
    window.history.replaceState(null, '', '/capacity/quota')
    render(<App />)
    const heading = await screen.findByRole('heading', { name: /No page at \/capacity\/quota/ })
    expect(heading).toBeTruthy()
    await waitFor(() => expect(window.location.pathname).toBe('/capacity/quota'))
    const page = within(document.querySelector<HTMLElement>('.nf-page')!)
    const near = page.getByRole('link', { name: '/capacity/accounts/quota' })
    expect(near.getAttribute('href')).toBe('/capacity/accounts/quota')
    expect(page.getByRole('link', { name: 'Overview' }).getAttribute('href')).toBe('/overview')
    // The nav is still there, so the reader can go anywhere.
    expect(document.querySelector('.sk-spine')).not.toBeNull()
    // The spine lights the section of the route it offers, not Overview.
    const lit = [...document.querySelectorAll('.sk-ri[data-sec][aria-current="page"]')].map((a) => a.getAttribute('data-sec'))
    expect(lit).toEqual(['capacity'])
  })
})
