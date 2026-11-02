import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from 'react'
import {
  beginAccountSignIn,
  finishAccountSignIn,
  loadAccountsBoard,
  loadTask,
  refreshAccount,
  removeAccount,
  setAccountLending,
  setAccountModelClasses,
  setAccountName,
  setAccountState,
  type AccountsBoard,
  type ServedAccount,
} from './api'
import { accountName } from './accountName'
import { agentName } from './agentlist'
import { errorHeading, read, route, type ApiError, type Result } from './fetch'
import type { TopicId } from './help'
import { ACCOUNTS_POLL_MS, capacityPoll, paneHref, useLinkedParam, usePhoneTables } from './capacityPoll'
import { HelpCard } from './HelpCard'
import { Mark } from './primitives'
import { Button, CIcon, Chip, Dialog, ToneMark, WarnMark } from './components'
import './styles/capacity.css'
import { FailedPanel, Screen, timeAgo } from './Shell'
import { TaskRef } from './TaskRef'
import { AGE_TICK_MS, useNow } from './useNow'
import { AccountsTable, ProviderName } from './AccountsTable'
import { AcctState } from './AccountState'
import { DuplicateBanner, Forecast, RuleLine, Verdict, usePoolWaiting, type WaitRead } from './AccountsVerdict'
import { agoPastDay, utcAt, type PoolAccount } from './accountsPool'
import { AgentCap, SheetAccount, SheetDuplicate, SheetForecast, SheetHealth, SheetVerdict, SheetWindows } from './AccountSheet'
import { AccountsTimeline, ViewToggle } from './AccountsTimeline'
import { VIEW_KEY, viewOf, type AccountsView } from './accountsLanes'
import { safeStorage } from './panes'
import { trapTab } from './focus'
import {
  FIVE_HOUR,
  SEVEN_DAY,
  ageSpan,
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
  type AccountStateName,
  type AccountWindow,
  type RefreshResult,
} from './types'

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
  /**
   * The account a reader CHOSE, or null when nobody has. Null is not "no
   * account": the pane then shows the default (`defaultChoice`), and on a
   * phone it means the list is the page.
   */
  chosen: string | null
  refresh: Record<string, RefreshState>
  /** Signing in again to an account that already exists, keyed by account id. */
  reauth: Record<string, SignIn>
  /** The add-an-account sign-in. */
  signin: SignIn
  /**
   * Whether `Add account` has opened the form (#127). Held here, not in the
   * form, because a reload remounts the screen and must not fold a form
   * somebody opened.
   */
  adding: boolean
  /**
   * The account whose `Refresh this account` held keyboard focus when its
   * answer reloaded the screen (a11y-keyboard-001). The reload remounts the
   * button, so focus went to <body>; the new button takes it back on mount.
   * Held here for the same reason as the rest: it has to outlive the remount.
   */
  focusRefresh: string | null
}

type Patch = (fn: (p: Persisted) => Persisted) => void

const EMPTY_UI: Persisted = { chosen: null, refresh: {}, reauth: {}, signin: { kind: 'idle' }, adding: false, focusRefresh: null }

/**
 * Capacity -> Accounts. The Claude subscriptions this platform runs agents on.
 *
 * WHY THE COLUMNS ARE `cs status`'s, EXACTLY. An operator already reads
 * ACCOUNT / 5H / 7D / CLEARS / STATE on their laptop. Inventing a second
 * vocabulary for the same five facts about the same five accounts costs them a
 * translation every time they look, and the translation is where the mistakes
 * live. So the scan line here is that line: same order, same headings, same
 * figures, same `~` for a figure that is projected rather than measured. (The
 * five-cell bar was part of that parity until CP-25, #85: the owner collapsed
 * it into §6.4's one proportion primitive, at the exact percentage.)
 * Everything this screen adds -- who it is lent to, how many agents hold it,
 * and every control -- lives in a row you open, so the table you scan is never
 * anything other than the table you already know.
 *
 * THE RULE THE WHOLE SCREEN TURNS ON. `stale` and "utilisation is zero" are
 * different claims, and so are "no reading has ever arrived" and "the reading
 * says 0%". An empty bar is the same picture for both, which is why an
 * unmeasured window draws NO bar at all and prints an em dash, and a measured
 * 0% draws the track's baseline tick.
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
      // THE PAGE'S ONE `?` OPENS THE SCREEN'S TOPICS (VQ-033, BGD 2026-10-26:
      // "Move the topics behind the page's one ?"). They were the `Reading
      // this screen:` footer under the pool; see ACCOUNT_TOPICS.
      help={ACCOUNT_TOPICS}
      load={load}
      // #117 (owner ruling 2026-10-07): every 30 s, 60 s on a phone, paused while the tab is hidden
      // (`Screen`). A poll re-reads in place; it does not bump `nonce`, so it
      // never remounts the screen under a sign-in in progress.
      pollMs={capacityPoll(ACCOUNTS_POLL_MS)}
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
        // `acct-page`: the Accounts section's own scope, which draws no box
        // (styles/capacity.css). The squared dashed side of the `partial`
        // mark is written against it, so it applies here and on Provider
        // quota and nowhere else (QA-049, owner 2026-10-20).
        <div className="acct-page">
          <Body
            board={b}
            ui={ui}
            patch={patch}
            reload={() => setNonce((n) => n + 1)}
          />
        </div>
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
  // "sign in again", the other is "this is not yours to fix".
  const mine = broken.filter((a) => isOwned(a, scope)).length
  const lent = broken.length - mine
  // HOW MANY THERE ARE, AND NO "AVAILABLE" (#2222). "7 accounts · 7
  // available" was the head while every one of them was under its floor and
  // 28 agents waited: available was the account's state, not whether it
  // could take an agent. The verdict under the head answers that.
  return (
    <>
      {n} account{n === 1 ? '' : 's'}
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
  // Choosing an account folds an add form nobody is using -- the pane shows
  // one thing at a time -- but never one with a sign-in under way (`addOpen`).
  // THE CHOICE IS IN THE ADDRESS TOO (QA G5-23): `?account=<id>`, so a chosen
  // account can be linked to and a reload reopens it. The address wins over
  // the held choice, which is what a followed link means.
  const linked = useLinkedParam('account')
  // TABLE OR TIMELINE (#2222, BGD "Table default, remembered"): the address
  // wins (`?view=timeline`, a followed link), then this browser's last
  // choice, then Table. Choosing writes both, and an account chosen after
  // keeps the view in the address it writes -- the view SHOWN, not the
  // address's: a Timeline restored from this browser's memory under a bare
  // address (arriving from the sidebar) still writes `?view=timeline`, so the
  // link the reader copies opens what they see.
  const linkedView = useLinkedParam('view')
  const [storedView, setStoredView] = useState<string | null>(() => {
    try {
      return safeStorage()?.getItem(VIEW_KEY) ?? null
    } catch {
      return null
    }
  })
  const pageView = viewOf(linkedView, storedView)
  const viewHref = (v: AccountsView) => paneHref('capacity/accounts', { account: linked, view: v })
  const chooseView = (v: AccountsView) => {
    try {
      safeStorage()?.setItem(VIEW_KEY, v)
    } catch {
      /* Storage blocked: the address still carries the choice. */
    }
    setStoredView(v)
    if (typeof window !== 'undefined') followHash(viewHref(v))
  }
  // A TIMELINE RESTORED FROM THIS BROWSER'S MEMORY IS WRITTEN TO THE ADDRESS
  // (the review's minor, ACCOUNTS-TIMELINE-MERGE): arriving from the sidebar
  // under a bare address drew the Timeline while the address still said
  // nothing, so a link copied off the bar opened Table. Replaced, not pushed:
  // it is the same visit, and Back must not land on the bare address first.
  // A Table address stays bare, as it always was.
  useEffect(() => {
    if (typeof window === 'undefined' || linkedView !== null || pageView !== 'timeline') return
    replaceHash(paneHref('capacity/accounts', { account: linked, view: 'timeline' }))
  }, [linkedView, pageView, linked])
  const choose = (id: string | null) => {
    patch((p) => ({ ...p, chosen: id, adding: p.signin.kind === 'idle' ? false : p.adding }))
    if (typeof window !== 'undefined') followHash(paneHref('capacity/accounts', { account: id, view: pageView === 'timeline' ? pageView : linkedView }))
  }
  const view = linked !== null ? { ...ui, chosen: linked } : ui
  // THE ANSWER FIRST (#2222, proposal A): can the pool take another agent,
  // and if not, when. Re-read with the board, so the two agree.
  const waiting = usePoolWaiting(board.readAt)
  const scope = board.page.tenant_id
  const pool = accounts as PoolAccount[]
  // ON A PHONE THE OPENED ACCOUNT IS THE SCREEN (#2222, mock-up A P3): the
  // list steps aside (capacity.css `.is-detail`), and so does the page's lead
  // -- the verdict, the floors and the banners -- which sat above the sheet and
  // pushed `‹ Accounts` a screen down. The account's own verdict and duplicate
  // advice are in the sheet; Back brings the lead back. A desktop keeps it,
  // because there the table is still the page beside the sheet.
  const phone = usePhoneTables()
  const opened = view.chosen !== null && accounts.some((a) => a.account_id === view.chosen)
  const lead = !(phone && opened)
  return (
    <>
      {lead && (
        <>
          {/* THE TOGGLE BESIDE THE VERDICT (BGD): the answer is the same in
              both views, so it stays above them and only what follows it
              changes. */}
          <div className="acct-verdict-row">
            <Verdict accounts={pool} scope={scope} shortfall={shortfall(board)} now={Date.now()} waiting={waiting} />
            <ViewToggle view={pageView} hrefOf={viewHref} onChange={chooseView} />
          </div>
          {accounts.length > 0 && <RuleLine accounts={pool} />}
          <DuplicateBanner accounts={pool} scope={scope} onOpen={choose} />
          <Broken accounts={accounts} scope={board.page.tenant_id} onOpen={choose} />
        </>
      )}
      {/* A LINK TO AN ACCOUNT THIS READ DOES NOT HAVE IS SAID (live QA
          CAP-12): `?account=eng:nope` used to show another account under that
          address with nothing to say the link missed. Above the split, so a
          phone -- which shows the list, not the pane -- reads it too. */}
      {linked !== null && !accounts.some((a) => a.account_id === linked) && (
        <p className="warn-text acct-missing" role="status">
          No account <code>{linked}</code> is in this read.{' '}
          <button type="button" className="ctl-link" onClick={() => choose(null)}>
            Clear the link
          </button>
        </p>
      )}
      <Pool
        board={board}
        accounts={accounts}
        ui={view}
        patch={patch}
        reload={reload}
        choose={choose}
        view={pageView}
        waiting={waiting}
        addForm={<AddAccount board={board} accounts={accounts} ui={ui} patch={patch} reload={reload} />}
      />
    </>
  )
}

/**
 * THE SIGN-IN FORM, BEHIND A CONTROL (#127). It was always drawn, and filled
 * half the screen of somebody who came to watch the pool. Two cases still
 * draw it with no click, because hiding it there would hide the answer:
 *
 *   * an EMPTY pool -- the form is the one control that fixes it, and the
 *     empty state says "add the first one below";
 *   * a sign-in UNDER WAY or just finished -- its link, its code box and its
 *     report are the form. Starting one opens the form (`adding`), and a
 *     close FOLDS it without losing it (visual QA pass 2 cap2-02: the sheet
 *     had no close and ignored Escape once Sign in started): the sign-in is
 *     held in `signin`, and Add account opens it again where it was.
 *
 * The control sits in the list's header row (capacity.html A1, #503): it was a
 * bar of its own under the table, where it read as belonging to nothing.
 */
function addOpen(ui: Persisted): boolean {
  return ui.adding
}

function AddButton({ ui, patch }: { ui: Persisted; patch: Patch }) {
  return (
    <button
      type="button"
      className="acct-add"
      aria-label="Add account"
      aria-expanded={addOpen(ui)}
      // A folded sign-in under way opens again where it was: see above.
      onClick={() => patch((p) => ({ ...p, adding: !p.adding }))}
    >
      <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
        <path d="M6 2v8M2 6h8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
      </svg>
      {/* `Add` alone on a phone (QA-050): the word that drops is a span the
          sheet hides there, and the name is said whole for every width. */}
      Add<span className="acct-add-what"> account</span>
    </button>
  )
}

/**
 * How badly an account needs a person, lower first; null when it does not.
 *
 *   0  REAUTH_REQUIRED: its sign-in expired. The sweep has stopped retrying,
 *      so it stays until a person signs in again.
 *   1  the broker refuses it to this tenant (`unreadable_now`): its secret
 *      could not be read, so it looks idle while every task goes elsewhere.
 *   2  exhausted: its binding window is spent (100%) and has not reset, so it
 *      gives out nothing until that window refills.
 *
 * PAUSED and DRAINING are not here: a person chose them, and they are not
 * waiting on anyone.
 */
export function attentionRank(a: Account, scope: string | null): number | null {
  if (needsAHuman(a)) return 0
  if (unreadableFor(a, scope)) return 1
  const binding = bindingWindow(a)
  if (binding !== null) {
    const r = readingOf(a, binding.key)
    if ((r.kind === 'live' || r.kind === 'stale') && r.pct >= 100) return 2
  }
  return null
}

/**
 * Alphabetical, the way a reader sees the list: by what each row's heading
 * says -- its Name, or its label when it has none (#1913) -- then by id for
 * two equal headings.
 */
function byName(a: Account, b: Account): number {
  return accountName(a).localeCompare(accountName(b)) || a.account_id.localeCompare(b.account_id)
}

/**
 * THE ACCOUNT THE PAGE OPENS ON when nobody chose one (#503: it opened on an
 * arbitrary account, the first by opaque id). The rule, in order:
 *
 *   1. the first account that NEEDS ATTENTION -- the most urgent by
 *      `attentionRank` (expired sign-in, then unreadable, then exhausted),
 *      alphabetical among equals. Its controls are the reason to visit;
 *   2. else the MOST USED -- the most agents holding it now (`assigned`),
 *      alphabetical among equals. It is the account whose figures are moving;
 *   3. else, when no account is held at all, ALPHABETICAL by label.
 */
export function defaultAccount(accounts: readonly Account[], scope: string | null): Account | null {
  if (accounts.length === 0) return null
  const ranked = accounts
    .map((a) => ({ a, rank: attentionRank(a, scope) }))
    .filter((x): x is { a: Account; rank: number } => x.rank !== null)
    .sort((x, y) => x.rank - y.rank || byName(x.a, y.a))
  if (ranked[0] !== undefined) return ranked[0].a
  const used = accounts.filter((a) => a.assigned > 0).sort((x, y) => y.assigned - x.assigned || byName(x, y))
  return used[0] ?? [...accounts].sort(byName)[0] ?? null
}

/**
 * The account the pane shows: the one a reader chose, else `defaultAccount`.
 *
 * THE DEFAULT IS AN ACCOUNT, NEVER A PLACEHOLDER (#503). A dashed "Choose an
 * account." beside a list of accounts was a pane spent on saying nothing.
 *
 * A chosen account that is no longer in the read (removed, or no longer lent)
 * falls back to the default rather than to nothing.
 */
function shownAccount(accounts: Account[], chosen: string | null, scope: string | null): { account: Account; explicit: boolean } | null {
  const picked = chosen === null ? undefined : accounts.find((a) => a.account_id === chosen)
  if (picked !== undefined) return { account: picked, explicit: true }
  const first = defaultAccount(accounts, scope)
  return first === null ? null : { account: first, explicit: false }
}

/**
 * The accounts a timer will never fix, at the top, before anything else.
 *
 * REAUTH_REQUIRED is the one state the sweep deliberately stops retrying, so
 * it is the one state that stays until a person acts. Reporting it only as a
 * mark in a list it shares with five healthy ones is how it gets scrolled past.
 *
 * WHICH PERSON, THOUGH. A LENT account in this state is on the reader's screen
 * and is not theirs to fix: its pane shows no sign-in control, no state control
 * and no refresh button, because those routes answer 404 for a tenant that does
 * not own the account. Telling every borrower to "sign in again" sends them to
 * a pane that deliberately refuses, so the two cases are separate sentences and
 * only the owner's carries the link.
 *
 * A WARNING, NOT A FAILURE: the amber triangle (brand §3). Nothing failed; a
 * credential expired and a person has to renew it.
 */
function Broken({
  accounts,
  scope,
  onOpen,
}: {
  accounts: Account[]
  scope: string | null
  onOpen: (id: string) => void
}) {
  const broken = accounts.filter(needsAHuman)
  if (broken.length === 0) return null
  const mine = broken.filter((a) => isOwned(a, scope))
  const lent = broken.filter((a) => !isOwned(a, scope))
  const owners = [...new Set(lent.map((a) => a.owner_tenant))]
  const names = (xs: Account[]) => xs.map((a) => accountName(a)).join(', ')
  return (
    <div className="acct-callout" role="status">
      <WarnMark />
      <div className="acct-callout-body">
        {mine.length > 0 && (
          <p>
            <b>{names(mine)}</b> need{mine.length === 1 ? 's' : ''} someone to sign in again.{' '}
            {mine.length === 1 ? 'It gives' : 'They give'} out nothing until then.{' '}
            {/* ONE LINK PER ACCOUNT, SEPARATED (visual QA V016): two or more ran
                together as "Sign in again to aSign in again to b". */}
            {mine.map((a, i) => (
              <span key={a.account_id}>
                {i > 0 && <span aria-hidden="true"> &middot; </span>}
                <button
                  type="button"
                  className="acct-link"
                  aria-label={`Sign in again to ${accountName(a)}`}
                  onClick={() => onOpen(a.account_id)}
                >
                  {mine.length === 1 ? 'Sign in again' : `Sign in again to ${accountName(a)}`}
                </button>
              </span>
            ))}
          </p>
        )}
        {lent.length > 0 && (
          <p>
            <b>{names(lent)}</b> need{lent.length === 1 ? 's' : ''} a sign-in by{' '}
            {owners.length === 1 ? owners[0] : 'their owners'}; lent to you, so nothing here can do it.
          </p>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// The list and the chosen account, side by side (capacity.html A1)
// ---------------------------------------------------------------------------

/**
 * THE ADDRESS FOLLOWS A CHOICE WITHOUT CLAIMING TO BE BACK OR FORWARD (visual
 * QA V158, 2026-10-16). Assigning `location.hash` fires `popstate` as well as
 * `hashchange`, and the shell reads a `popstate` as a history traversal and
 * puts the page back at the scroll it last recorded for the address -- for
 * three seconds, over the scroll that brings a phone's chosen account into
 * view. A pushed entry plus the `hashchange` the router follows is the same
 * navigation (Back still returns to the list) without the restore.
 */
function followHash(hash: string): void {
  const oldURL = window.location.href
  window.history.pushState(window.history.state, '', hash)
  window.dispatchEvent(new HashChangeEvent('hashchange', { oldURL, newURL: window.location.href }))
}

/** `followHash`, rewriting this entry rather than adding one: the address catching up with what is drawn. */
function replaceHash(hash: string): void {
  const oldURL = window.location.href
  window.history.replaceState(window.history.state, '', hash)
  window.dispatchEvent(new HashChangeEvent('hashchange', { oldURL, newURL: window.location.href }))
}

function Pool({
  board,
  accounts,
  ui,
  patch,
  reload,
  choose,
  view,
  waiting,
  addForm,
}: {
  board: AccountsBoard
  accounts: Account[]
  ui: Persisted
  patch: Patch
  reload: () => void
  choose: (id: string | null) => void
  view: AccountsView
  waiting: WaitRead
  addForm: ReactNode
}) {
  // One clock for the whole render. Reading Date.now() per item would let two
  // figures in the same pane disagree by a second, which is exactly the kind
  // of flicker that makes a number look untrustworthy when it is not.
  const now = Date.now()
  const shown = shownAccount(accounts, ui.chosen, board.page.tenant_id)
  const phone = usePhoneTables()
  // BACK RETURNS FOCUS TO THE ROW IT LEFT (live QA CAP-11): on a phone
  // `‹ Accounts` hides the pane and focus fell to <body>. The row is drawn
  // only after the render the click causes, so it is focused from an effect.
  // A sheet opened from a Timeline lane returns to that lane's name.
  const backTo = useRef<string | null>(null)
  useEffect(() => {
    const id = backTo.current
    if (id === null) return
    backTo.current = null
    const row = [...document.querySelectorAll<HTMLButtonElement>('.acct-open, .acct-tl-open')].find((b) => b.dataset.account === id)
    row?.focus()
  })
  // A CHOSEN ACCOUNT OPENS AT ITS TITLE (visual QA V157, V158; 2026-10-16).
  // On a phone the list -> detail switch kept the list's scroll offset, so a
  // row chosen low in a long list landed the reader at the bottom of the
  // shorter detail; focus fell to <body>. The page is scrolled to the pane
  // and focus goes to `‹ Accounts`, its first control. On a desktop the
  // detail is a SIDE SHEET (#2222, mock-up A F4) that scrolls itself: it opens
  // at its top, and focus goes to its close button, so Escape and Tab start
  // inside it.
  const paneRef = useRef<HTMLElement | null>(null)
  const opened = shown !== null && shown.explicit ? shown.account.account_id : null
  useEffect(() => {
    const pane = paneRef.current
    if (opened === null || pane === null) return
    if (phone) {
      pane.scrollIntoView?.({ block: 'start' })
      pane.querySelector<HTMLButtonElement>('.acct-back')?.focus({ preventScroll: true })
    } else {
      pane.scrollTop = 0
      // A control inside that already took focus keeps it: the reload a
      // refresh causes remounts the sheet, and its button's own hand-over
      // (a11y-keyboard-001) runs first, as a child's effect does.
      if (!pane.contains(document.activeElement)) {
        pane.querySelector<HTMLButtonElement>('.acct-sheet-close')?.focus({ preventScroll: true })
      }
    }
    // Only when the choice changes: a poll's re-render must not move the page.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opened])
  // ESCAPE CLOSES THE SHEET, unless a dialog on it is open: Remove's typed
  // confirmation and lending's are `Dialog`s, and Escape is theirs first.
  // TAB STAYS IN THE DESKTOP SHEET (visual QA pass 1 capacity-07): it dims the
  // page like a modal, and after its last control Tab walked to the Reads dock
  // and the rail behind the scrim. It wraps at the sheet's ends now
  // (focus.ts `trapTab`), as People's drawer does. A phone's sheet is the
  // screen under the app bar, not a layer over a page, so it traps nothing.
  const closeRef = useRef<() => void>(() => {})
  const sheetOpen = shown !== null && (shown.explicit || addOpen(ui))
  useEffect(() => {
    if (!sheetOpen || typeof document === 'undefined') return
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return
      if (document.querySelector('.c-dlg') !== null) return
      if (e.key === 'Escape') closeRef.current()
      else if (!phone) trapTab(e, paneRef.current)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [sheetOpen, phone])

  // THE TIMELINE (#2222, proposal B): every account on one axis. Pressing a
  // lane opens THE SAME SIDE SHEET a Table row opens -- the whole screen on a
  // phone -- not proposal B's detail under the lane (BGD, 2026-10-25 10:05Z).
  // Only a CHOSEN account opens: the Table's default account is a desktop
  // convenience, and the Timeline has no place to show it.
  const timeline =
    view === 'timeline' ? (
      <AccountsTimeline
        accounts={accounts as PoolAccount[]}
        scope={board.page.tenant_id}
        now={now}
        waiting={waiting}
        phone={phone}
        openId={shown !== null && shown.explicit ? shown.account.account_id : null}
        onChoose={choose}
        addButton={shown === null ? null : <AddButton ui={ui} patch={patch} />}
        // An empty pool draws the form with no click, in the lane the first
        // account will take (B6): it is the one control that fixes the zero.
        // With accounts, the form opens in the sheet, as the Table's does.
        addForm={shown === null ? <div className="acct-tl-add">{addForm}</div> : null}
      />
    ) : null
  if (timeline !== null && shown === null) return timeline

  if (shown === null) {
    const unread = shortfall(board)
    const unreadIds = board.page.unreadable_documents ?? []
    return (
      /* THE EMPTY POOL IS THE SAME CARD (visual QA V072, 2026-10-11). The
         heading sat on the page above a bordered panel and the add form floated
         on the page background under it. Now the list half carries the heading
         and the zero, and the pane half the form -- the layout a pool with
         accounts has, so nothing moves when the first one lands. `is-adding`
         keeps the form's pane drawn on a phone. */
      <div className="acct-split is-adding is-empty">
        <section className="section panel acct-list" aria-label="Subscription accounts">
          <div className="acct-list-head">
            <h2>Subscription accounts</h2>
          </div>
          <div className="state acct-empty" role="status">
            {/* A REAL ZERO, SAID AS ONE, on the surface. What the zero COSTS --
                that work waits rather than fails, quietly -- is the topic. */}
            {/* ONE OF THIS SCREEN'S TWO `?` (B7.4), AND IT IS THE ONE THAT
                RENDERS WHEN THE POOL IS EMPTY -- the other sits on the list's
                foot, which this branch does not draw. What it holds is the only
                thing an empty pool cannot show: the COST of the zero. Nothing on
                this panel says that work submitted now parks and waits instead of
                failing, and an operator who assumes the second will go looking
                for an error that was never raised.
                `honesty.prose.test.tsx` pins a `?` to this panel. */}
            {/* AN UNREADABLE DOCUMENT IS NOT A ZERO (rendered 2026-10-25): the
                page head said "1 document unreadable, so this list is short"
                while this panel said "No accounts registered" with the zero
                mark. The store holds that account; this page cannot read it. */}
            <h3>
              {unread > 0 ? 'No account could be read' : 'No accounts registered'}
              <HelpCard topic="park-on-missing-credential" />
            </h3>
            {/* B4.4: THE MARK IS THE CLAIM. `.ctl-mark.is-zero` says "the read
                succeeded and returned nothing" in two words, in the fixed
                vocabulary every other screen uses for the same kind of nothing. */}
            {unread > 0 ? (
              <p className="acct-flags">
                <Mark kind="partial" say="The store holds account documents this read could not parse." />
                <span>
                  {pluralise(unread, 'registered account')} could not be read
                  {unreadIds.length > 0 && ': '}
                  {unreadIds.map((id, i) => (
                    <span key={id}>
                      {i > 0 && ', '}
                      <span className="acct-nm raw">{id}</span>
                    </span>
                  ))}
                </span>
              </p>
            ) : (
              <p className="acct-flags">
                <Mark kind="zero" say="The account read succeeded and this pool has no accounts yet." />
                <span>{phone ? 'add the first one below' : 'add the first one beside this list'}</span>
              </p>
            )}
            {/* WHICH ORGANISATION (#2222): one login can hold two, and the
                sign-in takes the one selected in that browser. */}
            <p className="muted small">
              Sign in to the organisation you mean: one claude.ai login can hold two, and the sign-in takes the one that is
              selected.
            </p>
          </div>
        </section>
        {/* The form with no click: on an empty pool it is the one fix. */}
        <div className="acct-pane">{addForm}</div>
      </div>
    )
  }

  const adding = addOpen(ui)
  const a = shown.account
  // CLOSING: an opened account goes back to the table, focus on its row (live
  // QA CAP-11); an add form folds, at every step (visual QA pass 2 cap2-02).
  // A sign-in under way is folded, not dropped: `signin` keeps its link and
  // code box, and Add account opens them again.
  closeRef.current = () => {
    if (shown.explicit) {
      backTo.current = a.account_id
      choose(null)
    }
    patch((p) => ({ ...p, adding: false }))
    if (!shown.explicit && typeof document !== 'undefined') document.querySelector<HTMLButtonElement>('.acct-add')?.focus()
  }
  // THE OPENED ACCOUNT, AS A SIDE SHEET (#2222, mock-up A F4; a full screen
  // on a phone, P3). It is drawn only when a row or a lane was opened or the
  // add form is open: the table (or the timeline) is the page, and the sheet
  // covers its right side over a scrim that closes it, as Escape and the
  // close button do. The detail is the component the pane drew -- facts,
  // controls, and PR #413's holds -- so nothing it guarded changed by moving.
  const sheet = sheetOpen ? (
      <>
        <div className="acct-scrim" aria-hidden="true" onClick={() => closeRef.current()} />
        {/* A DIALOG ON A DESKTOP (capacity-07): it is drawn over a scrim, so a
            screen reader is told the page behind is not the place to be. */}
        <aside
          className="acct-pane acct-sheet"
          ref={paneRef}
          role={phone ? undefined : 'dialog'}
          aria-modal={phone ? undefined : true}
          aria-label={shown.explicit ? `Account ${accountName(a)}` : 'Add an account'}
        >
          <div className="acct-sheet-head">
            <button type="button" className="acct-back" onClick={() => closeRef.current()}>
              <span aria-hidden="true">‹</span> Accounts
            </button>
            {/* The crumb names where it is (capacity-22: it read `Accounts ›` and nothing). */}
            <span className="acct-sheet-crumb">
              {shown.explicit ? `Accounts › ${accountName(a)}` : 'Accounts › Add an account'}
            </span>
            <Button
              iconOnly
              size="sm"
              icon={<CIcon name="close" />}
              className="acct-sheet-close"
              onClick={() => closeRef.current()}
            >
              Close
            </Button>
          </div>
          {adding && addForm}
          {/* ONE THING AT A TIME (live QA CAP-11): an add form nobody has
              started stacked above the chosen account's detail, so the pane held
              two. Cancel brings the account back. A sign-in under way keeps both,
              so the pool can still be read while the provider's page is open. */}
          {shown.explicit && !(adding && (ui.signin.kind === 'idle' || ui.signin.kind === 'start_failed')) && (
            <section key={a.account_id} className="acct-pane-detail" aria-label={`Account ${accountName(a)}`}>
              <Detail
                account={a}
                accounts={accounts as PoolAccount[]}
                board={board}
                now={now}
                ui={ui}
                patch={patch}
                reload={reload}
              />
            </section>
          )}
        </aside>
      </>
    ) : null
  // THE TIMELINE SHARES THE SPLIT'S FRAME CLASSES, NOT ITS FRAME: the sheet's
  // placement, its scrim and the phone's "the account is the screen" are
  // `.acct-split` rules (accounts-ranked.css, capacity.css), and so is the
  // detail's layout inside it; `is-timeline` drops the card's own border so
  // the Timeline's panel keeps its look (accounts-timeline.css).
  if (timeline !== null)
    return (
      <div
        className={`acct-split is-ranked is-timeline${shown.explicit ? ' is-detail' : ''}${adding ? ' is-adding' : ''}${sheetOpen ? ' has-sheet' : ''}`}
      >
        {timeline}
        {sheet}
      </div>
    )
  return (
    /* `is-detail`: a reader chose an account, so on a phone the account is the
       page and the list steps aside (frame 10). With no choice the phone shows
       the list -- the default account is a desktop convenience, not a page a
       phone should land on. `is-adding`: the add form is open in the pane, so
       the pane shows on a phone too. */
    <div
      className={`acct-split is-ranked${shown.explicit ? ' is-detail' : ''}${adding ? ' is-adding' : ''}${sheetOpen ? ' has-sheet' : ''}`}
    >
      <section className="section panel acct-list" aria-label="Subscription accounts">
        <div className="acct-list-head">
          <h2>
            Subscription accounts
            {/* THE QUALIFIER SLOT, NOT A CHIP (CP-22). */}
            <span className="ctl-card-note is-end">{pluralise(accounts.length, 'account')}</span>
          </h2>
          <AddButton ui={ui} patch={patch} />
        </div>
        {/* RANKED BY WHEN EACH CAN TAKE A NEW AGENT (#2222): one table on a
            desktop, a card each on a phone. The name is the `acct-open`
            button the list was, so choosing one opens the same detail under
            it, with every control the page had. */}
        <p className="acct-rank-note">Ranked by when each can take a new agent. Times are UTC.</p>
        <AccountsTable
          accounts={accounts as PoolAccount[]}
          scope={board.page.tenant_id}
          now={now}
          // A ROW IS MARKED ONLY WHEN IT IS OPEN (visual QA V067; #2222): the
          // detail is a sheet a reader opens, so nothing is chosen by default.
          chosen={shown.explicit ? a.account_id : null}
          phone={phone}
          onChoose={(id) => choose(id)}
        />
        <PoolFindings
          accounts={accounts}
          scope={board.page.tenant_id}
          unreadableDocuments={board.page.unreadable_documents ?? []}
          unreadableDocumentCount={shortfall(board)}
        />
        {/* THE SHORTFALL AND THE TILDE BOTH STAY, and both are provenance about
            the READ rather than about any account -- which is why they are one
            foot line under the list and not a paragraph per figure. A count
            over documents that were dropped is not a count; a figure that is
            real but not current has to say so where it is read. */}
        {/* EACH CLAUSE IS ONE NOWRAP ITEM (CP-26), so at 390 the foot wraps
            BETWEEN clauses and never inside `readings 2m–14m old`. */}
        <p className="provenance acct-foot">
          <span className="acct-clause">
            {accounts.length} row{accounts.length === 1 ? '' : 's'}
            {shortfall(board) > 0 && <> of {accounts.length + shortfall(board)}</>}
          </span>
          {shortfall(board) > 0 && (
            <>
              {' '}
              &middot; <span className="acct-clause">{shortfall(board)} unread</span>
            </>
          )}
          <ReadingAges accounts={accounts} />{' '}
          {/* THE TILDE'S LEGEND ONLY WHERE A TILDE IS (#127): the ranked table
              draws an old reading pale with its age, so a `~` is only ever in
              an open account's sheet (a window with no column). */}
          &middot;{' '}
          <span className="acct-clause">
            {shown.explicit && anyProjected([a]) ? '~ is an old reading, not current' : 'no projected figures'}
          </span>
          {/* THE OTHER OF THIS SCREEN'S TWO `?` (B7.4), and the reason it is one
              of the two is the STALE MARK. This console draws three different
              things -- a measured value, a value too old to trust, and one nobody
              measured -- and the tilde is the middle one, the only mark of the
              three that is a claim about TIME rather than about presence. The
              topic is what may and may not be concluded from it. */}
          <HelpCard topic="projected-not-measured" />
        </p>
        <Forecast accounts={accounts as PoolAccount[]} scope={board.page.tenant_id} now={now} />
      </section>
      {sheet}
    </div>
  )
}


export { agoPastDay }



/**
 * THE AGE OF THE READINGS, in the provenance foot (CP-26, #85; §8.4(4)).
 *
 * `readings 2m–14m old`, the youngest to the oldest `observed_at` of the rows
 * in the table; one age when they agree (`readings 4m old`); rows with no
 * reading left out of the range; and no clause at all when no row has one.
 * The head's `read just now` is the age of the FETCH, which says nothing
 * about how old the broker's readings were when it answered -- the stale
 * window is thirty minutes, and until this a live reading's age was only in a
 * `title=` and the opened row.
 *
 * ITS OWN CLOCK, `useNow(AGE_TICK_MS)`, the five-second tick the head's fetch
 * age moves on. The table's per-render `now` is not used, because Accounts
 * does not re-render on its own and a range computed from it would freeze at
 * whatever it said when the board arrived.
 */
function ReadingAges({ accounts }: { accounts: Account[] }) {
  const now = useNow(AGE_TICK_MS)
  const ages = accounts
    .map((a) => (a.observed_at === null ? Number.NaN : now - new Date(a.observed_at).getTime()))
    .filter((ms) => Number.isFinite(ms))
  if (ages.length === 0) return null
  const youngest = ageSpan(Math.min(...ages))
  const oldest = ageSpan(Math.max(...ages))
  return (
    <>
      {' '}
      &middot;{' '}
      <span className="acct-clause">
        readings {youngest === oldest ? youngest : `${youngest}–${oldest}`} old
      </span>
    </>
  )
}





/**
 * Whether any `~` is on screen (#127): the sheet's two windows draw an old
 * reading pale with its age and never a `~` (#2222), so a `~` is only ever on
 * an open account's window that has no column (`AllWindows`), read stale. A
 * window already reset draws `reset`, not a `~` (live QA CAP-3). The legend
 * follows it.
 */
function anyProjected(accounts: Account[]): boolean {
  return accounts.some((a) =>
    Object.keys(a.windows ?? {})
      .filter((k) => k !== FIVE_HOUR && k !== SEVEN_DAY)
      .some((k) => readingOf(a, k).kind === 'stale'),
  )
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
          <b>unreadable account records</b>
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
            <Mark
              kind="zero"
              say="No account in this pool has ever been assigned to an agent. An idle pool and a pool nothing can reach look identical on this screen, and this is the shape of the second."
            />
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

/**
 * An identifier that never breaks inside itself (visual QA V070): a hyphen is
 * a line-break opportunity, so `u-bogdan` wrapped to `u-` and `bogdan`. One
 * longer than its line is cut with an ellipsis, whole in its title.
 */
function Id({ children, className, after }: { children: string; className?: string; after?: string }) {
  // `after` -- a list's comma -- rides INSIDE the id's box (visual QA V154,
  // 2026-10-16). Beside it, the line could break between the id and its comma
  // and start the next line with an orphaned `,`.
  return (
    <span className={className === undefined ? 'acct-id' : `acct-id ${className}`} title={children}>
      {children}
      {after}
    </span>
  )
}

/**
 * The pane's heading, and RENAME (#1913; owner decision 2026-10-22, BGD,
 * "Editable display name").
 *
 * The heading is the account's Name, or its label when it has none. Rename
 * edits the Name in place -- the field opens on the current Name and takes
 * focus; Enter or Save sends it at once, with no sign-in; Escape or Cancel
 * sends nothing -- and an emptied field CLEARS the Name, so the label shows
 * again. A refusal is drawn in the broker's own words and the field stays, so
 * the typed Name is not lost.
 *
 * Owner only, as lending and the served classes are: a borrower's Rename
 * would be refused by the broker after the console had offered it. The id,
 * the credential secret, the holds and the lending do not change, and work on
 * the account goes on through a rename.
 */
function NameHead({ account, owned, reload }: { account: Account; owned: boolean; reload: () => void }) {
  // null: not editing. A string: the field's text.
  const [draft, setDraft] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  // FOCUS BACK ON `Rename` (visual QA pass 2 cap2-03): Escape or Cancel
  // unmounted the editor under the keyboard and left focus on <body>. The
  // button is drawn again by that render, so it takes focus once it mounts
  // (`Button` takes no ref, so it is found in its row).
  const headRef = useRef<HTMLDivElement>(null)
  const back = useRef(false)
  useEffect(() => {
    if (draft !== null || !back.current) return
    back.current = false
    headRef.current?.querySelector<HTMLButtonElement>('button')?.focus()
  }, [draft])

  if (draft === null) {
    return (
      <div className="acct-dhead-name" ref={headRef}>
        <h3>{accountName(account)}</h3>
        {owned && (
          <Button
            kind="ghost"
            size="sm"
            onClick={() => {
              setError(null)
              setDraft(account.display_name ?? '')
            }}
          >
            Rename
          </Button>
        )}
      </div>
    )
  }

  const cancel = () => {
    if (busy) return
    back.current = true
    setDraft(null)
    setError(null)
  }
  const save = async (e: FormEvent) => {
    e.preventDefault()
    if (busy) return
    const name = draft.trim()
    setBusy(true)
    setError(null)
    const res = await setAccountName(account.account_id, name === '' ? null : name)
    setBusy(false)
    if (res.status === 'ok') {
      setDraft(null)
      reload()
    } else if (res.status === 'error' || res.status === 'stale') {
      setError(res.error)
    }
  }

  return (
    <form className="acct-rename" onSubmit={(e) => void save(e)}>
      <span className="limit-edit">
        <input
          type="text"
          className="acct-wide"
          value={draft}
          disabled={busy}
          // Focus follows the click that opened the editor, so a keyboard
          // reader lands in the field rather than back at the page's top.
          autoFocus
          // What an empty field saves as: no Name, so the label shows.
          placeholder={account.label}
          aria-label={`Name for ${account.account_id}`}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Escape') {
              e.preventDefault()
              cancel()
            }
          }}
        />
        <Button kind="primary" size="sm" type="submit" busy={busy ? 'Saving…' : false}>
          Save name
        </Button>
        <Button kind="ghost" size="sm" disabled={busy} onClick={cancel}>
          Cancel
        </Button>
      </span>
      {error && (
        <p className="warn-text" role="alert">
          {errorHeading(error)} &mdash; {error.message} The Name was not changed.
        </p>
      )}
    </form>
  )
}

function Detail({
  account,
  accounts,
  board,
  now,
  ui,
  patch,
  reload,
}: {
  account: Account
  /** The whole read, so a duplicate's other side is named only when it is in it. */
  accounts: PoolAccount[]
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
  const scope = board.page.tenant_id
  const pool = account as PoolAccount
  const names = new Map(accounts.map((x) => [x.account_id, accountName(x)]))
  // EDIT LENDING OPENS THE EDITOR THE PANE ALWAYS HAD (#2222, mock-up A F4):
  // the sheet leads with who it is lent to, and the field, its tenant check
  // and its confirmation are `Lending`'s, unchanged.
  const [lending, setLending] = useState(false)

  const lent = (account.lend_to ?? []).length > 0
  return (
    <div className="acct-detail">
      {/* THE HEAD: the label, WHOSE it is -- the line that decides whether the
          controls below exist at all -- its state, and whether it is lent. */}
      <header className="acct-dhead">
        <NameHead account={account} owned={owned} reload={reload} />
        <div className="acct-dhead-row">
          <AcctState state={account.state} />
          {/* The provider in words (#2688): `ChatGPT plan`, `Claude
              subscription`, or an unknown provider's own id -- never a guess. */}
          <Chip>
            <ProviderName provider={account.provider} /> &middot; {lent ? 'lending on' : 'lending off'}
          </Chip>
        </div>
        {/* AN ID IS ONE WORD (visual QA V070): `u-bogdan` broke at its hyphen
            onto two lines. Each id is an `.acct-id`, which never breaks inside
            itself; the line breaks between words. */}
        <p className="acct-idt">
          {owned ? (
            <>
              You own this account (tenant <Id>{account.owner_tenant}</Id>).
            </>
          ) : (
            <>
              You borrow this account from <Id>{account.owner_tenant}</Id>
              {scope !== null && (
                <>
                  {' '}
                  (tenant <Id>{scope}</Id>)
                </>
              )}
              .
            </>
          )}
        </p>
      </header>
      {/* THE ANSWER FOR THIS ACCOUNT, as its row says it (mock-up A F4). */}
      <SheetVerdict a={pool} scope={scope} now={now} names={names} />
      <SheetDuplicate a={pool} accounts={accounts} owned={owned} />
      <SheetWindows a={pool} now={now} />
      <AllWindows account={account} now={now} />
      <SheetHolds account={pool} owned={owned} now={now} reload={reload} fixture={board.fixture === true ? scope : undefined} />
      <SheetAccount a={pool} scope={scope} owned={owned} editing={lending} onEditLending={() => setLending(true)} />
      {owned && lending && (
        <div className="acct-lending-edit">
          <Lending account={account} board={board} reload={reload} />
          <Button kind="ghost" size="sm" onClick={() => setLending(false)}>
            Done
          </Button>
        </div>
      )}
      {/* B4.4: `.ctl-facts`, every clause either a MARK or that value's
          accessible name. When it was last handed out, which classes it
          serves, and who could not read its secret: the facts the sheet's
          sections above do not already carry. */}
      <ul className="ctl-facts acct-facts">
        <li className={`ctl-fact${neverAssigned(account) ? ' is-absent' : ''}`}>
          <b>last given out</b>
          {neverAssigned(account) ? (
            <span className="acct-flags">
              <Mark
                kind="zero"
                say="No agent has ever been handed this account. On its own that is simply a new account; across the whole pool it is the shape of workers that cannot reach the broker."
              />
            </span>
          ) : (
            // THE CLOCK, THEN THE AGE (G5-16): a clock lines the hand-out up
            // against the holds' Since column. The age is `agoPastDay`, the
            // row's own, so the two never round one hand-out two ways.
            <span title={account.last_assigned_at as string}>
              {givenAt(account.last_assigned_at as string, now)} ({agoPastDay(account.last_assigned_at as string, now)})
            </span>
          )}
        </li>
        {/* WHICH STEPS IT TAKES, for every reader and not only the owner: a
            borrower whose steps of one class sit parked needs to see that
            this account was never going to serve them (R5). */}
        <li className="ctl-fact">
          <b>classes</b>
          <span className="mono" data-classes={servedClasses(account).join(' ')}>
            {classesWord(servedClasses(account))}
          </span>
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
                <Mark
                  kind="zero"
                  say="Every one of those reports has aged out, so the pool is skipping this account for nobody. The record is kept because a report that keeps coming back is a missing secretAccessor grant rather than an onboarding delay."
                />
              )}
            </span>
          </li>
        )}
      </ul>
      <SheetForecast a={pool} now={now} />
      {/* ITS HEALTH (#2302, mock-up A's F2): after the facts, before the
          actions. One read when the sheet opens; it never polls. */}
      <SheetHealth a={pool} owned={owned} />
      {/* THE ACTIONS THE PAGE HAD, each with its own behaviour and
          confirmation: Pause and Drain (state), Replace the credential,
          Refresh, the served classes, and Remove last. */}
      {owned ? (
        <>
          <StateControls account={account} reload={reload} />
          <Reauth account={account} ui={ui} patch={patch} reload={reload} />
          <RefreshControl account={account} ui={ui} patch={patch} reload={reload} />
          <ModelClasses account={account} reload={reload} />
          <Remove account={account} reload={reload} />
        </>
      ) : (
        <Borrowed account={account} />
      )}
    </div>
  )
}

/**
 * AGENTS ON IT (#2222, mock-up A F4): the broker's count against the cap, the
 * live holds each linked to its agent (`HoldingNow`, swarm-api's tenant
 * filtering untouched), the loans in use, History behind one control, and the
 * cap with its Change for the owner.
 */
function SheetHolds({
  account,
  owned,
  now,
  reload,
  fixture,
}: {
  account: PoolAccount
  owned: boolean
  now: number
  reload: () => void
  fixture: FixtureViewer
}) {
  const [history, setHistory] = useState(false)
  const max = typeof account.max_agents === 'number' ? account.max_agents : null
  return (
    <section className="acct-ss acct-holding" aria-label="Agents on it">
      <h4>
        Agents on it &middot;{' '}
        <span
          aria-label={`${account.assigned} agents, as the broker last recorded it, of at most ${max ?? 'an unreported number'}. This count is advisory: the lease is the authoritative record of who holds what.`}
        >
          {account.assigned} of {max ?? '—'}
        </span>
      </h4>
      <HoldingNow account={account} now={now} fixture={fixture} bare />
      <p className="acct-ss-note">
        <button type="button" className="c-link acct-history-toggle" aria-expanded={history} onClick={() => setHistory(!history)}>
          {history ? 'Hide history' : 'History'}
        </button>
      </p>
      {history && <HoldingHistory account={account} now={now} fixture={fixture} />}
      <AgentCap a={account} owned={owned} reload={reload} />
    </section>
  )
}

// ---------------------------------------------------------------------------
// Who holds it, and who held it (#379 parts 2 and 3)
// ---------------------------------------------------------------------------

/**
 * One live hold, as `GET /v1/accounts/{id}/holders` serves it to THIS viewer.
 *
 * swarm-api does every bit of the tenant filtering (`swarm_api/accountholds.py`)
 * and this screen renders what arrives: it never decides on its own who may
 * see what. So a `task_id` is present only on the caller's own VERIFIED holds
 * (the admin view aside), `tenant` only where the caller may know it, and no
 * assignment id or secret name is ever in the payload to leak.
 */
export interface AccountHolder {
  since: string | null
  task_id?: string
  attempt?: number | null
  tenant?: string
  /** The worker named a task at all. False: an older worker, or an older hold. */
  recorded: boolean
  /** The hold's tenant owns the task it named. */
  verified: boolean
}

export interface AccountHolders {
  account_id: string
  viewer: 'owner' | 'borrower' | 'platform'
  total: number
  holders: AccountHolder[]
  /** Live holds that are not the caller's. A count, and for a borrower nothing more. */
  others: number
  /** The owner's view of who borrowed: tenant and count, never task ids. */
  by_tenant?: { tenant: string; n: number }[]
}

export interface HoldSpan {
  since: string | null
  until: string | null
  end: 'released' | 'unusable' | 'expired' | null
  mine: boolean
  tenant?: string
  task_id?: string
  attempt?: number | null
  recorded?: boolean
  verified?: boolean
}

export interface HoldHistory {
  account_id: string
  viewer: 'owner' | 'borrower' | 'platform'
  from: string | null
  to: string | null
  spans: HoldSpan[]
  next_cursor?: string | null
  /** A borrower only: other tenants' spans in the window, counted and nothing more. */
  others?: number
  /** A borrower only: 20 broker pages held none of its own rows, so the scan stopped. */
  scan_limited?: boolean
}

/**
 * The two reads, here rather than in api.ts because nothing else calls them.
 * `() => false`: an account nobody holds is a real answer, rendered inside the
 * panel, not a missing response. `test_every_path_the_ui_calls_exists_on_this_api`
 * reads this file as well as api.ts, so a path written here is held to the
 * router the same way.
 */
export function loadAccountHolders(accountId: string, fixture: HoldFixture | null = null): Promise<Result<AccountHolders>> {
  if (fixture !== null) return Promise.resolve(fixtureHolders(fixture))
  return read<AccountHolders>(
    route('/v1/accounts/{account_id}/holders', { account_id: accountId }),
    () => false,
  )
}

export function loadAccountHistory(
  accountId: string,
  cursor: string | null,
  fixture: HoldFixture | null = null,
): Promise<Result<HoldHistory>> {
  if (fixture !== null) return Promise.resolve(fixtureHistory(fixture))
  const query = cursor ? new URLSearchParams({ cursor }) : undefined
  return read<HoldHistory>(
    route('/v1/accounts/{account_id}/history', { account_id: accountId }, query),
    () => false,
  )
}

/**
 * THE DEVELOPMENT ANSWERS (visual QA V017, 2026-10-11). Holding now and
 * History had no fixture, so in development both always drew a 404 and
 * nobody could look at them. These are used only when the board itself came
 * from the fixture (`AccountsBoard.fixture`), and they are built from the
 * fixture account so the two agree with its row: as many live holds as its
 * `assigned`, a lent account's holds split between owner and borrower, and a
 * history that carries every ending -- released, unusable, expired, still
 * holding -- plus one hold whose task was not recorded and one unverified.
 */
export interface HoldFixture {
  account: Account
  /** The tenant the fixture board was read as: whose holds are "mine". */
  viewer: string | null
}

/** A task id that differs from its neighbours in its last eight, as real ones do. */
function fixtureTask(account: Account, n: number): string {
  let h = n + 1
  for (const c of account.account_id) h = (h * 31 + c.charCodeAt(0)) >>> 0
  return `task_fixture_${h.toString(16).padStart(8, '0')}`
}

function fixtureViewer({ account, viewer }: HoldFixture): 'owner' | 'borrower' {
  return viewer === null || account.owner_tenant === viewer ? 'owner' : 'borrower'
}

function fixtureHolders(fx: HoldFixture): Result<AccountHolders> {
  const { account } = fx
  const now = Date.now()
  const viewer = fixtureViewer(fx)
  const lent = account.lend_to[0]
  const holders: AccountHolder[] = []
  let others = 0
  for (let i = 0; i < account.assigned; i++) {
    const since = new Date(now - (i + 1) * 23 * 60_000).toISOString()
    // A lent account's last hold is the borrower's: the owner sees whose, a
    // borrower sees only that there is one.
    const borrowed = lent !== undefined && i === account.assigned - 1
    if (viewer === 'borrower' && !borrowed) {
      others++
      continue
    }
    holders.push({
      since,
      task_id: fixtureTask(account, i),
      attempt: i + 1,
      tenant: borrowed ? lent : account.owner_tenant,
      recorded: true,
      verified: i !== 1,
    })
  }
  const byTenant = viewer === 'owner' && lent !== undefined && account.assigned > 0 ? [{ tenant: lent, n: 1 }] : undefined
  return {
    status: 'ok',
    fetchedAt: now,
    data: { account_id: account.account_id, viewer, total: account.assigned, holders, others, by_tenant: byTenant },
  }
}

function fixtureHistory(fx: HoldFixture): Result<HoldHistory> {
  const { account } = fx
  const now = Date.now()
  const at = (minutesAgo: number) => new Date(now - minutesAgo * 60_000).toISOString()
  const viewer = fixtureViewer(fx)
  const tenant = account.owner_tenant
  const spans: HoldSpan[] = [
    { since: at(23), until: null, end: null, mine: true, tenant, task_id: fixtureTask(account, 0), attempt: 1, recorded: true, verified: true },
    { since: at(190), until: at(95), end: 'released', mine: true, tenant, task_id: fixtureTask(account, 7), attempt: 2, recorded: true, verified: true },
    { since: at(400), until: at(330), end: 'unusable', mine: true, tenant, task_id: fixtureTask(account, 8), attempt: 1, recorded: true, verified: false },
    { since: at(1300), until: at(1180), end: 'expired', mine: true, tenant, recorded: false },
  ]
  return {
    status: 'ok',
    fetchedAt: now,
    data: { account_id: account.account_id, viewer, from: at(7 * 24 * 60), to: at(0), spans, next_cursor: null, others: viewer === 'borrower' ? 2 : undefined },
  }
}

/** "38m", "2h 5m", "3d 4h". Whole minutes: a hold is not timed to the second. */
function heldFor(ms: number): string {
  const m = Math.max(0, Math.floor(ms / 60_000))
  if (m < 1) return '<1m'
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ${m % 60}m`
  return `${Math.floor(h / 24)}d ${h % 24}h`
}

/**
 * A clock in UTC, `17:46Z` (VQ-035, visual QA pass 1). The sheet printed
 * `last given out 17:46` and the holds' Since in the browser's zone, beside
 * `resets 05:30Z` and the Timeline's `Times are UTC`: two clocks on one
 * sheet, one of them unmarked. Every time on Accounts is now UTC with its Z.
 */
function clockOf(iso: string): string {
  const d = new Date(iso)
  return Number.isFinite(d.getTime())
    ? `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}Z`
    : '?'
}

/**
 * WHEN AN ACCOUNT WAS LAST GIVEN OUT, as a clock (G5-16): `22:30Z` TODAY, and
 * the date before it on any other day -- a bare `22:30Z` from three days ago
 * reads as tonight's. An unreadable instant is the em dash.
 *
 * TODAY, NOT "WITHIN 24 HOURS" (live QA CAP-13): `20:00 · 9h ago` was
 * yesterday evening, and read at 05:30 it looked like a time later today.
 *
 * IN UTC, WITH ITS Z (VQ-035): `utcAt`, the Timeline's and the Clears
 * column's own clock, so "today" is the UTC day the Z names.
 */
export function givenAt(iso: string, now: number): string {
  return utcAt(iso, now)
}

/**
 * What a hold was running, in the three cases that must not read alike:
 * a task the hold's tenant owns (a link), a task it named and does NOT own
 * (`unverified`, no id), and no task named at all (`task not recorded`).
 * Only the platform view is told the second apart from the third: a tenant's
 * own hold naming a task it does not own arrives as `recorded: false` (#361),
 * so to a tenant it reads as `task not recorded`.
 */
function HoldWork({ h }: { h: { task_id?: string; attempt?: number | null; recorded?: boolean; verified?: boolean } }) {
  if (h.task_id) {
    return (
      <>
        {/* ONE TASK REFERENCE (QA G5-15): `task_…` and the last eight, as
            Holders prints the same task -- never cut from the end. */}
        <TaskRef id={h.task_id} />
        {typeof h.attempt === 'number' && <span className="muted"> attempt {h.attempt}</span>}
        {h.verified === false && <span className="ctl-mark is-partial">unverified</span>}
      </>
    )
  }
  if (h.recorded) {
    return (
      <span
        className="ctl-mark is-partial"
        aria-label="The worker named an agent this tenant does not own, so it is not shown."
      >
        unverified
      </span>
    )
  }
  return (
    <span
      className="ctl-mark is-absent"
      aria-label="No agent of this tenant's was recorded on this hold."
    >
      agent not recorded
    </span>
  )
}

/**
 * The account's holders, fetched only when asked for.
 *
 * Collapsed by default and loaded on the click, deliberately: a row opening
 * should not cost two broker round trips nobody wanted, and the count on the
 * tab is the one the listing already carried (`assigned`), so nothing has to
 * load to say how many.
 */
/** `fixture`: the viewing tenant when the board came from the development fixture, else undefined. */
type FixtureViewer = string | null | undefined

function holdFixture(account: Account, fixture: FixtureViewer): HoldFixture | null {
  return fixture === undefined ? null : { account, viewer: fixture }
}


function useLoad<T>(load: () => Promise<Result<T>>, key: string): [Result<T>, () => void] {
  const [res, setRes] = useState<Result<T>>({ status: 'loading', since: Date.now() })
  const [n, setN] = useState(0)
  useEffect(() => {
    let live = true
    setRes({ status: 'loading', since: Date.now() })
    void load().then((r) => {
      if (live) setRes(r)
    })
    return () => {
      live = false
    }
    // `load` is rebuilt each render; `key` and `n` are what change the read.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, n])
  return [res, () => setN((x) => x + 1)]
}

function HoldingNow({
  account,
  now,
  fixture,
  bare = false,
}: {
  account: Account
  now: number
  fixture: FixtureViewer
  /** Inside the sheet's `Agents on it`, which already says the count: no heading of its own. */
  bare?: boolean
}) {
  const accountId = account.account_id
  const [res, retry] = useLoad(() => loadAccountHolders(accountId, holdFixture(account, fixture)), accountId)
  // The table is named by its heading (visual QA run 3, a11y-keyboard-003).
  const headId = useId()
  if (res.status === 'loading') return <p className="muted small">Loading holders…</p>
  if (res.status === 'error') return <FailedPanel error={res.error} onRetry={retry} />
  if (res.status === 'empty') return <p className="muted small">Nobody holds it.</p>
  const b = res.data
  return (
    <div className="acct-holders">
      {!bare && <h4 id={headId}>Holding now · {b.total}</h4>}
      {b.holders.length > 0 && (
        // THE MOCK-UP'S TABLE (G5-16, QA 2026-10-07; capacity.html §D): Agent ·
        // Attempt · Since. It was a bulleted list of `task_…` ids, the one
        // thing on the row that says nothing about what the agent is doing.
        // The agent is named the way every other screen names it (`agentName`),
        // from the task read; until that read lands, or if it fails, the id is
        // the name, and the whole id is always the link's title.
        <table
          className="acct-hist acct-holding-now"
          aria-labelledby={bare ? undefined : headId}
          aria-label={bare ? `Holding now · ${b.total}` : undefined}
        >
          <thead>
            <tr>
              <th scope="col" data-col="holder">Agent</th>
              <th scope="col" data-col="attempt" className="is-num">Attempt</th>
              <th scope="col" data-col="since">Since</th>
            </tr>
          </thead>
          <tbody>
            {b.holders.map((h, i) => (
              <tr key={i}>
                <td className="acct-hist-who" title={[h.task_id, h.tenant].filter(Boolean).join(' · ') || undefined}>
                  <span className="acct-hist-whoin">
                    {h.tenant !== undefined && <TenantCut id={h.tenant} />}
                    {h.task_id ? <HolderAgent taskId={h.task_id} verified={h.verified} /> : <HoldWork h={h} />}
                  </span>
                </td>
                <td className="is-num">{typeof h.attempt === 'number' ? h.attempt : '—'}</td>
                <td>
                  {h.since ? (
                    `${clockOf(h.since)} (${heldFor(now - Date.parse(h.since))})`
                  ) : (
                    <span className="muted">not recorded</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {b.total === 0 && <p className="muted small">Nobody holds it.</p>}
      {(b.by_tenant ?? []).length > 0 && bare && <p className="acct-ss-k">Loans in use</p>}
      {(b.by_tenant ?? []).length > 0 && (
        <ul className="acct-holder-tenants">
          {(b.by_tenant ?? []).map((t) => (
            <li key={`t:${t.tenant}`}>
              {pluralise(t.n, 'agent')} · <Id className="mono">{t.tenant}</Id>
            </li>
          ))}
        </ul>
      )}
      {b.others > 0 && b.by_tenant === undefined && <p className="muted small">+{b.others} held by other tenants</p>}
    </div>
  )
}

/**
 * A tenant in a holder cell, cut in its middle (visual QA V155, 2026-10-16).
 * End-cut, a phone's Agent cell kept `data-platform-in…` -- the part many
 * teams' ids share -- and lost the part that names the team. An id longer
 * than the cell keeps its last eight, as a task reference does; the cell's
 * title holds it whole. A short one is one box, as it was.
 */
function TenantCut({ id, lead }: { id: string; lead?: string }) {
  if (id.length <= 16) {
    return (
      <span className="mono">
        {lead}
        {id}
      </span>
    )
  }
  const cut = id.length - 8
  return (
    <span className="mono acct-cut">
      {lead}
      <span className="acct-cut-head">{id.slice(0, cut)}</span>
      <span className="acct-cut-tail">{id.slice(cut)}</span>
    </span>
  )
}

/**
 * One holder's agent, by name (G5-16). The task read is the one the agent's
 * own page makes; until it lands, or if it fails, the link is `TaskRef`'s
 * short id (QA G5-15), and the whole id is its title and its copy.
 */
function HolderAgent({ taskId, verified }: { taskId: string; verified?: boolean }) {
  const [res] = useLoad(() => loadTask(taskId), taskId)
  const name = res.status === 'ok' || res.status === 'stale' ? agentName(res.data) : null
  return (
    <>
      <TaskRef id={taskId} title={name} />
      {verified === false && <span className="ctl-mark is-partial">unverified</span>}
    </>
  )
}

function HoldingHistory({ account, now, fixture }: { account: Account; now: number; fixture: FixtureViewer }) {
  const [cursor, setCursor] = useState<string | null>(null)
  const [res, retry] = useLoad(
    () => loadAccountHistory(account.account_id, cursor, holdFixture(account, fixture)),
    `${account.account_id}|${cursor ?? ''}`,
  )
  // The table is named by its heading (visual QA run 3, a11y-keyboard-003).
  const headId = useId()
  if (res.status === 'loading') return <p className="muted small">Loading history…</p>
  if (res.status === 'error') return <FailedPanel error={res.error} onRetry={retry} />
  const h = res.status === 'empty' ? null : res.data
  const spans = h?.spans ?? []
  const from = Date.parse(h?.from ?? '') || now - 7 * 86_400_000
  const to = Date.parse(h?.to ?? '') || now
  // The bar's scale: the first hold shown to the window's end, never the
  // whole seven days a few short holds would be slivers in (D24).
  const starts = spans.map((x) => Date.parse(x.since ?? '')).filter((t) => Number.isFinite(t))
  const lo = starts.length === 0 ? from : Math.max(from, Math.min(...starts))
  const scale = Math.max(1, to - lo)
  const five = readingOf(account, FIVE_HOUR)
  return (
    <div className="acct-holders">
      <h4 id={headId}>History</h4>
      {/* THE UTILISATION IS TODAY'S READING, NOT A SERIES. The broker keeps
          one reading per window, so there is no past utilisation to draw the
          spans over; the current one is printed beside them and labelled as
          current rather than stretched across the window as if it were. */}
      {(five.kind === 'live' || five.kind === 'stale') && (
        <p className="muted small">
          Five-hour window now: {Math.round(five.pct)}%
          {five.kind === 'stale' && <span className="ctl-mark is-partial">partial</span>}
        </p>
      )}
      {spans.length === 0 ? (
        <p className="muted small">No holds recorded in this window.</p>
      ) : (
        // THE PICKED TABLE (browser QA D24, 2026-10-04; components.html A): it
        // was a bulleted list whose 8rem bars drew a seven-day window, so a
        // 30-minute hold was a 1px sliver. The bar is scaled to the holds shown
        // -- the first one's start to now -- and its head says so.
        <table className="acct-hist" aria-labelledby={headId}>
          <thead>
            <tr>
              <th scope="col" data-col="holder">Holder</th>
              <th scope="col" data-col="started">Started</th>
              <th scope="col" data-col="held" className="is-num">Held</th>
              <th scope="col" data-col="ended">Ended</th>
              <th scope="col" data-col="when" title={`From ${clockOf(new Date(lo).toISOString())} to ${h?.to ? clockOf(h.to) : 'now'}`}>
                When, from {clockOf(new Date(lo).toISOString())}
              </th>
            </tr>
          </thead>
          <tbody>
            {spans.map((s, i) => {
              const a = Date.parse(s.since ?? '')
              const b = s.until ? Date.parse(s.until) : now
              const left = Number.isFinite(a) ? ((a - lo) / scale) * 100 : 0
              const span = Number.isFinite(a) && Number.isFinite(b) ? ((b - a) / scale) * 100 : 0
              return (
                <tr key={i}>
                  {/* CUT, WHOLE IN ITS TITLE (browser QA D24, 2026-10-04). */}
                  <td
                    className="acct-hist-who"
                    title={[s.task_id, typeof s.attempt === 'number' ? `attempt ${s.attempt}` : null, s.tenant].filter(Boolean).join(' · ') || undefined}
                  >
                    {/* ONE ROW THAT GIVES WAY BY ITS ID (owner QA D24, 2026-10-04):
                        the task link ran 2-3px over "attempt 1" in a 196px
                        cell. The link and the tenant shrink, each cut with
                        an ellipsis; "attempt N" and a mark never do. */}
                    <span className="acct-hist-whoin">
                      {s.mine ? <HoldWork h={s} /> : <TenantCut id={s.tenant ?? ''} />}
                      {s.mine && s.tenant !== undefined && <TenantCut id={s.tenant} lead=" " />}
                      {!s.mine && h?.viewer === 'platform' && s.task_id && <HoldWork h={s} />}
                    </span>
                  </td>
                  <td>{s.since ? clockOf(s.since) : <span className="muted">start not recorded</span>}</td>
                  <td className="is-num">{s.since ? heldFor(b - a) : '—'}</td>
                  <td title={s.end ?? 'holding'}>{s.end ?? <span className="muted">holding</span>}</td>
                  {/* ITS OWN CLASS (visual QA V015): the phone hides this column
                      by name. `td:last-child` also matched Holding now's Since,
                      the same table class with a different last column. */}
                  <td className="acct-hist-when">
                    <span className="acct-hist-bar" aria-hidden="true">
                      <i
                        style={{
                          left: `${Math.max(0, Math.min(100, left))}%`,
                          width: `${Math.max(1, Math.min(100, span))}%`,
                        }}
                      />
                    </span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      {h?.viewer === 'borrower' && (h.others ?? 0) > 0 && (
        <p className="muted small">
          {h.others} other agent{h.others === 1 ? '' : 's'} in this window
        </p>
      )}
      {h?.scan_limited && (
        <p className="muted small">
          Older history was not searched further: none of this tenant’s own holds were found in
          the most recent pages.
        </p>
      )}
      {h?.next_cursor && (
        <button type="button" onClick={() => setCursor(h.next_cursor ?? null)}>
          Older
        </button>
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
        No windows reported &mdash; the 5-hour and 7-day windows above are{' '}
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
              {/* No column head over this figure, so the word goes on it (OV-1). */}
              {r.kind === 'never' || r.kind === 'absent'
                ? '—'
                : r.kind === 'reset'
                  ? `reset, was ${Math.round(r.pct)}%`
                  : `${isProjected(r) ? '~' : ''}${Math.round(r.pct)}% used`}
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
            agent for an account whose credential is working perfectly, so seeing
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

  // FOCUS STAYS ON THIS BUTTON (a11y-keyboard-001, visual QA run 3). It is
  // `aria-disabled` while it exchanges, not `disabled`, which a browser
  // blurs to <body>; a press from the failure panel's `Try again`, which
  // unmounts with the panel, hands focus here first; and the reload an answer
  // causes remounts the screen, so the new button takes focus back on mount.
  const button = useRef<HTMLButtonElement>(null)
  const sending = state.kind === 'sending'
  const refocus = ui.focusRefresh === account.account_id
  useEffect(() => {
    if (!refocus) return
    button.current?.focus()
    patch((p) => ({ ...p, focusRefresh: null }))
    // `patch` is recreated per render; the hand-over is keyed on the flag.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refocus])

  const run = async () => {
    if (sending) return
    const action = button.current?.closest('.acct-action')
    if (action?.contains(document.activeElement)) button.current?.focus()
    setState({ kind: 'sending' })
    const res = await refreshAccount(account.account_id)
    if (res.status === 'ok') {
      const held = button.current !== null && document.activeElement === button.current
      patch((p) => ({
        ...p,
        refresh: { ...p.refresh, [account.account_id]: { kind: 'answered', result: res.data.refresh } },
        focusRefresh: held ? account.account_id : p.focusRefresh,
      }))
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
      <button
        ref={button}
        type="button"
        aria-disabled={sending || undefined}
        onClick={sending ? undefined : () => void run()}
      >
        {sending ? 'Exchanging…' : 'Refresh this account'}
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
  const [confirming, setConfirming] = useState(false)

  const parsed = splitTenants(draft)
  const owner = account.owner_tenant
  // Silently dropping the owner would make the saved list differ from the typed
  // one with nothing on screen to explain why, so it is called out instead.
  const includesOwner = parsed.includes(owner)
  const willSave = parsed.filter((t) => t !== owner)
  const dirty = willSave.join(' ') !== [...account.lend_to].join(' ')
  // CHECKED AGAINST THE TENANTS THIS PAGE ALREADY READ (live QA CAP-2). Free
  // text used to go straight to the PUT, so `no-such-tenant, BAD ID!` armed the
  // button. An id the account is already lent to is kept even if the list no
  // longer has it, so a deleted tenant can still be taken OFF the list.
  const unknown = unknownTenants(willSave, board.tenants, account.lend_to)
  const added = willSave.filter((t) => !account.lend_to.includes(t))
  const removed = account.lend_to.filter((t) => !willSave.includes(t))

  const save = async () => {
    setBusy(true)
    setError(null)
    setSaved(false)
    const res = await setAccountLending(account.account_id, willSave)
    setBusy(false)
    if (res.status === 'ok') {
      setSaved(true)
      setConfirming(false)
      reload()
    } else if (res.status === 'error' || res.status === 'stale') {
      setError(res.error)
    }
  }
  const closeConfirm = () => {
    if (!busy) setConfirming(false)
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
          <span className="mono acct-ids">
            {[owner, ...account.lend_to].map((t, i, all) => (
              <span key={t}>
                {i > 0 && ' '}
                <Id after={i < all.length - 1 ? ',' : undefined}>{t}</Id>
              </span>
            ))}
          </span>
        </li>
      </ul>
      <span className="limit-edit">
        <input
          type="text"
          className="mono acct-wide"
          value={draft}
          disabled={busy}
          // THE ADD FORM'S WORDS (visual QA V181, 2026-10-16): at 400 the
          // longer `…; empty means no lending` was cut mid-word. What an
          // empty list does is the confirmation's title, `Stop lending …?`,
          // before anything is sent.
          placeholder="tenant ids, comma separated"
          aria-label={`Tenants ${account.account_id} is lent to`}
          list={board.tenants ? 'acct-tenant-ids' : undefined}
          onChange={(e) => {
            setDraft(e.target.value)
            setSaved(false)
          }}
        />
        <button
          type="button"
          onClick={() => {
            setError(null)
            setConfirming(true)
          }}
          disabled={!dirty || busy || unknown.length > 0}
        >
          Save lending
        </button>
        {/* The second `.tag` on this screen, collapsed into the same primitive
            for the same reason as the state chip above. */}
        {saved && (
          <ToneMark tone="is-ok">saved</ToneMark>
        )}
      </span>
      {includesOwner && (
        <p className="muted small">
          <code>{owner}</code> owns this account, so it is not sent as a loan.
        </p>
      )}
      {unknown.length > 0 && (
        <p className="warn-text" role="alert">
          {board.tenants === null ? 'Not a tenant id: ' : 'No tenant named '}
          {unknown.map((t, i) => (
            <span key={t}>
              {i > 0 && ', '}
              <code>{t}</code>
            </span>
          ))}
          . Nothing was sent.
        </p>
      )}
      {confirming && (
        <Dialog
          title={
            willSave.length === 0
              ? `Stop lending ${accountName(account)}?`
              : `Lend ${accountName(account)} to ${willSave.join(', ')}?`
          }
          onClose={closeConfirm}
          actions={
            <>
              <Button kind="ghost" onClick={closeConfirm}>
                Keep the list as it is
              </Button>
              <Button kind="primary" busy={busy ? 'Saving…' : false} onClick={() => void save()}>
                Save lending
              </Button>
            </>
          }
        >
          <ul className="ctl-facts">
            <li className="ctl-fact">
              <b>account</b>
              <Id>{account.account_id}</Id>
            </li>
            {added.length > 0 && (
              <li className="ctl-fact">
                <b>lent to</b>
                <span className="mono">{added.join(', ')}</span>
              </li>
            )}
            {removed.length > 0 && (
              <li className="ctl-fact">
                <b>no longer lent to</b>
                <span className="mono">{removed.join(', ')}</span>
              </li>
            )}
          </ul>
          {added.length > 0 && (
            <p className="muted small">
              Agents in {added.join(', ')} will be able to run on this account&rsquo;s credential.
            </p>
          )}
          {error && (
            <p className="c-hint" role="alert">
              {errorHeading(error)} &mdash; {error.message} The lending list was not changed.
            </p>
          )}
        </Dialog>
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
    </div>
  )
}

/** The shape a tenant id can take; anything else is not one. */
const TENANT_ID = /^[a-z0-9-]{1,63}$/

/**
 * The lend-to ids that name no tenant. Against the tenant list when it was
 * read; when it was not, against the id's shape alone -- the platform still
 * checks the rest. Ids already on the account are never refused here.
 */
export function unknownTenants(
  ids: readonly string[],
  tenants: readonly { tenant_id: string }[] | null,
  already: readonly string[],
): string[] {
  const known = tenants === null ? null : new Set(tenants.map((t) => t.tenant_id))
  return ids.filter((t) => !already.includes(t) && (known === null ? !TENANT_ID.test(t) : !known.has(t)))
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
// Model classes
// ---------------------------------------------------------------------------

/**
 * The model-class vocabulary, in the order the broker stores and this screen
 * sends it. Frozen with the contract; it belongs beside `Account` in types.ts
 * and lives here only until the field does.
 */
export const MODEL_CLASSES = ['opus', 'fable', 'sonnet', 'haiku'] as const

/**
 * The classes an account serves, as the broker listed them. `[]` -- and an
 * absent field, from a broker older than R5 -- is EVERY class, never none.
 */
export function servedClasses(account: Account): string[] {
  return (account as ServedAccount).model_classes ?? []
}

/** What the fact row prints: the list, or the words for the empty one. */
function classesWord(classes: readonly string[]): string {
  return classes.length === 0 ? 'every class' : classes.join(', ')
}

/**
 * What a set of ticked boxes is SAVED as. All four and none are both `[]`:
 * an account that serves nothing is a pause, which is the state control's job
 * and has a reason attached, so this control cannot make one. Anything else
 * goes in vocabulary order, so the same choice always sends the same body.
 */
export function classesToSave(checked: readonly string[]): string[] {
  const kept = MODEL_CLASSES.filter((c) => checked.includes(c))
  return kept.length === 0 || kept.length === MODEL_CLASSES.length ? [] : [...kept]
}

/**
 * Which model classes this account takes steps of (owner decision R5,
 * 2026-10-17).
 *
 * NARROWING IS NOT FREE, AND IT IS NOT SILENT EITHER. The scheduler never
 * hands a step of a class to an account that does not serve it, and a step
 * whose class NO account serves waits parked (`no_account_serves_class`),
 * holding nothing -- so the cost of a too-narrow list is waiting work, which
 * the task screens show, rather than work run on the wrong subscription.
 * Owner only, for the same reason as lending: `isOwned` decides, in Detail.
 */
function ModelClasses({ account, reload }: { account: Account; reload: () => void }) {
  const served = servedClasses(account)
  // EVERY CLASS STARTS WITH EVERY BOX TICKED: an empty list is drawn as what
  // it means, not as what it looks like.
  const [draft, setDraft] = useState<string[]>(
    served.length === 0 ? [...MODEL_CLASSES] : served,
  )
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const [saved, setSaved] = useState(false)

  const willSave = classesToSave(draft)
  const dirty = willSave.join(' ') !== served.join(' ')

  const save = async () => {
    setBusy(true)
    setError(null)
    setSaved(false)
    const res = await setAccountModelClasses(account.account_id, willSave)
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
      <h4>Model classes</h4>
      <span className="limit-edit">
        <fieldset
          className="acct-classes"
          disabled={busy}
          aria-label={`Model classes ${account.account_id} serves`}
        >
          {MODEL_CLASSES.map((c) => (
            <label key={c} className="acct-class">
              <input
                type="checkbox"
                checked={draft.includes(c)}
                onChange={(e) => {
                  setDraft(e.target.checked ? [...draft, c] : draft.filter((d) => d !== c))
                  setSaved(false)
                }}
              />{' '}
              <span className="mono">{c}</span>
            </label>
          ))}
        </fieldset>
        <button type="button" onClick={() => void save()} disabled={!dirty || busy}>
          {busy ? 'Saving…' : 'Save classes'}
        </button>
        {saved && <ToneMark tone="is-ok">saved</ToneMark>}
      </span>
      <p className="muted small">All four or none saves as every class.</p>
      {error && (
        <p className="c-hint" role="alert">
          {errorHeading(error)} &mdash; {error.message} The class list was not changed.
        </p>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

/**
 * A state button's label, and the one spelling the prose that names the button
 * uses too -- so "use move to available" cannot drift from the button it means.
 */
function moveLabel(to: AccountStateName): string {
  // MOCK-UP A's WORDS (#2222, F4): Pause and Drain, and Resume for the way
  // back. The buttons, their confirmations and the writes are unchanged.
  if (to === 'PAUSED') return 'Pause'
  if (to === 'DRAINING') return 'Drain'
  if (to === 'AVAILABLE') return 'Resume'
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
  // CONFIRM BEFORE WRITING (live QA CAP-1). A state button used to PUT on its
  // first click: one stray click on `move to draining` took an account that
  // was serving seven agents out of rotation for the whole tenant. A button
  // now only OPENS the confirm, which names the account and how many agents
  // hold it; the write happens from the dialog's verb.
  const [asking, setAsking] = useState<AccountStateName | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const [done, setDone] = useState<AccountStateName | null>(null)

  const close = () => {
    if (busy) return
    setAsking(null)
    setReason('')
    setError(null)
  }

  const move = async (to: AccountStateName) => {
    // Taking an account OUT of rotation needs a reason: it is what the next
    // person reads on the account to know whether it is safe to put it back.
    // Putting it back needs none.
    if (stateNeedsReason(to) && reason.trim() === '') return
    setBusy(true)
    setError(null)
    setDone(null)
    const res = await setAccountState(account.account_id, to, reason.trim())
    setBusy(false)
    if (res.status === 'ok') {
      setDone(to)
      setAsking(null)
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
  const asked = targets.find((t) => t.to === asking) ?? null
  const needsReason = asking !== null && stateNeedsReason(asking)
  const agents = `${account.assigned} agent${account.assigned === 1 ? '' : 's'}`

  return (
    <div className="acct-action">
      <h4>
        State
      </h4>
      {/* VERBS, NOT THE ENUM (CP-23; #2222 mock-up A F4): Pause, Drain,
          Resume. The enum printed raw was the only thing on the screen still
          shouting; the prose that names these buttons (`SignInDone`) spells
          them through `moveLabel` too. */}
      <div className="acct-buttons">
        {targets.map((t) => (
          <button
            key={t.to}
            type="button"
            title={t.why}
            disabled={account.state === t.to}
            onClick={() => {
              setDone(null)
              setError(null)
              setAsking(t.to)
            }}
          >
            {moveLabel(t.to)}
          </button>
        ))}
      </div>
      {done && <p className="muted small">Moved to {done.toLowerCase()}.</p>}
      {asked !== null && (
        <Dialog
          title={`Move ${accountName(account)} to ${asked.to.toLowerCase()}?`}
          onClose={close}
          actions={
            <>
              <Button kind="ghost" onClick={close}>
                Leave it {account.state.toLowerCase()}
              </Button>
              <Button
                kind={asked.to === 'AVAILABLE' ? 'primary' : 'danger'}
                disabled={needsReason && reason.trim() === ''}
                busy={busy ? 'Saving…' : false}
                onClick={() => void move(asked.to)}
              >
                {moveLabel(asked.to)}
              </Button>
            </>
          }
        >
          <ul className="ctl-facts">
            <li className="ctl-fact">
              <b>account</b>
              <Id>{account.account_id}</Id>
            </li>
            <li className="ctl-fact">
              <b>agents on it</b>
              <span>{account.assigned}</span>
            </li>
            <li className="ctl-fact">
              <b>after</b>
              <span>{asked.why}</span>
            </li>
          </ul>
          {asked.to !== 'AVAILABLE' && account.assigned > 0 && (
            <p className="warn-text">
              {asked.to === 'DRAINING'
                ? `${agents} on it now will be moved off.`
                : `${agents} on it now keep running; no new ones start on it.`}{' '}
              {/* AN ID IS ONE WORD (V070): "loses it u-⏎bogdan". */}
              Every agent in <Id>{account.owner_tenant}</Id>
              {account.lend_to.length > 0 ? ` and the tenants it is lent to` : ''} loses it for new work.
            </p>
          )}
          <label className="c-field">
            <span className="c-lbl">{needsReason ? 'Reason (required)' : 'Reason'}</span>
            <input
              type="text"
              className="c-inp"
              value={reason}
              disabled={busy}
              placeholder="why, e.g. held back for Monday's release run"
              aria-label={`Reason for changing the state of ${account.account_id}`}
              aria-required={needsReason}
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
          {error && (
            <p className="c-hint" role="alert">
              {errorHeading(error)} &mdash; {error.message} The state was not changed.
            </p>
          )}
        </Dialog>
      )}
    </div>
  )
}

/** Taking an account out of rotation is the move that has to say why. */
export function stateNeedsReason(to: AccountStateName): boolean {
  return to === 'PAUSED' || to === 'DRAINING'
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
// application cannot register `https://swarm.example.com/callback`, so the browser
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
/**
 * THE PLATFORM'S REFUSAL, WITHOUT ITS RESTART INSTRUCTION, for a code from
 * another sign-in (visual QA V018). The broker refuses that paste before it
 * reads the pending record, so this sign-in is untouched and still open --
 * which `ExchangeAdvice` says, directly under the panel. The broker's message
 * ends "Press Add account and sign in again.", so the two read as opposite
 * instructions. The sentence saying WHAT was refused stays verbatim; only the
 * instruction this page knows to be unnecessary is dropped.
 */
export function withoutRestart(e: ApiError): ApiError {
  const kept = e.message.replace(/\s*Press Add account and sign in again\.?\s*$/i, '').trim()
  return kept === '' || kept === e.message ? e : { ...e, message: kept }
}

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

  // THE STEPS COUNT FROM THE FIRST ONE THIS PANEL DRAWS (visual QA V066).
  // The add form's step 1 is naming the account; a re-auth has no such step
  // -- the account is named -- and started at "2 ·" with no 1.
  const first = mode === 'add' ? 2 : 1
  return (
    <>
      <div className="acct-action">
        <h4>
          {first} &middot; Sign in to Claude
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
          {first + 1} &middot; Paste the code that page shows you
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
              error={refusal === 'other_sign_in' ? withoutRestart(at.failed) : at.failed}
              what="the code exchange"
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

/** The platform's message, ended as a sentence: the broker's label refusal has no full stop. */
function asSentence(message: string): string {
  const m = message.trim()
  return /[.!?]$/.test(m) ? m : `${m}.`
}

/** The broker's `validate_label` (`quota_broker/accounts.py` `_LABEL`), restated. */
const LABEL_MAX = 40

/**
 * WHY THIS LABEL WOULD BE REFUSED, in words, or null when it fits (#1912).
 *
 * The broker's rule is `^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$`: the label
 * becomes part of a Secret Manager name and a Kubernetes annotation, so it is
 * not this form's to relax. `AI-ENG-2` was sent as typed and came back as "a
 * service the API depends on did not answer". Capitals never reach here --
 * the input lowercases as it is typed (owner decision 2026-10-22, "Lowercase
 * as typed, plus 400") -- so everything below is a character or a shape that
 * has no lowercase spelling. An empty label is not a fault: Start is simply
 * disabled, as it always was.
 */
function labelFault(label: string): string | null {
  if (label === '') return null
  const bad = [...label].find((c) => !/[a-z0-9-]/.test(c))
  if (bad !== undefined) {
    return /\s/.test(bad)
      ? 'a space is not allowed: use a dash. Only lowercase letters, digits and dashes.'
      : `"${bad}" is not allowed: only lowercase letters, digits and dashes.`
  }
  if (label.startsWith('-')) return 'it must start with a letter or a digit, not a dash.'
  if (label.endsWith('-')) return 'it must end with a letter or a digit, not a dash.'
  if (label.length > LABEL_MAX) return `it is ${label.length} characters; at most ${LABEL_MAX}.`
  return null
}

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
  // OPENED BY A CLICK, IT TAKES FOCUS AND CAN BE CLOSED (live QA CAP-11). It
  // opened with focus left on <body>, had no Cancel and ignored Esc. Only a
  // form somebody opened folds: on an empty pool it is drawn with no click,
  // as the one fix, and there is nothing to go back to.
  const labelRef = useRef<HTMLInputElement | null>(null)
  const closable = ui.adding && accounts.length > 0
  useEffect(() => {
    if (ui.adding && state.kind === 'idle') labelRef.current?.focus()
    // Only on opening: a re-render while typing must not move the caret.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ui.adding])
  // A NEW STEP TAKES FOCUS (visual QA pass 2 cap2-02): Start unmounted under
  // the keyboard and left focus on <body>. When the sign-in link arrives, or
  // the receipt, focus goes to the step's first control.
  const panelRef = useRef<HTMLElement | null>(null)
  const stepWas = useRef(state.kind)
  useEffect(() => {
    const was = stepWas.current
    stepWas.current = state.kind
    if (was === state.kind || (state.kind !== 'open' && state.kind !== 'done')) return
    panelRef.current?.querySelector<HTMLElement>('a[href], button:not([disabled]), input:not([disabled])')?.focus()
  }, [state.kind])
  const cancel = () => {
    patch((p) => ({ ...p, adding: false, signin: p.signin.kind === 'start_failed' ? { kind: 'idle' } : p.signin }))
    setLabel('')
    setLend('')
    // Back to the control that opened it.
    if (typeof document !== 'undefined') document.querySelector<HTMLButtonElement>('.acct-add')?.focus()
  }

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
  // Read off the label as typed, not `trimmed`: a space anywhere, the ends
  // included, is a character the broker refuses, and trimming it away
  // silently would send something other than what is on screen.
  const fault = labelFault(label)
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
    // A REFUSED LABEL IS NEVER SENT (#1912): Start is disabled for it, and
    // Enter in the field reaches here without the button.
    if (trimmed === '' || fault !== null) return
    const lendTo = splitTenants(lend).filter((t) => t !== owner)
    // A sign-in holds the form open (`addOpen`) until it is closed.
    patch((p) => ({ ...p, adding: true, signin: { kind: 'starting' } }))
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
    <section
      ref={panelRef}
      className="section panel acct-add-panel"
      onKeyDown={(e) => {
        if (e.key === 'Escape' && closable) {
          e.stopPropagation()
          cancel()
        }
      }}
    >
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

      {/* THE RECEIPT IS THE PANEL'S ANSWER, AND THE FORM IS DONE (visual QA
          V182, 2026-10-16). The empty form stayed open under it, Start
          disabled, so the receipt sat between owner and provider as if it
          were one of the form's fields. `done` closes the panel and the new
          account is in the list beside it; Add account opens a fresh form. */}
      {state.kind === 'done' && (
        <SignInDone
          state={state}
          onAgain={() => {
            patch((p) => ({ ...p, adding: false, signin: { kind: 'idle' } }))
            setLabel('')
            setLend('')
            // Back to Add account -- once the render has enabled it: it is
            // disabled while a sign-in is not idle, and a disabled button
            // takes no focus.
            if (typeof window !== 'undefined') {
              window.setTimeout(() => document.querySelector<HTMLButtonElement>('.acct-add')?.focus(), 0)
            }
          }}
        />
      )}

      {state.kind === 'done' ? null : state.kind === 'open' ? (
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
          // `acct-add-form` (visual QA V069): label, field and its rule as one
          // group, groups 12px apart. The hints sat 4px above the next label.
          className="acct-add-form"
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
            <ToneMark tone="is-info"
              label="Fixed. This is the only credential kind this pool handles, so there is nothing to choose between — but the form states the value it sends rather than hiding it.">fixed</ToneMark>
          </p>

          <label className="t-label" htmlFor="acct-label">
            label
          </label>
          <input
            id="acct-label"
            ref={labelRef}
            className="mono acct-wide"
            value={label}
            spellCheck={false}
            autoComplete="off"
            placeholder="laptop"
            aria-invalid={fault !== null ? true : undefined}
            aria-describedby="acct-label-rule"
            // LOWERCASED AS IT IS TYPED (#1912, owner decision 2026-10-22):
            // the broker accepts only lowercase, and `AI-ENG-2` is plainly
            // meant as `ai-eng-2`, so the field holds what will be sent.
            onChange={(e) => setLabel(e.target.value.toLowerCase())}
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
            <span
              id="acct-label-rule"
              className={`mono acct-label-rule${fault !== null ? ' is-fault warn-text' : ''}`}
              aria-label="Lowercase letters, digits and dashes, starting and ending alphanumeric, at most 40 characters."
            >
              a-z 0-9 - · ≤40
            </span>
          </p>
          {/* THE FAULT IS SAID IN WORDS, under the rule it breaks (#1912). The
              rule line above is marked as the fault, and Start stays disabled
              until the label fits: the form never sends a label the broker
              would refuse. */}
          {fault !== null && (
            <p className="warn-text acct-label-fault" role="alert">
              <strong>This label would be refused:</strong> {fault}
            </p>
          )}
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
            <button type="submit" disabled={state.kind === 'starting' || trimmed === '' || fault !== null}>
              {state.kind === 'starting' ? 'asking for a sign-in link…' : 'Start the sign-in'}
            </button>
            {closable && (
              <button type="button" className="acct-add-cancel" onClick={cancel}>
                Cancel
              </button>
            )}
          </p>
          {/* A REFUSED LABEL IS SAID AT THE FORM, NOT IN A FAILURE PANEL
              (visual QA V010, 2026-10-11). A 422 is the platform answering
              about the input -- usually the label rule -- and the read-failure
              panel drew it as "a failure to read the platform" with a Try again
              that resent the same refused label. It is a line under the button
              naming the field to change; nothing was created. */}
          {state.kind === 'start_failed' && state.error.kind === 'invalid' && (
            <p className="warn-text acct-refused" role="alert">
              <strong>The platform refused this sign-in:</strong> {asSentence(state.error.message)}{' '}
              Nothing was created. Correct what it names above and press <em>Start the sign-in</em>.
            </p>
          )}
          {state.kind === 'start_failed' && state.error.kind !== 'invalid' && (
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
                what="the sign-in start"
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
          Done
        </button>
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Sign in again, on a row that already exists
// ---------------------------------------------------------------------------

/** routes/accounts.py's ChatGPT-plan provider (#2688): codex runs on it. */
const PLAN_PROVIDER = 'openai'

/** A word a POSIX shell reads as itself; anything else is single-quoted. */
function shellWord(word: string): string {
  return /^[A-Za-z0-9._:@/-]+$/.test(word) ? word : `'${word.replace(/'/g, `'\\''`)}'`
}

/**
 * THE COMMAND THAT SIGNS A PLAN IN AGAIN: `sc account add`, aimed at this
 * label, with the provider and every loan. Re-registering a label REPLACES
 * `lend_to` rather than merging it (the reason `Reauth` sends it back), so
 * a command without each `--lend-to` would revoke every loan as a side effect.
 */
export function planSignInCommand(account: Pick<Account, 'label' | 'lend_to'>): string {
  const loans = (account.lend_to ?? []).map((t) => ` --lend-to ${shellWord(t)}`).join('')
  return `sc account add --label ${shellWord(account.label)} --provider ${PLAN_PROVIDER}${loans}`
}

/**
 * The sign-in section of a ChatGPT plan: what happens, and the command.
 *
 * THE STATE NEEDS NO SECOND CALL HERE. The broker clears REAUTH_REQUIRED
 * itself when a sign-in registers a working pair (`_provision_and_register`
 * in quota_broker/main.py), and this page reads the pool on its own poll, so
 * the account's new state appears here without anyone saying they are done.
 */
function PlanReauth({ account, broken }: { account: Account; broken: boolean }) {
  return (
    <div className="acct-action" data-replace={account.account_id} data-plan-sign-in="">
      <h4>{broken ? 'Sign in again' : 'Replace the credential'}</h4>
      <p className="muted small">
        {broken ? (
          <>
            <strong>Only a person can replace this.</strong> The refresh token
            behind <Id className="mono">{account.account_id}</Id> is gone or unreadable.
          </>
        ) : (
          <>
            Replaces the credential behind <Id className="mono" after=".">{account.account_id}</Id>{' '}
            Nothing here needs doing on a healthy account.
          </>
        )}
      </p>
      <p className="muted small">
        A ChatGPT plan&rsquo;s sign-in finishes on your own computer, not in this browser
        tab: the plan sends its code to <span className="mono">localhost:1455</span>, where{' '}
        <span className="mono">sc</span> listens. Run this in a terminal on the computer
        this browser runs on:
      </p>
      <p>
        <code className="acct-hs-msg" data-cmd="">
          {planSignInCommand(account)}
        </code>
      </p>
      <p className="muted small">
        It opens the sign-in page and registers the plan under the same label, with the same
        loans.{' '}
        {broken
          ? 'The account leaves REAUTH_REQUIRED as soon as it registers, and this page shows it on its next read.'
          : 'This page shows the new credential’s expiry on its next read.'}
      </p>
    </div>
  )
}

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

  // A CHATGPT PLAN SIGNS IN ON THE PERSON'S OWN MACHINE (#2688). swarm-api's
  // sign-in start takes `provider: "openai"` now, but the plan's OAuth client
  // has one registered redirect, `http://localhost:1455/auth/callback`
  // (quota_broker/openai_plan.py, measured), and only `sc account add` listens
  // there. A browser control would open a page whose code lands on a port
  // nothing is listening on, so this section says so and gives the command
  // instead -- and posts nothing, least of all a Claude sign-in.
  if (account.provider === PLAN_PROVIDER) {
    return <PlanReauth account={account} broken={broken} />
  }

  // A CONTROL THAT WOULD REWRITE A FIELD IT CANNOT SEND. The sign-in below
  // produces a `SUBSCRIPTION_PROVIDER` credential and its request body carries
  // no provider, so re-registering a row that says anything else -- a provider
  // the API cannot sign in at all -- would file the account under a provider
  // nobody chose on this screen, the silent rewrite the removed
  // `provider: account.provider` argument existed to prevent. Refused and
  // named, rather than offered and surprising.
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
              <Mark
                kind="zero"
                say="Nothing here failed and nothing was changed. This control simply does not apply to an account of this provider."
              />
            </li>
          </ul>
        </div>
      </div>
    )
  }

  const start = async () => {
    set({ kind: 'starting' })
    const res = await beginAccountSignIn({
      // NEITHER THE TENANT NOR THE PROVIDER IS SENT. swarm-api resolves the
      // tenant from the verified token -- and this control renders only on a
      // row this tenant owns -- and a sign-in with no provider is a Claude
      // sign-in, which is the only kind that reaches this call: a plan is
      // sent to `PlanReauth` above and any other provider is refused.
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
    // `data-replace`: the duplicate banner's "Replace the credential" lands here (#2222).
    <div className="acct-action" data-replace={account.account_id}>
      <h4>{broken ? 'Sign in again' : 'Replace the credential'}</h4>
      <p className="muted small">
        {broken ? (
          <>
            <strong>Only a person can replace this.</strong> The refresh token
            behind <Id className="mono">{account.account_id}</Id> is gone or unreadable.
          </>
        ) : (
          <>
            {/* The full stop rides inside the id's box (V154's comma). */}
            Replaces the credential behind <Id className="mono" after=".">{account.account_id}</Id>{' '}
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
              what="the sign-in start"
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
  const whyId = useId()

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
              <ToneMark tone="is-warn"
                label={`${account.assigned} agent${account.assigned === 1 ? '' : 's'} currently hold${account.assigned === 1 ? 's' : ''} this account. Move it to DRAINING first if you want them off it before it goes.`}>in use</ToneMark>
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
        {/* A DANGER OUTLINE UNTIL ARMED (owner decision 2026-10-22, "Danger
            outline unarmed", box comment 6314115845, part of #1680). Unarmed,
            it was first the filled red pale-tinted for disabled, which read
            as ready to fire (capacity-002), then the plain grey button, which
            hid that it is the one irreversible control here. It is the danger
            ink on its edge and its words with no fill until the label is
            typed (`.c-btn.is-danger:disabled` in styles/capacity.css), and the
            filled red only once a click would remove the account. THE LABEL,
            NOT THE NAME, ARMS IT: the label is the id, and a Name is free
            text that two accounts may share. */}
        {/* WHY IT IS OFF IS ITS DESCRIPTION (visual QA pass 2 cap2-13): the
            line beside it was not tied to the button, so a reader on it heard
            a dimmed control with no reason. */}
        <Button
          kind={armed ? 'danger-filled' : 'danger'}
          disabled={!armed || busy}
          aria-describedby={armed ? undefined : whyId}
          onClick={() => void run()}
        >
          {busy ? 'Removing…' : `Remove ${account.label}`}
        </Button>
        {!armed && (
          <span className="client-side" id={whyId}>
            off until you type {account.label} above
          </span>
        )}
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
 * sentence of it is now a topic in `help.ts`, reachable from the index the
 * page title's `?` opens (a footer of links until VQ-033, BGD 2026-10-26),
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
 * See the note inside it. Since VQ-033 (BGD 2026-10-26) the list is the index
 * the page title's `?` opens rather than a footer, so it renders no words with
 * the card closed; the budget the note weighs it against no longer reads it,
 * and the list stays as it was chosen.
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
