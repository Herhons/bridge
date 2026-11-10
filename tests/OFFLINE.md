# Offline unittest isolation

Run the ordinary command from this WIP checkout:

```text
py -3.10 -X utf8 -B -m unittest discover -s tests -v
```

Every test module bootstraps `offline_guard` before importing product modules.
No external environment wrapper is required, including single-file discovery.
The guard also travels through PYTHONPATH to Python child processes. Setup
failure exits 87; unexpected blocked operations are recorded even if application
code catches the exception, and cause exit 86 at interpreter shutdown.

Default logs, failure PNG/JSON, tier receipts, UI locks, progress and synthetic
input markers go under `_selftest/<run>`. TEMP/TMP are restricted to `_tmp`.
Python file mutations elsewhere are rejected. The original checkout and the WIP
source/configuration are read-only during tests. `Screen.grab` yields a 2x2
synthetic image; native capture, UIA roots, input, clipboard, window mutation and
shell launches are blocked. Fake screens/OS APIs in individual tests still run
the real surrounding product code. Only loopback ports bound by this process
can be contacted; this permits fake HTTP servers and prevents notifying a real
local service. Python children require the inherited guard. Node is used solely
for the reviewed deterministic JS harnesses. taskkill can target only a tracked,
still-running test child, so timeout/lock lifetime tests remain real. icacls is
limited to temporary test files with the exact current-user ACL argument shape.
The two unavailable-service CLI fixtures model ConnectionRefusedError directly;
they no longer probe an unowned, deliberately closed local port.

`guard_manifest_<pid>.json` records the effective roots and protection mode.
Intentional negative cases use expected_denials logs, never permission to perform
the operation. The swallowed-exception regression launches a separate
`*-expected-fail-child` run which must contain one outside_write event and exit
86; it is retained as positive evidence that a swallowed denial cannot pass.
Normal parent/child runs require zero unexpected guard events. This is a test
fixture fence against accidental side effects, not a security sandbox against
hostile Python/native code. It is not enabled for normal production CLI runs.
No real UI end-to-end success is inferred from synthetic screenshots or mocks.
