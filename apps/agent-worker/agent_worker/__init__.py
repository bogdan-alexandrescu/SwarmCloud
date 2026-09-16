"""Agent worker: the process that actually runs one attempt of one task.

The worker is deliberately the only component that touches a tenant's provider
credentials, their workspace and their artifacts. It is also the component most
likely to be killed mid-flight, which is why its lifecycle is written as an
explicit, ordered state machine in `lifecycle.py` rather than as a script:

    validate fencing generation  <-- before ANYTHING else, including the workspace
    mark STARTING / RUNNING
    isolated workspace
    restore latest checkpoint
    optional shallow clone
    run the runner child process
    heartbeat + MANDATORY periodic checkpoint while it runs
    react to cancellation / quota / backpressure
    upload artifacts + final checkpoint
    persist terminal state
    release the lease
    exit
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
