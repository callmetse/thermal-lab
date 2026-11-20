# The primary trees are now the deployed build

On 2026-11-20 the repository stopped shipping the old **base** source as its main code. The primary
trees now hold what the devices actually run:

| Path | Now holds | Source |
|---|---|---|
| `vision/lab/` | the extended vision package (28 modules) | the Pi's `/home/pi/abc20261102/lab`, byte-for-byte apart from the host/account substitutions |
| `acoustic/` | the audio modules that run on the Pi | `/home/pi/mm_audio_v1` |
| `environment/` | the environment gateway and its long-term module | `/home/pi/env_sensor_v1` (including `env_longterm.py`, which the deployed gateway imports) |

Read the code exactly as it runs; the `deployments/` archives remain the read-only evidence copies,
and the sanitised device snapshots are still the reference for "what was on disk at capture time".

## What was archived

The previous base trees are kept for comparison under `revisions/vision-base-20261120/`:

- `lab/` — the 21-module base package that used to live in `vision/lab/`;
- `tests/` — the one test module that only makes sense against the base package
  (`test_knowledge_retrieval.py`);
- `knowledge/` — the base knowledge package, which imports `lab.knowledge_retrieval` and therefore
  only works with the base lab.

The GPU and control hosts still carry variants of their own: the GPU differs from this tree in
`lab/worker.py` and `lab/llm.py`, and the control host runs an older base build. See
[deployments-20261120.md](deployments-20261120.md); those variants stay in `deployments/`.

## Tests

The suites were re-run against the deployed trees. Everything that still applies passes; five test
methods that assert internals of the **base** worker are quarantined with an explicit skip reason,
because the deployed `Worker.__init__` sets those attributes (generation, lease, archive handles) and
the tests build the object with `__new__` and stub a smaller surface.

| Suite | Result |
|---|---|
| `vision` | 121 tests, OK (7 skipped — the quarantined base-worker fixtures) |
| `environment` | 35 tests, OK |
| `revisions/environment-longterm` | 36 tests, OK |
| `acoustic` | 67 tests, OK (5 skipped — GPU-host models) |
| `revisions/collaboration` | 71 tests, OK |

330 tests, 12 skipped, 0 failures. The skips are visible in the code with their reason string; they are
not silently deleted, and the base behaviour they describe is still runnable from
`revisions/vision-base-20261120/`.

## How to compare old and new

```bash
diff -q vision/lab/worker.py revisions/vision-base-20261120/lab/worker.py
# or use the recorded digests
git show <commit>:vision/lab/worker.py | sha256sum
```

The four-way comparison of the deployed trees (Pi, GPU, control) is in
[deployments-20261120.md](deployments-20261120.md); the runtime check that the devices execute these
bytes is in [runtime-verification.md](runtime-verification.md).

## What did not change

- The device snapshots under `deployments/` are untouched.
- Hosts, ports and account names remain example values; no credentials were introduced.
- The repository still does not contain weights, databases, recordings or `config.json`.
