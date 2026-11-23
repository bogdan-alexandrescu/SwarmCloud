### `status <wf_id|task_id>`

ONE read, then report it. An id beginning `wf_` is a workflow: call
`swarm_workflow_status` with it once and report the workflow's `state` (null
means it was not derived — say that, never quote `stored_state`) and each
step's state. Any other id is a task: call `swarm_status` with
`task_ids: [<id>]` once and report its state. Start the line with the
workspace the reply names, `[<workspace>] <id> · <state>` (the reply's
`workspace`; leave it out only when the reply has none -- never fill it in
from the session's tenant). A waiting task's reply carries `waiting_line` --
why it waits, in the API's own words, with its next check when known: print
it after the state, `[<workspace>] <id> · <state> · <waiting_line>`, e.g.
`[eng@example.com] task_31c3… · PARKED · CI on archived pull request 1779: 5 checks pending; next
check 02:46Z`, and nothing more of `waiting` (its detail and history are the
console page's). No `waiting_line` -- a task not waiting, or an API older
than it -- is the line without it. An error is reported verbatim:
an id the API does not have and another tenant's id read the same. Never poll;
a second look is the developer's next `/sc status`. In the `band` layout the
strip above the prompt queues this verb itself when it notices a change, with
what changed after the id -- `/sc status wf_… · finished: succeeded · PR
archived pull request 2463`, `· <step> waits for a person`, `· merge parked on owner questions
(<step>)`, `· new owner questions: N` -- so the session learns of it as it
did from a row: make the ONE read, report it, and say what the strip noticed
(a waiting step: `/sc answer`; owner questions: `swarm_result`'s
`owner_questions`, put to the person). Both tools are the sc
plugin's own read tools and work outside a checkout; they are not pre-granted
here -- this skill grants only the `sc` and `swarm` commands -- so
the session may ask once before the first read.
