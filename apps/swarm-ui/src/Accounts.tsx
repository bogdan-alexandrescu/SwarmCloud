import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import {
  beginAccountSignIn,
  finishAccountSignIn,
  loadAccountsBoard,
  refreshAccount,
  removeAccount,
  setAccountLending,
  setAccountState,
  type AccountsBoard,
} from './api'
import { errorHeading, type ApiError, type Result } from './fetch'
import type { TopicId } from './help'
import { HelpCard, HelpLinks } from './HelpCard'
import { FailedPanel, Screen, timeAgo } from './Shell'
import {
  BAR_CELLS,
  FIVE_HOUR,
  SEVEN_DAY,
  accountTone,
  barFilled,
  bindingWindow,
  callbackHostOf,
  clearsIn,
  humaniseUntil,
  isProjected,
  needsAHuman,
  neverAssigned,
  pastedCodeHint,
  pluralise,
  readingOf,
  unreadableFor,
  type Account,
  type AccountAuthorization,
  type AccountReading,
  type AccountStateName,
  type AccountTone,
  type AccountWindow,
  type RefreshResult,
} from './types'

/**
 * `accountTone` -> the `.ctl-chip` modifier that draws it.
 *
 * WHY A TABLE RATHER THAN A TEMPLATE STRING. The five tones and the chip's
 * five modifiers are NOT the same vocabulary: `wait` (DRAINING) has no `is-wait`
 * and takes the caution triangle, which is the shape §6.6 assigns to
 * "approaching a limit" and is what DRAINING is. Interpolating the tone into
 * `is-${tone}` would have produced a class no rule matches, and an unmatched
 * modifier on `.ctl-chip` does not fail loudly -- it falls back to
 * `--chip-tone: var(--text-faint)`, the UNKNOWN mark. A DRAINING account would
 * have rendered as an account whose state nobody derived, which is the exact
 * class of lie §8.1 forbids. The map is what makes that impossible to write.
 */
const CHIP_MOD: Record<AccountTone, string> = {
  ok: 'is-ok',
  paused: 'is-paused',
  wait: 'is-warn',
  bad: 'is-bad',
  unknown: 'is-unknown',
}

/**
 * THE ONE PROVIDER, AND THE ONE CREDENTIAL KIND -- stated, never picked.
 *
 * This pool holds Claude subscription credentials: the OAuth pair a sign-in
 * produces, which quota-broker can exchange for a successor indefinitely.
 * There is no API-key path here and no credential-kind selector, because there
 * is nothing to choose between -- and a hardcoded value with no words around
 * it is worse than a picker, since the operator cannot even tell that a
 * decision was made for them. So the form SAYS what it sends.
 *
 * It is a named constant rather than a literal in the request body so that the
 * value printed on the form and the value the request carries cannot drift
 * apart.
 */
const SUBSCRIPTION_PROVIDER = 'anthropic'

/**
 * The state that must OUTLIVE a reload.
 *
 * Every mutation here reloads the table, and Screen reloads by being remounted
 * -- so anything held inside it is destroyed at the exact moment its answer
 * matters. A refresh that reported "the refresh token is gone" and then
 * vanished because the table refreshed is worse than no button at all: the
 * operator is left with a spinner that ended and no verdict, which is the
 * question the button existed to answer.
 *
 * A SIGN-IN IS THE SHARPEST CASE OF THAT. It spans a trip to another tab, in
 * another application, and back. What has to survive is not a verdict but a
 * live server-side record -- the `state` identifying the pending sign-in and
 * the URL that reopens the login page. Losing those to a remount makes a person
 * sign in again having done nothing wrong. So sign-ins live here, keyed by what
 * they are for: one for the add form, one per account for signing in again.
 *
 * The genuinely transient things -- a half-typed reason, a dirty lending field,
 * an unsent code -- deliberately do NOT, because they should not survive the
 * write that consumed them.
 *
 * Holding them here is only half of it. `Screen` renders `children` -- and so
 * everything below -- ONLY when it has data, so a reload that FAILS would take
 * the verdict off screen anyway, along with the table. `lastBoard` below is the
 * other half.
 */
interface Persisted {
  /** Explicitly opened or closed rows. Absent means "whatever the default is". */
  open: Record<string, boolean>
  refresh: Record<string, RefreshState>
  /** Signing in again to an account that already exists, keyed by account id. */
  reauth: Record<string, SignIn>
  /** The add-an-account sign-in. */
  signin: SignIn
}

type Patch = (fn: (p: Persisted) => Persisted) => void

const EMPTY_UI: Persisted = { open: {}, refresh: {}, reauth: {}, signin: { kind: 'idle' } }

/**
 * Capacity -> Accounts. The Claude subscriptions this platform runs agents on.
 *
 * WHY THE COLUMNS ARE `cs status`'s, EXACTLY. An operator already reads
 * ACCOUNT / 5H / 7D / CLEARS / STATE on their laptop. Inventing a second
 * vocabulary for the same five facts about the same five accounts costs them a
 * translation every time they look, and the translation is where the mistakes
 * live. So the scan line here is that line: same order, same headings, same
 * five-cell bar, same `~` for a figure that is projected rather than measured.
 * Everything this screen adds -- who it is lent to, how many agents hold it,
 * and every control -- lives in a row you open, so the table you scan is never
 * anything other than the table you already know.
 *
 * THE RULE THE WHOLE SCREEN TURNS ON. `stale` and "utilisation is zero" are
 * different claims, and so are "no reading has ever arrived" and "the reading
 * says 0%". A five-cell bar with no cells filled is the same picture for both,
 * which is why an unmeasured window draws NO bar at all and prints an em dash.
 * `account_to_api` computes `stale` server-side for the same reason: a
 * reading's age is what decides whether to believe it, and the server owns the
 * clock.
 *
 * WHAT THIS SCREEN MAY NOT DO. quota-broker is the platform's single writer for
 * subscription credentials, because refreshing an OAuth credential REVOKES the
 * token it replaces and two writers racing on one account brick it. Every
 * button here is a call to a broker route, addressed to swarm-api, which
 * proxies all of them -- the list, the register, the refresh, the lending, the
 * state, and both sign-in routes. That last pair was missing for one deploy and
 * the browser got a 404 from a path nobody had built; `SignInFailure` below is
 * what a 404 or 405 from them still reaches, because a proxy that exists today
 * can be absent in an older deployment and the screen should say which.
 * Nothing in this file reads a secret, exchanges a
 * token, retries a refresh on its own initiative, or renders key material --
 * not even its length. It does not hold the PKCE verifier either: that stays
 * server-side, keyed by `state`, because a verifier the client holds is a PKCE
 * flow that proves nothing.
 */
export function AccountsScreen() {
  // Bumping this remounts Screen, which re-runs `load`. Every mutation here
  // changes what the table says, so every mutation reloads rather than patching
  // a local copy -- a table edited client-side after a write is a table that
  // can disagree with the platform and never say so.
  const [nonce, setNonce] = useState(0)
  const [ui, setUi] = useState<Persisted>(EMPTY_UI)
  const patch: Patch = (fn) => setUi(fn)

  /**
   * THE LAST READ THAT PRODUCED ROWS, kept on OUR side of the remount.
   *
   * `Screen` owns the stale rule -- data in hand plus a failed refresh is the
   * old rows, dimmed, never a blank screen -- and implements it with a ref that
   * a remount destroys. Reloading by remount therefore disabled the one rule
   * that keeps a failed reload from wiping the screen: the table vanished, and
   * with it the refresh verdict, the sign-in in progress and the report of what
   * a sign-in did, all of which are rendered by `children` and all of which are
   * the answer to something somebody just did.
   *
   * This ref lives in `AccountsScreen`, which is NOT remounted, so it survives
   * every reload the screen triggers. `load` below hands the previous read back
   * as `stale` when a reload fails, which is the same shape `Screen` would have
   * produced from its own ref had it not just been thrown away. It is the
   * memory that was missing, not a second copy of the rule.
   *
   * The whole body is then DIMMED, verdict included -- `Screen` dims everything
   * it renders from a stale read, and opacity applies to the group, so nothing
   * inside can opt out. That is the right trade: the stale banner above says
   * the figures were read earlier, and a dimmed verdict that is still on screen
   * is what the button existed to produce. A blank one is not.
   */
  const lastBoard = useRef<{ data: AccountsBoard; fetchedAt: number } | null>(null)

  const load = async (): Promise<Result<AccountsBoard>> => {
    const res = await loadAccountsBoard()
    if (res.status === 'ok') {
      lastBoard.current = { data: res.data, fetchedAt: res.fetchedAt }
      return res
    }
    // Only a FAILED read degrades to stale. An `empty` read is a real zero and
    // must replace the rows -- showing data the platform says is gone is the
    // mirror of the bug this app exists to avoid -- and `loadAccountsBoard`
    // never reports empty anyway, so this is belt and braces rather than a
    // branch the screen relies on.
    if (res.status === 'error' && lastBoard.current !== null) {
      return {
        status: 'stale',
        data: lastBoard.current.data,
        fetchedAt: lastBoard.current.fetchedAt,
        error: res.error,
      }
    }
    return res
  }

  return (
    <Screen
      key={nonce}
      title="Accounts"
      load={load}
      summary={(b) => <SummaryLine board={b} />}
      /*
       * NO `empty` PROP, DELIBERATELY. Screen renders `empty` INSTEAD of its
       * children, so an empty pool would hide the add form -- the single
       * control that fixes an empty pool. `loadAccountsBoard` therefore never
       * reports empty, and zero accounts is handled below as a state panel
       * sitting above a form that is still on screen.
       */
    >
      {(b) => (
        <Body
          board={b}
          ui={ui}
          patch={patch}
          reload={() => setNonce((n) => n + 1)}
        />
      )}
    </Screen>
  )
}

/**
 * OWNERSHIP, NOT VISIBILITY, and the one place the rule is written.
 *
 * `GET /v1/accounts` returns what a tenant may RUN on -- owned AND lent -- so
 * "it is on my screen" is not "it is mine to change". Every mutating route in
 * routes/accounts.py resolves the account and requires ownership, which is why
 * a lent row shows no controls. Anything that TELLS a reader to use those
 * controls has to apply the same test, or it points at a row that deliberately
 * cannot answer.
 *
 * A null scope is a platform-wide listing this UI never asks for; nothing is
 * claimed either way in that case, and the account counts as the caller's,
 * because being silent about an account that IS theirs is the worse mistake.
 */
function isOwned(a: Account, scope: string | null): boolean {
  return scope === null || a.owner_tenant === scope
}

/**
 * How many documents the store could not turn into an account.
 *
 * `?? 0` because an older API serves neither field, and a page that crashed on
 * a missing count would be a worse failure than the one this reports. Zero is
 * also what the screen says nothing about, so an old deployment reads exactly
 * as it did before rather than claiming a shortfall it cannot measure.
 */
function shortfall(board: AccountsBoard): number {
  return board.page.unreadable_document_count ?? 0
}

function SummaryLine({ board }: { board: AccountsBoard }) {
  const n = board.page.accounts.length
  const scope = board.page.tenant_id
  const missing = shortfall(board)
  const broken = board.page.accounts.filter(needsAHuman)
  // Split, because they are two different jobs for two different people: one is
  // "open the row and sign in", the other is "this is not yours to fix".
  const mine = broken.filter((a) => isOwned(a, scope)).length
  const lent = broken.length - mine
  return (
    <>
      {n} account{n === 1 ? '' : 's'} &middot;{' '}
      {scope === null ? 'every tenant' : `${scope} — owned and lent to it`}
      {/* BEFORE anything derived from the rows, because it is the sentence
          that says how much of the pool the rows are. A count of accounts is
          not a fact when documents were dropped reaching it. */}
      {missing > 0 && (
        <>
          {' '}
          &middot;{' '}
          <strong>
            {missing} document{missing === 1 ? '' : 's'} unreadable, so this list is short
          </strong>
        </>
      )}
      {mine > 0 && (
        <>
          {' '}
          &middot; <strong>{mine} need{mine === 1 ? 's' : ''} a sign-in</strong>
        </>
      )}
      {lent > 0 && (
        <>
          {' '}
          &middot; {lent} lent account{lent === 1 ? '' : 's'} waiting on{' '}
          {lent === 1 ? 'its' : 'their'} owner
        </>
      )}
    </>
  )
}

function Body({
  board,
  ui,
  patch,
  reload,
}: {
  board: AccountsBoard
  ui: Persisted
  patch: Patch
  reload: () => void
}) {
  const accounts = [...board.page.accounts].sort((a, b) =>
    a.account_id.localeCompare(b.account_id),
  )
  return (
    <>
      <Broken accounts={accounts} scope={board.page.tenant_id} />
      <Pool board={board} accounts={accounts} ui={ui} patch={patch} reload={reload} />
      <AddAccount board={board} accounts={accounts} ui={ui} patch={patch} reload={reload} />
      <HelpLinks topics={ACCOUNT_TOPICS} />
    </>
  )
}

/**
 * The accounts a timer will never fix, at the top, before anything else.
 *
 * REAUTH_REQUIRED is the one state the sweep deliberately stops retrying, so
 * it is the one state that stays until a person acts. Reporting it only as a
 * chip in a row it shares with five healthy ones is how it gets scrolled past.
 *
 * WHICH PERSON, THOUGH. A LENT account in this state is on the reader's screen
 * and is not theirs to fix: its row shows no sign-in control, no state control
 * and no refresh button, because those routes answer 404 for a tenant that does
 * not own the account. Telling every borrower to "open the row and sign in"
 * sends them to a row that deliberately refuses, so the two cases are separated
 * here and only one of them is an instruction.
 */
function Broken({ accounts, scope }: { accounts: Account[]; scope: string | null }) {
  const broken = accounts.filter(needsAHuman)
  if (broken.length === 0) return null
  const mine = broken.filter((a) => isOwned(a, scope))
  const lent = broken.filter((a) => !isOwned(a, scope))
  const soleLentOwner = lent.length === 1 ? (lent[0]?.owner_tenant ?? null) : null
  return (
    /* B4.4: TWO FACT ROWS, NOT TWO PARAGRAPHS.
       The banner said the same thing three times -- once in the heading, once
       per group, and once more in the per-row `Warnings` list underneath. What
       a reader has to know is WHICH accounts and WHO can act, and both are
       keys and values.

       THE OWNERSHIP SPLIT SURVIVES INTACT, because it is the part that decides
       whether the reader can do anything: a LENT account in this state shows
       no sign-in control, no state control and no refresh button, since those
       routes answer 404 for a tenant that does not own it. The `yours` row
       carries the action; the `theirs` row names the owner to ask. Sending
       every borrower to "open the row and sign in" sends them to a row that
       deliberately refuses. */
    <div className="banner bad" role="status">
      <ul className="ctl-facts">
        <li className="ctl-fact">
          <b>needs a person</b>
          <span className="acct-flags">
            <span
              className="ctl-mark is-unread"
              aria-label={`${broken.length} account${broken.length === 1 ? '' : 's'} cannot be refreshed by the platform: the refresh token is gone or unreadable, and the sweep has stopped retrying. Nothing new is assigned to them until a person signs in.`}
            >
              not read
            </span>
            <span>{broken.length}</span>
          </span>
        </li>
        {mine.length > 0 && (
          <li className="ctl-fact">
            <b>
              yours
            </b>
            <span className="acct-flags">
              <span className="mono">{mine.map((a) => a.account_id).join(', ')}</span>
              <span>open the row · Sign in again</span>
            </span>
          </li>
        )}
        {lent.length > 0 && (
          <li className="ctl-fact">
            <b>
              not yours
            </b>
            <span className="acct-flags">
              <span className="mono">{lent.map((a) => a.account_id).join(', ')}</span>
              <span>
                ask {soleLentOwner !== null ? soleLentOwner : 'the owners'}
              </span>
            </span>
          </li>
        )}
      </ul>
    </div>
  )
}

// ---------------------------------------------------------------------------
// The pool table -- exactly the five columns `cs status` prints
// ---------------------------------------------------------------------------

function Pool({
  board,
  accounts,
  ui,
  patch,
  reload,
}: {
  board: AccountsBoard
  accounts: Account[]
  ui: Persisted
  patch: Patch
  reload: () => void
}) {
  // REAUTH_REQUIRED rows start open: that is the one state where the row's
  // controls are the reason for visiting the screen. `ui.open` holds only the
  // rows somebody has explicitly opened or closed, so the default can change
  // under an operator (an account breaking while they look at it) without
  // overriding a choice they made.
  const isOpen = (a: Account) => ui.open[a.account_id] ?? needsAHuman(a)
  const toggle = (a: Account) =>
    patch((p) => ({ ...p, open: { ...p.open, [a.account_id]: !isOpen(a) } }))

  // One clock for the whole render. Reading Date.now() per cell would let two
  // cells in the same row disagree by a second, which is exactly the kind of
  // flicker that makes a number look untrustworthy when it is not.
  const now = Date.now()

  if (accounts.length === 0) {
    return (
      <section className="section panel">
        <h2>The pool</h2>
        <div className="state" role="status">
          {/* A REAL ZERO, SAID AS ONE, on the surface. What the zero COSTS --
              that work waits rather than fails, quietly -- is the topic. */}
          {/* ONE OF THIS SCREEN'S TWO `?` (B7.4), AND IT IS THE ONE THAT
              RENDERS WHEN THE POOL IS EMPTY -- the other sits on the table
              below, which this branch does not draw. What it holds is the only
              thing an empty pool cannot show: the COST of the zero. Nothing on
              this panel says that work submitted now parks and waits instead of
              failing, and an operator who assumes the second will go looking
              for an error that was never raised.
              `honesty.prose.test.tsx` pins a `?` to this panel. */}
          <h3>
            No accounts registered
            <HelpCard topic="park-on-missing-credential" />
          </h3>
          {/* B4.4: THE MARK IS THE CLAIM. "The read succeeded and returned
              nothing -- a real zero, not a failed query" was a sentence a
              reader had to find and parse; `.ctl-mark.is-zero` says the same
              thing in two words, in the fixed vocabulary every other screen
              uses for the same kind of nothing, and it survives greyscale and
              a screenshot. The argument -- what a zero here COSTS, which is
              that work parks rather than fails -- is the `?` above. */}
          <p className="acct-flags">
            <span className="ctl-mark is-zero">real zero</span>
            <span>add the first one below</span>
          </p>
        </div>
      </section>
    )
  }

  return (
    <section className="section panel">
      <h2>
        The pool
        {/* THE QUALIFIER SLOT, NOT A CHIP (CP-22). Body-size mono beside the
            title, where Pools, Holders and Provider quota put a fact about
            the section in `.ctl-card-note`. */}
        <span className="ctl-card-note is-end">{pluralise(accounts.length, 'account')}</span>
      </h2>
      {/* §B6.3: `is-stacked`, because `the pool` is the widest table in the
          app -- 909px of columns inside a 358px phone, 61% of it behind a
          scrollbar this platform does not paint. */}
      <div className="table-wrap is-stacked">
        <table role="table" className="pools accounts">
          <thead role="rowgroup">
            <tr role="row">
              {/* §13.2: sentence case, in the SOURCE. `table.pools thead th`
                  used to uppercase these and no longer does, so a literal
                  written in capitals is now the only thing on the screen still
                  shouting -- and it shouts in one table rather than all of
                  them, which is the exact inconsistency the rule removes. */}
              <th role="columnheader" scope="col">Account</th>
              <th role="columnheader" scope="col" className="n">5h</th>
              <th role="columnheader" scope="col" className="n">7d</th>
              <th role="columnheader" scope="col" className="n">Clears</th>
              <th role="columnheader" scope="col">State</th>
            </tr>
          </thead>
          <tbody role="rowgroup">
            {accounts.map((a) => (
              <PoolRows
                key={a.account_id}
                account={a}
                board={board}
                now={now}
                readAt={board.readAt}
                open={isOpen(a)}
                onToggle={() => toggle(a)}
                ui={ui}
                patch={patch}
                reload={reload}
              />
            ))}
          </tbody>
        </table>
      </div>
      <PoolFindings
        accounts={accounts}
        scope={board.page.tenant_id}
        unreadableDocuments={board.page.unreadable_documents ?? []}
        unreadableDocumentCount={shortfall(board)}
      />
      {/* THE SHORTFALL AND THE TILDE BOTH STAY, and both are provenance about
          the READ rather than about any row -- which is why they are one foot
          line under the card and not a paragraph per figure. A row count over
          documents that were dropped is not a count; a figure that is real but
          not current has to say so where it is read. */}
      <p className="provenance">
        {accounts.length} row{accounts.length === 1 ? '' : 's'}
        {shortfall(board) > 0 && (
          <>
            {' '}
            of {accounts.length + shortfall(board)} &middot; {shortfall(board)} unread
          </>
        )}{' '}
        &middot; ~ is projected, not measured
        {/* THE OTHER OF THIS SCREEN'S TWO `?` (B7.4), and the reason it is one
            of the two is the STALE MARK. This console draws three different
            things -- a measured value, a value too old to trust, and one nobody
            measured -- and the tilde is the middle one, the only mark of the
            three that is a claim about TIME rather than about presence. "~12%"
            is a real reading that has aged; what a reader must not do is treat
            it as current, and what they must also not do is treat it as
            missing. The line beside this says which mark the tilde is; the
            topic is what may and may not be concluded from it. No label carries
            that. */}
        <HelpCard topic="projected-not-measured" />
      </p>
    </section>
  )
}

function PoolRows({
  account,
  board,
  now,
  readAt,
  open,
  onToggle,
  ui,
  patch,
  reload,
}: {
  account: Account
  board: AccountsBoard
  now: number
  /** When the board these rows came from was read. See `AccountsBoard.readAt`. */
  readAt: number
  open: boolean
  onToggle: () => void
  ui: Persisted
  patch: Patch
  reload: () => void
}) {
  const tone = accountTone(account.state)
  const five = readingOf(account, FIVE_HOUR)
  const seven = readingOf(account, SEVEN_DAY)
  // THE ROW THAT READS HEALTHIEST AND SERVES NOBODY. `choose()` skips an
  // account this tenant has reported unreadable, so its window, its headroom
  // and its state all describe an account that will not be handed out here --
  // and every one of those cells says so confidently. Marked like a row that
  // cannot serve, because that is what it is.
  const unusable = unreadableFor(account, board.page.tenant_id)

  return (
    <>
      <tr
        role="row"
        className={
          tone === 'bad' || unusable ? 'over' : tone === 'paused' ? 'paused' : undefined
        }
      >
        <th role="rowheader" scope="row" className="pool-name">
          <button type="button" className="acct-open" aria-expanded={open} onClick={onToggle}>
            <span className="acct-caret" aria-hidden>
              {open ? '▾' : '▸'}
            </span>
            {/* The LABEL is the name `cs status` prints; the full id is what
                gets pasted into a command. Both are here, neither transformed. */}
            {account.label}
          </button>
          <span className="raw">{account.account_id}</span>
        </th>
        <WindowCell reading={five} window="five-hour" label="5h" />
        <WindowCell reading={seven} window="seven-day" label="7d" />
        <ClearsCell account={account} now={now} readAt={readAt} />
        <td role="cell" data-label="State" className="acct-statecell">
          {/* B4.6: THE STATE IS A MARK AND A WORD, NOT A BADGE (§6.6).
              This was `.tag.acct-state` -- a 1px border in the state hue, a
              6px radius, 13px mono 500 UPPERCASE tracked, and the WORD itself
              painted in the hue. Six of them down one column is six boxes to
              say one word six times, which is the owner's "status chips are
              heavy" on this screen.

              `.ctl-chip` is the primitive that already made this decision for
              the whole product, so this is a COLLAPSE (§9.3), not a restyle:
              a 10px hued mark whose silhouette differs per state, and the word
              beside it at --t-body in --text. NOTHING THAT CARRIED INFORMATION
              WAS REMOVED -- the tone moved from `color` to `--chip-tone`, and
              the shape vocabulary is a second, greyscale-safe channel the
              bordered pill never had. The word arrives from the API shouting
              (`REAUTH_REQUIRED`); the chip lowercases it exactly as `.wf-state`
              does on Workflows, which §6.6 names as the screen to match. */}
          <span className={`ctl-chip acct-state ${CHIP_MOD[tone]}`}>
            <i aria-hidden="true" />
            {account.state}
          </span>
          {/* BEFORE the state's own note, and not instead of it. The state is
              still AVAILABLE and that is still true: the account is fine, the
              pool simply will not hand it to THIS tenant while the report
              stands. Two facts, and the one that decides whether work runs
              here is this one. */}
          {/* B4.4: A MARK, NOT A CLAUSE. This was a seven-word sentence per
              affected row. The mark carries the same fact in the vocabulary
              every screen uses for it, and the full explanation -- that the
              report expires after thirty minutes, and that a persistent one
              means a missing roles/secretmanager.secretAccessor grant for this
              tenant's worker service account -- is the mark's accessible name,
              which a `title=` alone was not: a mark is focusable and a span is
              not. */}
          {unusable && (
            <span
              className="ctl-mark is-unread acct-unusable"
              aria-label={`The pool is skipping this account for ${board.page.tenant_id}: this tenant reported it could not read the account's secret, and accounts.choose skips it for this tenant because of that. It is not skipped for anyone else. The report is forgotten thirty minutes after it was made, so this clears by itself if the cause was a freshly onboarded account; if it persists, the missing piece is a roles/secretmanager.secretAccessor grant on the secret for this tenant's worker service account.`}
            >
              skipped here
            </span>
          )}
          <StateNote account={account} />
        </td>
      </tr>
      {open && (
        <tr role="row" className="acct-detail-row">
          <td role="cell" colSpan={5}>
            <Detail
              account={account}
              board={board}
              now={now}
              ui={ui}
              patch={patch}
              reload={reload}
            />
          </td>
        </tr>
      )}
    </>
  )
}

/**
 * Why a reading is what it is, in the cell's own tooltip.
 *
 * SHORT, AND CARRYING NO ARGUMENT. These were five sentences each, reachable
 * only by hovering a table cell with a mouse -- which is the failure mode the
 * `?` exists to fix, not a use of it. The reasoning is at
 * `#help/absent-vs-zero` and `#help/projected-not-measured`, reachable from
 * the provenance line under the table and from the footer.
 *
 * NOTHING MEASURED LEFT WITH THEM. An unmeasured cell still draws NO BAR at
 * all and still prints an em dash; a projected one still prints its tilde.
 * That is what a reader sees without hovering anything, and it is what
 * `src/__tests__/honesty.prose.test.tsx` asserts.
 *
 * The 'stale' case also drops a restatement of the broker's staleness
 * window, which §5 of the migration table lists as one of five copies of a
 * platform figure no route publishes.
 */
function readingTitle(r: AccountReading, window: string): string {
  switch (r.kind) {
    case 'never':
      return 'No reading has ever arrived. Unknown is not zero.'
    case 'absent':
      return `The last reading carried no ${window} window. Unmeasured is not zero.`
    case 'reset':
      return `Passed its reset ${timeAgo(r.resetsAt)}; this describes the window before it. Marked ~.`
    case 'stale':
      return `Last read ${timeAgo(r.observedAt)}, past the age a reading is trusted for. Marked ~.`
    case 'live':
      return `Read ${timeAgo(r.observedAt)}, within the age a reading is trusted for.`
  }
}

function WindowCell({
  reading,
  window,
  // The column name, passed rather than derived: below 900px §B6.3 prints it
  // beside the value, and `five-hour` is not what the column is called.
  label,
}: {
  reading: AccountReading
  window: string
  label: string
}) {
  const title = readingTitle(reading, window)

  if (reading.kind === 'never' || reading.kind === 'absent') {
    return (
      <td role="cell" data-label={label} className="n acct-window acct-unmeasured" title={title}>
        {/* AN EM DASH AND NO BAR. Drawing an empty five-cell bar here would be
            pixel-for-pixel identical to a measured 0%, which is the one
            confusion this column must never allow. `ctl-em` is the shared
            treatment for an absent measurement, so this cell and every other
            screen's em dash are the same class of thing rather than the same
            character by coincidence. */}
        <span className="acct-pct ctl-em">&mdash;</span>
        <span className="acct-why">not measured</span>
      </td>
    )
  }

  const projected = isProjected(reading)
  return (
    <td
      role="cell"
      data-label={label}
      className={`n acct-window${projected ? ' acct-projected' : ''}`}
      title={title}
    >
      <span className="acct-pct">
        {projected && <span className="acct-tilde">~</span>}
        {Math.round(reading.pct)}%
      </span>
      <Bar pct={reading.pct} projected={projected} />
    </td>
  )
}

/**
 * The five-cell bar, as `miniBar` draws it in claudeswitch: round to the
 * nearest fifth, clamp, five cells either filled or not.
 *
 * `aria-hidden`, because the percentage beside it is the same fact and a screen
 * reader announcing five geometric shapes learns nothing. Colour is never the
 * only signal here either: a projected bar is grey AND its figure carries `~`.
 *
 * NO AMBER BAND, and that is a decision rather than an omission: any threshold
 * between "fine" and "getting full" would be a number invented in this file,
 * and the platform's own assign floor lives in quota-broker where nothing
 * checks that a TypeScript copy still matches it. The one treatment here that
 * is a fact rather than a judgement is a window that is fully spent.
 */
function Bar({ pct, projected }: { pct: number; projected: boolean }) {
  const filled = barFilled(pct)
  const spent = !projected && pct >= 100
  return (
    <span
      className={`acct-bar${projected ? ' acct-projected' : ''}${spent ? ' spent' : ''}`}
      aria-hidden
    >
      {Array.from({ length: BAR_CELLS }, (_, i) => (
        <i key={i} className={i < filled ? 'on' : ''} />
      ))}
    </span>
  )
}

/**
 * Time until the BINDING window resets -- the one that will actually stop you.
 *
 * Not the five-hour and not an average: an account at 5% on its five-hour and
 * 90% on its weekly is stopped by the weekly, and refilling the five-hour does
 * nothing for it.
 */
function ClearsCell({
  account,
  now,
  readAt,
}: {
  account: Account
  now: number
  readAt: number
}) {
  const binding = bindingWindow(account)
  if (binding === null) {
    return (
      <td
        role="cell"
        data-label="Clears"
        className="n acct-unmeasured ctl-em"
        title="No window reading, so there is no reset instant to count down to. This is an absence of information, not a window that never clears."
      >
        &mdash;
      </td>
    )
  }
  const reading = readingOf(account, binding.key)
  const windowName = binding.key.replace(/_/g, '-')

  // A binding window that has ALREADY reset does not have a countdown; it has
  // happened. "~now" would be arithmetic pretending to be an answer, and it
  // reads as "about to clear" when the truth is the opposite -- it cleared, and
  // what is missing is a reading taken since.
  if (binding.window.reset) {
    return (
      <td
        role="cell"
        data-label="Clears"
        className="n acct-projected"
        title={`The ${windowName} window, the binding one, passed its reset ${timeAgo(binding.window.resets_at)}. It has cleared; no reading taken since has arrived, so the figures on this row still describe the window before it.`}
      >
        cleared
      </td>
    )
  }
  // THE INSTANT HAS PASSED AND THIS BOARD DID NOT CALL IT RESET. Two different
  // things put a row here and NOTHING ON THIS SCREEN MEASURES WHICH: the window
  // may have cleared in the time between the read and this render, or the
  // instant and this browser's clock may simply not agree. `reset` was computed
  // server-side when the board was serialised; `now` is this browser's clock
  // at render; there is no second reading of the platform's clock to difference
  // them against. So the tooltip reports the two things that WERE measured --
  // the instant is behind us, and the board is this old -- and names no cause.
  //
  // `~now`, AND THE MARK IS NOT OPTIONAL. "now" here is a countdown that has
  // run out, not a claim that anything cleared, and this row reached this
  // branch precisely because no reading confirms either. The provenance line
  // under the table and the legend both tell the reader that a figure marked ~
  // is projected rather than measured, and `.acct-projected` is grey with an
  // amber `~` so that the distinction survives a printout or a photograph --
  // grey alone is the failure that treatment exists to prevent. Before this
  // branch existed such a row fell through to the one below and was marked;
  // dropping the mark here made the same cell quietly assert a reading.
  const resetsAt = new Date(binding.window.resets_at).getTime()
  if (Number.isFinite(resetsAt) && resetsAt <= now) {
    return (
      <td
        role="cell"
        data-label="Clears"
        className="n acct-projected"
        title={`The ${windowName} window is the binding one, and the reset instant the platform gave for it is already in the past. The board this row came from was read ${timeAgo(readAt)} and had not marked the window reset. This page does not compare its clock with the platform's, so it cannot tell you whether the window has cleared since that read -- reload, and the platform answers.`}
      >
        <span className="acct-tilde">~</span>
        now
      </td>
    )
  }
  // The INSTANT is the provider's and counting down to it is arithmetic; what
  // is projected is the CHOICE of which window binds, when that choice came
  // from a figure that is no longer current. Marked, and the tooltip says which
  // part is uncertain.
  const uncertain = isProjected(reading)
  return (
    <td
      role="cell"
      data-label="Clears"
      className={`n${uncertain ? ' acct-projected' : ''}`}
      title={
        uncertain
          ? `Counts down to the ${windowName} window's reset, which the provider gave as an exact instant. Marked ~ because which window binds was decided from a figure that is no longer current.`
          : `The ${windowName} window is the binding one, the one that will refuse first, and this is when it resets.`
      }
    >
      {uncertain && <span className="acct-tilde">~</span>}
      {clearsIn(binding.window.resets_at, now)}
    </td>
  )
}

/** The second line under a state chip, when there is a real one to write. */
function StateNote({ account }: { account: Account }) {
  if (account.reason) return <span className="acct-why">{account.reason}</span>
  const binding = bindingWindow(account)
  if (binding && binding.window.reset) {
    // `cs status` words this "window reset - re-reading", and it means the
    // figures on this row describe a window that has already refilled.
    return <span className="acct-why">window reset, awaiting a new reading</span>
  }
  if (account.observed_at === null) return <span className="acct-why">never observed</span>
  if (account.stale) {
    return <span className="acct-why">reading is {timeAgo(account.observed_at)}</span>
  }
  return null
}

/**
 * THE TWO FINDINGS THAT ARE ABOUT THE POOL RATHER THAN ABOUT A ROW.
 *
 * B4.4 -- WHAT THIS REPLACED, AND WHY IT WAS THE CLEAREST CASE IN THE BRIEF.
 * This was `Warnings`: a `<ul>` under the table with one sentence per account,
 * ~75 rendered words on a four-account pool. Every per-row line in it restated
 * a fact the row was ALREADY drawing:
 *
 *   "X has never been observed -- unmeasured, not idle"  the row's 5H and 7D
 *       cells are already `.acct-unmeasured`, already print an em dash, and
 *       already draw NO bar; `StateNote` already says `never observed`.
 *   "X needs a sign-in. Open its row and press Sign in again."  the row
 *       already carries a red `REAUTH_REQUIRED` chip and its reason, and the
 *       row is already open -- `Pool` opens REAUTH_REQUIRED rows by default
 *       precisely because that is the state whose controls are the reason for
 *       visiting it.
 *   "X reading is 40m, past the age a reading is trusted for"  the row
 *       already prints the tilde and `StateNote` already prints the age.
 *   "the pool is skipping X for eng"  now `.ctl-mark` on the row itself.
 *
 * A list of sentences under a table is a set of claims the reader has to pair
 * back up with the rows they came from, and pairing one wrongly is worse than
 * not reading it at all -- which is the general form of the rule this console
 * is built on: an attribute of a figure cannot be separated from the figure,
 * and a paragraph beside it can.
 *
 * WHAT DID NOT MOVE ONTO A ROW, because it is not about one:
 *
 *   THE SHORTFALL. Documents the store could not parse are missing from the
 *   table AND from every count on the screen -- a partial total, §8.6. There
 *   is no row to hang it on, by definition. It is `.ctl-mark.is-partial` plus
 *   the count and the ids, which is what the sentence carried.
 *   NOTHING EVER ASSIGNED. Per row this is just a new account; across the
 *   whole pool it is the one symptom of workers that cannot reach the broker
 *   at all -- no QUOTA_BROKER_URL, or a missing run.invoker grant -- which
 *   otherwise looks exactly like a quiet week. It is a property of the SET,
 *   so it is stated once, about the set.
 */
function PoolFindings({
  accounts,
  scope,
  unreadableDocuments,
  unreadableDocumentCount,
}: {
  accounts: Account[]
  scope: string | null
  /** Documents the store could not parse, narrowed to what this caller may see. */
  unreadableDocuments: string[]
  /** How many there were in total. Never smaller than the list above. */
  unreadableDocumentCount: number
}) {
  const neverAny = accounts.length > 0 && accounts.every(neverAssigned)
  // A row the pool is skipping for THIS tenant reads healthiest and serves
  // nobody, so the count is surfaced here as well as marked on the row.
  const skipped = accounts.filter((a) => unreadableFor(a, scope)).length
  if (unreadableDocumentCount === 0 && !neverAny && skipped === 0) return null

  return (
    <ul className="ctl-facts">
      {unreadableDocumentCount > 0 && (
        <li className="ctl-fact">
          <b>unread docs</b>
          <span className="acct-flags">
            <span
              className="ctl-mark is-partial"
              aria-label={`${unreadableDocumentCount} account documents could not be read, so they are missing from this table and from every count on this screen.`}
            >
              partial
            </span>
            <span>
              {unreadableDocumentCount}
              {unreadableDocuments.length > 0 && ` \u00b7 ${unreadableDocuments.join(', ')}`}
            </span>
          </span>
        </li>
      )}
      {neverAny && (
        <li className="ctl-fact">
          <b>
            ever assigned
          </b>
          <span className="acct-flags">
            <span
              className="ctl-mark is-zero"
              aria-label="No account in this pool has ever been assigned to an agent. An idle pool and a pool nothing can reach look identical on this screen, and this is the shape of the second."
            >
              real zero
            </span>
          </span>
        </li>
      )}
      {skipped > 0 && (
        <li className="ctl-fact">
          <b>skipped here</b>
          <span className="acct-flags">
            <span className="ctl-mark is-unread">not read</span>
            <span>{skipped}</span>
          </span>
        </li>
      )}
    </ul>
  )
}

// ---------------------------------------------------------------------------
// One account, opened
// ---------------------------------------------------------------------------

function Detail({
  account,
  board,
  now,
  ui,
  patch,
  reload,
}: {
  account: Account
  board: AccountsBoard
  now: number
  ui: Persisted
  patch: Patch
  reload: () => void
}) {
  // `tenant_id` is echoed from the verified token by routes/accounts.py, so it
  // is the caller's tenant and nothing else. `isOwned` is the one definition of
  // this test, shared with the banner and the warnings list so that what points
  // at a row and what the row offers cannot disagree.
  const owned = isOwned(account, board.page.tenant_id)

  return (
    <div className="acct-detail">
      {/* B4.4: `.ctl-facts` REPLACES THE DEFINITION LIST, and every `.acct-why`
          clause under a value became either a MARK or that value's accessible
          name. The clauses were the screen's densest prose -- five of them,
          ~70 rendered words, under five one-word values -- and each was doing
          one of exactly two jobs:

            saying WHICH KIND OF NOTHING a `never` is  -> `.ctl-mark`, which is
                the vocabulary the rest of the console already uses for that
                and which a reader has learned by the time they reach this row;
            saying HOW MUCH TO TRUST a figure          -> the figure's own
                `aria-label`, plus the `?`. A caveat that can be read only by
                hovering was already a failure; a caveat attached to the figure
                is one that cannot be separated from it.

          `Agents on it` is the clearest of the five. "advisory; the lease is
          the authoritative record of who holds what" is a statement about
          WHICH RECORD TO BELIEVE -- it is an argument, and §8.4(5) puts an
          argument at `#help/<topic>`. B7.4 took the `?` off this row: the
          whole sentence is the figure's own accessible name, one element
          below, and `advisory-vs-lease` is in this screen's footer index. */}
      <ul className="ctl-facts acct-facts">
        <li className="ctl-fact">
          <b>id</b>
          <span className="mono">{account.account_id}</span>
        </li>
        <li className="ctl-fact">
          <b>owner</b>
          <span className="mono">{account.owner_tenant}</span>
          {!owned && <span className="ctl-mark is-admin">admin only</span>}
        </li>
        <li className="ctl-fact">
          <b>provider</b>
          <span className="mono">{account.provider}</span>
        </li>
        <li className="ctl-fact">
          <b>
            agents
          </b>
          <span aria-label={`${account.assigned} agents, as the broker last recorded it. This count is advisory: the lease is the authoritative record of who holds what.`}>
            {account.assigned}
          </span>
        </li>
        <li className={`ctl-fact${account.observed_at === null ? ' is-absent' : ''}`}>
          <b>last read</b>
          {account.observed_at === null ? (
            <span className="acct-flags">
              <span
                className="ctl-mark is-absent"
                aria-label="No worker has ever reported a rate-limit reading for this account. Unknown is not zero."
              >
                not measured
              </span>
            </span>
          ) : (
            <span className="acct-flags">
              <span
                aria-label={
                  account.stale
                    ? `Read ${timeAgo(account.observed_at)}, past the age a reading is trusted for, so its figures are shown projected.`
                    : `Read ${timeAgo(account.observed_at)}, within the age a reading is trusted for.`
                }
              >
                {timeAgo(account.observed_at)}
              </span>
              {account.stale && <span className="ctl-mark is-partial">partial</span>}
            </span>
          )}
        </li>
        {/* NEVER IS NOT RECENTLY. An account registered and never handed to an
            agent is indistinguishable, everywhere else on this screen, from a
            healthy one nobody happened to need -- and it is also the per-row
            shape of a pool no worker can reach. The broker serves the instant
            precisely so this row can tell the two apart, and the mark is what
            makes that distinction visible without reading a clause. */}
        <li className={`ctl-fact${neverAssigned(account) ? ' is-absent' : ''}`}>
          <b>
            last given out
          </b>
          {neverAssigned(account) ? (
            <span className="acct-flags">
              <span
                className="ctl-mark is-zero"
                aria-label="No agent has ever been handed this account. On its own that is simply a new account; across the whole pool it is the shape of workers that cannot reach the broker."
              >
                real zero
              </span>
            </span>
          ) : (
            timeAgo(account.last_assigned_at as string)
          )}
        </li>
        {/* Only when there is something to say. An empty entry here every time
            would train the eye to skip the one row that matters. */}
        {(account.unreadable_by ?? []).length > 0 && (
          <li className="ctl-fact rt-fact-wide">
            <b>unreadable by</b>
            <span className="acct-flags">
              <span className="mono">{account.unreadable_by.join(', ')}</span>
              {(account.unreadable_now ?? []).length > 0 ? (
                <span
                  className="ctl-mark is-unread"
                  aria-label={`The pool is skipping this account right now for ${account.unreadable_now.join(', ')} -- for those tenants only. The report is forgotten thirty minutes after it was made.`}
                >
                  not read
                </span>
              ) : (
                <span
                  className="ctl-mark is-zero"
                  aria-label="Every one of those reports has aged out, so the pool is skipping this account for nobody. The record is kept because a report that keeps coming back is a missing secretAccessor grant rather than an onboarding delay."
                >
                  real zero
                </span>
              )}
            </span>
          </li>
        )}
      </ul>

      <AllWindows account={account} now={now} />
      {owned ? (
        <>
          <RefreshControl account={account} ui={ui} patch={patch} reload={reload} />
          <Lending account={account} board={board} reload={reload} />
          <StateControls account={account} reload={reload} />
          <Reauth account={account} ui={ui} patch={patch} reload={reload} />
          <Remove account={account} reload={reload} />
        </>
      ) : (
        <Borrowed account={account} />
      )}
    </div>
  )
}

/**
 * A lent account, with no controls and the reason in place of them.
 *
 * `GET /v1/accounts` returns what a tenant may RUN on -- owned AND lent -- and
 * that is right for the list. It is wrong as an authorisation set for a write,
 * so every mutating route in routes/accounts.py resolves the account and
 * requires ownership. Rendering the buttons anyway would be a control that
 * silently 404s, which is worse than no control: the operator would conclude
 * the account had vanished rather than that it was never theirs to change.
 */
function Borrowed({ account }: { account: Account }) {
  return (
    <div className="acct-action">
      <h4>
        Lent to you
      </h4>
      {/* THE ABSENCE OF CONTROLS IS THE FACT, and it needs a name beside it:
          a row with no buttons and no sentence reads as a row that failed to
          render them. */}
      <p className="muted small">
        <strong>{account.owner_tenant}</strong> owns it. Your agents can run on
        it; nothing here can change it.
      </p>
    </div>
  )
}

/**
 * Every window the provider reported, not only the two with columns.
 *
 * `Account.windows` is keyed by the provider's own window names precisely so a
 * new one can appear without a schema change. A screen that reads exactly two
 * keys and silently drops the rest would make that design invisible -- and the
 * first new window would be invisible with it.
 */
function AllWindows({ account, now }: { account: Account; now: number }) {
  const keys = Object.keys(account.windows).sort()
  const extra = keys.filter((k) => k !== FIVE_HOUR && k !== SEVEN_DAY)
  if (keys.length === 0) {
    return (
      <p className="muted small">
        No windows reported &mdash; 5H, 7D and CLEARS above are{' '}
        <strong>unmeasured, not zero</strong>.
      </p>
    )
  }
  if (extra.length === 0) return null
  return (
    <div className="acct-extra-windows">
      <h4>
        Windows with no column
      </h4>
      {extra.map((k) => {
        const r = readingOf(account, k)
        const w = account.windows[k]
        return (
          <div className="split-row" key={k}>
            <span className="sr-name">{k}</span>
            <span className="sr-n">
              {r.kind === 'never' || r.kind === 'absent'
                ? '—'
                : `${isProjected(r) ? '~' : ''}${Math.round(r.pct)}%`}
            </span>
            <ExtraClears name={k.replace(/_/g, '-')} w={w} now={now} />
          </div>
        )
      })}
    </div>
  )
}

/**
 * The clears figure for a window with no column, under the same rule CLEARS
 * uses: a window that has ALREADY reset does not get a countdown.
 *
 * `humaniseUntil` returns `now` for any duration at or below zero, and `now`
 * reads as "about to clear" when the truth is the opposite -- it cleared, and
 * what is missing is a reading taken since. These are exactly the windows a
 * provider can add without a schema change, so this is the block where the
 * first such window will appear, and it has to be right here too rather than
 * only in the column.
 */
function ExtraClears({
  name,
  w,
  now,
}: {
  name: string
  w: AccountWindow | undefined
  now: number
}) {
  if (!w) {
    return (
      <span
        className="sr-by acct-unmeasured ctl-em"
        title={`The provider reported no ${name} window in the last reading, so there is no reset instant to count down to.`}
      >
        —
      </span>
    )
  }
  if (w.reset) {
    return (
      <span
        className="sr-by acct-projected"
        title={`The ${name} window passed its reset ${timeAgo(w.resets_at)}. It has cleared; no reading taken since has arrived, so the percentage beside it still describes the window before it.`}
      >
        cleared
      </span>
    )
  }
  return (
    <span
      className="sr-by"
      title={`When the ${name} window resets, as the provider gave it.`}
    >
      {clearsIn(w.resets_at, now)}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Refresh
// ---------------------------------------------------------------------------

type RefreshState =
  | { kind: 'idle' | 'sending' }
  | { kind: 'answered'; result: RefreshResult }
  | { kind: 'failed'; error: ApiError }

/**
 * What each of the outcomes in quota_broker/credentials.py actually means for
 * the person who pressed the button.
 *
 * `ok` is NOT `refreshed`. Two of these exchanged nothing and are still
 * complete successes -- `still_valid` means the stored token is fine and the
 * pod-facing secret already carries it, which is the commonest answer on a
 * healthy account and must not be painted as a failure.
 *
 * FOUR OF THEM END IN THE SAME INSTRUCTION -- sign in again -- and they still
 * get four different explanations, because the thing that went wrong differs
 * and the next person to read this screen needs to know which. Collapsing them
 * into one sentence is exactly the mistake this codebase keeps paying for.
 */
function refreshVerdict(r: RefreshResult): { ok: boolean; heading: string; body: ReactNode } {
  switch (r.reason) {
    case 'refreshed':
      return {
        ok: true,
        heading: 'Exchanged',
        body: (
          <>
            A new pair was obtained and written: the refresh half first, then the
            access token the pods mount.{' '}
            <strong>The previous access token is now revoked</strong> &mdash; that
            is what exchanging one does, and it is why nothing outside
            quota-broker may ever do it.
          </>
        ),
      }
    case 'published':
      return {
        ok: true,
        heading: 'Published',
        body: (
          <>
            The stored token was still valid, but the secret a pod mounts did not
            carry it. It does now. This is the case that silently breaks every
            task for an account whose credential is working perfectly, so seeing
            it here means something was genuinely fixed.
          </>
        ),
      }
    case 'published_unverified':
      return {
        ok: true,
        heading: 'Published, but nothing confirmed it was needed',
        body: (
          <>
            The stored token was still valid and was written to the secret a pod
            mounts &mdash; without the broker being able to read that secret
            first, so it could not tell whether the write was necessary. The
            credential is fine and pods have the right token. What is wrong is
            the broker&rsquo;s IAM: it needs{' '}
            <code>roles/secretmanager.secretAccessor</code> on this
            account&rsquo;s secret. Until it has that, the usage poller cannot
            read the token either, so this account&rsquo;s headroom stays at
            whatever it was last seen to be.
          </>
        ),
      }
    case 'unverified_skipped':
      return {
        ok: false,
        heading: 'Nothing could confirm what the secret holds, so nothing was written',
        body: (
          <>
            The stored token is still valid, but the broker could read neither the
            secret a pod mounts nor its own record of what it last published
            there. With no evidence in either direction it declined to write &mdash;
            deliberately, because writing on a failed read is what added ~1,700
            identical versions to every account secret in September. The
            credential itself is fine. Two things to check: the broker needs{' '}
            <code>roles/secretmanager.secretAccessor</code> on this
            account&rsquo;s secret, and its Firestore{' '}
            <code>credential_publications</code> collection has to be reachable.
            Until one of them works, this account&rsquo;s pod-facing secret is
            only rewritten when the credential actually rotates.
          </>
        ),
      }
    case 'still_valid':
      return {
        ok: true,
        heading: 'Nothing to do',
        body: (
          <>
            The stored token is still valid and the pod-facing secret already
            carries it, so nothing was exchanged and nothing was written. This is
            the healthy answer, not a failure &mdash; an exchange only happens
            inside the last three hours of the access token&rsquo;s life.
          </>
        ),
      }
    case 'reauth_required':
      return {
        ok: false,
        heading: 'The refresh token is gone',
        body: (
          <>
            The token endpoint refused the exchange. Only a person can fix this,
            so the broker has stopped trying and moved this account to
            REAUTH_REQUIRED. Press <em>Sign in again</em> below &mdash; it is the
            same sign-in that added the account, and this screen puts the state
            back afterwards as a visible second step.
          </>
        ),
      }
    case 'unreadable':
      return {
        ok: false,
        heading: 'The stored credential could not be parsed',
        body: (
          <>
            What is in Secret Manager is not a credential this can read, so there
            is nothing to exchange &mdash; a different fault from a refresh token
            the endpoint rejected. The account has been moved to REAUTH_REQUIRED.
            Press <em>Sign in again</em> below: a fresh sign-in writes the shape
            the broker produces, which is the shape it can read.
          </>
        ),
      }
    case 'no_refresh_credential':
      return {
        ok: false,
        heading: 'There is no refresh half to exchange',
        body: (
          <>
            No <code>-refresh</code> secret exists for this account, so there is
            nothing to exchange and nothing was rejected. That is the shape a
            lone long-lived access token has &mdash; one token, nothing beside it
            &mdash; and it cannot be kept alive by anything. Press{' '}
            <em>Sign in again</em> below: a sign-in always yields a pair.
          </>
        ),
      }
    case 'store_unavailable':
      return {
        ok: false,
        heading: 'Secret Manager did not answer',
        body: (
          <>
            The credential was never read, so this says nothing about whether it
            is healthy &mdash; it is a failure to reach the store, not a verdict
            on the account. Nothing was written. Try again.
          </>
        ),
      }
    case 'publish_failed':
      return {
        ok: false,
        heading: 'The token is fine; writing it failed',
        body: (
          <>
            A valid access token could not be written to the secret pods mount, so
            pods are still reading whatever was there before. The credential is
            not the problem. Try again, and if it persists this is a Secret
            Manager permission or availability problem rather than an account one.
          </>
        ),
      }
    case 'refresh_failed':
      return {
        ok: false,
        heading: 'The exchange failed, and may be transient',
        body: (
          <>
            The token endpoint did not complete the exchange, and did not say the
            credential is dead. The account has deliberately NOT been moved to
            REAUTH_REQUIRED, so the sweep will try again on its next tick. If it
            keeps answering this, treat it as needing a sign-in.
          </>
        ),
      }
    default:
      return {
        ok: r.refreshed,
        heading: r.refreshed ? 'Refreshed' : 'Not refreshed',
        // An unrecognised reason is reported verbatim rather than mapped onto
        // the nearest known one. A wrong explanation is worse than none.
        body: (
          <>
            The broker answered with a reason this screen has no copy for:{' '}
            <code>{r.reason}</code>. It is shown as the broker wrote it rather
            than guessed at.
          </>
        ),
      }
  }
}

function RefreshControl({
  account,
  ui,
  patch,
  reload,
}: {
  account: Account
  ui: Persisted
  patch: Patch
  reload: () => void
}) {
  // Held above the remount boundary: the verdict is the whole point of the
  // button, and reloading the table must not take it away.
  const state = ui.refresh[account.account_id] ?? { kind: 'idle' }
  const setState = (next: RefreshState) =>
    patch((p) => ({ ...p, refresh: { ...p.refresh, [account.account_id]: next } }))

  const run = async () => {
    setState({ kind: 'sending' })
    const res = await refreshAccount(account.account_id)
    if (res.status === 'ok') {
      setState({ kind: 'answered', result: res.data.refresh })
      reload()
      return
    }
    if (res.status === 'error' || res.status === 'stale') {
      setState({ kind: 'failed', error: res.error })
      return
    }
    setState({ kind: 'idle' })
  }

  return (
    <div className="acct-action">
      <h4>
        Refresh now
      </h4>
      <button type="button" disabled={state.kind === 'sending'} onClick={() => void run()}>
        {state.kind === 'sending' ? 'exchanging…' : 'Refresh this account'}
      </button>

      {state.kind === 'answered' && <RefreshAnswer result={state.result} />}
      {state.kind === 'failed' && (
        <>
          <FailedPanel error={state.error} onRetry={() => void run()} />
          {/* A write is not a read. A failure after the request left the browser
              does not prove nothing happened, and here "something happened"
              means a token may already have been revoked. */}
          {/* THE INSTRUCTION STAYS ON THE SURFACE (§6: an actionable
              explanation may be summarised in the card, but its imperative
              clause stays next to the control it constrains). Burying this
              one costs a revoked token. */}
          <p className="warn-text">
            <strong>Reload this screen before pressing refresh again.</strong>{' '}
            This failed on a write, so the credential may have been exchanged
            anyway.
          </p>
        </>
      )}
    </div>
  )
}

function RefreshAnswer({ result }: { result: RefreshResult }) {
  const verdict = refreshVerdict(result)
  // An expiry already in the past is a real and alarming answer -- a published
  // token that is dead on arrival -- so it gets its own words rather than
  // "expires in now", which reads as "shortly" and means the opposite.
  const expiresAt = result.expires_at ? new Date(result.expires_at).getTime() : null
  const expired = expiresAt !== null && Number.isFinite(expiresAt) && expiresAt <= Date.now()
  return (
    <div className={`state ${verdict.ok ? 'acct-ok' : 'failed'}`} role="status">
      <h3>{verdict.heading}</h3>
      <p>{verdict.body}</p>
      <p className="checked-at">
        {/* Both raw fields, because they are what the broker actually said and
            what a log line or a bug report will be matched against. */}
        <code>refreshed={String(result.refreshed)}</code>{' '}
        <code>reason={result.reason}</code>
        {result.expires_at &&
          (expired ? (
            <>
              {' '}
              &middot;{' '}
              <strong>
                the access token it reports is ALREADY EXPIRED, so pods will fail
                on it
              </strong>
            </>
          ) : (
            <>
              {' '}
              &middot; the access token this account publishes expires in{' '}
              {clearsIn(result.expires_at, Date.now())}
            </>
          ))}
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Lending
// ---------------------------------------------------------------------------

/**
 * Which other tenants this account will serve.
 *
 * ISOLATION IS THE DEFAULT AND SHARING IS A DECISION WITH A NAME ON IT.
 * CONTRACT.md invariant 9 says a tenant's credentials must never be reachable
 * from another tenant's pod; lending narrows that to "unless its owner said so,
 * in writing, per account" rather than punching a hole in it. So this control
 * starts empty, says plainly who can reach the credential, and never offers an
 * "everyone" shortcut.
 */
function Lending({
  account,
  board,
  reload,
}: {
  account: Account
  board: AccountsBoard
  reload: () => void
}) {
  const [draft, setDraft] = useState(account.lend_to.join(', '))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const [saved, setSaved] = useState(false)

  const parsed = splitTenants(draft)
  const owner = account.owner_tenant
  // Silently dropping the owner would make the saved list differ from the typed
  // one with nothing on screen to explain why, so it is called out instead.
  const includesOwner = parsed.includes(owner)
  const willSave = parsed.filter((t) => t !== owner)
  const dirty = willSave.join(' ') !== [...account.lend_to].join(' ')

  const save = async () => {
    setBusy(true)
    setError(null)
    setSaved(false)
    const res = await setAccountLending(account.account_id, willSave)
    setBusy(false)
    if (res.status === 'ok') {
      setSaved(true)
      reload()
    } else if (res.status === 'error' || res.status === 'stale') {
      setError(res.error)
    }
  }

  return (
    <div className="acct-action">
      <h4>
        Lending
      </h4>
      {/* WHO IT SERVES IS THE FACT, and it is the whole reason the panel is
          open. What lending MEANS is the topic beside the heading. B4.4: the
          fact was a sentence with a verb in it and is now a fact row -- the
          key `serves` carries the verb, which is why the sentence never
          needed one. */}
      <ul className="ctl-facts">
        <li className="ctl-fact">
          <b>serves</b>
          <span className="mono">
            {account.lend_to.length === 0
              ? owner
              : `${owner}, ${account.lend_to.join(', ')}`}
          </span>
        </li>
      </ul>
      <span className="limit-edit">
        <input
          type="text"
          className="mono acct-wide"
          value={draft}
          disabled={busy}
          placeholder="tenant ids, comma separated; empty means no lending"
          aria-label={`Tenants ${account.account_id} is lent to`}
          list={board.tenants ? 'acct-tenant-ids' : undefined}
          onChange={(e) => {
            setDraft(e.target.value)
            setSaved(false)
          }}
        />
        <button type="button" onClick={() => void save()} disabled={!dirty || busy}>
          {busy ? 'saving…' : 'save lending'}
        </button>
        {/* The second `.tag` on this screen, collapsed into the same primitive
            for the same reason as the state chip above. */}
        {saved && (
          <span className="ctl-chip is-ok">
            <i aria-hidden="true" />
            saved
          </span>
        )}
      </span>
      {includesOwner && (
        <p className="muted small">
          <code>{owner}</code> owns this account, so it is not sent as a loan.
        </p>
      )}
      {/* THE CONTROL IS ABSENT AND SAYS WHY. A picker that is simply not
            drawn is indistinguishable from one that failed to render. */}
      {board.tenants === null && (
        <p className="muted small">
          <strong>No tenant picker:</strong> the tenant list could not be read.{' '}
          {board.tenantsDetail} Ids typed here are still checked by the
          platform.
        </p>
      )}
      {board.tenants !== null && (
        <datalist id="acct-tenant-ids">
          {board.tenants.map((t) => (
            <option key={t.tenant_id} value={t.tenant_id} />
          ))}
        </datalist>
      )}
      {error && (
        <p className="warn-text">
          {errorHeading(error)} &mdash; {error.message} The lending list was not
          changed.
        </p>
      )}
    </div>
  )
}

function splitTenants(raw: string): string[] {
  const seen: string[] = []
  for (const part of raw.split(/[\s,]+/)) {
    const t = part.trim()
    if (t !== '' && !seen.includes(t)) seen.push(t)
  }
  return seen
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

/**
 * A state button's label, and the one spelling the prose that names the button
 * uses too -- so "use move to available" cannot drift from the button it means.
 */
function moveLabel(to: AccountStateName): string {
  return `move to ${to.toLowerCase()}`
}

/**
 * PAUSED, DRAINING and back.
 *
 * The three are not degrees of one thing and the copy must not let them read
 * that way: PAUSED means "no new work, keep what is running", DRAINING means
 * "get off", and AVAILABLE is the only state a NEW agent may be started on.
 * REAUTH_REQUIRED is deliberately absent from these buttons -- it is a verdict
 * the broker reaches from a failed exchange, not a state to declare by hand.
 */
function StateControls({ account, reload }: { account: Account; reload: () => void }) {
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState<AccountStateName | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [done, setDone] = useState<AccountStateName | null>(null)

  const move = async (to: AccountStateName) => {
    setBusy(to)
    setError(null)
    setDone(null)
    const res = await setAccountState(account.account_id, to, reason.trim())
    setBusy(null)
    if (res.status === 'ok') {
      setDone(to)
      setReason('')
      reload()
    } else if (res.status === 'error' || res.status === 'stale') {
      setError(res.error)
    }
  }

  const targets: { to: AccountStateName; why: string }[] = [
    { to: 'AVAILABLE', why: 'assignable to new agents' },
    { to: 'PAUSED', why: 'no new agents; the ones on it keep running' },
    { to: 'DRAINING', why: 'no new agents, and the ones on it are moved off' },
  ]

  return (
    <div className="acct-action">
      <h4>
        State
      </h4>
      <span className="t-label">
        reason
      </span>
      <span className="limit-edit">
        <input
          type="text"
          className="acct-wide"
          value={reason}
          disabled={busy !== null}
          placeholder="why, e.g. held back for Monday's release run"
          aria-label={`Reason for changing the state of ${account.account_id}`}
          onChange={(e) => setReason(e.target.value)}
        />
      </span>
      {/* LOWERCASE, AS THE STATE CHIP BESIDE THEM IS (CP-23). The chip
          lowercases the enum the API sends; these printed it raw, so the row
          read `available` in its state column and `move to AVAILABLE` on the
          button that sets it -- one word, two spellings, and the capitals were
          the only thing on the screen still shouting. The prose that names
          these buttons (`SignInDone`) spells them the same way. */}
      <div className="acct-buttons">
        {targets.map((t) => (
          <button
            key={t.to}
            type="button"
            title={t.why}
            disabled={busy !== null || account.state === t.to}
            onClick={() => void move(t.to)}
          >
            {busy === t.to ? 'saving…' : moveLabel(t.to)}
          </button>
        ))}
      </div>
      {done && <p className="muted small">Moved to {done.toLowerCase()}.</p>}
      {error && (
        <p className="warn-text">
          {errorHeading(error)} &mdash; {error.message} The state was not changed.
        </p>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// The sign-in -- the only way a credential gets into this pool
// ---------------------------------------------------------------------------
//
// WHAT USED TO BE HERE, and why it is not coming back. A textarea, and
// instructions to run `security find-generic-password -s "Claude
// Code-credentials" -w`, copy the JSON it printed and paste it "verbatim, the
// wrapper included". It was a bad experience and it was also fragile: the
// commonest failure was a hand-edited value that parsed on the operator's
// machine and not here, reported as a 422 about a missing refresh token, which
// named the symptom rather than the cause. There is no paste box left in this
// file and no fallback to one -- a keychain field kept "just in case" is how
// the flow that was removed comes back.
//
// TWO STEPS, BECAUSE THE PROVIDER'S OAUTH CLIENT ALLOWS EXACTLY ONE REDIRECT
// TARGET: its own callback page, which DISPLAYS a code. A third-party
// application cannot register `https://swarm.saga.xyz/callback`, so the browser
// cannot come back here and the code on that page is what closes the loop. The
// screen says that out loud rather than leaving the paste looking like an
// oversight somebody will try to "fix" later.

/**
 * One sign-in, as the five things that can be true of it.
 *
 * `open` IS THE STATE THAT MATTERS. It holds a record that exists on the
 * SERVER -- a pending sign-in keyed by `state`, holding the PKCE verifier, the
 * label and the lending list -- so it is not a UI mode, it is a claim about
 * something real that will expire. That is why it carries `startedAt`: the
 * deadline is measured from the moment the server issued it, not from a render.
 *
 * `open` also survives a FAILED exchange, on purpose: the broker deletes the
 * pending record only once an account exists, so a mistyped or stale code costs
 * one paste rather than the whole sign-in.
 */
type SignIn =
  | { kind: 'idle' }
  | { kind: 'starting' }
  | { kind: 'start_failed'; error: ApiError }
  | {
      kind: 'open'
      label: string
      lendTo: string[]
      auth: AccountAuthorization
      /** When the server issued it. The deadline is measured from here. */
      startedAt: number
      /** What happened when the sign-in page was asked to open, if it was. */
      opened: 'not_yet' | 'opened' | 'blocked'
      sending: boolean
      /**
       * The last exchange refusal, if there was one.
       *
       * The sign-in is STILL `open` as far as this page is concerned, which is
       * not the same as the platform still holding it: two of the four
       * refusals mean the pending record is gone. `exchangeRefusal` tells them
       * apart, and it is what decides whether the paste field stays live.
       */
      failed: ApiError | null
    }
  | {
      kind: 'done'
      account: Account
      expiresAt: string | null
      /** Only for a sign-in that had to put a state back. Null otherwise. */
      restore: { ok: true } | { ok: false; error: ApiError | null } | null
    }

/** A non-ok Result turned into the one error it carries. */
function failureOf(res: Result<unknown>): ApiError {
  if (res.status === 'error' || res.status === 'stale') return res.error
  return {
    kind: 'server_error',
    httpStatus: null,
    code: null,
    message: 'The request did not complete, and the platform did not say why.',
  }
}

/**
 * The one failure this screen has to NAME rather than describe.
 *
 * The sign-in routes are ones this page is certain it asked for at the right
 * path, so a 404 or a 405 from them is not "you typed something wrong" and it
 * is not "the platform is down" -- it is an API that does not serve the sign-in
 * yet. A 405 in particular is what an API whose only route at that path is a
 * DELETE answers, and bare "HTTP 405" sends an operator looking at their own
 * request. `classify` in fetch.ts cannot know that, because it is per-status
 * and this is per-route; so it is decided here, where the route is known.
 */
function isRouteMissing(e: ApiError): boolean {
  return e.httpStatus === 404 || e.httpStatus === 405
}

/**
 * WHICH OF THE EXCHANGE'S REFUSALS THIS IS. The test is not "did it fail" but
 * "WHAT IS TRUE NOW", and the answers are opposites of each other.
 *
 * `sign_in_gone` used to hold two of these at once, and the single paragraph
 * it printed asserted "Nothing was created and nothing was changed" for both.
 * That sentence is right for one of them and false for the other in the way
 * that costs most: `finish_account_authorization` in quota_broker/main.py
 * deletes the pending record on exactly two paths -- for age BEFORE anything
 * is redeemed, and immediately AFTER an account has been written by
 * `_provision_and_register`. An absent record is
 * therefore, most often, a sign-in that SUCCEEDED -- so the reader is told an
 * account does not exist while it sits in the pool, and on a re-auth while the
 * credential they were replacing has already been replaced, which revokes the
 * one it replaced. The reader's next action differs completely, and that is
 * the only reason to tell them anything at all.
 *
 * The buckets, and what each one leaves behind:
 *
 *  - `route_missing` -- 404/405. The request never reached a handler.
 *  - `accepted_unnamed` -- the platform answered 201 and named no account.
 *    Synthesised in `finishAccountSignIn`; see `httpStatus === 201` below.
 *    The sign-in is OVER and an account has probably been created.
 *  - `no_answer` -- `fetch` itself failed, so `httpStatus` is null. NOBODY
 *    REFUSED ANYTHING: the exchange may have been completed in full and only
 *    the answer lost. Unknowable from here, and said so.
 *  - `sign_in_expired` -- "took too long". The record was deleted for age
 *    before the token endpoint was called, so NOTHING WAS CREATED. Terminal.
 *  - `sign_in_absent` -- "expired or was already completed". The record is not
 *    there; the commonest reason is that it was redeemed. Terminal, and the
 *    opposite of the one above in consequence.
 *  - `other_sign_in` -- refused BEFORE the record is read, so it is untouched.
 *  - `code_refused` -- the token endpoint rejected the code, before the delete
 *    that only runs once an account exists. Still open.
 *
 * WHAT A FIXTURE CAN REACH, exactly: `fixtureFinishSignIn` produces
 * `other_sign_in` (paste a code whose `#state` is another's), `sign_in_absent`
 * (NOT reachable from a fixture: both fixture deletes close the panel first,
 * so there is no second paste -- kept in the type because the BROKER raises it
 * whenever a pending sign-in is gone, which is the ordinary case for a code
 * redeemed in another tab), `code_refused` (a code under six characters),
 * `sign_in_expired` (paste `expired`) and `accepted_unnamed` (paste
 * `unnamed`). `route_missing` and `no_answer` are transport failures a fixture
 * does not have; they are the two this list cannot promise, and this sentence
 * says which rather than claiming all of them.
 *
 * MATCHED ON THE BROKER'S OWN WORDING, AND POSITIVELY. Substring matching is
 * fragile, so an unrecognised message falls through to `unknown`, which
 * promises nothing -- rather than being forced into the bucket that happens to
 * be commonest, which is how "paste again" ended up printed over a sign-in
 * that had already ended. `classify` in fetch.ts makes the same trade for the
 * three unrelated 403s.
 */
type Refusal =
  | 'route_missing'
  | 'accepted_unnamed'
  | 'no_answer'
  | 'sign_in_expired'
  | 'sign_in_absent'
  | 'other_sign_in'
  | 'code_refused'
  | 'unknown'

function exchangeRefusal(e: ApiError): Refusal {
  if (isRouteMissing(e)) return 'route_missing'
  // AN EXACT MARKER RATHER THAN A SENTENCE. `classify` in fetch.ts only builds
  // an ApiError for a response that was NOT ok, so no refusal can carry a 201;
  // the usual source is `finishAccountSignIn`'s own synthesis for a 201 whose
  // body named no account. Matching its prose instead would put the page one
  // copy edit away from telling somebody to paste a code that was redeemed.
  //
  // IT IS NOT THE ONLY SOURCE, and an earlier version of this comment said it
  // was. `write()` in fetch.ts has a second path that never reaches `classify`:
  // an `ok` response whose body is not JSON becomes a synthesised
  // `session_expired` carrying the real status. This route answers 201, so an
  // intermediary answering HTML in front of swarm-api lands here with a
  // message that says the change was NOT saved -- under a heading saying the
  // platform accepted it. The message is therefore checked first, below, and
  // only a 201 that does not carry that signature is treated as unnamed.
  if (e.httpStatus === 201 && !e.message.toLowerCase().includes('not saved')) {
    return 'accepted_unnamed'
  }
  // `httpStatus === null` is `fetch` rejecting -- DNS, TLS, offline, a proxy
  // that timed out. Tested BEFORE the message, because the message is then the
  // browser's own and nothing in it is the platform speaking.
  if (e.httpStatus === null) return 'no_answer'
  const m = e.message.toLowerCase()
  if (m.includes('took too long')) return 'sign_in_expired'
  if (m.includes('already completed')) return 'sign_in_absent'
  if (m.includes('different sign-in')) return 'other_sign_in'
  if (m.includes('code was not accepted')) return 'code_refused'
  return 'unknown'
}

/**
 * Whether this refusal ended the sign-in for good.
 *
 * ONE DEFINITION, because three controls turn on it -- the paste field, the
 * button that reopens the authorize URL, and the link that hands the same URL
 * out as text. Two of them used to be gated here and the third was not, so an
 * operator could copy a dead link, sign in with it somewhere else and come
 * back to a disabled field.
 *
 * `no_answer` is deliberately NOT here: nothing measured says the record is
 * gone, and disabling a control on a guess is the same fault pointed the other
 * way.
 */
function signInIsOver(refusal: Refusal | null): boolean {
  return (
    refusal === 'sign_in_expired' ||
    refusal === 'sign_in_absent' ||
    refusal === 'accepted_unnamed'
  )
}

/**
 * What to do about a refused exchange, which is a different sentence for each
 * refusal because what is TRUE after each of them is different.
 *
 * TWO RULES, and they are the same defect from two sides. It may never tell
 * somebody to paste again into a sign-in the platform has already finished
 * with; and it may never contradict the platform's own message, which
 * `SignInFailure` has just rendered immediately above it. "Pasting again costs
 * nothing" printed under "do not sign in again yet" is both at once, and it
 * shipped once already.
 */
function ExchangeAdvice({
  refusal,
  mode,
  onStartOver,
  reload,
}: {
  refusal: Refusal
  /** Which control opened this sign-in, so the advice names the right one. */
  mode: 'add' | 'reauth'
  onStartOver: () => void
  /**
   * Re-reads the pool. OFFERED RATHER THAN DESCRIBED: three of these refusals
   * end with "look at the pool before you do anything else", and a sentence
   * telling somebody to find a control is worse than the control.
   *
   * WHAT IT COSTS, stated because an earlier version of this comment claimed
   * the opposite. The sign-in panel itself survives -- `ui` lives in
   * `AccountsScreen`, above the remount -- but `reload` bumps the nonce used
   * as `key` on `<Screen>`, so the subtree below it unmounts and AddAccount's
   * `label` and `lend` fields, which are plain local state, are cleared. Three
   * of the refusals that offer this button advise checking exactly those two
   * fields. So the copy beside it must tell the reader to look BEFORE pressing
   * it, and must not promise the form is still there afterwards.
   */
  reload: () => void
}) {
  // A missing route is a deployment fault and `SignInFailure` has just said so
  // at length. Adding "paste again" under it would be advice about a request
  // that never reached a handler.
  if (refusal === 'route_missing') return null

  if (refusal === 'accepted_unnamed') {
    return (
      <>
        {/* THE IMPERATIVE IS THE WHOLE PANEL and it never moves (§6). The
            mechanism -- what the success code means and why the record is
            already gone -- is the topic. */}
        <p className="warn-text">
          <strong>Do not sign in again until you have looked.</strong> The
          platform said yes; what is missing from its answer is the
          account&rsquo;s name, not the account.
        </p>
        <p className="muted small">
          {mode === 'add' ? (
            <>
              Reload the pool and look for the label you typed.{' '}
              <strong>
                Read the label and the lending list off the form first
              </strong>
              {' '}&mdash; reloading clears them.
            </>
          ) : (
            <>
              Reload the pool and open this account&rsquo;s row.{' '}
              <strong>
                Treat the credential as already replaced until you have looked.
              </strong>
            </>
          )}
        </p>
        <p className="acct-buttons">
          <button type="button" onClick={reload}>
            reload the pool
          </button>
          <button type="button" onClick={onStartOver}>
            start over
          </button>
        </p>
      </>
    )
  }

  if (refusal === 'no_answer') {
    return (
      <>
        <p className="warn-text">
          <strong>Nothing refused this &mdash; nothing answered it.</strong> The
          request did not complete, so this page never learned what the platform
          did with it, and <strong>nothing here measures which</strong>.
        </p>
        <p className="muted small">
          Reload the pool and look{' '}
          {mode === 'add' ? 'for the label you typed' : "at this account's row"}{' '}
          first &mdash; that is measurable and this is not. The paste field
          above is still live.
        </p>
        <p className="acct-buttons">
          <button type="button" onClick={reload}>
            reload the pool
          </button>
        </p>
      </>
    )
  }

  if (refusal === 'sign_in_expired') {
    return (
      <>
        {/* THE BLUNT CLAUSE STAYS. This is the one ending where the platform
            deletes BEFORE it redeems, so it is the one ending that can say
            nothing happened -- and saying it is the whole value of the
            panel. */}
        <p className="warn-text">
          <strong>
            This sign-in was held past its deadline, and pasting cannot reopen
            it.
          </strong>{' '}
          <strong>Nothing was created and nothing was changed.</strong>
        </p>
        <p className="muted small">
          {mode === 'add'
            ? 'Read the label and the lending list off the form before you start over \u2014 nothing kept a copy.'
            : 'The account in the row is untouched.'}
        </p>
        <p className="acct-buttons">
          <button type="button" onClick={onStartOver}>
            start over
          </button>
        </p>
      </>
    )
  }

  if (refusal === 'sign_in_absent') {
    return (
      <>
        <p className="warn-text">
          <strong>This sign-in is over, and it may well have worked.</strong>{' '}
          The platform is not holding a record for it, and this page cannot
          tell you whether it completed.{' '}
          <strong>So this is not &ldquo;nothing happened&rdquo;.</strong>
        </p>
        <p className="muted small">
          {mode === 'add' ? (
            <>
              Reload the pool and look for the label{' '}
              <strong>before you start another sign-in</strong>. Read the label
              and the lending list off the form first.
            </>
          ) : (
            <>
              Reload the pool and open this account&rsquo;s row.{' '}
              <strong>
                Treat the credential as possibly already replaced.
              </strong>
            </>
          )}
        </p>
        <p className="acct-buttons">
          <button type="button" onClick={reload}>
            reload the pool
          </button>
          <button type="button" onClick={onStartOver}>
            start over
          </button>
        </p>
      </>
    )
  }

  if (refusal === 'other_sign_in') {
    return (
      <p className="muted small">
        <strong>That code belongs to a different sign-in.</strong>{' '}
        <strong>This sign-in is untouched and still open</strong> &mdash; paste
        the code from the page <em>this</em> panel opened.
      </p>
    )
  }

  if (refusal === 'code_refused') {
    return (
      <p className="muted small">
        <strong>The code was refused, and this sign-in is still open.</strong>{' '}
        Paste again above, or press <em>Open the sign-in page again</em> for a
        fresh code.
      </p>
    )
  }

  return (
    <>
      <p className="muted small">
        <strong>
          The platform refused the exchange, and what it said above is all it
          said.
        </strong>{' '}
        This page does not recognise that refusal, so it does not know whether
        an account was written.
      </p>
      <p className="muted small">
        Reload the pool and look{' '}
        {mode === 'add' ? 'for the label you typed' : "at this account's row"}{' '}
        first &mdash; that is measurable and this is not.
      </p>
      <p className="acct-buttons">
        <button type="button" onClick={reload}>
          reload the pool
        </button>
      </p>
    </>
  )
}

function SignInFailure({
  error,
  what,
  onRetry,
  heading,
  tone = 'failed',
}: {
  error: ApiError
  what: string
  /**
   * A retry that actually re-sends this request, or nothing.
   *
   * OMITTED FOR THE EXCHANGE, deliberately: the code that failed was taken out
   * of the field before the request left, so there is nothing left to re-send
   * and a retry would post an empty paste. Every failure panel on this screen
   * used to render `FailedPanel`'s "Try again" wired to `() => undefined` --
   * the most prominent control on the panel, producing no request, no spinner
   * and no message, which is indistinguishable from a platform that swallowed
   * the retry.
   */
  onRetry?: () => void
  /**
   * Overrides `errorHeading`, for the one failure that is not one.
   *
   * A 201 whose body named no account arrives here as a `server_error`, whose
   * heading is "The API failed on this request" -- printed directly above a
   * message that begins "The platform accepted the sign-in". The heading and
   * the tone are the loudest things on the panel, so leaving them to the kind
   * would contradict the body text in the largest type on the screen.
   */
  heading?: string
  /** `partial` for a request that did something, `failed` for one that did not. */
  tone?: 'failed' | 'partial'
}) {
  if (isRouteMissing(error)) {
    return (
      <div className="ctl-empty is-partial" role="status">
        <h3>
          This deployment&rsquo;s API does not serve the sign-in yet
        </h3>
        {/* THE STATUS AND THE ROUTE STAY: they are what distinguishes "this
            deployment lacks the route" from "your request was refused". */}
        <p>
          The API answered <strong>HTTP {error.httpStatus}</strong> for{' '}
          <code>{what}</code> itself &mdash; not an answer about your request.
        </p>
        <p>Nothing was created and nothing was changed.</p>
        <span className="ctl-empty-foot">
          {error.code ? `${error.code} · ` : ''}
          {error.message}
        </span>
      </div>
    )
  }
  if (onRetry) return <FailedPanel error={error} onRetry={onRetry} />
  // NO `FailedPanel` WITHOUT A RETRY. It renders "Try again" for every error
  // kind but four, so the only way to show a failure with no retry is to show
  // it here. The route and the status are printed because this failure is
  // about one request rather than about the screen, and the caller renders the
  // controls that DO something directly under it.
  return (
    <div className={`state ${tone}`} role="status">
      <h3>{heading ?? errorHeading(error)}</h3>
      <p>{error.message}</p>
      <p className="checked-at">
        {what}
        {error.httpStatus !== null ? ` · HTTP ${error.httpStatus}` : ''}
        {error.code ? ` · ${error.code}` : ''}
      </p>
    </div>
  )
}

/**
 * How long the platform says it will hold this sign-in, counted down.
 *
 * TWO CLOCKS, AND THEY ARE NOT THE SAME ONE. The platform holds its pending
 * record for `expires_in_seconds`, which it reported, so that one can be
 * counted down honestly. The CODE on the callback page is the provider's and
 * expires on its own schedule, which this platform does not know and this
 * screen therefore puts no number on. One measured figure covering both would
 * be it lending its credibility to a guess.
 */
function Deadline({ auth, startedAt }: { auth: AccountAuthorization; startedAt: number }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])

  // NO COUNTDOWN, AND THE ABSENCE IS NAMED where a countdown would be. A
  // plausible number here would be this screen guessing at a deadline the
  // platform declined to give.
  if (auth.expires_in_seconds === null) {
    return (
      <p className="muted small">
        <strong>No deadline was given</strong>, so there is no countdown here.
        Finish promptly: <strong>the code is single-use</strong>.
      </p>
    )
  }
  const left = startedAt + auth.expires_in_seconds * 1000 - now
  if (left <= 0) {
    return (
      <p className="warn-text">
        The platform&rsquo;s {humaniseUntil(auth.expires_in_seconds * 1000)} hold
        has passed on this browser&rsquo;s clock. The field is still live.
      </p>
    )
  }
  return (
    <p className="muted small">
      Held for another <strong>{humaniseUntil(left)}</strong> · the code itself{' '}
      <strong>expires sooner</strong>.
    </p>
  )
}

/**
 * The warning that stops somebody adding the same account twice.
 *
 * WHICH ORGANISATION YOU SIGN IN AS COMES FROM THE COOKIE JAR, and nothing in
 * the request overrides it: there is no account hint this platform could send
 * that would change it. A private window SHARES that jar, so it is not enough
 * -- it signs you in as the same organisation again and you get a second row
 * holding the same subscription, with nothing on any screen explaining why the
 * pool did not really grow.
 *
 * It is shown BEFORE the sign-in page is opened, because once it has been
 * opened in the wrong browser the advice is useless.
 */
function DifferentBrowserNote({ mode }: { mode: 'add' | 'reauth' }) {
  return (
    // `warn`, not `bad`: nothing has failed, and nothing will fail loudly
    // either -- that is the problem. Signing in as the wrong organisation
    // succeeds, so this has to be read BEFORE the button rather than diagnosed
    // after it. `role="note"` rather than `status` because it is static text
    // that was always here, not the result of something the person just did.
    <div className="banner warn" role="note">
      <strong>A different account needs a different browser application</strong>
      {/* THE INSTRUCTION IS THE BANNER (§6). Nothing here fails loudly --
          signing in as the wrong organisation SUCCEEDS -- so this has to be
          read before the button, not diagnosed after it. */}
      <div>
        {mode === 'add' ? (
          <>
            <strong>A private window is not enough</strong> &mdash; it shares the
            same cookie jar. Copy the link below into a{' '}
            <em>different browser application</em>, or sign out of Claude in
            this one first.
          </>
        ) : (
          <>
            This replaces the credential with whichever organisation{' '}
            <em>this browser</em> is signed in as, and{' '}
            <strong>a private window shares that jar</strong>. If it is the
            wrong organisation, copy the link below into a different browser
            application instead.
          </>
        )}
      </div>
    </div>
  )
}

/**
 * Open the sign-in page, and know whether it opened.
 *
 * SYNCHRONOUS, INSIDE THE CLICK. That is why the URL is fetched in an earlier
 * step and opened in this one rather than both on one press: a `window.open`
 * in a promise continuation is the pattern popup blockers exist to stop, and a
 * sign-in that silently does not open is indistinguishable from a platform that
 * is broken.
 *
 * `noopener` is deliberately NOT in the feature string, because with it
 * `window.open` returns null unconditionally and the one thing worth knowing --
 * whether a tab actually opened -- is lost. The opener is severed immediately
 * instead. The trade is sound: the tab is being pointed at the provider's own
 * sign-in page, so even in the window between the two there is nothing
 * untrusted on the other end of the reference.
 */
function openSignInPage(url: string): 'opened' | 'blocked' {
  const w = window.open(url, '_blank')
  if (!w) return 'blocked'
  try {
    w.opener = null
  } catch {
    // A cross-origin refusal. The tab is open, which is what was asked for.
  }
  return 'opened'
}

/**
 * The link, always visible and always copyable.
 *
 * NOT A CONVENIENCE. Adding a second account requires opening this URL in a
 * different browser application, and the only way to do that is to have the
 * URL as text. So it is rendered whether or not the button worked, and it is
 * rendered before anyone has had a chance to press the wrong one.
 *
 * ONLY WHILE THE SIGN-IN IS LIVE, though, and the caller enforces that. This
 * is a control that hands out the same `authorize_url` the reopen button does,
 * so a refusal that disables that button has to withdraw this too -- and the
 * clipboard-failure sentence below promises that nothing is wrong with the
 * sign-in, which is only true where `signInIsOver` is false.
 */
function SignInLink({ url }: { url: string }) {
  const [copied, setCopied] = useState<'no' | 'yes' | 'unavailable'>('no')

  const copy = async () => {
    // `navigator.clipboard` is undefined outside a secure context and can
    // reject when the document is not focused. Either way the URL is on screen
    // and selectable, so the failure is reported rather than swallowed -- a
    // button that says "copied" when nothing was copied is the small version
    // of the bug this whole app is about.
    try {
      await navigator.clipboard.writeText(url)
      setCopied('yes')
    } catch {
      setCopied('unavailable')
    }
  }

  return (
    <>
      <div className="acct-buttons">
        <button type="button" onClick={() => void copy()}>
          {copied === 'yes' ? 'link copied' : 'copy the sign-in link'}
        </button>
      </div>
      {copied === 'unavailable' && (
        <p className="muted small">
          <strong>This browser refused the clipboard.</strong> Select the link
          below and copy it by hand.
        </p>
      )}
      <p className="acct-fixed" style={{ wordBreak: 'break-all' }}>
        {url}
      </p>
    </>
  )
}

/**
 * Steps two and three: open the page, then paste what it shows.
 *
 * Shared by the add form and by a row's sign-in-again, because they are the
 * same two calls with the same failure modes; only the label and the lending
 * list differ, and both were decided before this component is reached. A second
 * copy of this would drift on the thing least likely to be noticed -- which of
 * the refusals leaves the sign-in open.
 */
function SignInSteps({
  at,
  mode,
  set,
  onExchanged,
  reload,
}: {
  at: Extract<SignIn, { kind: 'open' }>
  mode: 'add' | 'reauth'
  set: (next: SignIn) => void
  /** Called with the account the platform registered. The caller owns what
      happens next -- which, for a broken account, is putting its state back. */
  onExchanged: (account: Account, expiresAt: string | null) => void | Promise<void>
  /** Re-reads the pool, for the advice that ends in "go and look". */
  reload: () => void
}) {
  const [code, setCode] = useState('')
  const host = callbackHostOf(at.auth.authorize_url)
  const hint = pastedCodeHint(code)
  // WHAT THE LAST REFUSAL WAS, and whether it ended the sign-in. `dead` gates
  // the THREE controls that cannot work any more, all of which are the same
  // dead `state` wearing different clothes: the paste field, because the record
  // its code would be checked against is gone; `Open the sign-in page again`,
  // because it reopens THIS `authorize_url` carrying THAT `state`; and the
  // link, which hands that same URL out as text for another browser.
  //
  // THE LINK WAS THE ONE THAT WAS MISSED. Gating two of the three left an
  // operator free to copy it, sign in somewhere else, come back with a code and
  // find the field disabled -- a dead end the page invited them into, which is
  // worse than the unbounded loop gating was added to stop.
  const refusal = at.failed ? exchangeRefusal(at.failed) : null
  const dead = signInIsOver(refusal)
  // ONE WORDING FOR WHY, printed by the reopen button's tooltip and by the
  // paragraph that replaces the link -- the two places with room for a
  // sentence. The paste field's placeholder is a few words instead, because a
  // placeholder is; it is the only other statement of this, and it says the
  // same thing in them.
  const deadWhy =
    refusal === 'accepted_unnamed'
      ? 'The platform has already accepted this sign-in, so it is no longer holding a record for it. Look for the account in the pool before signing in again.'
      : 'The platform is no longer holding this sign-in, so this page would hand you a code nothing here can redeem. Start over instead.'

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (code.trim() === '') return
    // Out of the field before the request leaves. Not because a single-use code
    // is a credential -- it is not -- but because leaving it there invites a
    // second press that spends a code the platform has already consumed.
    const pasted = code
    setCode('')
    set({ ...at, sending: true, failed: null })
    const res = await finishAccountSignIn({ state: at.auth.state, code: pasted })
    if (res.status !== 'ok') {
      // STILL `open`, AND THE INSTRUCTION UNDER IT IS WHAT MAKES THAT MEAN
      // ANYTHING. Some refusals leave the pending record in place -- a
      // mismatched paste is refused before it is read, a token-endpoint refusal
      // happens before `ref.delete()`, and a request nothing answered may not
      // have arrived at all -- so dropping back to `idle` would throw away a
      // sign-in that is still redeemable. The rest are terminal, `signInIsOver`
      // is where that is decided, and `dead` above is what stops this screen
      // inviting a paste into one of them forever.
      set({ ...at, sending: false, failed: failureOf(res) })
      return
    }
    await onExchanged(res.data.account, res.data.expires_at)
  }

  return (
    <>
      <div className="acct-action">
        <h4>
          2 &middot; Sign in to Claude
        </h4>
        <DifferentBrowserNote mode={mode} />
        {/* THE HOST IS THE CHECKABLE FACT -- it is what a reader compares
            against the address bar of the tab that opens. The paragraph
            around it was the explanation. */}
        <p className="muted small">
          {host ? (
            <>
              Opens <code>{host}</code> in a new tab.
            </>
          ) : (
            <>Opens Claude&rsquo;s own sign-in page in a new tab.</>
          )}
        </p>
        {/* BOTH DISABLED WHILE A CODE IS IN FLIGHT, and `start over` is the one
            that matters: abandoning a sign-in whose exchange is already on its
            way to the platform would leave an account registered and this page
            claiming nothing happened. The other is disabled for the same
            reason in miniature -- these handlers write from the state this
            render captured, so firing one mid-request would roll the request
            back on screen while it was still running. */}
        <div className="acct-buttons">
          <button
            type="button"
            disabled={at.sending || dead}
            title={dead ? deadWhy : undefined}
            onClick={() => set({ ...at, opened: openSignInPage(at.auth.authorize_url) })}
          >
            {at.opened === 'not_yet' ? 'Sign in to Claude ↗' : 'Open the sign-in page again ↗'}
          </button>
          <button
            type="button"
            disabled={at.sending}
            onClick={() => set({ kind: 'idle' })}
            title="Forget this sign-in on this page and start a new one"
          >
            start over
          </button>
        </div>
        {/* `!dead` for the second half of the sentence: the popup was still
            blocked, but "the sign-in is still open" stopped being true the
            moment the platform said it had finished with it. */}
        {at.opened === 'blocked' && !dead && (
          <p className="warn-text">
            <strong>This browser refused to open the tab</strong> (a popup
            blocker or an extension). The sign-in is still open &mdash; use the
            link below.
          </p>
        )}
        {dead ? (
          /* WITHDRAWN, NOT ANNOTATED. A sentence under a live "copy the
             sign-in link" button saying the link will not work is exactly the
             shape this screen keeps being wrong about: the control still
             works, it just produces a dead end two browsers away from here,
             where nothing explains it. */
          <p className="muted small">
            <strong>The sign-in link has been withdrawn.</strong> {deadWhy}
          </p>
        ) : (
          <SignInLink url={at.auth.authorize_url} />
        )}
      </div>

      <form className="acct-action" onSubmit={(e) => void submit(e)}>
        <h4>
          3 &middot; Paste the code that page shows you
        </h4>
        {/* THE IMPERATIVE STAYS (§6). "Paste all of it" is what stops a
            credential landing on the wrong account from a second tab; the
            mechanism behind it is the topic. */}
        <p className="muted small">
          <strong>Paste all of it</strong> &mdash; the code, the{' '}
          <code>#</code>, and the long string after it.
        </p>
        {/* NOT WHEN THE SIGN-IN IS OVER. This counts down the platform's hold
            on a pending record that no longer exists, and its expired branch
            says the paste field is still live -- which is exactly what `dead`
            has just stopped being true. */}
        {!dead && <Deadline auth={at.auth} startedAt={at.startedAt} />}
        <input
          type="text"
          className="mono acct-wide"
          value={code}
          spellCheck={false}
          autoComplete="off"
          disabled={at.sending || dead}
          placeholder={
            dead
              ? refusal === 'accepted_unnamed'
                ? 'the platform already accepted this sign-in'
                : 'this sign-in has ended — start over'
              : 'paste the code here'
          }
          aria-label="The code the Claude callback page displayed"
          onChange={(e) => setCode(e.target.value)}
        />
        {hint && <p className="muted small">{hint}</p>}
        <p className="acct-buttons">
          <button type="submit" disabled={at.sending || dead || code.trim() === ''}>
            {at.sending
              ? 'redeeming…'
              : mode === 'add'
                ? 'Add this account'
                : 'Replace the credential'}
          </button>
        </p>
        {at.failed && refusal !== null && (
          <>
            {/* NO RETRY BUTTON ON THIS ONE. The code was taken out of the field
                before the request left, so there is nothing to re-send; what
                follows names the controls that do work, per refusal. */}
            <SignInFailure
              error={at.failed}
              what="POST /v1/accounts/exchange"
              /* A 201 is not a failure, and the panel may not shout that it
                 is over a message saying the platform accepted the sign-in. */
              heading={
                refusal === 'accepted_unnamed'
                  ? 'The platform accepted this sign-in without naming the account'
                  : undefined
              }
              tone={refusal === 'accepted_unnamed' ? 'partial' : 'failed'}
            />
            <ExchangeAdvice
              refusal={refusal}
              mode={mode}
              onStartOver={() => set({ kind: 'idle' })}
              reload={reload}
            />
          </>
        )}
      </form>
    </>
  )
}

/**
 * PUTTING THE STATE BACK IS A SECOND CALL, and it is the same second call
 * wherever the sign-in was started from.
 *
 * Registering an existing label REPLACES the credential and deliberately
 * PRESERVES the state (accountstore.py:110-125), so that a credential
 * replacement cannot silently un-pause an account somebody paused. The
 * consequence is that an account in REAUTH_REQUIRED is STILL in
 * REAUTH_REQUIRED after a perfectly successful sign-in, and nothing will be
 * assigned to it.
 *
 * THE ADD FORM REACHES THAT CASE AS READILY AS THE ROW DOES -- signing in
 * under a label that already exists is one of the ways to fix such an account,
 * and the form says so -- so it may not hardcode "nothing to put back" and
 * then render the result green. That is this repository's defining bug reached
 * from the screen's primary control.
 *
 * THE EVIDENCE IS THE REGISTERED DOCUMENT, not the row this page was looking
 * at before: the exchange response carries the account the store wrote, so its
 * `state` is the one that decides. Null out means there was nothing to
 * restore, which is a different thing from a restore that failed, and
 * `SignInDone` renders the two differently again.
 */
async function restoreStateIfNeeded(
  registered: Account,
): Promise<Extract<SignIn, { kind: 'done' }>['restore']> {
  if (!needsAHuman(registered)) return null
  const back = await setAccountState(
    registered.account_id,
    'AVAILABLE',
    'signed in again from the Accounts screen',
  )
  return back.status === 'ok'
    ? { ok: true }
    : {
        ok: false,
        error: back.status === 'error' || back.status === 'stale' ? back.error : null,
      }
}

// ---------------------------------------------------------------------------
// Add an account
// ---------------------------------------------------------------------------

function AddAccount({
  board,
  accounts,
  ui,
  patch,
  reload,
}: {
  board: AccountsBoard
  accounts: Account[]
  ui: Persisted
  patch: Patch
  reload: () => void
}) {
  // THE OWNER IS THE TOKEN'S TENANT, and `tenant_id` here is that value echoed
  // by routes/accounts.py -- deliberately not taken from the broker's answer,
  // "so the page can never be told it is looking at a tenant it is not". It is
  // therefore the tenant the sign-in will file this under, and naming anything
  // else on screen would be a promise the API does not keep.
  const owner = board.page.tenant_id
  const [label, setLabel] = useState('')
  const [lend, setLend] = useState('')
  const state = ui.signin
  const set = (next: SignIn) => patch((p) => ({ ...p, signin: next }))

  // NOT CONFIGURED MEANS REFUSE, never accept-anything. Without a named tenant
  // this form could still submit safely -- the API files the account under the
  // verified token whatever the page thinks -- but it could not TELL the
  // operator whose pool the account is about to land in, and a sign-in is not
  // something to spend on a maybe.
  if (owner === null) {
    return (
      <section className="section panel">
        <h2>Add an account</h2>
        <div className="ctl-empty is-partial" role="status">
          {/* THE HEADING IS THE MARKER: the form is WITHDRAWN, and a form that
              is simply not drawn is indistinguishable from one that failed to
              render. */}
          <h3>
            Adding an account is unavailable until your tenant is named
          </h3>
          <p>
            This response named no tenant, so this form cannot tell you whose
            pool an account would land in. This is a gap in what was read, not a
            fault in the platform, and it says nothing about the accounts above.
          </p>
        </div>
      </section>
    )
  }

  const trimmed = label.trim()
  // Owned labels only: a LENT account shares the screen and is not in this
  // tenant's namespace, so warning that its name is taken would be wrong.
  const replacing = accounts.some((a) => a.owner_tenant === owner && a.label === trimmed)

  // NO EVENT ARGUMENT, so the failure panel can call it too. A "Try again"
  // that cannot re-send the request it is offered for is worse than no button.
  //
  // AN EMPTY LABEL IS REFUSED HERE AND OFFERED NOWHERE. The platform checks the
  // label before it hands back a sign-in link, so an empty one is a refusal
  // this page can see coming and returning early is right. What was wrong was
  // the failure panel offering "Try again" wired to this anyway: the most
  // prominent control on the panel, producing no request, no spinner and no
  // message, which is indistinguishable from a platform that swallowed it.
  // BOTH callers now gate on the same `trimmed`, and this line is the backstop
  // rather than the rule.
  const start = async () => {
    if (trimmed === '') return
    const lendTo = splitTenants(lend).filter((t) => t !== owner)
    set({ kind: 'starting' })
    const res = await beginAccountSignIn({
      // THE LABEL AND THE LENDING LIST, AND NOTHING ELSE. The pending record
      // the broker writes is what decides where the account lands when the code
      // comes back minutes later, and the tenant on it is the one swarm-api
      // resolves from the VERIFIED TOKEN. `AccountSignInStart` has no
      // `owner_tenant` field and no `provider` field and forbids extra keys, so
      // sending either -- which this call used to do, because the broker's own
      // route takes them -- is a 422 from validation instead of a sign-in.
      // `owner` is that same resolved tenant echoed back by routes/accounts.py,
      // so what this form names and what the record files under cannot
      // disagree; it is displayed, not transmitted.
      label: trimmed,
      lend_to: lendTo,
    })
    if (res.status !== 'ok') {
      set({ kind: 'start_failed', error: failureOf(res) })
      return
    }
    set({
      kind: 'open',
      label: trimmed,
      lendTo,
      auth: res.data,
      startedAt: Date.now(),
      opened: 'not_yet',
      sending: false,
      failed: null,
    })
  }

  return (
    <section className="section panel">
      <h2>Add an account</h2>
      {/* B4.4: "This is a sign-in, not a paste" is the single most important
          thing about this form and it is now the SUBMIT BUTTON's label --
          `Start the sign-in`, which is §8.5(3), a sentence that IS the
          control. A reader who is about to paste a token finds out from the
          button they are reaching for, not from a line above the form they
          have already scrolled past. The argument is `#help/sign-in-not-paste`
          in the rail's Help section; B7.4 took the `?` that sat on `owner`,
          because a glyph on a field naming the tenant is not where anyone
          looks for what the submit button already says. */}
      <ul className="ctl-facts">
        <li className="ctl-fact">
          <b>
            owner
          </b>
          <span className="mono">{owner}</span>
        </li>
      </ul>

      {state.kind === 'done' && (
        <SignInDone
          state={state}
          onAgain={() => {
            set({ kind: 'idle' })
            setLabel('')
            setLend('')
          }}
        />
      )}

      {state.kind === 'open' ? (
        <>
          <div className="acct-action">
            <h4>
              1 &middot; Named
            </h4>
            {/* THE EXACT ID AND THE LENDING LIST STAY. This is the last point
                at which either can be changed, so both are printed in full. */}
            <p className="muted small">
              Reserved for{' '}
              <code>
                {owner}:{state.label}
              </code>
              {state.lendTo.length > 0 ? (
                <>
                  , lent to <strong>{state.lendTo.join(', ')}</strong>
                </>
              ) : (
                <>, lent to nobody</>
              )}
              . <em>start over</em> below is the only way to change either.
            </p>
          </div>
          <SignInSteps
            at={state}
            mode="add"
            set={set}
            reload={reload}
            onExchanged={async (account, expiresAt) => {
              // `restore: null` was hardcoded here, which reported a green
              // "Signed in" for an account the store had handed back still in
              // REAUTH_REQUIRED -- unassignable, with nothing on screen saying
              // so. The registered account says whether there is anything to
              // put back; the row's `Sign in again` has always done this and
              // the primary control has to do the same.
              const restore = await restoreStateIfNeeded(account)
              set({ kind: 'done', account, expiresAt, restore })
              setLabel('')
              setLend('')
              reload()
            }}
          />
        </>
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault()
            void start()
          }}
        >
          {/* NOT A PICKER, AND NOT A SILENT DEFAULT EITHER. There is one kind of
              credential in this pool, so there is nothing to choose; but a value
              the request carries and the form never mentions is a decision made
              for the operator with nothing on screen admitting it. So it is
              shown, named, and said to be fixed. */}
          <span className="t-label">
            provider
          </span>
          {/* SHOWN, NAMED, AND SAID TO BE FIXED. A value the request carries
              and the form never mentions is a decision made for the operator
              with nothing on screen admitting it. */}
          <p className="acct-fixed mono">
            {SUBSCRIPTION_PROVIDER}
            <span
              className="ctl-chip is-info"
              aria-label="Fixed. This is the only credential kind this pool handles, so there is nothing to choose between — but the form states the value it sends rather than hiding it."
            >
              <i aria-hidden="true" />
              fixed
            </span>
          </p>

          <label className="t-label" htmlFor="acct-label">
            label
          </label>
          <input
            id="acct-label"
            className="mono acct-wide"
            value={label}
            spellCheck={false}
            autoComplete="off"
            placeholder="laptop"
            onChange={(e) => setLabel(e.target.value)}
          />
          {/* THE CONSTRAINT STAYS BESIDE THE INPUT. A field whose rule is one
              hover away is a field people fill in wrong, so this is one of the
              few places B4.4 does NOT move text behind a `?`. What changed is
              that the rule is now written as a rule rather than as a sentence
              about one: `a-z 0-9 -` is the character set, `≤40` is the length,
              and the preview is what the label becomes. Sixteen words to five
              tokens, beside the input, where it was always meant to be. */}
          <p className="muted small acct-flags">
            <code>
              {owner}:{trimmed || 'label'}
            </code>
            <span className="mono" aria-label="Lowercase letters, digits and dashes, starting and ending alphanumeric, at most 40 characters.">
              a-z 0-9 - · ≤40
            </span>
          </p>
          {/* A DESTRUCTIVE OUTCOME, NAMED BEFORE IT HAPPENS (§6). */}
          {replacing && (
            <p className="warn-text">
              <strong>{trimmed} already exists in this pool.</strong> Signing in
              under that label <strong>REPLACES its credential</strong> rather
              than adding a second account. For another subscription, give it a
              different name.
            </p>
          )}

          <label className="t-label" htmlFor="acct-lend">
            lend to (optional)
          </label>
          <input
            id="acct-lend"
            className="mono acct-wide"
            value={lend}
            spellCheck={false}
            autoComplete="off"
            placeholder="tenant ids, comma separated"
            aria-describedby="acct-lend-rule"
            onChange={(e) => setLend(e.target.value)}
          />
          {/* THE ISOLATION RULE IS A LINE UNDER THE FIELD, NOT ITS PLACEHOLDER
              (CP-20, visual QA 2026-09-25). It lived only in the placeholder,
              which is truncated at the field's width, vanishes on the first
              keystroke, and is not reliably announced -- so the rule was gone
              exactly when somebody was filling the field in. A comment here
              promised a `?` beside the label carried the reason; none has
              rendered since B7.4 took it, and none is added: the rule itself is
              short enough to BE the line, beside the input, which is where the
              label's own `a-z 0-9 - · ≤40` rule above already sits. The
              placeholder keeps only the format, so the rule is stated once. */}
          <p className="muted small acct-flags" id="acct-lend-rule">
            empty: only {owner} runs on it
          </p>

          <p className="acct-buttons">
            <button type="submit" disabled={state.kind === 'starting' || trimmed === ''}>
              {state.kind === 'starting' ? 'asking for a sign-in link…' : 'Start the sign-in'}
            </button>
          </p>
          {state.kind === 'start_failed' && (
            <>
              {/* THE RETRY IS NOT OFFERED WITHOUT SOMETHING TO RETRY WITH. The
                  label field sits directly above this panel and stays editable
                  while it is open, so it can be empty by the time anybody reads
                  it -- and `start` refuses an empty label, which made this
                  button a silent no-op: no request, no spinner, no message.
                  Withdrawing the button and naming the empty field is the same
                  rule the submit button below follows by being disabled: a
                  control that cannot act does not look like one that can. */}
              <SignInFailure
                error={state.error}
                what="POST /v1/accounts/authorize"
                onRetry={trimmed === '' ? undefined : () => void start()}
              />
              {/* THE MISSING CONTROL IS NAMED WHERE IT WOULD BE. A "Try
                    again" that cannot re-send is worse than none, and none
                    with no explanation reads as a panel that failed to draw
                    its own button. */}
              {trimmed === '' && (
                <p className="warn-text">
                  No <em>Try again</em>: the{' '}
                  <strong>label field above is empty</strong>. Type a name and
                  press <em>Start the sign-in</em>.
                </p>
              )}
            </>
          )}
        </form>
      )}
    </section>
  )
}

/**
 * The receipt.
 *
 * IT SAYS WHAT EXPIRES AND WHAT DOES NOT, because an expiry in eight hours
 * otherwise reads as "do this again tonight" and the whole promise of this pool
 * is that you sign in once. The ACCESS half expires; the pair does not, because
 * the sweep exchanges it for a successor before it can.
 */
function SignInDone({
  state,
  onAgain,
}: {
  state: Extract<SignIn, { kind: 'done' }>
  onAgain: () => void
}) {
  const restore = state.restore
  const partial = restore !== null && !restore.ok
  return (
    <div className={`state ${partial ? 'partial' : 'acct-ok'}`} role="status">
      <h3>
        {partial ? 'Signed in, but the state was not put back' : 'Signed in'}{' '}
        &mdash; {state.account.account_id}
      </h3>
      {/* AN EXPIRY, OR THE ABSENCE OF ONE, NEVER A GUESS. The figure and the
          phrase are different shapes on purpose: "expires in 8h" and "no
          expiry was reported" are different facts and must not read alike. */}
      <p>
        Two secrets were written.{' '}
        {state.expiresAt ? (
          <>
            The mounted token expires in{' '}
            <strong>{clearsIn(state.expiresAt, Date.now())}</strong> &mdash; not
            a deadline for you.
          </>
        ) : (
          <>
            <strong>No expiry was reported</strong>, so there is no figure here
            rather than a guessed one. <em>Refresh this account</em> in its row
            asks the broker directly.
          </>
        )}
      </p>
      {/* THE SECOND BRANCH IS A SUCCESS AND A PROBLEM AT ONCE, and its
          required follow-up stays on the surface (§6): a credential that is
          in place and good, on an account nothing will be assigned to. */}
      {restore !== null &&
        (restore.ok ? (
          <p>
            Its state was put back to AVAILABLE as a second, separate call, and
            that call succeeded.
          </p>
        ) : (
          <p>
            <strong>
              This account is STILL in REAUTH_REQUIRED, so nothing will be
              assigned to it.
            </strong>{' '}
            The credential is good; the second call failed:{' '}
            {restore.error?.message ?? 'the request did not complete.'} Use{' '}
            <em>{moveLabel('AVAILABLE')}</em> in the row above.
          </p>
        ))}
      {/* WHAT THE POOL WILL SHOW FOR IT, READ OFF THE ACCOUNT THE EXCHANGE
          RETURNED. This panel is shared by the add form and by every
          `Sign in again`/`Replace the credential`, and re-registering carries
          `windows`, `observed_at`, `holds` and `assigned` through
          (accountstore.py:110-125). An unconditional "no reading yet" is
          therefore false for every re-authentication and for every add under a
          label that already exists -- and the table one panel above would be
          showing live 5H/7D figures for the same account at the same moment.
          The fact was in hand and was never consulted. */}
      {state.account.observed_at === null ? (
        <p className="checked-at">
          No reading yet &mdash; <em>unmeasured</em>, not idle.
        </p>
      ) : (
        <p className="checked-at">
          Readings kept: the last arrived {timeAgo(state.account.observed_at)}
          {state.account.stale ? ', so the table above marks its figures ~' : ''}.
        </p>
      )}
      {/* NOT AVAILABLE, AND NOTHING PUT IT BACK. `restore === null` means there
          was nothing to restore -- this account is not in REAUTH_REQUIRED --
          which is not the same as "it will take work now". PAUSED and DRAINING
          are preserved through a re-registration on purpose, and AVAILABLE is
          the only member of ASSIGNABLE_STATES, so a green receipt over a
          paused account would be a success claimed for something that will not
          run. */}
      {restore === null && state.account.state !== 'AVAILABLE' && (
        <p className="warn-text">
          <strong>
            It is in {state.account.state.toLowerCase()}, and available is the
            only state an agent is started on.
          </strong>{' '}
          Its row has <em>{moveLabel('AVAILABLE')}</em> for when it should take work
          again.
        </p>
      )}
      <p className="acct-buttons">
        <button type="button" onClick={onAgain}>
          done
        </button>
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Sign in again, on a row that already exists
// ---------------------------------------------------------------------------

/**
 * The same sign-in, aimed at a label that is already in the pool.
 *
 * TWO STEPS, VISIBLY, because the store makes them two steps. Registering an
 * existing label replaces the credential and deliberately PRESERVES the state
 * (accountstore.py:106-124) so that it cannot silently un-pause an account
 * somebody paused. The consequence is that an account in REAUTH_REQUIRED is
 * still in REAUTH_REQUIRED after a perfectly successful sign-in, and nothing
 * will be assigned to it. So the state is put back as an explicit second call,
 * and if that call fails this says so rather than reporting one green tick for
 * two operations.
 *
 * THE LENDING LIST IS SENT BACK AS IT IS. Re-registering a label REPLACES
 * `lend_to` rather than merging it, so omitting it here would quietly revoke
 * every loan the account had as a side effect of fixing its credential.
 */
function Reauth({
  account,
  ui,
  patch,
  reload,
}: {
  account: Account
  ui: Persisted
  patch: Patch
  reload: () => void
}) {
  const state = ui.reauth[account.account_id] ?? { kind: 'idle' }
  const set = (next: SignIn) =>
    patch((p) => ({ ...p, reauth: { ...p.reauth, [account.account_id]: next } }))
  const broken = needsAHuman(account)

  // A CONTROL THAT WOULD REWRITE A FIELD IT CANNOT SEND. The sign-in swarm-api
  // performs produces a `SUBSCRIPTION_PROVIDER` credential and its request body
  // has no provider field, so re-registering a row that says anything else
  // would file the account under a provider nobody chose on this screen -- the
  // silent rewrite the removed `provider: account.provider` argument existed to
  // prevent. Refused and named, rather than offered and surprising.
  if (account.provider !== SUBSCRIPTION_PROVIDER) {
    return (
      <div className="acct-action">
        <h4>{broken ? 'Sign in again' : 'Replace the credential'}</h4>
        {/* NOT ENTITLED IS NOT BROKEN (§8.7.3). This control genuinely cannot
            act on this account -- the sign-in produces one kind of credential
            and this account is another kind -- and nothing failed to produce
            that state. B4.4: the mismatch is now the two provider names beside
            each other, which is the whole argument; the sentence that spelled
            them out added a verb and a reassurance and no third fact. */}
        <div className="ctl-empty is-partial" role="status">
          <h3>
            Provider mismatch
          </h3>
          <ul className="ctl-facts">
            <li className="ctl-fact">
              <b>account</b>
              <span className="mono">{account.provider}</span>
            </li>
            <li className="ctl-fact">
              <b>sign-in makes</b>
              <span className="mono">{SUBSCRIPTION_PROVIDER}</span>
            </li>
            <li className="ctl-fact">
              <b>changed</b>
              <span
                className="ctl-mark is-zero"
                aria-label="Nothing here failed and nothing was changed. This control simply does not apply to an account of this provider."
              >
                real zero
              </span>
            </li>
          </ul>
        </div>
      </div>
    )
  }

  const start = async () => {
    set({ kind: 'starting' })
    const res = await beginAccountSignIn({
      // NEITHER THE TENANT NOR THE PROVIDER IS OURS TO SEND. swarm-api resolves
      // the tenant from the verified token -- and this control renders only on
      // a row this tenant owns -- and it fixes the provider itself;
      // `AccountSignInStart` has a field for neither and forbids extra keys, so
      // sending `account.owner_tenant` and `account.provider`, which this call
      // used to do, is a 422 from validation rather than a sign-in. The
      // provider that used to be sent here is now checked above instead: a row
      // carrying anything else is refused rather than quietly re-registered.
      label: account.label,
      lend_to: account.lend_to,
    })
    if (res.status !== 'ok') {
      set({ kind: 'start_failed', error: failureOf(res) })
      return
    }
    set({
      kind: 'open',
      label: account.label,
      lendTo: account.lend_to,
      auth: res.data,
      startedAt: Date.now(),
      opened: 'not_yet',
      sending: false,
      failed: null,
    })
  }

  const finish = async (registered: Account, expiresAt: string | null) => {
    // The REGISTERED account decides, not `broken` -- which was read off the
    // row before the sign-in and can disagree with what the store wrote.
    const restore = await restoreStateIfNeeded(registered)
    set({ kind: 'done', account: registered, expiresAt, restore })
    reload()
  }

  return (
    <div className="acct-action">
      <h4>{broken ? 'Sign in again' : 'Replace the credential'}</h4>
      <p className="muted small">
        {broken ? (
          <>
            <strong>Only a person can replace this.</strong> The refresh token
            behind <code>{account.account_id}</code> is gone or unreadable.
          </>
        ) : (
          <>
            Replaces the credential behind <code>{account.account_id}</code>.
            Nothing here needs doing on a healthy account.
          </>
        )}
      </p>

      {state.kind === 'done' && (
        <SignInDone state={state} onAgain={() => set({ kind: 'idle' })} />
      )}

      {state.kind === 'open' ? (
        <SignInSteps
          at={state}
          mode="reauth"
          set={set}
          onExchanged={finish}
          reload={reload}
        />
      ) : (
        <>
          <div className="acct-buttons">
            <button
              type="button"
              disabled={state.kind === 'starting'}
              onClick={() => void start()}
            >
              {state.kind === 'starting'
                ? 'asking for a sign-in link…'
                : broken
                  ? 'Sign in again'
                  : 'Start a replacement sign-in'}
            </button>
          </div>
          {state.kind === 'start_failed' && (
            <SignInFailure
              error={state.error}
              what="POST /v1/accounts/authorize"
              onRetry={() => void start()}
            />
          )}
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Remove
// ---------------------------------------------------------------------------

/**
 * Typed confirmation, and there is deliberately no "don't ask again".
 *
 * The thing that makes this recoverable is worth saying out loud rather than
 * leaving as a surprise: the Firestore document goes, the SECRET STAYS.
 * Deleting a Secret Manager secret is irreversible and takes its version
 * history with it, so an account removed by mistake stays re-registerable under
 * the same label. Cleaning secrets up is the reconciler's job, on its own
 * schedule.
 */
function Remove({ account, reload }: { account: Account; reload: () => void }) {
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)

  const armed = typed === account.label

  const run = async () => {
    if (!armed) return
    setBusy(true)
    setError(null)
    const res = await removeAccount(account.account_id)
    setBusy(false)
    if (res.status === 'ok') {
      setTyped('')
      reload()
    } else if (res.status === 'error' || res.status === 'stale') {
      setError(res.error)
    }
  }

  return (
    <div className="acct-action danger">
      <h4>
        Remove
      </h4>
      {/* THE COUNT OF AFFECTED AGENTS IS THE FACT (§6) and it is a digit on
          the surface: it is the difference between a reversible tidy-up and
          pulling an account out from under running work. */}
      {/* B4.4: THE TWO FACTS, AS FACTS. "the credential is retained, not
          deleted" is what `credential: retained` says; the sentence around it
          was reassurance. THE COUNT OF AFFECTED AGENTS STAYS A DIGIT ON THE
          SURFACE and keeps its warning tone, because it is the difference
          between a reversible tidy-up and pulling an account out from under
          running work -- and the remedy (move it to DRAINING first) is the
          `?`, which is where a procedure belongs. */}
      <ul className="ctl-facts">
        <li className="ctl-fact">
          <b>credential</b>
          <span>retained</span>
        </li>
        <li className="ctl-fact">
          <b>agents on it</b>
          <span className="acct-flags">
            <span>{account.assigned}</span>
            {/* A MEASURED WARNING, NOT AN ABSENCE. `.ctl-mark` is the
                vocabulary for kinds of nothing; this is a kind of something,
                so it is a chip -- the word plus the caution triangle -- and
                the remedy is its accessible name. Drawing it as a mark would
                have said "we could not read this", which is the opposite of
                what the digit beside it means. */}
            {account.assigned > 0 && (
              <span
                className="ctl-chip is-warn"
                aria-label={`${account.assigned} agent${account.assigned === 1 ? '' : 's'} currently hold${account.assigned === 1 ? 's' : ''} this account. Move it to DRAINING first if you want them off it before it goes.`}
              >
                <i aria-hidden="true" />
                in use
              </span>
            )}
          </span>
        </li>
      </ul>
      <span className="limit-edit">
        <input
          type="text"
          className="mono"
          value={typed}
          disabled={busy}
          placeholder={account.label}
          aria-label={`Type ${account.label} to confirm removal`}
          onChange={(e) => setTyped(e.target.value)}
        />
        <button
          type="button"
          className="danger"
          disabled={!armed || busy}
          onClick={() => void run()}
        >
          {busy ? 'removing…' : `remove ${account.label}`}
        </button>
        <span className="client-side">type the label to arm this</span>
      </span>
      {error && (
        <p className="warn-text">
          {errorHeading(error)} &mdash; {error.message} The account was not
          removed.
        </p>
      )}
    </div>
  )
}
// ---------------------------------------------------------------------------
// Reading this screen
// ---------------------------------------------------------------------------

/**
 * WHAT USED TO BE HERE: `Instructions()`, four numbered sections with a
 * paragraph and a list each, and `Legend()`, eleven `<dt>`/`<dd>` pairs. About
 * 1,400 rendered words, under every load of this screen, under a table.
 *
 * NONE OF IT WAS ANNOTATING ANYTHING ON SCREEN. It was a help article that
 * happened to be rendered below a pool, which is exactly what the owner
 * directive means by prose that belongs in a dedicated Help section. Every
 * sentence of it is now a topic in `help.ts`, reachable from the links below,
 * from `#help/<id>` and from the Help section in the rail. It was also
 * reachable from a `?` beside the control it was about, on fifty-four of them;
 * B7.4 cut that to two. See the note on ACCOUNT_TOPICS.
 *
 * THE MARKS THEMSELVES DID NOT MOVE, and that is the test. An unmeasured cell
 * still draws NO BAR and an em dash, a projected figure still carries its
 * tilde, a row count still says how many documents it is short by, and a row
 * nothing can be assigned on still carries `.ctl-mark` beside its state chip.
 * A reader who never opens one of these links can still tell a missing figure
 * from a measured zero; `src/__tests__/honesty.prose.test.tsx` renders this
 * screen with every card closed and asserts it.
 *
 * B4.4 narrowed the index below to the six topics that have no `?` anchor on
 * the surface; B7.4 put three back, because they no longer have one either.
 * See the note inside it.
 */
const ACCOUNT_TOPICS: readonly TopicId[] = [
  // B4.4: SEVENTEEN ENTRIES BECAME SIX, AND NOTHING BECAME UNREACHABLE.
  //
  // Eleven of the seventeen named a topic that ALREADY has a `?` beside the
  // figure or the control it is about, on this screen, in this render -- so
  // the footer was a second route to a destination the reader could already
  // reach from the thing they were looking at, and the better of the two
  // routes is the one attached to the subject. Eighty-five rendered words of
  // documentation index at the foot of a data screen is precisely the clutter
  // the brief names, and `#help/<id>` deep links plus the Help section in the
  // rail mean none of the eleven is now further than one click from where it
  // matters.
  //
  // THE SIX THAT STAY ARE THE ONES WITH NO ANCHOR. Each is a property of the
  // WHOLE screen rather than of any one figure, so there is nothing on the
  // surface to hang a `?` on:
  'accounts-table-shape', // why these five columns and this order
  'binding-window', // which window the utilisation figure is of
  'no-amber-band', // why there is no amber band, deliberately
  'unreadable-documents', // why a count is only a fact if every document was read
  'skipped-for-this-tenant', // healthy, and still serving nobody here
  'refresh-token-required', // a credential that cannot be exchanged is refused

  // B7.4: AND THE THREE B4.4 REMOVED ON THE GROUNDS THAT A `?` COVERED THEM.
  //
  // That reasoning was sound and its premise is gone. This screen held FIFTY-
  // FOUR help anchors -- more than a third of the console's total, on one
  // route -- and fifty-one of them have been deleted: the sign-in flow alone
  // carried thirty, and every panel they sat in was already a written
  // paragraph, so the glyph opened a shorter version of the text under it.
  // Two remain, both on the pool table, and these two are the row-level facts
  // that lost theirs and have nowhere else on the surface to live.
  //
  // THEY ARE NOT THE OTHER FORTY-NINE. Those are procedural -- what to do
  // next in a flow whose own panels say what to do next -- and listing them
  // here would rebuild the help article this footer replaced, in the place it
  // was removed from. These three are properties of a FIGURE in the table: a
  // count that is advisory rather than authoritative, a row that has never
  // been assigned anything. A reader comparing two rows needs both and can get
  // neither from anything else on screen.
  //
  // `provider-defines-windows` was the third candidate and is deliberately NOT
  // here: the panel that lost its `?` for it already prints "5H, 7D and CLEARS
  // above are unmeasured, not zero" in words, which is the whole claim. It is
  // also what keeps this row inside the screen's budget --
  // `prose.budget.capacity.test.tsx` caps Accounts at 250 rendered words and
  // measured 227, and the two titles below are ten of the twenty-three left.
  'advisory-vs-lease', // the agent count is the broker's, not the lease's
  'never-assigned-pool', // nothing has ever been assigned, which is not idle
]
