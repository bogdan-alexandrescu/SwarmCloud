# swarm-ui

The SwarmCloud web UI. React + TypeScript + Vite, no component framework.

## Running it

```bash
npm install
npm run dev          # http://localhost:5173, fixtures, no network
```

Against the real API, once `swarm.saga.xyz` resolves:

```bash
SWARM_API_ORIGIN=https://swarm.saga.xyz VITE_LIVE=1 npm run dev
```

The dev server proxies `/v1` so the app talks to a same-origin API in
development exactly as it does in production behind the load balancer. The app
holds no tokens and knows nothing about auth: IAP sits in front and the browser
carries the session.

## The rule this codebase is built around

**A failed request must never be representable as empty data.**

That is not a general principle, it is this platform's defining bug. A sweep of
its operational scripts on 2026-09-18 confirmed 56 places where a probe failure
was rendered as an absence -- `status.sh` printed "no swarm services deployed"
when a session had expired and finished on "nothing needs attention".

So `api.ts` returns a discriminated union:

```ts
type Loaded<T> =
  | { status: 'loading' }
  | { status: 'ok'; data: T; fetchedAt: Date }
  | { status: 'failed'; detail: string; hint?: string }
```

There is no path to the rows that does not force a decision about both the
empty case and the never-arrived case. A component cannot render `[]` for a 403,
because a 403 never produces an array. Every screen added here must keep that
property; it is the reason the app exists rather than a dashboard library.

The capacity screen shows the shape: a failed read gets its own panel saying
"this is a failure to READ the platform; it says nothing about whether agents
are running", and a genuinely empty result gets a different one explaining that
pools are created at provisioning time.

## Fixtures are real

`api.ts` ships the actual pool names and live values read from dev on
2026-09-19, including `tenant:u-bogdan` at 40 against a configured 2 -- the
out-of-band drift `make pool-check` found. A fixture showing a tidy platform
teaches the wrong thing about what this screen is for.

## Notes that will bite whoever adds the next screen

* `pool.active` is **weighted units, not agents**. Admission increments by the
  resource class's `units` (standard 1, browser 2, large 4), so `active: 8` may
  be two agents or eight. Never label it "agents".
* Types in `types.ts` mirror `apps/swarm-api/swarm_api/codec.py` by hand. If a
  field changes there, nothing here notices. That parity is unguarded and
  probably should not stay that way.
* Not yet deployed. The load balancer currently routes everything to swarm-api;
  serving this needs either a second backend with path rules, or swarm-api
  serving the static bundle. That decision is open.
