# Contributing

This repository is a review snapshot of a research platform. Changes should stay reviewable:
one topic per branch, one commit per logical step, and the suites green before a pull request.

## Workflow

1. Branch from `main`: `git switch -c fix/<short-name>` or `feat/<short-name>`.
2. Make the change. If it touches a component, run that component's suite (see below).
3. Commit with an English subject line and a prefix: `feat:`, `fix:`, `docs:`, `test:`.
   Say what changed and why; do not mix formatting churn with behaviour changes.
4. Open a pull request against `main` and describe the evidence: which command you ran and what
   it returned.

## Running the suites

```bash
python -m pip install -r requirements-dev.txt

python -m unittest discover -s environment/tests -v
python -m unittest discover -s revisions/environment-longterm/tests -v
(cd acoustic && python -m unittest discover -s tests -v)
cp vision/config.example.json vision/config.json
(cd vision && python -m unittest discover -s tests -v)
(cd revisions/collaboration && PYTHONPATH=../../vision python -m unittest discover -s tests -v)
```

All five suites must pass. The five skipped audio tests need Sherpa-ONNX models and are expected
to skip on a workstation.

## What belongs in the tree

- Source, tests, configuration templates and documentation.
- **Not** model weights, databases, recordings, `config.json`, `secrets.yaml` or key material.
  They are gitignored; keep them that way.
- Host addresses in committed files are examples (`192.0.2.x`, `gpu.example.invalid`). Do not add
  real addresses, account names or credentials.

## Recording evidence

New deployment facts belong in [`docs/system-verification.md`](docs/system-verification.md): which
host, which revision, which command, and the raw result. A claim that a device runs this code
needs the revision on the device, not only a passing local suite.

## Documentation language

`README.md`, `docs/` and commit messages are English; `README.zh-CN.md` keeps the Chinese entry
point for the project supervisor. Keep both in step when the structure changes.
