/**
 * THE CONSOLE FINISH'S RECORD MOMENTS: fixtures for the console's fixture
 * build and its tests only. The live page never draws them: it draws the
 * record `GET /v1/admin/setup` answers.
 *
 * Each moment is the install record that route answers at one moment of the
 * Day One story, in the route's own shape (what `normSetupState` reads).
 * Every time is counted from the clock passed in (the fixture clock in the
 * fixture build), so "seen 20 s ago" and "21 h left" read the same on every
 * open. In the fixture build (`USE_FIXTURES`, a development build only)
 * `/admin/setup?moment=<name>` draws Admin › Setup at that moment; a live
 * build never reads the parameter.
 *
 * The story, in minutes from the install's start (stage 0) to the read:
 * cloud part done at 38 (11 of 11 stages), the hand-off at 38; arrive 39;
 * signin-typing 44; signin-saved 47; github-callback 52; github-installed 55;
 * team-building 68; Lea's link made at 70; accounts-waiting 250 (Ana came back
 * hours later: 21 hours left on Lea's link); smoke-failed 256; terminal-closed
 * 270 (terminal last seen at 256); verify-running 300; verify-failed 318;
 * ready 340. one-person is its own install, read at its accounts step.
 */
import { USE_FIXTURES } from '../api'
import { fixturePluginStep, plugFixture } from './pluginStep' // MOCK-HOOK

export const FINISH_MOMENTS = [
  'arrive',
  'signin-typing',
  'signin-saved',
  'github-callback',
  'github-installed',
  'team-building',
  'accounts-waiting',
  'smoke-failed',
  'terminal-closed',
  'verify-running',
  'verify-failed',
  'ready',
  'one-person',
] as const

export type FinishMoment = (typeof FINISH_MOMENTS)[number]

/** The story minute each moment is read at (see above). */
const READ_AT: Record<FinishMoment, number> = {
  arrive: 39,
  'signin-typing': 44,
  'signin-saved': 47,
  'github-callback': 52,
  'github-installed': 55,
  'team-building': 68,
  'accounts-waiting': 250,
  'smoke-failed': 256,
  'terminal-closed': 270,
  'verify-running': 300,
  'verify-failed': 318,
  ready: 340,
  'one-person': 52,
}

/**
 * The moment the fixture build's page answers: `?moment=<name>` on the
 * address. Always null in a live build, and for a name not in the story.
 */
export function fixtureMoment(search: string = typeof window === 'undefined' ? '' : window.location.search): FinishMoment | null {
  if (!USE_FIXTURES) return null
  const m = new URLSearchParams(search).get('moment')
  return m !== null && (FINISH_MOMENTS as readonly string[]).includes(m) ? (m as FinishMoment) : null
}

/** True from that story minute on: a minute at or before the moment's read is in its past. */
const reached = (m: FinishMoment, minute: number) => READ_AT[m] >= minute

/** The smoke agent's and the console verify runs' ids under a moment. */
export const MOMENT_RUN_IDS = { task: 'task_verify_console', workflow: 'wf_verify_console', smoke: 'task_smoke_engineering' }

/** How long each wait usually takes, in seconds, as the route would serve it (`typical_s`). */
const TYPICAL_S = { sign_in_pass: 180, pass_3: 240, workspace_build: 450, smoke: 180, groups: 300, verify: 1080, install: 2400 }

const SIGN_IN_PAGES = (() => {
  const page = (path: string) => `https://console.cloud.google.com/auth/${path}?project=swarmcloud-example`
  return { branding: page('branding'), audience: page('audience'), create_client: page('clients/create') }
})()

/** The Desktop client's id as it reads in the frames; its secret is never in any record. */
const CLIENT_ID = '409117-desktop.apps.googleusercontent.com'

const ANA = 'ana@example.com'
const RAVI = 'ravi@example.com'
const LEA = 'lea@example.com'

/**
 * The install record `GET /v1/admin/setup` answers at moment `m`, read at
 * `nowMs` (the fixture clock). Raw, in the route's shape: the caller passes
 * it through `normSetupState` as the live answer is.
 */
export function momentSetupState(m: FinishMoment, nowMs: number): Record<string, unknown> {
  const readAt = READ_AT[m]
  /** The ISO time of story minute `minute`, as seen from this moment's read. */
  const at = (minute: number) => new Date(nowMs - (readAt - minute) * 60_000).toISOString()
  /** The same instant in epoch seconds: the engine writes its own times so. */
  const sec = (minute: number) => Math.round((nowMs - (readAt - minute) * 60_000) / 1000)

  const onePerson = m === 'one-person'
  const closed = m === 'terminal-closed'
  // Stage 9 (pass 3, the App's switches) lands at 58 and pass 4 at 60; stage 14 is verify, 15 READY.
  const cloud = [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 13]
  const stages = Array.from({ length: 16 }, (_, i) => {
    if (cloud.includes(i)) return { state: 'done', at: sec(Math.round((cloud.indexOf(i) + 1) * (38 / 11))), by: ANA }
    if (i === 9 && !onePerson && reached(m, 58)) return { state: reached(m, 60) ? 'done' : 'switches_on', at: sec(58), by: ANA }
    if (i === 9 && onePerson) return { state: 'done', at: sec(50), by: ANA }
    if (i === 14 && reached(m, 291)) return { state: m === 'verify-failed' ? 'failed' : m === 'ready' ? 'done' : 'running', at: sec(291), by: ANA }
    if (i === 15 && m === 'ready') return { state: 'done', at: sec(338), by: ANA }
    return null
  })

  const verify =
    m === 'verify-running'
      ? { check: 'reconciler-jobs', done: 2, total: 8, started: sec(291), expected_end: sec(309), deadline: sec(336), result: null }
      : m === 'verify-failed'
        ? { check: 'plugin-browser', done: 5, total: 8, started: sec(291), expected_end: sec(309), deadline: sec(336), result: 'failed', detail: 'the live browser opened https://console.example.com but Google’s sign-in answered 403 for the test account' }
        : m === 'ready'
          ? { check: 'console-workflow', done: 8, total: 8, started: sec(291), expected_end: sec(309), deadline: sec(336), result: 'passed' }
          : null

  const engine = {
    release: '2026.11.0',
    hostname: 'console.example.com',
    stages,
    // The lease's holder heartbeats every 30 s while swarm install waits; terminal-closed: last seen 14 minutes ago.
    lease: { holder: `${ANA} laptop`, heartbeat: closed ? sec(256) : Math.round(nowMs / 1000) - 20 },
    verify,
    hand_off_at: sec(38),
    ready_at: m === 'ready' ? sec(338) : null,
    workspace_groups: onePerson ? null : { team: 'engineering@example.com' },
    // The engine's own record of the Desktop client once its pass applied it.
    iap_programmatic_client: reached(m, 49) ? CLIENT_ID : null,
  }

  const signIn =
    onePerson || reached(m, 49)
      ? { state: 'applied', client_id: CLIENT_ID, saved_at: at(onePerson ? 44 : 46), saved_by: ANA, links: SIGN_IN_PAGES }
      : m === 'signin-saved'
        ? { state: 'saved', client_id: CLIENT_ID, saved_at: at(46), saved_by: ANA, links: SIGN_IN_PAGES }
        : { state: 'waiting', client_id: null, saved_at: null, saved_by: null, links: SIGN_IN_PAGES }

  const githubApp = onePerson
    ? { started: true, id: 4242, slug: 'swarmcloud-console-example', installed_on: 'example-org', secrets_written_at: at(47), self_check: { ok: true, reason: null, at: at(51), state: 'passed' } }
    : !reached(m, 50)
      ? null
      : {
          started: true,
          id: reached(m, 52) ? 4242 : null,
          slug: reached(m, 52) ? 'swarmcloud-console-example' : null,
          installed_on: reached(m, 54) ? 'example-org' : null,
          secrets_written_at: reached(m, 52) ? at(52) : null,
          self_check: !reached(m, 55)
            ? null
            : reached(m, 59)
              ? { ok: true, reason: null, at: at(59), state: 'passed' }
              : { ok: false, reason: 'app_id_not_configured', at: at(55), state: 'waiting_for_infra_pass_3' },
        }

  const grant = { state: 'granted', account: 'example-org', selection: 'selected', repositories: ['example-org/app', 'example-org/web'] }
  const smoke =
    m === 'smoke-failed'
      ? { state: 'failed', detail: 'the smoke agent could not clone example-org/app: the GitHub App answered 404 for the repository', url: `/agents/${MOMENT_RUN_IDS.smoke}` }
      : reached(m, 266)
        ? { state: 'succeeded', detail: 'green in 3 min 12 s', url: `/agents/${MOMENT_RUN_IDS.smoke}` }
        : reached(m, 246)
          ? { state: 'running', detail: null, url: `/agents/${MOMENT_RUN_IDS.smoke}` }
          : null
  const leaIn = reached(m, 296)
  const ravi = reached(m, 240)
  const teams =
    onePerson || !reached(m, 62)
      ? []
      : [
          {
            id: 'engineering',
            group: 'engineering@example.com',
            workspace: 'engineering',
            workspace_id: 'w-engineering',
            first_admin: ANA,
            invites: [RAVI, LEA],
            progress: { created_at: at(62), namespace: reached(m, 70) ? 'ready' : 'creating', identity: reached(m, 72) ? 'ready' : 'waiting' },
            workspace_state: reached(m, 75) ? 'ready' : 'applying',
            smoke,
            grant: reached(m, 72) ? grant : { state: 'waiting_for_app', account: 'example-org', selection: null, repositories: [] },
            members: [
              { email: ANA, member: true },
              { email: RAVI, member: true },
              { email: LEA, member: reached(m, 90) },
            ],
            pool_accounts: [...(ravi ? ['ravi'] : []), ...(leaIn ? ['lea'] : [])],
            add_members_url: 'https://admin.google.com/ac/groups/engineering%40example.com/members',
          },
        ]

  const accounts: Record<string, unknown> = onePerson
    ? { ana: { owner: ANA, link_expires: null, signed_in_at: null } }
    : reached(m, 70)
      ? {
          // `signed_in_at` is stamped by the hand-over (290) for the accounts in the pool then; Lea signs in at 296.
          ravi: { owner: RAVI, link_made_at: at(69), link_expires: at(69 + 1440), signed_in_at: reached(m, 290) ? at(290) : null },
          lea: { owner: LEA, link_made_at: at(70), link_expires: at(70 + 1440), signed_in_at: null },
        }
      : {}

  const finished = reached(m, 290) ? { at: at(290), by: ANA } : null
  const verifyRuns = reached(m, 290)
    ? {
        verify_runs: { console_task: MOMENT_RUN_IDS.task, console_workflow: MOMENT_RUN_IDS.workflow },
        repository: 'example-org/app',
        retries: { console_task: 0, console_workflow: 0 },
      }
    : null

  return {
    env: 'prod',
    typical_s: TYPICAL_S,
    engine,
    // terminal-closed: the lease's heartbeat is over 2 minutes old, so the route says no CLI runs.
    cli_running: !closed,
    sign_in_client: signIn,
    github_app: githubApp,
    teams,
    accounts,
    finished,
    addons: [
      { name: 'brave-search', enable: 'swarm addon enable brave-search', secret_link: true },
      { name: 'ci', enable: 'swarm addon enable ci', secret_link: false },
    ],
    hostname: 'console.example.com',
    github_org: 'example-org',
    verify_runs: verifyRuns,
    mode: onePerson ? 'individual' : 'team',
    personal: onePerson
      ? {
          owner: ANA,
          workspace: 'u-ana',
          reason: null,
          progress: { created_at: at(51) },
          workspace_state: 'ready',
          grant,
          smoke: null,
          pool_accounts: [],
        }
      : null,
    steps: onePerson
      ? { first_team: { needed: false, state: 'not_needed', reason: 'individual' }, invites: { needed: false, state: 'not_needed', reason: 'individual' }, plugins: fixturePluginStep(plugFixture(), nowMs) /* MOCK-HOOK */ }
      : { first_team: { needed: true, state: reached(m, 62) ? 'started' : 'todo', reason: null }, invites: { needed: true, state: reached(m, 90) ? 'done' : reached(m, 62) ? 'waiting' : 'todo', reason: null }, plugins: fixturePluginStep(plugFixture(), nowMs) /* MOCK-HOOK */ },
    sizing: onePerson
      ? {
          mode: 'individual',
          members: 1,
          pool_limits: { global: 4, default_tenant: 4, provider_tenant: 4 },
          service_max_instances: { 'swarm-api': 1, 'swarm-scheduler': 1, 'swarm-quota-broker': 1, 'swarm-reconciler': 1 },
          pool_accounts: 1,
          budget_usd: 200,
        }
      : {
          mode: 'team',
          members: 5,
          pool_limits: { global: 12, default_tenant: 8, provider_tenant: 8 },
          service_max_instances: { 'swarm-api': 2, 'swarm-scheduler': 1, 'swarm-quota-broker': 1, 'swarm-reconciler': 1 },
          pool_accounts: 2,
          budget_usd: 1000,
        },
  }
}
