"""A tiny standalone child process used by test_runner.py to exercise the
real `SyncRunner` (streaming, cancellation, timeout, and deadlock avoidance)
without needing a real terraform/tofu binary."""

import json
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "normal"

if mode == "normal":
    for i in range(5):
        print(json.dumps({"@level": "info", "type": "log", "@message": f"line {i}"}))
        sys.stdout.flush()
    print(json.dumps({"type": "change_summary", "changes": {"add": 1, "change": 0, "remove": 0}}))
    sys.exit(0)
elif mode == "big_stderr":
    # Writes more than one pipe buffer's worth of stderr without ever
    # reading anything back; if the sync driver did not drain stderr
    # concurrently with stdout, this would deadlock.
    sys.stdout.write(json.dumps({"type": "log", "@message": "start"}) + "\n")
    sys.stdout.flush()
    sys.stderr.write("E" * (256 * 1024))
    sys.stderr.write("\n")
    sys.stderr.flush()
    print(json.dumps({"type": "log", "@message": "done"}))
    sys.exit(0)
elif mode == "hang":
    print(json.dumps({"type": "log", "@message": "before hang"}))
    sys.stdout.flush()
    while True:
        time.sleep(1)
elif mode == "exit_code":
    code = int(sys.argv[2])
    print(json.dumps({"type": "log", "@message": "about to exit"}))
    sys.exit(code)
elif mode == "malformed":
    sys.stdout.write("not json at all\n")
    sys.stdout.write(json.dumps({"type": "log", "@message": "recovered"}) + "\n")
    sys.exit(0)
elif mode == "bigline":
    # One JSON line far above a small max_line_bytes, to exercise truncation.
    print(json.dumps({"type": "log", "@message": "x" * 4096}))
    sys.exit(0)
elif mode == "no_trailing_newline":
    # No final newline: the assembler must still flush the buffered line.
    sys.stdout.write(json.dumps({"type": "log", "@message": "last line"}))
    sys.stdout.flush()
    sys.exit(0)
