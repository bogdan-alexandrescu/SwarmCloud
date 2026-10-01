// THE PRODUCT FRAME'S VOCABULARY: the mark, and the environment it is pointed
// at. Since the rebrand (2026-10-01) the frame that draws them is the Sky
// spine (Spine.tsx): its panel pill, its phone-header pill and its red bar all
// come from `classifyEnvironment` and `envTreatment` below. The product header
// that used to live in this file, with its identity read, is gone.
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
// Three honest sources, in the order they are believed:
//
//   1. THE API DECLARES IT -- `GET /v1/tenants/me` serves `environment` and
//      `environment_declared` (PR #19; ui-audit §B9.S5). That is the
//      environment the process this console is TALKING TO acts on --
//      `hardened` derives from it -- which is the thing a pool change will
//      actually reach. Only when `environment_declared` is true: the frozen
//      `Settings.from_env` fills in "dev" when ENVIRONMENT is unset, and a
//      defaulted dev is the badge this file exists not to draw.
//   2. THE BUILD DECLARES IT -- `VITE_SWARM_ENV`, baked into the bundle by
//      whoever built it (scripts/build-images.sh passes it). Declared, not
//      inferred.
//   3. THE BROWSER IS ON A LOOPBACK HOST -- which is a fact about this tab,
//      not a guess about a deployment.
//
// WHY THE API OUTRANKS THE BUILD AND THE HOST. The build says what the bundle
// was built for and the host says where the tab is; neither says where the
// requests go. `SWARM_API_ORIGIN=… VITE_LIVE=1 npm run dev` serves a console
// from localhost that proxies every /v1 call to a deployed API, and before
// this the badge there said `Local` -- quiet, dashed -- over a console whose
// pool ceilings were a deployed environment's. The API's own answer is the one
// source that cannot be pointed somewhere else. When the build disagrees with
// it, the badge follows the API and the tooltip names both.
//
// Until `/v1/tenants/me` lands, or when it fails, or when the API did not
// declare, the badge is exactly what it was: the build, then the host.
//
// And when none of them answers, the badge says ENVIRONMENT UNKNOWN and prints
// the host, LOUDLY -- the same treatment production gets. Not knowing which
// environment you are in is not permission to relax; it is the one case where
// a quiet badge would be actively dangerous. A `swarm.saga.xyz -> dev` table
// in this file was the obvious fourth option and is deliberately NOT here: it
// would be a TypeScript restatement of
// terraform/environments/dev/dev.tfvars:339, in a file nothing checks against
// it, and every restatement of another track's value in this repository has
// since drifted. A wrong environment badge is the exact failure this file
// exists to prevent, so it may not be produced by a copy of somebody else's
// variable.

import { useId } from 'react'
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
export const MARK_COMPACT_MAX = 24

/**
 * THE HIVE, REFINED (rebrand, owner's pick 2026-10-01; brand.html). A 32-unit
 * grid, a pointy-top hexagon with six vertex agents and a core.
 *
 *   FULL (24px and up)    a faint ring (opacity .5, 1.6 stroke), THREE spokes
 *                         from the core (up, lower right, lower left), six
 *                         vertex discs and the core.
 *   COMPACT (below 24px)  the ring and the spokes go and the seven dots grow,
 *                         because a 1px spoke at 16px is a grey smear. This is
 *                         the favicon and the phone header's cut.
 */
const HEX = [
  [16, 4],
  [26.39, 10],
  [26.39, 22],
  [16, 28],
  [5.61, 22],
  [5.61, 10],
] as const
const HEX_COMPACT = [
  [16, 5.2],
  [25.35, 10.6],
  [25.35, 21.4],
  [16, 26.8],
  [6.65, 21.4],
  [6.65, 10.6],
] as const
/** The three spokes the full cut draws: to the top, lower-right and lower-left vertex. */
const SPOKES = [HEX[0], HEX[2], HEX[4]] as const

/** The small-size geometry. index.html's favicon draws these same numbers. */
export const COMPACT = { vertex: 3.3, core: 4.9, centre: 16 } as const
/** The large-size geometry. */
const FULL = { vertex: 2.6, core: 4.6 } as const

/**
 * `paint` is `currentColor` by default, which is how the mark works on both
 * grounds without a token. `sky` is the spine's cut: a white -> #7dd3fc
 * gradient on the deep-blue spine (white 11.05:1, #7dd3fc 6.63:1 on #0b3a7a),
 * the same in both themes because the spine is.
 */
export function SwarmMark({
  size = 28,
  title,
  paint = 'current',
}: {
  size?: number
  title?: string
  paint?: 'current' | 'sky'
}) {
  const compact = size < MARK_COMPACT_MAX
  const gid = useId().replace(/:/g, '')
  const fill = paint === 'sky' ? `url(#hive-${gid})` : 'currentColor'
  const dots = compact ? HEX_COMPACT : HEX
  const r = compact ? COMPACT.vertex : FULL.vertex

  return (
    <svg
      className="brand-mark"
      width={size}
      height={size}
      viewBox="0 0 32 32"
      role={title ? 'img' : 'presentation'}
      aria-label={title}
      aria-hidden={title ? undefined : true}
      focusable="false"
    >
      {paint === 'sky' && (
        <defs>
          <linearGradient id={`hive-${gid}`} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor="#ffffff" />
            <stop offset="1" stopColor="#7dd3fc" />
          </linearGradient>
        </defs>
      )}
      {!compact && (
        <>
          <polygon
            points={HEX.map(([x, y]) => `${x},${y}`).join(' ')}
            fill="none"
            stroke={fill}
            strokeWidth={1.6}
            opacity={0.5}
          />
          {SPOKES.map(([x, y]) => (
            <line key={`s${x}-${y}`} x1={16} y1={16} x2={x} y2={y} stroke={fill} strokeWidth={1.3} opacity={0.5} />
          ))}
        </>
      )}
      {dots.map(([x, y]) => (
        <circle key={`v${x}-${y}`} cx={x} cy={y} r={r} fill={fill} />
      ))}
      <circle cx={16} cy={16} r={compact ? COMPACT.core : FULL.core} fill={fill} />
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
  | { kind: 'production'; name: string; source: 'build' | 'api'; build?: string }
  | { kind: 'nonprod'; name: string; source: 'build' | 'api'; build?: string }
  | { kind: 'local'; name: string; source: 'host'; host: string }
  | { kind: 'unknown'; host: string }

/**
 * What the API said about the environment it runs as, or null when it has not
 * said -- still loading, failed, or an API older than the fields.
 */
export interface ServedEnvironment {
  name: string
  declared: boolean
}

/**
 * The API's answer, read DEFENSIVELY. `Me` declares both fields because the
 * API serves them, but a console served beside an older API gets neither, and
 * a missing `environment_declared` is not `true`.
 */
export function servedEnvironment(me: Me): ServedEnvironment | null {
  const name: unknown = me.environment
  const declared: unknown = me.environment_declared
  if (typeof name !== 'string' || typeof declared !== 'boolean') return null
  return { name, declared }
}

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

/** The two kinds a DECLARED name can be. */
type DeclaredEnvironment = Extract<Environment, { kind: 'production' | 'nonprod' }>

/** A declared name as production or not, by its leading word. */
function named(name: string, source: 'build' | 'api'): DeclaredEnvironment {
  // The leading word, so `prod-eu` and `production_2` are still production.
  const head = name.split(/[-_.\s]/)[0] ?? name
  const loud = PRODUCTION_NAMES.some((p) => head === p)
  return loud ? { kind: 'production', name, source } : { kind: 'nonprod', name, source }
}

export function classifyEnvironment(
  declared: string | undefined,
  host: string,
  served: ServedEnvironment | null = null,
): Environment {
  const name = (declared ?? '').trim().toLowerCase()

  // THE API'S OWN ANSWER FIRST, and only a DECLARED one: a defaulted "dev" is
  // an absence wearing a name. See the header of this file for why it
  // outranks the build and the host.
  const api = served !== null && served.declared ? served.name.trim().toLowerCase() : ''
  if (api !== '') {
    const env = named(api, 'api')
    // A build that said something else is kept, for the tooltip. It does not
    // change the badge: the API is what the requests reach.
    return name !== '' && name !== api ? { ...env, build: name } : env
  }

  if (name !== '') return named(name, 'build')

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
 * (The bar is the Sky spine's `.sk-prodbar` now; the product header it was
 * drawn under is gone.)
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

/**
 * Who said which environment this is, as the tooltip's first sentence. When
 * the API and the build disagree, both are named: the badge follows the API,
 * and a reader looking at a surprising badge is owed the reason.
 */
function declaredBy(env: DeclaredEnvironment): string {
  if (env.source === 'build') return `Environment ${env.name}, declared by the build.`
  const also = env.build === undefined ? '' : ` The build declared ${env.build}; the API is what these requests reach.`
  return `Environment ${env.name}, reported by the API this console is talking to.${also}`
}

export function envTreatment(env: Environment): EnvTreatment {
  switch (env.kind) {
    case 'production':
      return {
        className: 'is-prod',
        // A SAFETY SIGNAL, and the only declared name that shouts.
        label: env.name.toUpperCase(),
        explain: `${declaredBy(env)} Changes here are real.`,
        bar: true,
      }
    case 'nonprod':
      return {
        className: 'is-nonprod',
        label: sentenceCase(env.name),
        explain: declaredBy(env),
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
          `Nothing told this console which environment it is. The API has not ` +
          `declared one, the build did not declare VITE_SWARM_ENV, and ` +
          `${env.host || 'the host'} is not this machine, so treat it as ` +
          `production until you know otherwise.`,
        bar: true,
      }
  }
}

/**
 * The words the unknown badge stops DRAWING at 560px and below (CH-20). They
 * stay in the tree -- visually hidden, not removed -- so the badge's name is
 * still "ENVIRONMENT UNKNOWN" to a screen reader and in the DOM text, and the
 * capitals, the hatch and the left rule all stay. Drawn, it is "UNKNOWN"
 * after its `env` key: about 122px, near production's 130, which is what lets
 * the phone header hold one row.
 *
 * NOT RENDERED BY THE FRAME SINCE THE REBRAND: the Sky spine draws its own
 * pill from the same `envTreatment`. The badge and its `.brand-env` rules stay
 * while tests/unit/control_plane/test_ui_contrast.py measures
 * `.brand-env.is-unknown > span` by name; re-pointing that gate at the spine's
 * pill is what lets both go.
 */
const UNKNOWN_LEAD = 'ENVIRONMENT '

export function EnvironmentBadge({ env }: { env: Environment }) {
  const t = envTreatment(env)
  const lead = env.kind === 'unknown' && t.label.startsWith(UNKNOWN_LEAD)
  return (
    <span className={`brand-env ${t.className}`} title={t.explain}>
      <span className="brand-k">env</span>
      <span className="brand-env-name">
        {lead ? (
          <>
            <span className="brand-env-lead">{UNKNOWN_LEAD}</span>
            {t.label.slice(UNKNOWN_LEAD.length)}
          </>
        ) : (
          t.label
        )}
      </span>
      {/* The host it could not classify. Not drawn at 560px and below (the
          badge's title names it), and never removed from the tree. */}
      {env.kind === 'unknown' && env.host !== '' && <Id>{env.host}</Id>}
    </span>
  )
}
