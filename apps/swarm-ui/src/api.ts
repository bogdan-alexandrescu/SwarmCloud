import type { Capacity } from './types'

// THE ONE RULE THIS FILE EXISTS TO ENFORCE
// ----------------------------------------
// A failed request must never be representable as empty data.
//
// That is not a general principle here, it is this platform's defining bug. On
// 2026-09-18 a sweep of its operational scripts confirmed 56 places where a
// probe failure was rendered as an absence: `status.sh` printed "no swarm
// services deployed" when a session had expired, and finished on "nothing needs
// attention". The same day a build wrote a manifest listing zero images and
// reported "ok built 6 image(s)".
//
// So `load` returns a discriminated union and there is no way to reach the rows
// without saying what you will do when there are none AND what you will do when
// the answer never arrived. A component cannot accidentally render `[]` for a
// 403, because a 403 never produces an array.

export type Loaded<T> =
  | { status: 'loading' }
  | { status: 'ok'; data: T; fetchedAt: Date }
  /**
   * The request did not produce an answer. `detail` is for the person reading
   * the screen, and must say what failed rather than what it implies -- "could
   * not read capacity" not "no capacity configured".
   */
  | { status: 'failed'; detail: string; hint?: string }

/** Fixtures, so the UI can be worked on before DNS resolves. */
const USE_FIXTURES = import.meta.env.DEV && !import.meta.env.VITE_LIVE

async function request<T>(path: string): Promise<Loaded<T>> {
  try {
    const res = await fetch(path, {
      headers: { accept: 'application/json' },
      // Behind IAP the browser already holds the session cookie; nothing here
      // handles tokens, and nothing here should.
      credentials: 'same-origin',
    })

    if (res.status === 401 || res.status === 403) {
      return {
        status: 'failed',
        detail: `The API refused this request (HTTP ${res.status}).`,
        hint: 'Your IAP session may have expired. Reloading the page will re-authenticate.',
      }
    }
    if (!res.ok) {
      let body = ''
      try {
        body = (await res.text()).slice(0, 200)
      } catch {
        // A body we cannot read is not a body that changes the diagnosis.
      }
      return {
        status: 'failed',
        detail: `The API returned HTTP ${res.status}.`,
        hint: body || undefined,
      }
    }
    return { status: 'ok', data: (await res.json()) as T, fetchedAt: new Date() }
  } catch (err) {
    // Network failure, DNS, CORS, an aborted request. Emphatically NOT "no data".
    return {
      status: 'failed',
      detail: 'Could not reach the API.',
      hint: err instanceof Error ? err.message : undefined,
    }
  }
}

export async function loadCapacity(): Promise<Loaded<Capacity>> {
  if (USE_FIXTURES) return fixtureCapacity()
  return request<Capacity>('/v1/capacity')
}

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------
// These are the REAL pool names and the REAL live values read from the dev
// platform on 2026-09-19, not invented ones -- including tenant:u-bogdan at 40
// against a configured 2, which is the out-of-band drift `make pool-check`
// found. A fixture that shows a tidy platform teaches the wrong thing about
// what this screen is for.

async function fixtureCapacity(): Promise<Loaded<Capacity>> {
  await new Promise((r) => setTimeout(r, 400))
  const pool = (
    name: string,
    hard: number,
    active: number,
    extra: Partial<Capacity['pools'][number]> = {},
  ) => ({
    name,
    hard_limit: hard,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: hard,
    active,
    available: Math.max(0, hard - active),
    enabled: true,
    updated_at: new Date().toISOString(),
    ...extra,
  })

  return {
    status: 'ok',
    fetchedAt: new Date(),
    data: {
      generated_at: new Date().toISOString(),
      runner_profiles: {},
      pools: [
        pool('global', 20, 5),
        pool('tenant:u-bogdan', 40, 5),
        pool('provider:anthropic', 10, 5),
        pool('provider:anthropic:tenant:u-bogdan', 5, 5),
        pool('provider:anthropic:tenant:eng', 5, 0),
        pool('provider:openai', 10, 0),
        pool('provider:openai:tenant:eng', 5, 0),
        pool('runner:claude-code', 10, 5),
        pool('runner:mock', 20, 0),
        pool('runner:browser', 4, 0),
        pool('runner:codex', 10, 0),
        pool('runner:generic', 10, 0),
        pool('resource:standard', 20, 5),
        pool('resource:browser', 4, 0),
        pool('resource:large', 2, 0),
        pool('backend:CLOUD_RUN_JOB', 20, 5),
        pool('backend:GKE_AUTOPILOT', 4, 0),
        pool('tenant:smoke', 2, 0),
        // Paused, and at a reduced ceiling: the two states this screen must
        // make obvious at a glance.
        pool('tenant:eng', 10, 0, { enabled: false, adaptive_target: 6, effective_limit: 6 }),
      ],
    },
  }
}
