# Run work on a schedule

A schedule starts the same work again and again, for example every weekday at 09:00.

## Create a schedule in the console

1. Open [Automate › Schedules](/automate/schedules).
2. Press **New schedule**.
3. Fill in the five cards in order:
   1. **Type and template**: what to run, and a **Name**.
   2. **Target**: the **Repositories** it works on.
   3. **Cadence**: a preset such as **Weekdays at 09:00**, or a **Cron expression**, and a **Timezone**.
   4. **Gates and approvers**: who must approve a run before it goes ahead.
   5. **Budget and tripwire**: the most it may spend **Per run (USD)** and **Per day (USD)**.
4. Press **Create schedule**.

## Use a schedule

* **Run now** starts it once, outside its cadence.
* **Dry run** shows what it would do, without doing it.
* **Pause** and **Resume** stop and restart its cadence.
* Runs that need approval wait on [Automate › Approvals](/automate/approvals).

## In Claude Code

Ask in plain words, for example "list my SwarmCloud schedules" or "pause the nightly schedule". Claude Code says what it will change first.
