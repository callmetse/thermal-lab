# Thermal Lab · Supervisor review

Start with [PR #1 — Environment gateway](https://github.com/callmetse/thermal-lab/pull/1). The implementation is on the review branch. The default branch hosts this review guide while the pull request is open.

| Review item | Evidence |
|---|---|
| Install and run the gateway locally | [Gateway README and worked example](https://github.com/callmetse/thermal-lab/blob/review/environment-gateway/README.md) |
| Review or comment on individual lines | [PR #1: Files changed](https://github.com/callmetse/thermal-lab/pull/1/files) |
| Inspect validation and storage behavior | [Gateway regression tests](https://github.com/callmetse/thermal-lab/tree/review/environment-gateway/environment/tests) |

## System coverage

| Role | Source identified in the local inventory | Available in PR #1 |
|---|---|---|
| Raspberry Pi: environment gateway | HTTP service, event schema, SQLite store and Pi launch configuration | Python gateway and tests. Local acceptance: 35 tests, duplicate POST check and SQLite integrity check. Pi service configuration and a device-side run are pending. |
| Raspberry Pi: vision and audio | Edge vision worker, camera adapter and audio acquisition process | Pending source review and hardware validation. |
| GPU cloud | Cloud-role vision worker, audio inference worker and startup definitions | Pending source review and verification against the deployed version. |
| Control cloud | Node-RED flows, web console and broker configuration | Pending source review and verification against the deployed version. |
| ESP32 | Sensor firmware and HTTP publisher | Pending firmware review and end-to-end sensor validation. |

PR #1 verifies the gateway's handling of repeated sensor events. An audit of repeated AI development attempts would require task histories and token usage records.
