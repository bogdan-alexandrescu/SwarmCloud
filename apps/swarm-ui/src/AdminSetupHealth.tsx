/**
 * ADMIN › SETUP › HEALTH (DAYONE-SETUP-UI-1; part of #2688;
 * docs/design/day-one-plan.md §5.1, D27, frame B12b).
 *
 * The latest scheduled doctor run, read from `GET /v1/admin/setup/health`
 * (swarm-doctor writes it; this page only reads): when it ran, the checks it
 * ran and its findings. Each finding says what the probe saw, why it could
 * not check when it could not (a grant still propagating, or one missing),
 * the doctor's fix in words and its repair command (`swarm install --repair
 * <id>`, which re-runs the one step that satisfies the entry); an entry no
 * install step makes has no repair command, and the page says so rather than
 * inventing one. The full check list shows each check's last result.
 *
 * READ IN THE SHAPE THE DOCTOR WRITES (DAYONE-HEALTH-SHAPE): every row is
 * `doctor.Finding.to_json()`, `{id, status: met|unmet|unknown, detail, step,
 * fix, cause, repair}`; api.ts's normSetupHealth maps it, and the test holds
 * the fixture to the writer's keys.
 *
 * SAYS ONLY WHAT EXISTS (DAYONE-SETUP-UI-4): the hourly doctor is off unless
 * `enable_scheduled_doctor` turned it on (terraform/infra/doctor.tf), so no
 * run recorded says it is off rather than promising one every hour.
 *
 * "NOT CHECKED" IS SHOWN AS SUCH, NEVER AS PASSING: a check the doctor could
 * not decide (`unknown`, or any status it did not write) is drawn as not
 * checked and counted apart, and its finding says it could not be checked
 * rather than that it drifted; no doctor run at all says so rather than
 * drawing a clean page.
 */
import { useCallback, useEffect, useState } from 'react'
import { loadSetupHealth, type HealthCheck, type HealthFinding, type Result, type SetupHealth } from './api'
import { Banner, Button, Card, Chip, CodeBlock, ToneMark, type MarkTone } from './components'
import { spanWords } from './AdminSetupSteps'
import './styles/onboarding.css'

interface ResultLook {
  tone: MarkTone
  word: string
  cls: string
}

const PASSED: ResultLook = { tone: 'ok', word: 'passed', cls: 'is-done' }
const FAILED: ResultLook = { tone: 'bad', word: 'failed', cls: 'is-failed' }
const NOT_CHECKED: ResultLook = { tone: 'unknown', word: 'not checked', cls: 'is-todo' }

const resultOf = (c: HealthCheck): ResultLook => (c.result === 'passed' ? PASSED : c.result === 'failed' ? FAILED : NOT_CHECKED)

/** Why a probe saw a 403, in words (doctor.Finding.cause). */
const CAUSE_WORDS: Record<string, string> = {
  propagating: 'the doctor holds the permission, but IAM is still propagating the grant; the next run should see it.',
  missing_grant: 'the doctor’s identity lacks a permission this check needs, so it could not look.',
}

/** One finding: what the probe saw, why, the doctor's fix and its repair command. */
export function HealthFindingPanel({ finding }: { finding: HealthFinding }) {
  const drifted = finding.result === 'failed'
  const cause = finding.cause === null ? null : (CAUSE_WORDS[finding.cause] ?? finding.cause)
  return (
    <section aria-label={finding.id}>
      <Banner tone={drifted ? 'bad' : 'warn'} title={drifted ? `${finding.id} drifted` : `${finding.id} could not be checked`}>
        {finding.detail ?? (drifted ? `The ${finding.id} check failed on the last doctor run.` : `The ${finding.id} check could not decide on the last doctor run.`)}
      </Banner>
      {cause !== null && (
        <p className="ur-hint">
          <b>Why:</b> {cause}
        </p>
      )}
      {finding.fix !== null && (
        <p className="ur-hint">{finding.fix}</p>
      )}
      {finding.repair !== null ? (
        <CodeBlock title="Repair" lang="shell" text={finding.repair} />
      ) : (
        <p className="ur-hint">No install step makes this one, so there is no repair command: the doctor’s fix says what to do.</p>
      )}
    </section>
  )
}

/** The doctor run as it was recorded: findings first, then every check's last result. */
export function HealthPanel({ health, now, onReload, busy }: { health: SetupHealth | null; now: number; onReload?: () => void; busy?: boolean }) {
  const reload =
    onReload === undefined ? null : (
      <Button size="sm" onClick={onReload} busy={busy === true ? 'Reading' : false}>
        Read again
      </Button>
    )
  if (health === null) {
    return (
      <Card title="Health" action={reload ?? undefined}>
        <p className="ur-hint">The hourly doctor is off: no doctor run is recorded, so nothing here is known to pass. Its findings show here once it is on and has run.</p>
      </Card>
    )
  }
  const notChecked = health.checks.filter((c) => resultOf(c) === NOT_CHECKED).length
  const drifted = health.findings.filter((f) => f.result === 'failed').length
  const undecided = health.findings.length - drifted
  const at = health.run_at === null ? Number.NaN : Date.parse(health.run_at)
  const when = Number.isNaN(at) ? 'at an unrecorded time' : `${spanWords((now - at) / 1000)} ago`
  return (
    <Card
      title="Health"
      action={
        <span className="ob-acts">
          {drifted > 0 && <Chip tone="bad">{drifted} drifted</Chip>}
          {undecided > 0 && <Chip tone="park">{undecided} could not be checked</Chip>}
          {health.findings.length === 0 && <Chip>no findings</Chip>}
          {reload}
        </span>
      }
      foot={`The checks the hourly doctor runs, read-only, on the live deployment · last run ${when}`}
    >
      {health.findings.map((f) => (
        <HealthFindingPanel key={f.id} finding={f} />
      ))}
      <p className="ob-summary">
        {health.checks.length} checks · {health.checks.length - notChecked} run
        {notChecked > 0 ? ` · ${notChecked} of ${health.checks.length} not checked` : ''}
      </p>
      <ul className="ob-list" aria-label="Every check">
        {health.checks.map((c) => {
          const r = resultOf(c)
          return (
            <li key={c.id} className={`ob-step ${r.cls}`}>
              <ToneMark tone={r.tone} hidden />
              <div className="ob-step-main">
                <p className="ob-step-h">
                  <b>{c.id}</b>
                  <span className="ob-state">{r.word}</span>
                </p>
                {c.detail !== null && <p className="ur-hint ob-line">{c.detail}</p>}
              </div>
            </li>
          )
        })}
      </ul>
    </Card>
  )
}

/** Admin › Setup › Health: reads the latest doctor run, and again on request. `now` fixes the clock. */
export function AdminSetupHealth({ now }: { now?: number }) {
  const [result, setResult] = useState<Result<SetupHealth | null>>(() => ({ status: 'loading', since: Date.now() }))
  const [busy, setBusy] = useState(false)
  const load = useCallback(() => {
    setBusy(true)
    void loadSetupHealth().then((r) => {
      setBusy(false)
      setResult(r)
    })
  }, [])
  useEffect(load, [load])
  const nowMs = now ?? Date.now()
  if (result.status === 'loading') {
    return (
      <Card title="Health">
        <p className="ur-hint">Reading the latest doctor run…</p>
      </Card>
    )
  }
  if (result.status === 'error') {
    return (
      <Card title="Health">
        <Banner tone={result.error.kind === 'admin_required' ? 'warn' : 'bad'} title={result.error.kind === 'admin_required' ? 'Health is for platform admins' : 'The latest doctor run could not be read'} actions={<Button size="sm" onClick={load}>Read again</Button>}>
          {result.error.message}
        </Banner>
      </Card>
    )
  }
  return <HealthPanel health={result.status === 'empty' ? null : result.data} now={nowMs} onReload={load} busy={busy} />
}
