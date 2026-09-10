"""Read the per-resource diff out of a plan.

Run: python examples/02_plan_diff.py

The streamed `-json` plan output contains a change summary but no resource
diff. terrapy fills `resource_changes` by running `show -json` against the
saved plan file, which requires `out=`. Without `out=`, only `has_changes` and
`change_summary` are populated.
"""

from __future__ import annotations

from _sandbox import sandbox

from terrapy import Change, Terrapy


def label(change: Change) -> str:
    if change.is_replace:
        return "replace"
    if change.is_create:
        return "create"
    if change.is_delete:
        return "delete"
    if change.is_update:
        return "update"
    return "no-op"


def changed_keys(change: Change) -> list[str]:
    """Attributes whose value differs between before and after."""
    before = change.before if isinstance(change.before, dict) else {}
    after = change.after if isinstance(change.after, dict) else {}
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))


def main() -> int:
    with sandbox() as workdir:
        tf = Terrapy(workdir, timeout=600)
        tf.init()

        tf.apply(var={"greeting": "first", "file_count": 2})

        plan = tf.plan(out="tfplan", var={"greeting": "second", "file_count": 3})

        print(f"summary: {plan.change_summary}")
        print(f"plan file: {plan.plan_file}")
        print(f"terraform: {plan.plan.terraform_version if plan.plan else '?'}")
        print()

        for resource in plan.resource_changes:
            print(f"{label(resource.change):<8} {resource.address}")
            print(f"         type={resource.type} provider={resource.provider_name}")
            if resource.action_reason:
                print(f"         reason={resource.action_reason}")
            keys = changed_keys(resource.change)
            if keys:
                print(f"         changed: {', '.join(keys)}")

        print()
        for name, output in plan.output_changes.items():
            print(f"output {name}: {label(output.change)} sensitive={output.sensitive}")

        tf.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
