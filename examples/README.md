# Examples

Five runnable scripts, ordered by complexity. Run them from any directory:

```bash
python examples/01_lifecycle.py
```

| Script | Shows |
|---|---|
| [01_lifecycle.py](01_lifecycle.py) | init, plan, apply, outputs, destroy |
| [02_plan_diff.py](02_plan_diff.py) | reading the per-resource diff out of a saved plan |
| [03_streaming.py](03_streaming.py) | live events, `EventRouter`, cancelling a run |
| [04_errors.py](04_errors.py) | every exception terrapy raises, and how to handle it |
| [05_fake_runner.py](05_fake_runner.py) | driving the client with no binary installed |

## What they need

Scripts 01 through 04 need `terraform` or `tofu` on `PATH`, and network access
the first time, because `init` downloads the `local` provider. Each one creates
a temporary directory, applies a few `local_file` resources into it, and deletes
the directory on exit. No cloud credentials are required.

Script 05 needs neither a binary nor a network. It swaps in a fake runner and
replays `-json` output.

## Shared config

[_sandbox.py](_sandbox.py) holds the temporary Terraform config and the context
manager that writes it into a temp directory.
