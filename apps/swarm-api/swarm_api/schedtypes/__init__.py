"""Schedule type executors, one module per type (docs/schedules.md §3, §9).

A type is AVAILABLE when its file is here (`scheduletypes.executor_present`):
`issue-sweep` is `issue_sweep.py`, and so on. Each module defines
`create(firing)` and `dry_run(firing)`, the seam `schedulefire.Executor`
names, and is imported only when a firing of its type finishes
(`schedulefire.load_executor`). Lanes S6 and S10a-h each add one file here
and edit nothing shared.

This file imports nothing, on purpose: listing the catalogue must not import
an executor, and an executor must not be imported because another one was.
"""
