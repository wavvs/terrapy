"""init, plan, apply, read outputs, destroy.

Run: python examples/01_lifecycle.py

The first `init` downloads the `local` provider, so this example needs network
access once. After that everything is local.
"""

from __future__ import annotations

from _sandbox import sandbox

from terrapy import Terrapy


def main() -> int:
    with sandbox() as workdir:
        tf = Terrapy(workdir, timeout=600)
        print(f"binary:  {tf.binary_path}")
        print(f"version: {tf.version().version}")

        tf.init()

        plan = tf.plan(out="tfplan", var={"file_count": 2})
        if not plan.has_changes:
            print("no changes")
            return 0

        summary = plan.change_summary
        print(f"plan: +{summary.add} ~{summary.change} -{summary.remove}")

        applied = tf.apply("tfplan")
        for progress in applied.resources:
            took = f"{progress.elapsed_seconds:.1f}s"
            print(f"  {progress.action.value:<8} {progress.address}  {took}")

        for name, output in applied.outputs.items():
            print(f"output {name} = {output.value}")

        # Same information, fetched separately, when you did not keep the
        # apply result around.
        print("filenames:", tf.output("filenames").value)

        destroyed = tf.destroy()
        print(f"destroyed {destroyed.change_summary.remove} resource(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
