# terrapy

A Python wrapper around the Terraform and OpenTofu command-line tools.

Standard library only, no runtime dependencies.

```python
from terrapy import Terrapy

tf = Terrapy("infra/prod")
tf.init()

plan = tf.plan(out="tfplan")
if plan.has_changes:
    result = tf.apply("tfplan")
    print(result.outputs["endpoint"].value)
```

## Contents

- [Requirements](#requirements)
- [Install](#install)
- [Examples](#examples)
- [Client](#client)
- [Commands](#commands)
  - [version](#version) | [init](#init) | [validate](#validate) | [fmt](#fmt) | [plan](#plan)
  - [apply and destroy](#apply-and-destroy) | [Outputs](#outputs) | [Inspecting plans and state](#inspecting-plans-and-state)
  - [state](#state) | [workspace](#workspace) | [run_raw](#run_raw)
- [Data model](#data-model)
  - [Results](#results) | [Plan](#plan-1) | [State](#state-1) | [Diagnostics](#diagnostics)
  - [Summaries and version](#summaries-and-version) | [Enums](#enums)
- [Streaming](#streaming)
- [Events](#events)
  - [Lifecycle](#lifecycle) | [Planning](#planning) | [Per-resource hooks](#per-resource-hooks)
  - [Lines that are not events](#lines-that-are-not-events)
- [Timeouts and cancellation](#timeouts-and-cancellation)
- [Errors](#errors)
  - [Exit codes](#exit-codes) | [Exceptions](#exceptions)
- [Logging](#logging)
- [Testing code that uses terrapy](#testing-code-that-uses-terrapy)
- [Development](#development)

## Requirements

- Python 3.10 or newer.
- `terraform` or `tofu` on `PATH` (or an explicit path passed to the client).
- Terraform 1.9+ / OpenTofu 1.7+ (for a full `-json` argument support).

## Install

```bash
git clone <repository-url> terrapy
cd terrapy
pip install .
```

For development, install in editable mode with the test and lint tooling:

```bash
pip install -e ".[dev]"
```

## Examples

See [examples](examples/).

## Client

```python
from terrapy import Terrapy

tf = Terrapy("infra/prod", timeout=1800, parallelism=20)
```

| Argument | Default | Effect |
|---|---|---|
| `working_dir` | current directory | Directory the commands run in. |
| `binary_path` | `None` | Explicit binary. A bare name (`"tofu"`) is resolved on `PATH`; an absolute path is used as given. With `None`, `terraform` is tried first, then `tofu`. |
| `env` | `None` | Extra environment variables, merged last so they win. |
| `inherit_env` | `True` | Start from `os.environ`. Set `False` for a clean environment. |
| `automation` | `True` | Sets `TF_IN_AUTOMATION=1` and `CHECKPOINT_DISABLE=1`. |
| `input` | `False` | Sets `TF_INPUT=false` and passes `-input=false`, so a command never blocks on a prompt. Named after the CLI flag. |
| `no_color` | `True` | Passes `-no-color`. |
| `parallelism` | `None` | Default `-parallelism` for plan, apply and destroy. |
| `use_chdir` | `False` | When `True`, pass `-chdir=<working_dir>` and leave the process working directory alone, instead of running the child in `working_dir`. |
| `timeout` | `None` | Wall-clock seconds allowed per invocation. |
| `inactivity_timeout` | `None` | Seconds without a single event before the run is cancelled. Useful when a command can hang without dying. |
| `logger` | `logging.getLogger("terrapy")` | Logger for diagnostics. |
| `runner` | `SyncRunner()` | Injection point for tests. See [Testing code that uses terrapy](#testing-code-that-uses-terrapy). |

Choosing a tool explicitly:

```python
Terrapy("infra", binary_path="tofu")                       # PATH lookup, OpenTofu only
Terrapy("infra", binary_path="/opt/terraform/1.9.8/terraform")
```

Read-only properties: `tf.working_dir`, `tf.binary_path`.

You can also write `with Terrapy(...) as tf:`.

## Commands

Every method maps to one CLI subcommand. Result classes all inherit from `CommandResult`,
which carries `command`, `exit_code`, `stdout`, `stderr`, `duration` and `diagnostics`.

| Method | Command | Returns |
|---|---|---|
| `version()` | `version -json` | `VersionInfo` |
| `init()` | `init -json` | `InitResult` |
| `validate()` | `validate -json` | `ValidateResult` |
| `fmt()` | `fmt` | `FmtResult` |
| `plan()` | `plan -json -detailed-exitcode` | `PlanResult` |
| `apply()` | `apply -json` | `ApplyResult` |
| `destroy()` | `destroy -json` | `ApplyResult` |
| `output()` | `output -json` | `dict[str, OutputValue]` or one `OutputValue` |
| `show_plan(path)` | `show -json <path>` | `Plan` |
| `show_state()` | `show -json` | `State` |
| `force_unlock(id)` | `force-unlock` | `CommandResult` |
| `run_raw(*args)` | anything | `CommandResult` |
| `state.*` | `state <sub>` | see below |
| `workspace.*` | `workspace <sub>` | see below |

### version

```python
info = tf.version()
info.version          # "1.9.8"
info.version_tuple    # VersionTuple(major=1, minor=9, patch=8)
info.platform         # "linux_amd64"
info.provider_selections
```

`VersionTuple` compares with `<`, `<=`, `>`, `>=` on major/minor/patch, so a capability check
is a plain comparison:

```python
from terrapy.models import VersionTuple

if tf.version().version_tuple >= VersionTuple.parse("1.9.0"):
    ...
```

The result is cached per client, behind a lock, so repeated calls do not respawn the
process.

### init

```python
result = tf.init(
    backend_config={"bucket": "tf-state", "key": "prod/terraform.tfstate"},
    upgrade=True,
)
```

| Argument | Meaning |
|---|---|
| `backend` | `False` adds `-backend=false` and skips backend initialization. |
| `backend_config` | A mapping (each pair becomes `-backend-config=k=v`), a sequence of ready-made strings, or a path to a backend config file. |
| `reconfigure` | Discard the existing backend configuration instead of migrating. |
| `migrate_state` | Migrate state to the new backend. |
| `upgrade` | Upgrade modules and providers past the lock file. |
| `get` | `False` adds `-get=false` and skips module downloads. |
| `plugin_dirs` | Directories of pre-staged provider plugins. |
| `lockfile` | Value for `-lockfile`, for example `"readonly"`. |
| `from_module` | Copy a module's contents into the working directory before initializing it. The directory must be empty. |
| `on_event` | Callback invoked for each streamed event. |
| `timeout` | Overrides the client timeout for this call. Provider downloads dominate the runtime of most pipelines. |

Failure raises `InitializationError`.

### validate

```python
report = tf.validate()      # raises ValidationError if the config is invalid
report.warning_count
```

`validate -json` exits 1 when it ran fine and found problems, so terrapy accepts exit 1,
parses the report, then raises `ValidationError` carrying it:

```python
from terrapy import ValidationError

try:
    tf.validate()
except ValidationError as exc:
    for d in exc.result.diagnostics:
        print(d.severity, d.summary, d.range.filename if d.range else "")
```

### fmt

```python
result = tf.fmt(check=True, recursive=True)
if not result.ok:
    print("needs formatting:", result.changed_files)
```

With `check=True` the CLI exits 3 when files need formatting. terrapy counts that as success
and reports it through `result.ok` and `result.changed_files`, so an unformatted tree raises
nothing. `write=False` leaves every file untouched. `diff=True` prints the diff to stdout.

### plan

```python
plan = tf.plan(
    out="tfplan",
    var={"replicas": 3, "tags": {"env": "prod"}},
    var_files=["prod.tfvars"],
    targets=["module.db"],
)
plan.has_changes                 # True when the CLI exited 2
plan.change_summary.add          # 4
plan.resource_changes            # populated only when out= was given
```

| Argument | Meaning |
|---|---|
| `out` | Save the plan to this file. See the note below. |
| `destroy` | Plan a destroy without running one. |
| `refresh_only` | Plan only the state refresh. |
| `refresh` | `False` adds `-refresh=false`. |
| `var` | Mapping of variables. Strings and numbers pass through; booleans, lists and dicts are JSON-encoded, which is what HCL expects for complex values. |
| `var_files` | Paths passed as repeated `-var-file`. |
| `targets` | Repeated `-target`. |
| `replace` | Repeated `-replace`. |
| `parallelism` | Overrides the client default. |
| `on_event` | Called once per streamed event. |

`out` also controls how much of the result is filled in. The streamed `-json` plan output reports a change summary but not the full
resource diff. With `out`, terrapy runs a second command, `show -json <planfile>`, and
fills in `plan.plan`, `plan.resource_changes` and `plan.output_changes`. Without `out` those
fields stay empty, and only `change_summary` and `has_changes` are meaningful.

Inspecting a diff:

```python
for change in plan.resource_changes:
    if change.change.is_replace:
        print("replacing", change.address, "because", change.action_reason)
```

`is_replace` and its six siblings are properties. Full field list under
[Data model](#data-model).

### apply and destroy

```python
result = tf.apply("tfplan")                     # apply a saved plan
result = tf.apply(var={"replicas": 3})          # plan and apply in one step
result = tf.destroy(targets=["module.scratch"])
```

Both return `ApplyResult`:

```python
result.change_summary.add           # 4
result.outputs["endpoint"].value    # from the final outputs event
result.resources                    # per-resource progress, correlated by address
result.events                       # every event, in order
```

`auto_approve` defaults to `True`. In `-json` mode the CLI cannot ask for interactive
confirmation, so an apply with neither a saved plan file nor `auto_approve=True` raises
`UsageError` before any process starts.

`result.resources` is one `ResourceProgress` per resource, correlated from the four
`apply_*` events by resource address: final action, elapsed time and error flag.

### Outputs

```python
outputs = tf.output()             # dict[str, OutputValue]
endpoint = tf.output("endpoint")  # one OutputValue, or UsageError if absent

endpoint.value
endpoint.sensitive
```

Both forms run the same command. `output -json NAME` prints a bare value with no type or
sensitivity metadata, so terrapy always fetches the full map and filters locally.

### Inspecting plans and state

```python
plan = tf.show_plan("tfplan")   # static plan representation
state = tf.show_state()         # current state as the CLI renders it

for resource in state.resources:
    print(resource.address, resource.values.get("id"))
```

### state

```python
tf.state.list()                              # every address
tf.state.list(addresses=["module.db"])       # filtered
tf.state.show("aws_s3_bucket.logs")          # one StateResource
tf.state.pull()                              # State, straight from the backend
tf.state.push("terraform.tfstate", force=False)
tf.state.mv("aws_s3_bucket.a", "aws_s3_bucket.b", dry_run=True)
tf.state.rm("aws_s3_bucket.old")
tf.state.replace_provider("registry.terraform.io/-/aws", "registry.terraform.io/hashicorp/aws")
```

`state.show()` pulls the full state and filters in Python, because `state show` has no JSON
output. It raises `UsageError` when the address is not in state.

### workspace

```python
tf.workspace.list()                    # ["default", "prod"]
tf.workspace.show()                    # "prod"
tf.workspace.new("staging")
tf.workspace.select("staging", or_create=True)
tf.workspace.delete("staging", force=True)
```

For a scoped switch, use the context manager:

```python
with tf.workspace.use("staging"):
    tf.plan()
```

`use()` sets `TF_WORKSPACE` for the duration of the block through a `ContextVar` rather than
running `workspace select`. Nothing on disk changes, and two threads can target different
workspaces through the same client at the same time. `workspace select` mutates shared
on-disk state and would race.

### run_raw

For anything terrapy does not model:

```python
result = tf.run_raw("providers", "lock", "-platform=linux_amd64", timeout=600)
result = tf.run_raw("import", "aws_s3_bucket.x", "my-bucket", success_exit_codes=(0, 1))
```

The client's environment, working directory and binary still apply. The output is not
parsed; the return value is a plain `CommandResult`.

## Data model

Results and payloads are frozen dataclasses. Every payload model also keeps the JSON it was
parsed from on `.raw`.

Parsing is tolerant. A missing key, or a key of the wrong type, gives you the field default
instead of an exception, so a new field in a CLI release breaks nothing.

### Results

| Class | Returned by | Fields |
|---|---|---|
| `CommandResult` | `force_unlock`, `run_raw`, `state.*`, `workspace.*` | `command`, `exit_code`, `stdout`, `stderr`, `duration`, `diagnostics` |
| `PlanResult` | `plan()` | above, plus `has_changes`, `change_summary`, `resource_changes`, `output_changes`, `plan_file`, `plan`, `events` |
| `ApplyResult` | `apply()`, `destroy()` | above, plus `change_summary`, `outputs`, `resources`, `events` |
| `InitResult` | `init()` | above, plus `upgraded`, `reconfigured`, `events` |
| `FmtResult` | `fmt()` | above, plus `changed_files`, `ok` |
| `ValidateResult` | `validate()` | `valid`, `error_count`, `warning_count`, `diagnostics`, `format_version`, `raw` |

`ValidateResult` does not extend `CommandResult`. `events` holds every parsed
event in order, and stays out of `repr` and equality because one apply can produce thousands.

### Plan

`Plan` is the static form of `show -json <planfile>`.

| Class | Fields |
|---|---|
| `Plan` | `format_version`, `terraform_version`, `variables`, `planned_values`, `resource_changes`, `output_changes`, `prior_state`, `configuration`, `relevant_attributes`, `checks`, `timestamp`, `applyable`, `complete`, `errored`, `raw` |
| `ResourceChange` | `address`, `module_address`, `mode`, `type`, `name`, `provider_name`, `change`, `action_reason`, `index`, `deposed`, `raw` |
| `Change` | `actions`, `before`, `after`, `after_unknown`, `before_sensitive`, `after_sensitive`, `replace_paths`, `raw` |
| `OutputChange` | `name`, `change`, `sensitive`, `raw` |

`Change` has seven properties, not methods: `is_noop`, `is_create`, `is_read`, `is_update`,
`is_delete`, `is_replace`, `is_forget`. They are mutually exclusive, and more reliable than
reading `actions`, which encodes a replacement as `("delete", "create")` and never as
`("replace",)`.

### State

| Class | Fields |
|---|---|
| `State` | `format_version`, `terraform_version`, `serial`, `lineage`, `outputs`, `resources`, `raw` |
| `StateResource` | `address`, `mode`, `type`, `name`, `provider_name`, `module_address`, `index`, `schema_version`, `values`, `sensitive_values`, `dependencies`, `raw` |
| `OutputValue` | `value`, `type`, `sensitive`, `raw` |

`values` holds the provider's attributes. Check `sensitive_values` before logging them.

### Diagnostics

One error or warning, from a `-json` stream or a `validate` report. Diagnostics are attached
to every result and to every `ExecutionError`.

| Class | Fields |
|---|---|
| `Diagnostic` | `severity`, `summary`, `detail`, `address`, `range`, `snippet`, `raw` |
| `Range` | `filename`, `start`, `end` (both `Pos`) |
| `Pos` | `line`, `column`, `byte` |
| `Snippet` | `context`, `code`, `start_line`, `highlight_start_offset`, `highlight_end_offset`, `values` |

`range` and `snippet` are `None` when the CLI could not tie the message to a source location:

```python
for diag in result.diagnostics:
    where = f"{diag.range.filename}:{diag.range.start.line}" if diag.range else "<no source>"
    print(f"{diag.severity.value} {where}: {diag.summary}")
```

### Summaries and version

| Class | Fields |
|---|---|
| `ChangeSummary` | `add`, `change`, `remove`, `import_`, `operation` |
| `ResourceProgress` | `address`, `action`, `id_key`, `id_value`, `elapsed_seconds`, `errored` |
| `VersionInfo` | `version`, `version_tuple`, `platform`, `provider_selections`, `outdated`, `raw` |
| `VersionTuple` | `major`, `minor`, `patch`, `prerelease`, `build` |

`import_` has the trailing underscore because `import` is a Python keyword. `ChangeSummary`
reprs as `+3 ~1 -0`. `VersionTuple` compares on major, minor and patch alone, so
`1.6.0-rc1 >= 1.6.0` holds and a version gate does not fail on a release candidate.

### Enums

| Enum | Members |
|---|---|
| `Level` | `trace`, `debug`, `info`, `warn`, `error` |
| `MessageType` | the 18 `type` values the CLI emits, `version` through `test_abort` |
| `DiagnosticSeverity` | `error`, `warning` |
| `ChangeOperation` (alias `Action`) | `no-op`, `create`, `read`, `update`, `delete`, `replace`, `forget` |
| `ResourceMode` | `managed`, `data` |

All five subclass `str`, so `event.level == "error"` works and `json.dumps` needs no
conversion. An unrecognized value from a newer CLI becomes a member holding that exact
string, logged once at DEBUG, instead of raising `ValueError`.

## Streaming

The blocking methods above collect every event and return them at the end. The `*_stream`
methods return a live `Run` instead: an iterator of events that is also a context manager.

```python
from terrapy import Terrapy, ApplyProgressEvent, DiagnosticEvent

tf = Terrapy("infra/prod")

with tf.apply_stream(var={"replicas": 3}) as run:
    for event in run:
        if isinstance(event, ApplyProgressEvent):
            print(f"{event.address}: {event.elapsed_seconds:.0f}s elapsed")
        elif isinstance(event, DiagnosticEvent):
            print(event.diagnostic.severity, event.diagnostic.summary)
    result = run.result()

print(result.exit_code)
```

Available: `init_stream()`, `plan_stream()`, `apply_stream()`, `destroy_stream()`. They take
the same arguments as their blocking counterparts, without `on_event` (the caller consumes
the events), plus `timeout`.

`Run` members:

| Member | Behaviour |
|---|---|
| iteration | Yields `Event` objects as the child writes them. |
| `wait(timeout=None)` | Drains the rest of the run and returns the exit code. |
| `result()` | The `CommandResult`. Raises `UsageError` if the run has not finished. |
| `stderr_text()` | The buffered tail of stderr (last 1 MiB). |
| `cancel(grace=None)` | Graceful stop, then hard kill. See below. |
| `returncode`, `pid`, `args` | As on `subprocess.Popen`. |

Leaving the `with` block always cleans up: it cancels a run, joins the reader
threads and closes the pipes.

If you do not need the events themselves, the blocking methods take a callback instead:

```python
tf.apply(on_event=lambda ev: log.info("%s %s", ev.type, ev.message))
```

`EventRouter` turns that into per-type dispatch:

```python
from terrapy import EventRouter

router = EventRouter()
router.on("diagnostic", lambda ev: log.warning(ev.diagnostic.summary))
router.on("apply_complete", lambda ev: log.info("done: %s", ev.address))

tf.apply(on_event=router.dispatch)
```

Events cross from the reader thread on a bounded queue, so a slow handler slows the reader,
which slows the child. No events are dropped; a slow handler only makes the run take longer.

## Events

One line of `-json` output becomes one event. Every event carries the same envelope:

| Field | JSON key | Notes |
|---|---|---|
| `level` | `@level` | `Level` enum |
| `message` | `@message` | The line the CLI would have printed |
| `module` | `@module` | Usually `terraform.ui` |
| `timestamp` | `@timestamp` | RFC 3339, as sent |
| `type` | `type` | Picks the class below |
| `raw` | the whole line | Parsed JSON, untouched |

An unrecognized `type` becomes a base `Event` with everything in `.raw`, so output from a
newer CLI still parses.

### Lifecycle

| Class | `type` | Payload | Emitted |
|---|---|---|---|
| `VersionEvent` | `version` | `terraform_version`, `ui_version` | First line of every run |
| `LogEvent` | `log` | none | Progress with no structured form |
| `DiagnosticEvent` | `diagnostic` | `diagnostic` | On any error or warning |
| `ChangeSummaryEvent` | `change_summary` | `summary` | The add/change/remove tally. An apply emits two, one per phase, and terrapy keeps the last |
| `OutputsEvent` | `outputs` | `outputs` | End of a successful apply |

### Planning

| Class | `type` | Payload | Emitted |
|---|---|---|---|
| `PlannedChangeEvent` | `planned_change` | `resource`, `action`, `reason` | Once per resource the plan will touch |
| `ResourceDriftEvent` | `resource_drift` | above, plus `previous_run_action` | A resource changed outside Terraform |

`resource` is a `ResourceAddr`: `addr`, `module`, `resource`, `implied_provider`,
`resource_type`, `resource_name`, `resource_key`. Use those fields instead of parsing the
address string.

### Per-resource hooks

All six carry `address`, `resource`, `action`, `id_key`, `id_value` and `elapsed_seconds`.

| Class | `type` | Emitted |
|---|---|---|
| `ApplyStartEvent` | `apply_start` | Work began on this resource |
| `ApplyProgressEvent` | `apply_progress` | Still running, about every 10s |
| `ApplyCompleteEvent` | `apply_complete` | Done. `id_key`/`id_value` hold the assigned ID |
| `ApplyErroredEvent` | `apply_errored` | This resource failed. A `DiagnosticEvent` follows |
| `RefreshStartEvent` | `refresh_start` | Reading current state |
| `RefreshCompleteEvent` | `refresh_complete` | Refresh done |

`ProvisionStartEvent`, `ProvisionProgressEvent`, `ProvisionCompleteEvent` and
`ProvisionErroredEvent` add `provisioner` to the same set.

### Lines that are not events

| Class | `type` | Meaning |
|---|---|---|
| `MalformedLineEvent` | `""` | The line was not JSON. The text is in `.raw["text"]`. The CLI writes plain text in `-json` mode occasionally |
| `TruncatedLineEvent` | `truncated_line` | A line passed 10 MiB and was dropped. `result()` then raises `TerrapyError` |

Both subclass `LogEvent`, and neither reports `type="log"`. Match them with `isinstance`
rather than an `EventRouter` registration.

## Timeouts and cancellation

Two independent limits, settable on the client or per call:

- `timeout` is wall-clock for the whole invocation.
- `inactivity_timeout` is time since the last event. A provider stuck on a network call
  produces no events, so this catches hangs that a wall-clock limit would only catch much
  later.

Either one cancels the run and raises `TerrapyTimeoutError`, carrying the command, the limit
that fired, and whatever stdout, stderr and diagnostics had arrived.

`cancel()` sends the graceful interrupt that Terraform and OpenTofu handle themselves: SIGINT
on POSIX, `CTRL_BREAK_EVENT` on Windows. The child is started in its own process group
(POSIX) or console group (Windows), so the signal reaches the provider plugins instead of
orphaning them. If the child outlives `grace` seconds (default 30), the next `cancel()`, or
the cleanup on leaving the `with` block, escalates to a hard kill.

```python
with tf.apply_stream() as run:
    for event in run:
        if deadline_passed():
            run.cancel(grace=10)
```

Interrupting an apply while it is running leaves partially applied infrastructure, the same
as interrupting the CLI directly.

## Errors

### Exit codes

| Command | Treated as success | Meaning |
|---|---|---|
| `plan` | 0, 2 | 2 means changes are present, exposed as `has_changes` |
| `validate` | 0, 1 | 1 means it ran and found problems. The report is parsed, then `ValidationError` is raised |
| `fmt(check=True)` | 0, 3 | 3 means files need formatting, exposed as `ok` and `changed_files` |
| everything else | 0 | |
| `run_raw` | whatever you pass in `success_exit_codes` | |

Any other exit code raises.

### Exceptions

```
TerrapyError
├── BinaryNotFoundError        .searched
├── VersionDetectionError      .binary_path, .raw_output
├── UnsupportedFeatureError    .feature
├── UsageError
├── TerrapyTimeoutError        .command, .timeout, .stdout, .stderr, .diagnostics
└── ExecutionError             .command, .exit_code, .stdout, .stderr, .diagnostics
    ├── ValidationError        .result
    ├── LockError              .lock_id, .lock_info
    └── InitializationError
```

| Exception | Raised when |
|---|---|
| `BinaryNotFoundError` | The constructor found no `terraform` or `tofu` on `PATH`, or `binary_path` is not a file |
| `VersionDetectionError` | `version -json` failed, timed out, or returned something other than a JSON object |
| `UnsupportedFeatureError` | The CLI answered `flag provided but not defined: -x`, which usually means too old a binary |
| `UsageError` | Your call is wrong. See below |
| `TerrapyTimeoutError` | `timeout` or `inactivity_timeout` fired and the child was cancelled |
| `ExecutionError` | The exit code fell outside the success set for that command |
| `ValidationError` | `validate()` found errors. `.result` is the parsed report |
| `LockError` | The state lock was already held. `.lock_info` holds the `Who`, `Created` and `Operation` fields |
| `InitializationError` | `init` failed, or a command needed an initialized directory and did not find one |

The class is selected from stderr and the diagnostics, in this order:

1. `flag provided but not defined: -x` gives `UnsupportedFeatureError`, naming the flag.
2. A state lock message gives `LockError`, with the ID and lock fields parsed out.
3. `has not been initialized` or `Backend initialization required` gives
   `InitializationError`, whichever command produced it.
4. Everything else gives `ExecutionError`.

The last three extend `ExecutionError`, so a single `except ExecutionError` catches every
command failure, and more specific handlers above it still take precedence.

`UsageError` covers four cases: `apply()` with no plan file and `auto_approve=False`,
`output(name)` for a name that does not exist, `state.show(address)` for an address not in
state, and `Run.result()` before the run finished. Only the first is caught before a process
starts.

Recovering from a lock:

```python
from terrapy import LockError

try:
    tf.apply()
except LockError as exc:
    info = exc.lock_info or {}
    print(f"locked by {info.get('Who')} since {info.get('Created')}")
    if exc.lock_id and is_stale(exc):
        tf.force_unlock(exc.lock_id)
```

## Logging

terrapy logs under the `terrapy` logger (`terrapy.discovery` and `terrapy.runner` for the
submodules) and installs a `NullHandler`, so it stays silent until you configure logging:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("terrapy").setLevel(logging.DEBUG)
```

## Testing code that uses terrapy

The `runner` argument replaces the subprocess driver. A runner is anything satisfying this
protocol:

```python
from terrapy.config import RunSpec
from terrapy.runner import Runner      # a runtime-checkable Protocol

class Runner(Protocol):
    def start(self, spec: RunSpec) -> Run: ...
```

With a fake runner, no process is started:

```python
tf = Terrapy("infra", binary_path="/usr/bin/terraform", runner=fake_runner)
```

`tests/conftest.py` contains a usable `FakeRunner`. Register a predefined exit code, stdout and
stderr, or a list of JSONL lines to replay as events, keyed by argv prefix. It also records
every `RunSpec` it received, so a test can assert on the argv that was built. The whole unit
suite runs this way, with no Terraform installed.

`binary_path` still has to resolve at construction time: the client resolves the binary
before it looks at the runner.


## Development

```bash
pip install -e ".[dev]"

ruff check src tests
mypy src/terrapy
pytest                                      # unit tests only, no binary needed
TERRAPY_INTEGRATION=1 pytest tests/integration
```
