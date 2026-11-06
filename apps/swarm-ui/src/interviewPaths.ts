// The addresses of the plugin interview's two full pages (InterviewPage.tsx),
// apart from the page so the run's page and Needs you link to it without
// importing the page itself.
//
// Both pages ride on the Runs tab's query, so `/runs?interview=<task>`
// and `/runs?proposal=<run>` round-trip through paths.ts as the tab's own query.

export const INTERVIEW_KEY = 'interview'
export const PROPOSAL_KEY = 'proposal'

/** The interview page of one task: `work/runs?interview=<task>`. */
export function interviewAddress(taskId: string): string {
  return `work/runs?${new URLSearchParams({ [INTERVIEW_KEY]: taskId }).toString()}`
}

/** A planner run's proposal page: `work/runs?proposal=<run>`. */
export function proposalAddress(runId: string): string {
  return `work/runs?${new URLSearchParams({ [PROPOSAL_KEY]: runId }).toString()}`
}

export type InterviewAt = { kind: 'interview'; taskId: string } | { kind: 'proposal'; runId: string }

/** Which of the two pages a Runs view names, or null for any other view. */
export function interviewPageOf(view: string | null): InterviewAt | null {
  if (view === null || view === '') return null
  const q = new URLSearchParams(view)
  const task = q.get(INTERVIEW_KEY)
  if (task !== null && task !== '') return { kind: 'interview', taskId: task }
  const run = q.get(PROPOSAL_KEY)
  if (run !== null && run !== '') return { kind: 'proposal', runId: run }
  return null
}

