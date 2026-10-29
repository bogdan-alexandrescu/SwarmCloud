import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { createRun, loadCapacity, loadIssuePreview, loadRuns } from './api'
import { WorkspaceRefusalBanner, workspaceRefusal } from './WorkspaceRefusal'
import type { ApiError } from './fetch'
import { Button, NamedMark, Tag, WarnMark } from './components'
import { RunnerPicker, useProviderKeys } from './RunnerPicker'
import { FailedPanel, Screen } from './Shell'
import { Move, OutcomeSlot } from './Submit'
import { TASK_FORM } from './SubmitChooser'
import { timeAgo, type Capacity, type IssuePreviewRead, type IssueRunState, type IssueRunSummary, type RunCreateBody, type RunnerProfile } from './types'
import { Markdown } from './ArtifactViewer'
import { useNow } from './useNow'
import './styles/submit.css'
import './styles/intake.css'

/**
 * SUBMIT FROM A GITHUB ISSUE (/submit/issue; intake-tenants.html 1A, the
 * owner's pick 2026-10-02). One page, three moves, and a summary that repeats
 * every choice beside the one button.
 *
 * 1. NAME THE ISSUE, AND SEE IT BEFORE ANYTHING IS CREATED. `owner/repo#N` or
 *    its URL, read through `GET /v1/issues/preview` with the TENANT's forge
 *    credential (routes/issues.py). The preview is drawn as served -- the body
 *    masked and bounded by the server, never re-cut here -- and each refusal
 *    the route serves has its own words, keyed on its `code` (forge.py). A
 *    CLOSED issue is a warning with "Plan it anyway", never a refusal: the
 *    route serves it on purpose, because planning a closed issue is
 *    sometimes the point.
 *
 * 2. THE RUNNER, BY NAME, AS THE TASK FORM DRAWS IT (`RunnerPicker`). An issue
 *    run plans and builds with `claude-code` -- issueruns.PLANNER_PROFILE and
 *    STEP_PROFILE, fixed by the API, and `RunCreate` has no runner field
 *    (invariant 10) -- so that is the one card that can be chosen, and every
 *    other runner stays in the list, held back with that reason, rather than
 *    offering a choice the API would refuse.
 *
 * 3. WHAT IT MAY DO ON ITS OWN, decided now and nowhere else (#454): plan
 *    approval (Required by default), auto-merge (drawn OFF and DISABLED until
 *    the preview's `auto_merge` says POST /v1/runs would take it --
 *    `issueruns.auto_merge_availability`, the same answer `refuse_auto_merge`
 *    enforces; before a read it says the read decides it, and a server that
 *    does not say is read as unavailable), and the fix-round cap (3, within 1-5).
 *
 * WHAT IT COSTS (invariant 1). The planner is one ordinary task; a PLANNED run
 * is a Firestore document and nothing else, so the time a person takes to
 * read a plan is time no pool spends.
 *
 * THE CLASSES ARE `in-` so a later pass can swap them for lane U0's canonical
 * components by name (components.html A).
 */

/** The runner an issue run plans and builds with: issueruns.PLANNER_PROFILE / STEP_PROFILE. */
export const ISSUE_RUN_PROFILE = 'claude-code'

/** RunCreate's fix-round range and default (schemas.py, MIN/MAX/DEFAULT_FIX_ROUNDS). */
export const MIN_FIX_ROUNDS = 1
export const MAX_FIX_ROUNDS = 5
export const DEFAULT_FIX_ROUNDS = 3

/**
 * Why every runner but one is held back, as the submitter reads it
 * (walkthrough E): the reason the API takes none -- the run's create body has
 * no runner field -- is behind the list's `?` (`runner-unavailable`).
 */
const ONLY_RUNNER = `Issue runs always use ${ISSUE_RUN_PROFILE}`

/**
 * WHY AUTO-MERGE IS OFF, IN THE SUMMARY, by what the API has said (visual QA
 * chrome-C29, 2026-10-28). One reason, "not available yet", was printed before
 * any issue was read -- when nothing had been asked -- and its tooltip said the
 * merge chain had not shipped, while issue runs merge on their own (contract
 * request 47 enabled the merge step on 2026-10-04). Off now says which of the
 * three it is.
 */
export const AUTO_MERGE_UNREAD = 'decided when the issue is read'
export const AUTO_MERGE_UNSAID = 'not offered by this API'
export const AUTO_MERGE_REFUSED = 'refused by the API'

/**
 * TEXT WITH ITS INLINE CODE DRAWN AS CODE (browser QA N18, 2026-10-04): a
 * run's plan lead, prompts and requirements and an issue's title printed their
 * backticks (`Closes #N`, `apps/...`). Only a backtick pair is read -- React
 * elements, never HTML -- so the text cannot inject anything; an unpaired
 * backtick is left as it was written.
 */
export function InlineText({ text }: { text: string }) {
  const parts = text.split(/(`[^`\n]+`)/g)
  return (
    <>
      {parts.map((p, i) =>
        p.length > 2 && p.startsWith('`') && p.endsWith('`') ? <code key={i}>{p.slice(1, -1)}</code> : <Fragment key={i}>{p}</Fragment>,
      )}
    </>
  )
}

/** The router address of one run: `/runs/<id>`. */
export function runAddress(id: string): string {
  return `work/runs?${new URLSearchParams({ run: id }).toString()}`
}

type Preview =
  | { kind: 'idle' }
  | { kind: 'reading'; ref: string }
  | { kind: 'read'; ref: string; data: IssuePreviewRead; at: number }
  | { kind: 'refused'; ref: string; error: ApiError }

type Sending =
  | { kind: 'idle' | 'sending' }
  | { kind: 'failed'; error: ApiError }
  /** A 2xx whose answer named no run: something may exist that this page cannot name. */
  | { kind: 'unnamed' }

/**
 * AN ISSUE REFERENCE, PARSED HERE BEFORE ANYTHING IS SENT (QA G4-25), in the
 * two shapes `validation.parse_issue_ref` takes: `owner/repo#N`, or an https
 * URL on github.com naming `/owner/repo/issues/N`. "foo bar" went to the
 * server, came back as a sentence about `repository_url` -- a field this form
 * does not have -- and the dock counted a failed read. Null for anything the
 * API would refuse as malformed; a `/pull/N` URL parses, because the server's
 * answer to it is its own refusal, which points at the task form. The server
 * stays the decider: this only keeps a reference that cannot parse unsent.
 */
export function parseIssueRef(typed: string): { owner: string; repo: string; number: number } | null {
  const text = typed.trim()
  const m =
    /^([A-Za-z0-9][A-Za-z0-9-]{0,38})\/([A-Za-z0-9._-]{1,100})#([0-9]{1,7})$/.exec(text) ??
    /^https:\/\/(?:www\.)?github\.com\/([A-Za-z0-9][A-Za-z0-9-]{0,38})\/([A-Za-z0-9._-]{1,100})\/(?:issues|pull|pulls)\/([0-9]{1,7})\/?(?:[?#].*)?$/i.exec(text)
  const [owner, repo, raw] = [m?.[1], m?.[2], m?.[3]]
  if (owner === undefined || repo === undefined || raw === undefined) return null
  const name = repo.toLowerCase().endsWith('.git') ? repo.slice(0, -4) : repo
  if (name === '' || name.startsWith('.')) return null
  const number = Number(raw)
  if (number < 1 || number > MAX_ISSUE_NUMBER) return null
  return { owner, repo: name, number }
}

/** `validation.MAX_ISSUE_NUMBER`: the catalogue's ceiling on an issue number. */
const MAX_ISSUE_NUMBER = 999_999

/** The issue number out of what was typed, for a refusal's sentence; null when none is legible. */
function issueNumber(ref: string): string | null {
  const m = /(?:#|\/issues\/)(\d+)\/?$/.exec(ref.trim())
  return m?.[1] ?? null
}

/** The page head: `6 runners · issue runs use claude-code`. */
export function issueSubmitHead(n: number): string {
  return `${n} runner${n === 1 ? '' : 's'} · issue runs use ${ISSUE_RUN_PROFILE}`
}

export function IssueSubmitScreen({ go }: { go: (to: string) => void }) {
  return (
    <Screen
      title="Submit from a GitHub issue"
      load={loadCapacity}
      // A PLAIN HEAD, AS THE TASK AND WORKFLOW FORMS HAVE (visual QA runs2-08,
      // 2026-10-29): "6 runner profiles · 20 pools you are admitted against"
      // counted pools nobody picks here. The catalogue's size, and the one
      // runner an issue run takes, since "6 to choose from" would not be true.
      summary={(c) => issueSubmitHead(Object.keys(c.runner_profiles).length)}
      empty={{
        heading: 'No pools came back',
        body: 'The read of the pools this tenant is admitted against succeeded and returned none. The planner is an agent like any other and needs them, and the runner profiles arrive in that same response, so this page cannot submit.',
      }}
    >
      {(c) => <IssueForm capacity={c} go={go} />}
    </Screen>
  )
}

function IssueForm({ capacity, go }: { capacity: Capacity; go: (to: string) => void }) {
  const [typed, setTyped] = useState('')
  const [preview, setPreview] = useState<Preview>({ kind: 'idle' })
  const [closedOk, setClosedOk] = useState(false)
  const [approval, setApproval] = useState<'required' | 'auto'>('required')
  const [rounds, setRounds] = useState(String(DEFAULT_FIX_ROUNDS))
  // NULL UNTIL SOMEBODY TOUCHES THE SWITCH, and then their choice (live QA
  // SUB-2). The switch started `false` whatever the API said, so a repository
  // whose preview served `auto_merge.default: true` -- SwarmCloud's -- was
  // submitted with `auto_merge: false` under "Off (default)". Untouched, the
  // switch is the served default for the issue on screen; a new read resets it.
  const [merge, setMerge] = useState<boolean | null>(null)
  const [sending, setSending] = useState<Sending>({ kind: 'idle' })
  const keys = useProviderKeys()

  // THE CATALOGUE, every runner in it, and all but one held back with the
  // reason. A profile the platform already disables keeps its own reason.
  const catalogue = useMemo(
    () =>
      Object.entries(capacity.runner_profiles)
        .sort((a, b) => a[0].localeCompare(b[0]))
        .map(([n, p]): [string, RunnerProfile] =>
          n === ISSUE_RUN_PROFILE || (p.available ?? true) === false
            ? [n, p]
            : [n, { ...p, available: false, disabled_reason: ONLY_RUNNER }]),
    [capacity],
  )
  const runner = capacity.runner_profiles[ISSUE_RUN_PROFILE] ?? null
  const runnerOff = runner !== null && (runner.available ?? true) === false

  const read = preview.kind === 'read' ? preview.data : null
  const closed = read !== null && read.issue.state === 'closed'
  const roundsN = /^\d+$/.test(rounds.trim()) ? Number(rounds.trim()) : NaN
  const roundsOk = Number.isInteger(roundsN) && roundsN >= MIN_FIX_ROUNDS && roundsN <= MAX_FIX_ROUNDS
  const blocked = read === null || (closed && !closedOk) || !roundsOk || sending.kind === 'sending'
  // ONE TENANT, FROM ONE SOURCE, BEFORE AND AFTER THE READ (visual QA
  // runs-submit-13, 2026-10-28). It was the preview's tenant once an issue was
  // read and the capacity read's before, so one submission showed two tenants
  // -- beside a runner card whose pools are the capacity read's. That read is
  // the page's, it names the tenant the run's pools and runner are admitted
  // in, and POST /v1/runs resolves the same caller; the preview's is the
  // fallback for a server that does not send it there.
  const tenant = capacity.tenant_id ?? read?.tenant_id ?? null
  // Offered only on the API's word, for the issue on screen; off otherwise.
  const mergeServed = read?.auto_merge ?? null
  const mergeOffered = mergeServed?.available === true
  // `default` is served beside `available` (`issueruns.auto_merge_availability`)
  // and is already false whenever it is not available. Read as a boolean, so a
  // server older than the field reads as off, which is what it did.
  const mergeDefault = mergeOffered && (mergeServed as { default?: unknown } | null)?.default === true
  const mergeOn = mergeOffered && (merge ?? mergeDefault)

  const parses = parseIssueRef(typed) !== null

  // THE ISSUE'S OPEN RUNS (visual QA runs-submit-14, 2026-10-28): #512 had a
  // Planned and a Running run and the page offered "Plan this issue" as if it
  // were new. Read once per issue read, from the runs list Work › Runs draws.
  const openRuns = useOpenRuns(read)

  async function readIssue() {
    const ref = typed.trim()
    if (ref === '' || parseIssueRef(ref) === null) return
    setPreview({ kind: 'reading', ref })
    setClosedOk(false)
    setMerge(null)
    const r = await loadIssuePreview(ref)
    // A reference changed while its read was in flight: the answer is not the
    // one on screen any more, so it is dropped.
    setPreview((now) => {
      if (now.kind !== 'reading' || now.ref !== ref) return now
      if (r.status === 'ok' || r.status === 'stale') return { kind: 'read', ref, data: r.data, at: Date.now() }
      if (r.status === 'error') return { kind: 'refused', ref, error: r.error }
      return now
    })
  }

  function retype(value: string) {
    setTyped(value)
    // The preview belongs to the reference it was read for; another reference
    // has to be read again before it can be planned.
    if (preview.kind !== 'idle' && value.trim() !== preview.ref) setPreview({ kind: 'idle' })
    setSending({ kind: 'idle' })
  }

  // A REF, NOT `sending`: two activations inside one render both read the
  // state as it was before the first (live QA SUB-1, the task form's defect).
  const inFlight = useRef(false)

  async function submit(e?: FormEvent) {
    e?.preventDefault()
    if (inFlight.current || blocked || read === null) return
    const body: RunCreateBody = {
      // THE REFERENCE AS THE PREVIEW SERVED IT, so what is planned is what was shown.
      issue: read.issue.ref,
      plan_approval: approval,
      // True only when the switch was offered (the API said it would take it) and turned on.
      auto_merge: mergeOn,
      fix_rounds: roundsN,
    }
    setSending({ kind: 'sending' })
    inFlight.current = true
    const r = await createRun(body)
    inFlight.current = false
    if (r.status === 'ok' || r.status === 'stale') {
      const id = (r.data as { run?: { id?: unknown } } | null)?.run?.id
      if (typeof id === 'string' && id !== '') {
        setSending({ kind: 'idle' })
        go(runAddress(id))
        return
      }
      setSending({ kind: 'unnamed' })
      return
    }
    if (r.status === 'error') setSending({ kind: 'failed', error: r.error })
  }

  return (
    <form className="sbf in-form" onSubmit={submit}>
      <div className="sbf-build">
        <Move n={1} title="Name the issue">
          <div className="in-ask">
            <input
              className="mono in-ref"
              aria-label="Issue reference"
              placeholder="owner/repo#N or an issue URL"
              value={typed}
              spellCheck={false}
              autoComplete="off"
              aria-invalid={(typed.trim() !== '' && !parses) || undefined}
              aria-describedby={typed.trim() !== '' && !parses ? 'in-ref-say' : undefined}
              onChange={(e) => retype(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  void readIssue()
                }
              }}
            />
            <Button disabled={!parses || preview.kind === 'reading'}
              onClick={() => void readIssue()}>
              Read
            </Button>
          </div>
          {preview.kind === 'idle' && typed.trim() !== '' && !parses && (
            <p id="in-ref-say" className="sb-note sbf-bad">
              Not an issue reference yet: name it as <code>owner/repo#N</code> or paste its URL,{' '}
              <code>https://github.com/owner/repo/issues/N</code>.
            </p>
          )}
          {preview.kind === 'idle' && (typed.trim() === '' || parses) && (
            <p className="sb-note">
              Read with this tenant&rsquo;s own forge credential: only repositories it can read are reachable from
              here. Nothing is created by reading.
            </p>
          )}
          {preview.kind === 'reading' && (
            <div className="in-reading" aria-busy="true">
              <span className="skeleton ctl-skeleton-row" />
              <span className="skeleton ctl-skeleton-row" />
              <p className="sb-note">reading {preview.ref}…</p>
            </div>
          )}
          {preview.kind === 'read' && (
            <IssuePreviewCard read={preview.data} at={preview.at} closedOk={closedOk} onPlanAnyway={() => setClosedOk(true)}
              onTakeBack={() => setClosedOk(false)} />
          )}
          {preview.kind === 'refused' && (
            <Refusal error={preview.error} typed={preview.ref} tenant={tenant}
              onAgain={() => void readIssue()} onTask={() => go(TASK_FORM)} />
          )}
        </Move>

        <Move n={2} title="The runner it plans and builds with" dim={read === null}>
          <RunnerPicker group="issue-runner" label="runner" profiles={catalogue} chosen={ISSUE_RUN_PROFILE}
            keys={keys} onPick={() => {}} onlyUsable heldAs="not used for issue runs" />
          {runner === null && (
            <p className="warn-text" role="alert">
              {ISSUE_RUN_PROFILE} is not in this tenant&rsquo;s catalogue, so the API would have no runner to plan with.
            </p>
          )}
          {runnerOff && (
            <p className="warn-text" role="alert">
              {ISSUE_RUN_PROFILE} is disabled for this tenant: {runner?.disabled_reason || 'refused by the platform'}.
            </p>
          )}
          <p className="sb-note">The planner and every step it plans run as {ISSUE_RUN_PROFILE}.</p>
        </Move>

        <Move n={3} title="Decide what it may do on its own" dim={read === null}>
          <div className="in-choice">
            <div className="in-seg" role="radiogroup" aria-label="Plan approval">
              <label className={`in-seg-o${approval === 'required' ? ' is-on' : ''}`}>
                <input type="radio" name="plan-approval" value="required" checked={approval === 'required'}
                  onChange={() => setApproval('required')} />
                Required
              </label>
              <label className={`in-seg-o${approval === 'auto' ? ' is-on' : ''}`}>
                <input type="radio" name="plan-approval" value="auto" checked={approval === 'auto'}
                  onChange={() => setApproval('auto')} />
                Auto
              </label>
            </div>
            <div className="in-choice-t">
              <b>Plan approval</b>
              <small>
                {approval === 'required'
                  ? 'Required (default): the run waits, planned and holding no capacity, until someone in this workspace approves, edits or rejects the plan.'
                  : 'Auto: the plan is approved the moment it is read, and the work starts without anyone being asked.'}
              </small>
            </div>
          </div>

          <div className="in-choice in-merge">
            <span className="in-switch">
              {/* NAMED BY ITS OWN TEXT (visual QA runs2-34, 2026-10-29): the
                  switch's name was an aria-label beside a <b> that said the
                  same and was tied to nothing, so `input.labels` was empty
                  and a click on the words did not reach the switch. The words
                  are its <label> now, as the fix-rounds field's are. */}
              <input id="in-auto-merge" type="checkbox" role="switch" checked={mergeOn} disabled={!mergeOffered}
                onChange={(e) => setMerge(e.target.checked)} />
              <span className="in-switch-w">{mergeOn ? 'on' : 'off'}</span>
            </span>
            <div className="in-choice-t">
              <label htmlFor="in-auto-merge"><b>Merge the pull request when it is ready</b></label>
              {mergeOffered ? (
                <small>
                  {/* "(default)" FOLLOWS THE SERVED DEFAULT, not the word "off". */}
                  {mergeOn
                    ? `On${mergeDefault ? ' (default)' : ''}: once every required check is green, the merge step merges the pull request at the sha it proved.`
                    : `Off${mergeDefault ? '' : ' (default)'}: a person merges the run’s pull request.`}
                </small>
              ) : read === null ? (
                /* NOTHING HAS BEEN ASKED YET (chrome-C29): the preview is what
                   says whether POST /v1/runs takes auto-merge, so before it the
                   switch says that, not "not available". */
                <small>Decided when the issue is read: the API says then whether this run may merge its own pull request.</small>
              ) : mergeServed === null ? (
                <small>
                  This API did not say whether it takes auto-merge, so the run is created with it off and a person merges
                  its pull request.
                </small>
              ) : (
                /* The API's answer for the issue on screen, and its own reason. */
                <small>
                  Not available for this run: the API refuses auto-merge now, so a person merges the run&rsquo;s pull request.
                  {mergeServed.reason && <> The API says: <i>{mergeServed.reason}</i></>}
                </small>
              )}
            </div>
          </div>

          <div className="in-choice">
            <input id="in-fix-rounds" className="mono in-rounds" type="number" inputMode="numeric"
              min={MIN_FIX_ROUNDS} max={MAX_FIX_ROUNDS} step={1} value={rounds}
              aria-invalid={!roundsOk || undefined} aria-describedby={roundsOk ? undefined : 'in-fix-rounds-say'}
              onChange={(e) => setRounds(e.target.value)}
              // ENTER HERE CONFIRMS THE NUMBER, IT DOES NOT PLAN THE ISSUE (live
              // QA SUB-13). As an implicit submission it posted /v1/runs and
              // started a planner with no click; the reference field's Enter
              // reads, and the plan is started by its button only.
              onKeyDown={(e) => { if (e.key === 'Enter') e.preventDefault() }} />
            <div className="in-choice-t">
              <label htmlFor="in-fix-rounds"><b>Fix rounds when checks go red</b></label>
              <small>
                {MIN_FIX_ROUNDS}–{MAX_FIX_ROUNDS}. Once the pull request is open, each red CI reading spends one fix
                round; past the cap the run fails with the failing checks&rsquo; excerpt.
              </small>
              {/* A STATE, NOT A CLICK'S RESULT (VQA V132): "Not sent" is what a
                  click that was refused says (TS-15), and nothing had been
                  clicked. The workflow form's priority says it the same way. */}
              {!roundsOk && <small id="in-fix-rounds-say" className="sbf-bad" role="alert">Must be a whole number from {MIN_FIX_ROUNDS} to {MAX_FIX_ROUNDS}.</small>}
            </div>
          </div>
        </Move>
      </div>

      <aside className="sbf-side">
        <div className="sbf-send in-send">
          <h2>{blocked && sending.kind !== 'sending' ? 'Not ready to send' : 'Ready to send'}</h2>
          <ul className="ctl-facts">
            <li className={read === null ? 'ctl-fact is-absent' : 'ctl-fact'}>
              <b>issue</b>
              {/* THE NUMBER FIRST (live QA SUB-9): the cut fell on the repository
                  and took the number with it -- the live deployment's own
                  repository, drawn as its owner and a cut name with no number --
                  and the number is the one part that tells two issues apart. The
                  repository is the next row; the whole reference is the title. */}
              {/* AN UNPARSEABLE REFERENCE IS NOT "READ ONE FIRST" (runs-submit-23):
                  Read cannot read it, so the summary says what is wrong. */}
              {read === null ? <i className="ctl-em">&mdash; {typed.trim() !== '' && !parses ? 'not an issue reference yet' : 'read one first'}</i>
                : <span><code className="in-ref-code" title={read.issue.ref}>#{read.issue.number}</code> · {read.issue.state}{closed && !closedOk && <i className="sbf-bad"> · not confirmed</i>}</span>}
            </li>
            <li className={read === null ? 'ctl-fact is-absent' : 'ctl-fact'}>
              <b>repository</b>
              {/* WRAPPED, NOT CUT (visual QA runs-submit-24): `in-repo` lets the
                  repository break across lines (intake.css), the whole name
                  still its title. */}
              {read === null ? <i className="ctl-em">&mdash;</i> : <code className="in-repo" title={`${read.issue.owner}/${read.issue.repo}`}>{read.issue.owner}/{read.issue.repo}</code>}
            </li>
            <li className="ctl-fact">
              <b>runner</b>
              <code>{ISSUE_RUN_PROFILE}</code>
            </li>
            {/* THE SUMMARY FOLLOWS THE CHOICE (browser QA N13, 2026-10-04): on
                Auto it said "runs straight on" beside "lands as PLANNING,
                then PLANNED" and "nothing else runs until the plan is
                approved". Each line below reads `approval`. Planning is not
                free: the planner is an ordinary task that leases capacity, as
                RUNNING's steps do; only PLANNED holds nothing (invariant 1). */}
            <li className="ctl-fact">
              <b>plan</b>
              {approval === 'required' ? 'waits for approval' : 'approved as soon as it is written'}
            </li>
            <li className="ctl-fact">
              <b>auto-merge</b>
              {mergeOn ? 'on' : mergeOffered ? 'off' : `off · ${read === null ? AUTO_MERGE_UNREAD : mergeServed === null ? AUTO_MERGE_UNSAID : AUTO_MERGE_REFUSED}`}
            </li>
            <li className={roundsOk ? 'ctl-fact' : 'ctl-fact is-absent'}>
              <b>fix rounds</b>
              {roundsOk ? `up to ${roundsN}` : <i className="sbf-bad">a whole number from {MIN_FIX_ROUNDS} to {MAX_FIX_ROUNDS}</i>}
            </li>
            <li className={tenant === null ? 'ctl-fact is-absent' : 'ctl-fact'}>
              <b>tenant</b>
              {tenant === null ? <i className="ctl-em">&mdash; not served</i> : <code>{tenant}</code>}
            </li>
            {/* UNDER ITS KEY, NOT BESIDE IT (visual QA QA-073): the longest
                value in the card wrapped into a right-aligned column beside
                "lands as". `in-fact-long` stacks it, left-aligned. */}
            <li className="ctl-fact in-fact-long">
              <b>lands as</b>
              {approval === 'required'
                ? 'planning, then planned · a planned run holds no capacity'
                : 'planning, then planned, approved on the next tick, then running · only the planner and the workflow\'s steps hold capacity'}
            </li>
          </ul>
          <OpenRunsNote state={openRuns} go={go} />
          <Button type="submit" kind="primary" full disabled={blocked}>
            {sending.kind === 'sending' ? 'Planning…' : 'Plan this issue'}
          </Button>
          <p className="sb-note">
            {approval === 'required'
              ? 'A planner agent reads the issue and the repository and writes a plan; nothing else runs until the plan is approved.'
              : 'A planner agent reads the issue and the repository and writes a plan; the plan is approved the moment it is written and the work starts without anyone being asked.'}
          </p>
          {/* IN THE SLOT THE TASK AND WORKFLOW FORMS DRAW THEIRS IN (VQA V175):
              at 400 a POST 500 was drawn under the button at y 714-944 with
              no scroll, and only its heading showed above the Reads dock.
              The slot brings it into view and takes the keyboard to it. */}
          <OutcomeSlot at={sending} shown={sending.kind === 'failed' || sending.kind === 'unnamed'}>
            {/* The submission gate's refusal (#847): why, and the setup step that fixes it. */}
            {sending.kind === 'failed' && workspaceRefusal(sending.error) !== null && <WorkspaceRefusalBanner error={sending.error} />}
            {sending.kind === 'failed' && workspaceRefusal(sending.error) === null && (
              <>
                <FailedPanel error={sending.error} onRetry={() => void submit()} />
                <p className="warn-text">
                  <strong>Check Runs before submitting again.</strong> This failed on a write, so the run may exist anyway.
                </p>
              </>
            )}
            {sending.kind === 'unnamed' && (
              <p className="warn-text" role="alert">
                The API accepted this and named no run. Open Runs to find it before submitting again.
              </p>
            )}
          </OutcomeSlot>
        </div>
      </aside>
    </form>
  )
}


type OpenRuns =
  | { kind: 'none' }
  | { kind: 'found'; runs: IssueRunSummary[] }
  | { kind: 'unread'; message: string }

/** `owner/repo#N`, lower-cased: GitHub's names are case-insensitive. */
function issueKey(i: { owner: string; repo: string; number: number }): string {
  return `${i.owner}/${i.repo}#${i.number}`.toLowerCase()
}

/**
 * The read issue's runs that are not terminal, off `GET /v1/runs` (the first
 * page, newest first: the one Work › Runs and Submit's recent list read).
 * `none` before a read and when there are none; `unread` when the runs read
 * failed, because "no open run" is a figure nobody measured then.
 */
function useOpenRuns(read: IssuePreviewRead | null): OpenRuns {
  const [state, setState] = useState<OpenRuns>({ kind: 'none' })
  const key = read === null ? null : issueKey(read.issue)
  useEffect(() => {
    setState({ kind: 'none' })
    if (key === null) return
    let live = true
    void loadRuns().then((r) => {
      if (!live) return
      if (r.status === 'ok' || r.status === 'stale') {
        const runs = r.data.runs.filter((x) => !x.terminal && x.issue !== undefined && x.issue !== null && issueKey(x.issue) === key)
        setState(runs.length === 0 ? { kind: 'none' } : { kind: 'found', runs })
      } else if (r.status === 'error') {
        setState({ kind: 'unread', message: r.error.message })
      }
    })
    return () => {
      live = false
    }
  }, [key])
  return state
}

/** A run's state in words, sentence case: `Runs.tsx`'s `runStateWord`, which imports this file. */
function stateWord(state: IssueRunState): string {
  return (state.charAt(0) + state.slice(1).toLowerCase()).replace(/_/g, ' ')
}

/**
 * THE ISSUE'S OPEN RUNS, BEFORE THE BUTTON (runs-submit-14), so planning it
 * again is a choice and not an accident. Each run is its state and how long
 * ago it was created -- "Planned 10d ago" -- linked to its page, the id in the
 * link's title (visual QA runs2-35, 2026-10-29: "run_4c1e09d2 (Planned)" named
 * a run by its raw id, which tells a reader nothing). A creation instant that
 * does not parse leaves the state alone: never a made-up age.
 */
function OpenRunsNote({ state, go }: { state: OpenRuns; go: (to: string) => void }) {
  const now = useNow()
  if (state.kind === 'none') return null
  if (state.kind === 'unread') {
    return (
      <p className="sb-note in-open-runs-unread">
        Whether this issue already has open runs was not checked: {state.message}
      </p>
    )
  }
  return (
    <p className="warn-text in-open-runs" role="status">
      Open runs for this issue:{' '}
      {state.runs.map((r, i) => (
        <Fragment key={r.id}>
          {i > 0 && ', '}
          <a href={`/runs/${encodeURIComponent(r.id)}`} title={r.id} onClick={(e) => {
            if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
            e.preventDefault()
            go(runAddress(r.id))
          }}>{stateWord(r.state)}{Number.isFinite(Date.parse(r.created_at)) ? ` ${timeAgo(r.created_at, now)}` : ''}</a>
        </Fragment>
      ))}
      . Planning it again starts another run.
    </p>
  )
}

/**
 * When the preview was read, as an age (`just now`, `3m ago`) with the
 * absolute instant -- `2026-10-07 04:29:44 UTC` -- for its title. An instant
 * that is not a number is `at an unknown time` with no title: never a made-up
 * clock.
 */
export function readAtText(at: number, now: number): { text: string; title: string | undefined } {
  if (!Number.isFinite(at)) return { text: 'at an unknown time', title: undefined }
  const iso = new Date(at).toISOString()
  return { text: timeAgo(at, now), title: `${iso.slice(0, 10)} ${iso.slice(11, 19)} UTC` }
}

/** What was read, as served: title, state, comments, labels, the body, the link. */
export function IssuePreviewCard({ read, at, closedOk, onPlanAnyway, onTakeBack }: {
  read: IssuePreviewRead; at: number; closedOk: boolean; onPlanAnyway: () => void
  /** Withdraws "Plan it anyway" (live QA SUB-12); absent, nothing is offered. */
  onTakeBack?: () => void
}) {
  const { issue } = read
  const [whole, setWhole] = useState(false)
  // WHETHER THE CUT HIDES ANYTHING (VQA V133), measured off the box: "Show the
  // whole body · 5 characters" was offered under a body already fully on
  // screen. Measured once per body while cut; open, it stays offered so it
  // can close again.
  const bodyRef = useRef<HTMLDivElement>(null)
  const [hidden, setHidden] = useState(false)
  useLayoutEffect(() => {
    const el = bodyRef.current
    if (el === null || whole) return
    setHidden(el.scrollHeight > el.clientHeight + 1)
  }, [issue.body, whole])
  const closed = issue.state === 'closed'
  // AN AGE, LIKE EVERYTHING ELSE IN THE AREA (QA G4-34, 2026-10-07). It
  // printed the local wall clock -- "read 21:29:44", no date and no zone --
  // beside relative ages; before that "11:45 PM" (N20). It reads "read just
  // now" and moves on the console's shared age tick, and the absolute instant,
  // in UTC and saying so, is the line's title.
  const now = useNow()
  const time = readAtText(at, now)
  return (
    <section className="in-preview" aria-label="The issue as read">
      <h3>{issue.title === '' ? <i className="ctl-em">&mdash; untitled</i> : <InlineText text={issue.title} />}</h3>
      {/* EACH SEPARATOR ENDS ITS ITEM (N20): between items, a wrapped line
          opened on "·". Now an item carries the dot after it, so a line can
          end on one but never start with one. */}
      {/* NO DANGLING DOT (owner QA R15, 2026-10-04): the read's provenance
          wrapped under its own dot, leaving "3 comments ·" at a line's end
          with nothing after it. The issue's three facts are one line, dotted
          between and never after the last; when it was read is its own. */}
      <p className="in-meta">
        {/* THE DOT FOLLOWS THE REFERENCE'S LAST LINE (VQA V252): in the
            item's flex row a wrapped reference was one box and the dot a
            second, stranded at the card's right edge. As inline text
            (`.in-meta-i.is-ref`, submit.css) it sits after the last
            character, held there by a no-break space. */}
        <span className="in-meta-i is-ref"><a href={issue.url} target="_blank" rel="noreferrer" className="mono">{issue.ref}</a>{'\u00a0·'}</span>
        <span className="in-meta-i">
          <NamedMark mark={closed ? 'succeeded' : 'ready'} hue={closed ? 'neu' : 'live'} word={issue.state} /> ·
        </span>
        <span className="in-meta-i">{issue.comments === 1 ? '1 comment' : `${issue.comments} comments`}</span>
      </p>
      <p className="in-meta in-read-at" title={time.title}>read {time.text} with this tenant&rsquo;s forge credential</p>
      {issue.labels.length > 0 ? (
        <p className="in-chips">{issue.labels.map((l) => <Tag key={l}>{l}</Tag>)}</p>
      ) : (
        <p className="sb-note">no labels</p>
      )}
      {issue.body === '' ? (
        <p className="sb-note">The issue has no body.</p>
      ) : (
        <>
          {/* THE BODY AS MARKDOWN (browser QA D23, 2026-10-04): it printed its
              markers ("### What are you trying to do?"). `Markdown` is the
              console's own renderer -- React elements, never HTML, links only
              for http(s) -- so the issue's text cannot inject anything. */}
          <div ref={bodyRef} className={`in-body${whole ? ' is-whole' : ''}`}>
            <Markdown source={issue.body} />
          </div>
          {(hidden || whole) && (
            <p className="in-row">
              <Button onClick={() => setWhole((w) => !w)} aria-expanded={whole}>
                {whole ? 'Show less of the body' : `Show the whole body · ${issue.body.length.toLocaleString()} characters`}
              </Button>
            </p>
          )}
        </>
      )}
      {issue.body_truncated && (
        <p className="sb-note">
          The preview holds the first part of the body only; the API cut it. The planner reads all of it again.
        </p>
      )}
      {issue.body_redacted && (
        <p className="sb-note">Values that looked like credentials were masked by the API before this was served.</p>
      )}
      {closed && (
        <div className="in-closed" role="alert">
          <WarnMark />
          <span>
            <b>This issue is closed.</b> Planning it would reopen work someone marked done.
          </span>
          {/* AND IT CAN BE TAKEN BACK (live QA SUB-12): "planning it anyway"
              had no way back but reading the issue again. */}
          {closedOk ? (
            <>
              <span className="sb-note">planning it anyway</span>
              {onTakeBack && <Button onClick={onTakeBack}>Undo</Button>}
            </>
          ) : (
            <Button onClick={onPlanAnyway}>Plan it anyway</Button>
          )}
        </div>
      )}
    </section>
  )
}

/**
 * ONE FORM PER CODE THE PREVIEW ROUTE SERVES (forge.py), and the server's own
 * sentence under it, since it names the repository, the tenant and the secret.
 * No figure: nothing was read, so no state, count or label is drawn.
 */
function Refusal({ error, typed, tenant, onAgain, onTask }: {
  error: ApiError; typed: string; tenant: string | null; onAgain: () => void; onTask: () => void
}) {
  const n = issueNumber(typed)
  const issue = n === null ? 'this issue' : `issue #${n}`
  let words: ReactNode
  let action: ReactNode = null
  switch (error.code) {
    case 'not_found':
      words = (
        <>
          <b>{n === null ? 'No such issue' : `No issue #${n}`} that this tenant can see: not found or not visible.</b>{' '}
          GitHub gives the same answer for an issue that does not exist and for a private repository the credential
          cannot read, so this cannot tell the two apart. Check the number, or ask an operator whether this
          tenant&rsquo;s credential covers the repository.
        </>
      )
      break
    case 'no_access':
      words = (
        <>
          <b>This tenant&rsquo;s forge credential was refused.</b> It lacks read access to the repository, or the
          organisation requires single sign-on approval for it. Nothing was submitted. Another tenant&rsquo;s
          repository is not reachable from here.
        </>
      )
      break
    case 'is_pull_request':
      words = (
        <>
          <b>{n === null ? 'That' : `#${n}`} is a pull request.</b> This form plans issues; a pull request that needs
          finishing is an agent on its branch.
        </>
      )
      action = <Button onClick={onTask}>Start an agent instead</Button>
      break
    case 'no_forge_credential':
      words = (
        <>
          <b>{tenant === null ? 'This tenant has' : <>Tenant <code>{tenant}</code> has</>} no forge credential,</b> so no
          issue can be read and no pull request opened. An operator stores it with{' '}
          <code>scripts/create-secrets.sh --stdin</code>. It is never pasted here.
        </>
      )
      break
    case 'read_failed':
      words = (
        <>
          <b>The read did not finish.</b> The state of {issue} is unknown, so it is shown as unknown, not as open.
        </>
      )
      action = <Button onClick={onAgain}>Read again</Button>
      break
    case 'validation_failed':
      words = (
        <>
          <b>Not an issue reference.</b> Name it as <code>owner/repo#N</code> or paste its URL.
        </>
      )
      break
    default:
      // SPACED FROM THE INPUT ROW AS EVERY OTHER ANSWER IN THIS SLOT IS
      // (VQA V251): a 404's `.in-refusal` sits 12px under it, and a 500's
      // panel sat flush. `.in-read-failed`, submit.css.
      return <div className="in-read-failed"><FailedPanel error={error} onRetry={onAgain} /></div>
  }
  return (
    <div className="in-refusal" role="alert" data-code={error.code ?? ''}>
      <WarnMark />
      <div className="in-refusal-t">
        <p>{words}</p>
        {/* The server's sentence names the tenant and the secret -- except a
            malformed reference's, which names the API's own fields
            (`repository_url`, QA G4-25) and nothing the reader typed into. */}
        {error.code !== 'validation_failed' && <p className="sb-note">{error.message}</p>}
        {action}
      </div>
    </div>
  )
}
