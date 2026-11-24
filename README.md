# Software Factory

**From a ticket to a reviewed commit.**

Run coding agents locally with tests, independent review and recovery after restarts.

[![Release](https://img.shields.io/github/v/release/bucholcm/software-factory)](https://github.com/bucholcm/software-factory/releases/latest)
[![Tests](https://github.com/bucholcm/software-factory/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/bucholcm/software-factory/actions/workflows/tests.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-22C55E)](LICENSE)

[![Factory workflow: a scoped Beads ticket becomes an isolated implementation, passes tests and independent review, and produces a reviewed branch. Changes return through the fix loop.](docs/images/factory-workflow.svg)](docs/operations.md#workflow-and-modes)

Start from the project open in your coding agent:

```text
Codex       $factory PROJECT-TICKET
OpenCode    /factory PROJECT-TICKET
```

[Set up your project →](docs/operations.md#run-and-inspect) · [Latest release](https://github.com/bucholcm/software-factory/releases/latest)

## Keep the work moving

| Resume after a restart | Verify the commit | See what is happening |
| --- | --- | --- |
| The supervisor adopts its existing worker and keeps completion receipts. | Tests and independent review belong to an exact revision. Fixes trigger fresh checks. | Follow stages, elapsed time, blockers and reported tokens in your terminal. |

## Follow delivery in Herdr

[![Illustrative Herdr layout showing coding agents, factory status and a delivery report side by side](docs/images/herdr-factory-status.png)](docs/images/herdr-factory-status.png)

*Layout illustration with synthetic data, not a native screenshot.*

Your coding session and factory status stay side by side. The supervisor runs
independently of the chat session. [Open the status panel →](docs/operations.md#see-it-in-herdr)

## Plan with Beads and Scotty

[![Actual Scotty screenshot showing a synthetic Beads dependency graph with parent-child and blocking relations](docs/images/scotty-dependency-graph.png)](docs/images/scotty-dependency-graph.png)

*Scotty showing a synthetic demo project with real Beads relations.*

Decisions block their dependent tasks. Independent scoped work can continue.
[Explore the workflow and runnable demo →](docs/beads-scotty-workflow.md)

## Start here

Python 3.11+, Git, authenticated Codex CLI, Beads and GitHub CLI are required.
Codex executes implementation jobs. OpenCode can submit tickets and run planning
reviews; its native implementation runtime remains disabled pending an enforced sandbox.

| Guide | What you will find |
| --- | --- |
| [Setup and operations](docs/operations.md) | Install the skill, configure a project and choose an execution mode. |
| [Environments and recovery](docs/reliability-loop.md) | Prepare dependencies and investigate or recover failed work. |
| [Tokens and costs](docs/usage-and-costs.md) | Inspect attempts, model provenance and optional price estimates. |
| [Security](SECURITY.md) | Trust boundaries, private data and supported reporting channels. |

This is a pilot for trusted local projects. Configured commands use your account's
permissions. Read the [security assessment](security_best_practices_report.md)
before publishing or deploying it.

Licensed under [MIT](LICENSE).
