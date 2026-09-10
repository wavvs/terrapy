"""Consume events while the command runs, and cancel it cleanly.

Run: python examples/03_streaming.py

`apply()` collects every event and returns them at the end. `apply_stream()`
returns a live `Run` instead, so progress can be printed or forwarded to a UI
while the command runs. Press Ctrl-C during the apply to trigger the graceful
cancel path.
"""

from __future__ import annotations

import logging

from _sandbox import sandbox

from terrapy import (
    ApplyCompleteEvent,
    ApplyErroredEvent,
    ApplyProgressEvent,
    ApplyStartEvent,
    ChangeSummaryEvent,
    DiagnosticEvent,
    Event,
    EventRouter,
    Terrapy,
)

log = logging.getLogger("example")


def stream_apply(tf: Terrapy) -> None:
    """Iterate the events yourself."""
    with tf.apply_stream(var={"file_count": 4}) as run:
        print(f"pid {run.pid}: {' '.join(run.args)}")
        try:
            for event in run:
                if isinstance(event, ApplyStartEvent):
                    print(f"  start    {event.address}")
                elif isinstance(event, ApplyProgressEvent):
                    print(f"  running  {event.address} ({event.elapsed_seconds:.0f}s)")
                elif isinstance(event, ApplyCompleteEvent):
                    print(f"  done     {event.address} {event.id_key}={event.id_value}")
                elif isinstance(event, ApplyErroredEvent):
                    print(f"  failed   {event.address}")
                elif isinstance(event, DiagnosticEvent):
                    diag = event.diagnostic
                    print(f"  {diag.severity.value}: {diag.summary}")
        except KeyboardInterrupt:
            # SIGINT to the whole process group, so provider plugins stop too.
            # A hard kill follows if the child is still alive after 10s.
            print("cancelling")
            run.cancel(grace=10)
            run.wait()

        result = run.result()

    print(f"exit {result.exit_code} after {result.duration:.1f}s")
    if result.stderr:
        print(f"stderr tail: {result.stderr.strip()[:200]}")


def routed_plan(tf: Terrapy) -> None:
    """Register handlers per event type and let EventRouter dispatch them."""
    router = EventRouter()
    router.on("diagnostic", lambda ev: log.warning("%s", ev.message))
    router.on("planned_change", lambda ev: print(f"  planned  {ev.message}"))
    router.on("change_summary", lambda ev: print(f"  summary  {ev.message}"))

    tf.plan(on_event=router.dispatch)


def typed_callback(tf: Terrapy) -> None:
    """`on_event` is a plain callable, so isinstance checks work here too."""

    def handle(event: Event) -> None:
        if isinstance(event, ChangeSummaryEvent):
            print(f"  tally    +{event.summary.add} -{event.summary.remove}")

    tf.destroy(on_event=handle)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    with sandbox() as workdir:
        tf = Terrapy(workdir, timeout=600, inactivity_timeout=120)
        tf.init()

        print("streaming apply:")
        stream_apply(tf)

        print("routed plan:")
        routed_plan(tf)

        print("destroy:")
        typed_callback(tf)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
