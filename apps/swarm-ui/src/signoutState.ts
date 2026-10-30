/**
 * SIGN OUT (owner request 2026-10-14): end this browser's IAP session so
 * another Google account can sign in. Never a Google sign-out: the person stays
 * signed in to Gmail, Drive and every other Google tab.
 *
 * THE URL IS GOOGLE'S DOCUMENTED ONE, NOT `/_gcp_iap/clear_login_cookie`.
 * docs/web-ui/01-reachability.md §3 recorded that path as unverified and said to
 * confirm it against IAP's docs before shipping. IAP's "Using query parameters
 * and headers" page (cloud.google.com/iap/docs/special-urls-howto, read
 * 2026-10-14) documents `?gcp-iap-mode=CLEAR_LOGIN_COOKIE`: it "clears all the
 * IAP-issued cookies for your app and navigates the browser to YOUR_APP_URL".
 * The `/_gcp_iap/` path appears on no current IAP page, so it is not used.
 *
 * WHAT HAPPENS NEXT IS GOOGLE'S, and the same page says it: "a silent sign-in
 * might happen when there is only one account in use with the IdP. If there are
 * multiple accounts in use, an account selection page opens". This deployment
 * cannot ask for the chooser instead -- `prompt=select_account` is a parameter
 * of the OAuth request, and IAP builds that request with a Google-managed client
 * (terraform/modules/frontend/main.tf, the `iap {}` blocks carry no
 * oauth2_client_id), so nothing here can add to it. So the console notices the
 * silent case on the way back in -- the same email arrived -- and offers Google's
 * account chooser, which adds an account without signing out of the first
 * (`returnedAs`, Spine.tsx `SignedBackIn`).
 */
import { TENANT_PREF, clearTenantSwitch, forgetProbes, forgetReads } from './fetch'
import { VIEW_KEY as DIFF_VIEW_KEY } from './diff/storage'
import { LIST_SNAP_PREF } from './listSnap'
import { DOCK, INSPECTOR } from './panes'
import { THEME_KEY } from './theme'

/** IAP's documented clear-the-session URL, on this origin. */
export const SIGNOUT_PATH = '/?gcp-iap-mode=CLEAR_LOGIN_COOKIE'

/**
 * Who signed out, in this TAB's sessionStorage, which survives the round trip
 * through Google and back on the same origin and nothing longer. Written after
 * the clear, so it is the one thing of ours left.
 */
export const SIGNED_OUT_KEY = 'swarm.signout.from'

/**
 * PER-USER: everything that says who this person is or what they were doing.
 * Cleared on sign-out. A key ending in `:<tenant>` (recent and newest
 * workflows under a non-default tenant, Spine.tsx `recentKey`) is cleared with
 * its base.
 *
 * Written out rather than imported for the keys Spine.tsx, Activity.tsx and
 * RepoGraph.tsx declare: importing Spine.tsx from here, and this from
 * Spine.tsx, is a cycle, and the other two would pull whole screens (and
 * RepoGraph's network drawing) into the shell's bundle.
 * `signout.test.tsx` scans every source file for a declared `swarm.` key and
 * fails on one that is in neither list, so a renamed key cannot slip out.
 */
export const USER_KEYS: readonly string[] = [
  TENANT_PREF, // the tenant switcher's choice
  'swarm.tenant.default', // Spine.tsx DEFAULT_TENANT_KEY
  'swarm.tenants.names', // Spine.tsx TENANT_NAMES_STORE: the tenants this person could see
  'swarm.workflows.recent', // Spine.tsx RECENT_WORKFLOWS_KEY
  'swarm.workflows.newest', // Spine.tsx NEWEST_WORKFLOWS_KEY
  'swarm.timeline.view', // Activity.tsx VIEW_KEY: a saved filter query
  'swarm.access.owner', // Access.tsx CHOSEN_OWNER_KEY: the phone chooser's owner (sessionStorage)
  'swarm.people.workspace', // People.tsx PEOPLE_WORKSPACE_KEY: the workspace this administrator chose
  'swarm.timezone', // timezone.ts ZONE_KEY, as `swarm.timezone:<email>`: the zone this person picked in Profile
  'swarm.setup-link.pool', // SetupLink.tsx PENDING_KEY: this tab's redeemed pool link (sessionStorage)
  SIGNED_OUT_KEY,
]

/**
 * PER-BROWSER: how this screen is laid out and painted. Kept, deliberately:
 * the next person at this browser gets the same theme and pane sizes the
 * machine had, and none of these says anything about who used it.
 */
export const BROWSER_KEYS: readonly string[] = [
  THEME_KEY,
  'swarm.shell.collapsed', // Spine.tsx COLLAPSED_KEY
  INSPECTOR.key,
  DOCK.key,
  LIST_SNAP_PREF,
  'swarm.repograph.drawing', // RepoGraph.tsx DRAWING_KEY
  DIFF_VIEW_KEY,
  'swarm.accounts.view', // accountsLanes.ts VIEW_KEY: Table or Timeline
  'swarm.schedule.history.view', // ScheduleHistoryTimeline.tsx HISTORY_VIEW_KEY: Timeline or Table
  'swarm.config.setupSteps', // ConfigSetup.tsx SHOW_STEPS_KEY: a finished setup strip unfolded
  'swarm.live.logsSplit', // LiveBrowser.tsx SPLIT_KEY: the logs column's share of the Browser tab
]

function ours(key: string): boolean {
  return USER_KEYS.some((k) => key === k || key.startsWith(`${k}:`))
}

/** `localStorage`/`sessionStorage`, or null where touching them throws. */
function store(which: 'localStorage' | 'sessionStorage'): Storage | null {
  try {
    return globalThis[which] ?? null
  } catch {
    return null
  }
}

/**
 * Remove this console's per-user keys from `local`, and every `swarm.` key from
 * `session` (nothing of ours there is a display preference). Never another
 * app's key: the console shares an origin with nothing, but a key it did not
 * write is not its to delete. Returns what it removed. Storage that refuses
 * removes nothing and does not throw.
 */
export function clearUserState(local: Storage | null = store('localStorage'), session: Storage | null = store('sessionStorage')): string[] {
  const removed: string[] = []
  const sweep = (s: Storage | null, match: (k: string) => boolean) => {
    if (s === null) return
    try {
      const keys: string[] = []
      for (let i = 0; i < s.length; i += 1) {
        const k = s.key(i)
        if (k !== null && match(k)) keys.push(k)
      }
      for (const k of keys) {
        s.removeItem(k)
        removed.push(k)
      }
    } catch {
      /* refused: nothing of ours was readable, so nothing is left to leak */
    }
  }
  sweep(local, ours)
  sweep(session, (k) => k.startsWith('swarm.'))
  return removed
}

/** The navigation, behind a seam so a test can see it without leaving jsdom. */
export const leave = {
  to(url: string): void {
    window.location.assign(url)
  },
}

/**
 * SIGNING OUT, for the rest of this page's life. Clearing the tenant choice
 * drops the shell's `tenant` to null while it still holds the old `me`, and
 * its effect would learn the default tenant straight back into the storage
 * just cleared (Spine.tsx `learnDefaultTenant`). The page is leaving; nothing
 * per-user is written again before it goes.
 */
let leaving = false

/** Whether this page has signed out and is on its way to IAP's clear URL. */
export function signingOut(): boolean {
  return leaving
}

/**
 * Sign out: forget this person's state, note who left, then let IAP clear its
 * cookie. No confirmation -- signing back in is one Google page -- and no
 * keyboard shortcut, so it only ever happens on a deliberate click.
 */
export function signOut(email: string | null): void {
  leaving = true
  clearUserState()
  clearTenantSwitch()
  forgetReads()
  forgetProbes()
  if (email !== null && email !== '') {
    try {
      store('sessionStorage')?.setItem(SIGNED_OUT_KEY, email)
    } catch {
      /* no return-trip notice; the sign-out itself still happens */
    }
  }
  leave.to(SIGNOUT_PATH)
}

/**
 * Google's account chooser, coming back through IAP's clear URL. It lists the
 * accounts signed in to this browser and offers "Use another account", which
 * ADDS one -- nobody is signed out of Google. With two accounts in use, IAP's
 * own sign-in then shows its chooser too (see the header).
 */
export function accountChooserUrl(origin: string): string {
  const back = `${origin.replace(/\/+$/, '')}${SIGNOUT_PATH}`
  return `https://accounts.google.com/AccountChooser?continue=${encodeURIComponent(back)}`
}

/**
 * Who came back after a sign-out in this tab: `same` when IAP signed the same
 * account straight back in (the silent case), `other` when a different one
 * arrived -- the sign-out worked, and the note is dropped -- or null when this
 * tab did not sign out.
 */
export function returnedAs(email: string): 'same' | 'other' | null {
  const s = store('sessionStorage')
  let from: string | null = null
  try {
    from = s?.getItem(SIGNED_OUT_KEY) ?? null
  } catch {
    return null
  }
  if (from === null) return null
  if (from.toLowerCase() === email.toLowerCase()) return 'same'
  forgetSignOut()
  return 'other'
}

/** The person chose to stay as who they are: stop asking. */
export function forgetSignOut(): void {
  try {
    store('sessionStorage')?.removeItem(SIGNED_OUT_KEY)
  } catch {
    /* nothing to forget */
  }
}

const LOCAL_HOSTS = ['localhost', '127.0.0.1', '0.0.0.0', '::1', '[::1]']

/**
 * Whether this tab is plausibly behind IAP: a named host that is not this
 * machine. `npm run dev` (localhost) and a dev server reached by a bare
 * address have no IAP session to end, so the control is not drawn there; IAP
 * fronts a load balancer with a managed certificate, which is a domain name.
 */
export function iapFronted(host: string): boolean {
  const h = host.trim().toLowerCase()
  if (h === '' || LOCAL_HOSTS.includes(h) || h.endsWith('.local') || h.endsWith('.localhost')) return false
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(h) || h.startsWith('[')) return false
  return true
}
