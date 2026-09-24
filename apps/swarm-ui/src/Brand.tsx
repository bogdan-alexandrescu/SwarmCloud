// THE PRODUCT FRAME: the mark, the wordmark, and the one bar that says where
// you are and who you are.
//
// WHY THIS FILE EXISTS. Until now this app had no branding of any kind --
// `grep -riE "logo|wordmark"` over src/ returned nothing, and index.html
// carried a <title> and no icon. It opened on a bare row of section names.
// That is a cosmetic complaint on its own; it stops being cosmetic because of
// the thing the frame was ALSO not carrying:
//
//   THE ENVIRONMENT IS A SAFETY PROPERTY, NOT DECORATION.
//
// Every screen used to print a hardcoded `<span className="env">dev</span>`
// beside its own title (Shell.tsx did it for all of them). Nothing measured
// that word. A console that says "dev" while pointed at production is worse
// than one that says nothing, because the operator about to change a pool
// ceiling reads it and relaxes. Overview.tsx:179-183 had already refused to
// draw that badge for exactly this reason and said so in a comment. This file
// generalises that refusal: the badge is back, in one place, and it is only
// ever allowed to say something that was actually measured.
//
// WHERE THE ENVIRONMENT COMES FROM, AND WHAT IT DOES WHEN IT DOES NOT KNOW.
// There is no server answer to "which environment is this": the API serves 41
// routes and none of them report `Settings.core.environment`
// (apps/swarm-api/swarm_api/settings.py:222 has it; nothing exposes it).
// docs/web-ui/ui-audit-and-build-prompt.md §B9.S8 records that as an unbuilt
// seam and it is still unbuilt. So the two honest sources are:
//
//   1. THE BUILD DECLARES IT -- `VITE_SWARM_ENV`, baked into the bundle by
//      whoever built it. Declared, not inferred.
//   2. THE BROWSER IS ON A LOOPBACK HOST -- which is a fact about this tab,
//      not a guess about a deployment.
//
// and when neither answers, the badge says ENVIRONMENT UNKNOWN and prints the
// host, LOUDLY -- the same treatment production gets. Not knowing which
// environment you are in is not permission to relax; it is the one case where
// a quiet badge would be actively dangerous. A `swarm.saga.xyz -> dev` table
// in this file was the obvious third option and is deliberately NOT here: it
// would be a TypeScript restatement of
// terraform/environments/dev/dev.tfvars:339, in a file nothing checks against
// it, and every restatement of another track's value in this repository has
// since drifted. A wrong environment badge is the exact failure this file
// exists to prevent, so it may not be produced by a copy of somebody else's
// variable.
//
// CROSS-TRACK: making the deployed console say `Dev` rather than
// `ENVIRONMENT UNKNOWN` is one flag on the build -- `VITE_SWARM_ENV=dev npm
// run build` -- and the build lives in scripts/ and .github/, which are Track
// D. That change is reported, not made here.

import { useEffect, useState } from 'react'
import { loadMe } from './api'
import { errorHeading, type Result } from './fetch'
import { Id } from './Shell'
import type { Me } from './types'

// ---------------------------------------------------------------------------
// The mark
// ---------------------------------------------------------------------------

/**
 * Six agents on a hex ring around the thing that coordinates them.
 *
 * NOT A CLOUD. The product is a swarm and an orchestrator; a cloud glyph would
 * say "this runs somewhere else", which is the least interesting true thing
 * about it. The ring says "many, arranged"; the centre says "one of them is
 * the co-ordinator"; the spokes say the centre reaches all six.
 *
 * TWO GEOMETRIES, ONE MARK, AND THE THRESHOLD WAS MEASURED RATHER THAN
 * GUESSED. The full mark (hex outline + six spokes + seven discs) was
 * rasterised with `rsvg-convert` at 14, 16, 18, 20, 24 and 28 px on both
 * grounds and looked at, magnified 10x nearest-neighbour. Below about 22px the
 * 1px spokes and the 1.1px hex outline land on the same pixels as the discs
 * and the whole thing turns into grey noise with a bright middle; at 24px and
 * above it is crisp. So at `size < 22` the strokes are dropped entirely and
 * the discs grow to carry the silhouette alone -- that variant reads at 14px,
 * which is a size nothing here actually uses, so 16px has margin.
 *
 * EVERYTHING IS `currentColor`. That is how it works on both grounds in both
 * themes without a second copy and without a token: wherever it is placed it
 * inherits the text colour that was already proven readable there. There is no
 * hardcoded hex in this component, so there is no ground it can fail on that
 * text would not fail on too.
 */
export const MARK_COMPACT_MAX = 22

/** Centre, and the six vertex angles of a pointy-top hexagon. */
const C = 12
const ANGLES = [-90, -30, 30, 90, 150, 210] as const

function verts(radius: number): { x: number; y: number }[] {
  return ANGLES.map((a) => ({
    x: round2(C + radius * Math.cos((a * Math.PI) / 180)),
    y: round2(C + radius * Math.sin((a * Math.PI) / 180)),
  }))
}

function round2(n: number): number {
  return Math.round(n * 100) / 100
}

/** The small-size geometry. index.html's favicon draws these same numbers. */
export const COMPACT = { ring: 7.6, vertex: 2.4, core: 3.5 } as const
/** The large-size geometry. */
const FULL = { ring: 8.6, vertex: 1.9, core: 3 } as const

export function SwarmMark({ size = 28, title }: { size?: number; title?: string }) {
  const compact = size < MARK_COMPACT_MAX
  const g = compact ? COMPACT : FULL
  const points = verts(g.ring)

  return (
    <svg
      className="brand-mark"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      role={title ? 'img' : 'presentation'}
      aria-label={title}
      aria-hidden={title ? undefined : true}
      focusable="false"
    >
      {!compact && (
        <>
          <polygon
            points={points.map((p) => `${p.x},${p.y}`).join(' ')}
            fill="none"
            stroke="currentColor"
            strokeWidth={1.1}
            opacity={0.3}
          />
          {points.map((p) => (
            <line
              key={`s${p.x}-${p.y}`}
              x1={C}
              y1={C}
              x2={p.x}
              y2={p.y}
              stroke="currentColor"
              strokeWidth={1}
              opacity={0.55}
            />
          ))}
        </>
      )}
      {points.map((p) => (
        <circle key={`v${p.x}-${p.y}`} cx={p.x} cy={p.y} r={g.vertex} fill="currentColor" />
      ))}
      <circle cx={C} cy={C} r={g.core} fill="currentColor" />
    </svg>
  )
}

// ---------------------------------------------------------------------------
// The environment
// ---------------------------------------------------------------------------

/**
 * What this console was able to establish about where it is pointed.
 *
 * `production` and `unknown` are the two LOUD kinds and they are loud for the
 * same reason: both mean "assume a mistake here is expensive".
 */
export type Environment =
  | { kind: 'production'; name: string; source: 'build' }
  | { kind: 'nonprod'; name: string; source: 'build' }
  | { kind: 'local'; name: string; source: 'host'; host: string }
  | { kind: 'unknown'; host: string }

/**
 * The names that mean "this is the one you cannot undo".
 *
 * Anything NOT in this list is treated as non-production, which is the less
 * safe default of the two -- so it is deliberately a list of the loud names
 * rather than a list of the quiet ones. A new environment called `staging`
 * reads as non-production and that is correct; a new one called `production-eu`
 * would not match, which is why the test pins the prefix behaviour and why the
 * match is on the leading word rather than on equality.
 */
const PRODUCTION_NAMES = ['prod', 'production', 'live'] as const

/** Hosts that are this machine. A fact about the tab, not a guess. */
const LOCAL_HOSTS = ['localhost', '127.0.0.1', '0.0.0.0', '::1', '[::1]'] as const

export function classifyEnvironment(declared: string | undefined, host: string): Environment {
  const name = (declared ?? '').trim().toLowerCase()
  if (name !== '') {
    // The leading word, so `prod-eu` and `production_2` are still production.
    const head = name.split(/[-_.\s]/)[0] ?? name
    const loud = PRODUCTION_NAMES.some((p) => head === p)
    return loud ? { kind: 'production', name, source: 'build' } : { kind: 'nonprod', name, source: 'build' }
  }

  const h = host.trim().toLowerCase()
  const local =
    LOCAL_HOSTS.some((l) => l === h) || h.endsWith('.local') || h.endsWith('.localhost')
  if (local) return { kind: 'local', name: 'local', source: 'host', host: h }

  return { kind: 'unknown', host: h }
}

/**
 * How the badge is DRAWN, which is the part that has to work without reading.
 *
 * Four treatments, and the channel that separates them is never colour alone:
 * the audit's own rule for the state palette ("render it in greyscale; if two
 * marks become the same mark the treatment is incomplete") applies here too,
 * and an environment badge is the last thing that should fail it in a
 * screenshot pasted into an incident channel.
 *
 *   production  solid tinted fill, a 2px rule down its left edge, AND a bar
 *               across the full width of the header above it
 *   unknown     45deg hatch -- this sheet's established "we could not read
 *               this" surface -- AND the same bar
 *   nonprod     outlined, flat surface, no bar
 *   local       outlined, DASHED border, no bar
 *
 * `bar` is what makes "at a glance and without reading" true: it is 3px tall
 * and 100% wide, so it is visible in peripheral vision and survives being
 * scaled down to a thumbnail.
 *
 * CAPITALS ARE SPENT ONLY WHERE THE BAR IS. Owner decision, 2026-09-24
 * (design-system.md §13.6): design-system.md §13.2 bans emphasis on any string
 * this console authors, so `dev`, `staging` and `local` are written in
 * sentence case like every other label -- `Dev`, `Local`. The PRODUCTION
 * banner and ENVIRONMENT UNKNOWN keep their capitals, because on those two the
 * capitals are a safety signal, not typography: they are the same two kinds
 * that draw the bar, and `brand.test.tsx` holds the two channels together.
 */
export interface EnvTreatment {
  /** Modifier class on the badge. */
  className: string
  /** The word in the badge. */
  label: string
  /** Long form, for the tooltip and the screen reader. */
  explain: string
  /** True when the header draws its full-width bar. */
  bar: boolean
}

/**
 * A capital first letter and the rest as the build declared it (already
 * lower-cased by `classifyEnvironment`). Not `text-transform: capitalize` in
 * the sheet: the casing is part of the decision above, so it lives beside the
 * one branch that is allowed to shout rather than in a rule that could be
 * edited without seeing that branch.
 */
function sentenceCase(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1)
}

export function envTreatment(env: Environment): EnvTreatment {
  switch (env.kind) {
    case 'production':
      return {
        className: 'is-prod',
        // A SAFETY SIGNAL, and the only declared name that shouts.
        label: env.name.toUpperCase(),
        explain: `Environment ${env.name}, declared by the build. Changes here are real.`,
        bar: true,
      }
    case 'nonprod':
      return {
        className: 'is-nonprod',
        label: sentenceCase(env.name),
        explain: `Environment ${env.name}, declared by the build.`,
        bar: false,
      }
    case 'local':
      return {
        className: 'is-local',
        label: sentenceCase(env.name),
        explain: `Served from ${env.host}, which is this machine.`,
        bar: false,
      }
    case 'unknown':
      return {
        className: 'is-unknown',
        // Capitals kept on purpose (2026-09-24): not knowing is treated as
        // production, so it is written as loudly as production.
        label: 'ENVIRONMENT UNKNOWN',
        // Named rather than vague: someone has to be able to act on it.
        explain:
          `Nothing told this console which environment it is. The build did not ` +
          `declare VITE_SWARM_ENV and ${env.host || 'the host'} is not this ` +
          `machine, so treat it as production until you know otherwise.`,
        bar: true,
      }
  }
}

export function EnvironmentBadge({ env }: { env: Environment }) {
  const t = envTreatment(env)
  return (
    <span className={`brand-env ${t.className}`} title={t.explain}>
      <span className="brand-k">env</span>
      <span className="brand-env-name">{t.label}</span>
      {env.kind === 'unknown' && env.host !== '' && <Id>{env.host}</Id>}
    </span>
  )
}

// ---------------------------------------------------------------------------
// The header
// ---------------------------------------------------------------------------

/**
 * The bar at the top of every screen: mark, wordmark, environment, tenant,
 * signed-in principal.
 *
 * IT OBEYS THE SAME HONESTY RULE AS EVERY OTHER READ. `/v1/tenants/me` can
 * fail, and a header that silently renders an empty tenant slot when it does
 * is this platform's defining bug in the one place every screen shows. So the
 * identity half has three visible states -- reading, read, could-not-read --
 * and never a blank. It does NOT use `Screen`: `Screen` owns a title, a
 * provenance line and a skeleton, and the frame is not a screen.
 *
 * `load` is injected with a default so the states can be driven directly in a
 * test, which is the same shape `Screen` uses for the same reason.
 */
export function ProductHeader({
  load = loadMe,
  declaredEnv = import.meta.env.VITE_SWARM_ENV,
  host,
}: {
  load?: () => Promise<Result<Me>>
  declaredEnv?: string | undefined
  host?: string
} = {}) {
  const [me, setMe] = useState<Result<Me>>({ status: 'loading', since: Date.now() })

  useEffect(() => {
    let live = true
    load().then((next) => {
      if (live) setMe(next)
    })
    return () => {
      live = false
    }
  }, [load])

  const env = classifyEnvironment(
    declaredEnv,
    host ?? (typeof window === 'undefined' ? '' : window.location.hostname),
  )
  const t = envTreatment(env)

  return (
    <header className={`brand ${t.bar ? 'has-bar' : ''}`} data-env={env.kind}>
      {t.bar && <div className={`brand-bar ${t.className}`} aria-hidden />}
      <div className="brand-row">
        <a className="brand-home" href="#overview/now">
          {/* 36, not 28: the owner asked for the logo 30% larger and
              28 x 1.3 is 36.4. Rounded DOWN to a whole pixel because
              the mark strokes at 1.1 and 1.5 units in a 24-unit
              viewBox -- a fractional width puts those strokes on half
              pixels and the hexagon renders soft, which is the one
              thing a 6-vertex mark at this size cannot afford.
              36 > MARK_COMPACT_MAX, so the FULL geometry still
              applies and nothing about the drawing changes but scale. */}
          <SwarmMark size={36} title="SwarmCloud" />
          <span className="brand-word">
            <b>Swarm</b>Cloud
          </span>
        </a>
        <EnvironmentBadge env={env} />
        <Identity me={me} />
      </div>
    </header>
  )
}

function Identity({ me }: { me: Result<Me> }) {
  switch (me.status) {
    case 'loading':
      return (
        <p className="brand-who is-pending" role="status">
          Reading who you are…
        </p>
      )
    case 'ok':
    case 'stale':
      return <IdentityFacts me={me.data} />
    case 'empty':
      // `/v1/tenants/me` answering with nothing is not a tenant with no name;
      // it is a read that did not produce one.
      //
      // THE SENTENCE BECAME THE SLOT. It used to say, in 16 words, that the
      // header cannot name your tenant -- in the header, beside the empty
      // place where the tenant's name goes. The key stays, the value is an em
      // dash in `--ctl-absent`, and the fact is now an attribute of the slot
      // rather than a paragraph standing next to it: a paragraph can sit
      // beside a field it does not describe, and this cannot. The long form is
      // the accessible name.
      return (
        <p
          className="brand-who is-unread"
          role="status"
          aria-label="The identity read returned nothing, so this header cannot say whose tenant you are looking at."
        >
          <span className="brand-k">tenant</span>
          <i className="ctl-em">&mdash;</i>
          <i className="ctl-mark is-absent">not read</i>
        </p>
      )
    case 'error':
      return (
        <p className="brand-who is-unread" role="status" title={me.error.message}>
          <i className="ctl-mark is-unread">not read</i>
          {errorHeading(me.error)} — tenant and sign-in unread
        </p>
      )
  }
}

function IdentityFacts({ me }: { me: Me }) {
  return (
    <p className="brand-who">
      <span className="brand-k">tenant</span>
      {/* NEVER RESTYLED. A tenant id is pasted into `swarm` commands and into
          GCS prefixes; QuotaDetail.tsx:94-96 states the rule and this is the
          same rule applied in the frame. */}
      <Id>{me.tenant.tenant_id}</Id>
      {me.tenant.display_name !== null && (
        <span className="brand-who-name">{me.tenant.display_name}</span>
      )}
      <span className="brand-k">signed in</span>
      <Id>{me.principal.email}</Id>
      {me.principal.is_admin && (
        <span
          className="brand-admin"
          title="You are in an admin group, so the /v1/admin routes will answer for you."
        >
          admin
        </span>
      )}
    </p>
  )
}
