import type { WaitingRow } from './types'

/**
 * WAIT-UI-LISTS (#1782, owner decision 4): THE ONE-LINE REASON A LIST ROW HAS
 * ROOM FOR, cut from `waiting.summary`.
 *
 * During the 2026-10-20/21 CI stall every merge step in the Waiting tab said
 * the same "waiting for CI", whether its checks were queued, failing or done.
 * WAIT-API now serves `{reason, summary}` on every listing row, and the
 * summary is a whole sentence ("CI on PR #1779: 5 checks pending (shell,
 * ruff, python, ...); re-checked 2 times; next check 02:46Z"), which a
 * one-line row cuts before the fact that tells two rows apart. This keeps
 * that fact: "CI #1779: 5 pending", "capacity: runner:merge full".
 *
 * PARSED FROM THE SUMMARY, because a listing row carries nothing else: the
 * structured `detail` is served by `GET /v1/tasks/{id}` alone. The patterns
 * are the sentences `swarm_api/waiting_detail.py` writes. A sentence they do
 * not match -- a reason newer than this bundle, or a wording WAIT-API later
 * changes -- falls back to the summary's first clause (up to its first `; `),
 * so a drift costs brevity, never the reason. The whole summary stays the
 * row's title and accessible name.
 *
 * Null when there is no summary to say: the row keeps the line it had.
 */
export function waitingShort(w: Pick<WaitingRow, 'reason' | 'summary'>): string | null {
  const summary = typeof w.summary === 'string' ? w.summary.trim() : ''
  if (summary === '') return null
  const first = summary.split('; ')[0]!
  return byReason(String(w.reason), summary, first) ?? cut(first)
}

/** The longest fallback a row draws; the title holds the rest. */
const FALLBACK_MAX = 60

function cut(text: string): string {
  return text.length > FALLBACK_MAX ? `${text.slice(0, FALLBACK_MAX - 1)}…` : text
}

/** "(shell, ui (typecheck), ...)" removed, nested parentheses included. */
function dropParens(text: string): string {
  let out = text
  for (let prev = ''; prev !== out; ) {
    prev = out
    out = out.replace(/\s*\([^()]*\)/g, '')
  }
  return out
}

/** Upstream steps a row names before it counts the rest. */
const UPSTREAM_NAMED = 2

const PROVIDER_WORD: Readonly<Record<string, string>> = {
  'quota exhausted': 'quota',
  'cooling down': 'cooldown',
  outage: 'outage',
}

function byReason(reason: string, summary: string, first: string): string | null {
  switch (reason) {
    case 'CI_PENDING': {
      const m = /^CI on (?:PR #(\d+)|its pull request): (.*)$/.exec(first)
      if (m === null) return null
      const where = m[1] !== undefined ? `CI #${m[1]}` : 'CI'
      const rest = dropParens(m[2]!)
      if (/^checks not recorded$/.test(rest)) return `${where}: checks not recorded`
      const parts: string[] = []
      const pending = /(\d+) checks pending/.exec(rest)
      if (pending !== null) parts.push(`${pending[1]} pending`)
      const failed = /(\d+) failed/.exec(rest)
      if (failed !== null) parts.push(`${failed[1]} failed`)
      return parts.length === 0 ? null : `${where}: ${parts.join(', ')}`
    }
    case 'READY_CAPACITY': {
      const ready = summary.replace(/^Ready; /, '').split('; ')[0]!
      let m = /^capacity binds on (\S+) \((\d+) of (\d+) units in use\)$/.exec(ready)
      if (m !== null) {
        const [, pool, active, limit] = m
        return Number(active) >= Number(limit) ? `capacity: ${pool} full` : `capacity: ${pool} ${active}/${limit} used`
      }
      if ((m = /^pool (\S+) is paused$/.exec(ready)) !== null) return `capacity: ${m[1]} paused`
      if ((m = /^pool (\S+) could not be read$/.exec(ready)) !== null) return `capacity: ${m[1]} unread`
      if ((m = /^pool (\S+) admits \S+ units and this task needs \S+$/.exec(ready)) !== null) {
        return `capacity: ${m[1]} too small`
      }
      if ((m = /^the last scheduler pass found (\S+) refusing/.exec(ready)) !== null) return `capacity: ${m[1]} refused`
      if (/waiting for the scheduler's next pass$/.test(ready)) return 'next scheduler pass'
      return null
    }
    case 'QUEUED':
      return /^Queued\b/.test(first) ? 'queued' : null
    case 'PROVIDER_QUOTA_EXHAUSTED':
    case 'PROVIDER_COOLDOWN':
    case 'PROVIDER_OUTAGE': {
      const m = /^Provider (quota exhausted|cooling down|outage) on (.+?)(?: \(account [^)]*\))?$/.exec(first)
      if (m === null) return null
      const until = /; until (\S+)/.exec(summary)
      return `${PROVIDER_WORD[m[1]!]}: ${m[2]}${until !== null ? ` until ${until[1]}` : ''}`
    }
    case 'SCHEDULED_RETRY': {
      const m = /^Retry scheduled: attempt (\d+) of (\d+)(?: at (\S+))?/.exec(first)
      if (m === null) return null
      return `retry ${m[1]} of ${m[2]}${m[3] !== undefined ? ` at ${m[3]}` : ''}`
    }
    case 'DEPENDENCY_INCOMPLETE': {
      // QA1-FIX-36 (overview-agents-57): "upstream: 1 step" named no step.
      // The summary names each one ("...: scan-terraform (RUNNING), plan
      // (QUEUED)"), so the row says which: two names, the rest counted.
      const m = /^Waiting on (\d+) upstream steps?(?:: (.+))?$/.exec(first)
      if (m === null) return null
      const names = m[2] === undefined ? [] : dropParens(m[2]).split(', ').map((n) => n.trim()).filter((n) => n !== '')
      if (names.length === 0) return `upstream: ${m[1]} ${m[1] === '1' ? 'step' : 'steps'}`
      const more = names.length > UPSTREAM_NAMED ? ` +${names.length - UPSTREAM_NAMED} more` : ''
      return cut(`waiting on ${names.slice(0, UPSTREAM_NAMED).join(', ')}${more}`)
    }
    case 'CHILDREN_INCOMPLETE': {
      const m = /^Waiting for (\d+) of (\d+) children/.exec(first)
      if (m !== null) return `children: ${m[1]} of ${m[2]} open`
      return /^Waiting for its children/.test(first) ? 'children: not finished' : null
    }
    case 'AWAITING_INPUT':
      return /^Waiting for a member of /.test(first) ? 'awaiting an answer' : null
    case 'HUMAN_REQUIRED': {
      // A merge step held by the owner's questions parks HUMAN_REQUIRED too
      // (#2313): it waits for answers, and nobody signs in to anything.
      const m = /^Waiting for answers to (\d+) owner questions?/.exec(first)
      if (m !== null) return `answers: ${m[1]} owner ${m[1] === '1' ? 'question' : 'questions'}`
      if (/^Waiting for the owner questions/.test(first)) return 'answers: owner questions'
      return /^Waiting for a person/.test(first) ? 'needs a person to sign in' : null
    }
    case 'CREDENTIAL_MISSING':
      return /^Credential missing/.test(first) ? cut(first.replace(/^Credential missing/, 'credential missing')) : null
    case 'BUDGET_EXHAUSTED':
      return /^Budget exhausted/.test(first) ? cut(first.replace(/^Budget exhausted/, 'budget exhausted')) : null
    case 'MANUAL_PAUSE':
      return /^Paused\b/.test(first) ? cut(`p${first.slice(1)}`) : null
    default:
      return null
  }
}
