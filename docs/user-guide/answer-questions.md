# Answer a question from your work

An agent or a workflow step can stop and ask a person. It waits, using no capacity, until someone answers.

## In the console

1. Open [Home](/). Questions show in the card **Needs you**.
2. Read the question, pick or type your answer, and press **Send answer**.
   You should see "Answer sent to …". The agent goes back to waiting for room, then runs on.
3. A workflow step that waits for input shows a form on the workflow's page. Fill it in and press **Send**.
   A review step offers **Approve** or **Request changes** instead.

Any member of the workspace may answer. The first answer wins.

## In Claude Code

1. `/sc inputs` lists the workflow steps waiting for an answer.
2. `/sc answer wf_… step-id` opens a form in Claude Code. You type the answer; Claude Code never writes it for you.
3. When an agent you started asks questions, Claude Code puts them to you as a choice and sends your pick.
