/**
 * ONE SESSION'S RECORD, on Telemetry's picked session (Telemetry.tsx): its
 * transcript, as one button, and its summary (swarm-api
 * `GET /v1/voice/sessions/{id}/transcript` and `/summary`).
 *
 * THE TRANSCRIPT is read through the api layer (`loadVoiceTranscript`), which
 * carries the chosen tenant's header and the IAP cookie, and saved from a Blob
 * through an object URL: a bare link would carry no tenant header. A failed
 * read is two words, its reason behind the tooltip, and nothing is saved.
 *
 * THE SUMMARY (the owner's console rule: a button, a short heading and the
 * summary, nothing else on the surface): written is the heading and the text;
 * capped, failed and pending one short line each; none draws nothing. The
 * text is the session's own, not the console's prose, so it is marked
 * `data-prose-content` as the stage's lines are. A summary read that fails
 * draws nothing: the button still offers the transcript.
 */
import { useEffect, useState } from 'react'

import { loadVoiceSessionSummary, loadVoiceTranscript, type VoiceSessionSummary } from '../api'
import { Button } from '../components/Button'
import { Tooltip } from '../components/Overlay'
import type { ApiError } from '../fetch'

/** The one line each unwritten state shows; `none` shows nothing. */
export const SUMMARY_LINE: Readonly<Record<Exclude<VoiceSessionSummary['state'], 'written' | 'none'>, string>> = {
  capped: 'No summary: session at its cap.',
  failed: 'No summary.',
  pending: 'Summary pending.',
}

/** Hand `blob` to the browser as a saved file named `filename`. */
function save(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  try {
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.rel = 'noopener'
    document.body.appendChild(a)
    a.click()
    a.remove()
  } finally {
    URL.revokeObjectURL(url)
  }
}

function Summary({ summary }: { summary: VoiceSessionSummary | null }) {
  if (summary === null || summary.state === 'none') return null
  if (summary.state === 'written') {
    return (
      <div className="vtel-summary">
        <h2>Summary</h2>
        {summary.text !== null && <p data-prose-content="">{summary.text}</p>}
      </div>
    )
  }
  return <p className="vtel-summary-line">{SUMMARY_LINE[summary.state]}</p>
}

export function SessionRecord({ sessionId }: { sessionId: string }) {
  const [summary, setSummary] = useState<VoiceSessionSummary | null>(null)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<ApiError | null>(null)

  useEffect(() => {
    let gone = false
    setSummary(null)
    setFailed(null)
    void loadVoiceSessionSummary(sessionId).then((r) => {
      if (gone) return
      setSummary(r.status === 'ok' || r.status === 'stale' ? r.data : null)
    })
    return () => {
      gone = true
    }
  }, [sessionId])

  const download = async (): Promise<void> => {
    setBusy(true)
    setFailed(null)
    try {
      const r = await loadVoiceTranscript(sessionId)
      if (r.status === 'ok' || r.status === 'stale') save(r.data.blob, r.data.filename)
      else if (r.status === 'error') setFailed(r.error)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="vtel-record" aria-label="Session record" data-session-record={sessionId}>
      <div className="vtel-record-head">
        <Button kind="secondary" size="sm" busy={busy} onClick={() => void download()}>
          Download transcript
        </Button>
        {failed !== null && (
          <span role="alert" className="vtel-record-fail">
            <Tooltip tip={failed.message}>
              <span tabIndex={0}>Not downloaded</span>
            </Tooltip>
          </span>
        )}
      </div>
      <Summary summary={summary} />
    </section>
  )
}
