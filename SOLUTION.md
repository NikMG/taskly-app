# Solution: unit test agent and PR workflow

A custom Claude Code agent with six reusable skills writes unit tests for this repository, in two ways:

- **Backfill:** it covered the untested code. Backend coverage went from 42% to 100%. Frontend line coverage went from 47.68% to 100%, and branch coverage from 43.79% to 97.81%.
- **Pull requests:** each pull request is analysed and tests are created or updated when needed. The tests are verified independently, then proposed in a separate pull request that a person reviews. A pull request that changes the agent's own files skips the agent and runs the agent's test suite instead (`agent-tooling.yml`).

Claude Code was used while developing this solution, as well as being the agent's runtime.

## Where to look

| What | Where |
|---|---|
| Agent definition | `.claude/agents/pr-test-agent.md` |
| Skills | `.claude/skills/*/SKILL.md` |
| PR workflow | `.github/workflows/test-agent.yml` |
| Agent permissions | `.github/agent/agent-settings.json` |
| Choosing what runs, before any LLM cost | `.github/agent/scope_changes.py` |
| Test sandbox | `.github/agent/run_tests.sh`, `sandbox.Dockerfile` |
| Checks that do not trust the agent | `.github/agent/check_patch.py`, `verify_patch.py`, `check_backfill.py` |
| PR comment and bot PR | `.github/agent/render_report.py`, `publish_results.py` |
| Local runs | `.github/agent/backfill_agent.sh`, `smoke_agent.sh` |

## How the agent and skills are used

`run_agent.sh` is the only way to start the agent, in CI and locally. It runs `claude -p --agent pr-test-agent` with a fixed tool list, a private config directory and no MCP servers. The agent can run only `.github/agent/run_tests.sh`, in exact forms that a hook enforces.

| Skill | Purpose |
|---|---|
| `analyze-test-impact` | PR mode: per-file decision on whether tests are needed, and which behaviours to cover |
| `find-coverage-gaps` | Backfill mode: list every coverage gap and rank the work by risk |
| `write-backend-tests` | pytest conventions, fixtures and patterns for each layer |
| `write-frontend-tests` | Vitest and Testing Library conventions and patterns |
| `run-sandboxed-tests` | Allowed commands, reading results, classifying a failure, the retry limit |
| `report-test-results` | The structured report and its honesty rules |

The report lists which skills ran from the agent's log, not from what the agent claims.

## The PR workflow

| Job | Permissions and secrets | What it does |
|---|---|---|
| `scope` | Read only | Decides whether to run: skips forks, drafts, bots, control-plane changes, oversized diffs and PRs with nothing testable. No LLM cost when skipped |
| `prepare` | Read only | Builds the sandbox from the base branch and installs dependencies |
| `generate` | Read only, API key | Runs the agent with the agent and skills taken from the base branch. Produces a patch and a report |
| `verify` | Read only | Re-runs everything without the agent: baseline, patched run, repeat runs for flakiness, diff coverage, formatting and types |
| `publish` | Write, no API key | Never runs PR code. Pushes `test-agent/pr-<N>`, opens or updates the bot PR, posts one PR comment and a check |
| `cleanup` | Write | When the source PR closes, closes the bot PR and deletes its branch |

### Prerequisites

1. Add the secret: `gh secret set ANTHROPIC_API_KEY`. Use a dedicated key with a spending limit.
2. In Settings → Actions → General, enable "Allow GitHub Actions to create and approve pull requests".
3. Open a pull request from a branch in this repository. Nothing else is needed.

CI on a bot PR waits for "Approve and run", because GitHub does not start workflows automatically for a `GITHUB_TOKEN` author. To opt a PR out, add the `skip-test-agent` label.

Local backfill needs Docker, the Claude Code CLI and Python 3: `.github/agent/backfill_agent.sh backend` or `frontend`. It asks for the key with hidden input, works on a fresh clone, and leaves a patch for review. `.github/agent/smoke_agent.sh boundaries` proves the permission rules.

## Main decisions

**Tests go to a bot PR, not to the developer's branch.** An agent should not change a branch someone is working on, and a person should approve its tests.

**The API key and PR code never meet.** Tests run in Docker with no network, no environment, a non-root user, read-only code and memory and process limits. The key exists in one job and reaches only the Claude process.

**`pull_request`, never `pull_request_target`.** The agent, the skills and the checking scripts come from the base branch, so a PR cannot rewrite its own rules. Actions are pinned by SHA, and every job has minimal permissions.

**Code, comments and test output are data, not instructions.** The agent never sees PR titles or descriptions. A test PR with an embedded instruction to read `.env` and edit production code ([PR #4](https://github.com/NikMG/taskly-app/pull/4)) got a real test, no production change, and a note about the attempt in the report. Permissions and checks are what enforce this, because a model can still be persuaded.

**The numbers are not the agent's.** `verify` measures them separately, and the comment labels the agent's own text as "not independently verified". `check_patch.py` rejects anything outside test paths, disabled tests and, on a best-effort basis, weakened assertions.

**A failed check is not always a red PR.** A rejected patch or an uncertain decision gives a neutral "needs human review". Only a broken pipeline fails.

**Publishing survives GitHub outages.** A real 502 during testing left a verified patch with no bot PR. `publish` retries server errors and detects a pull request created by a request that reported failure, and re-running only that job recovers without calling the agent again.

**The script decides when a backfill is finished, not the model.** `check_backfill.py` compares the agent's report with a coverage run it did not control, and `backfill_agent.sh` repeats up to 3 passes until every gap has a test or a stated reason.

**Thresholds leave room.** CI requires 90% on the backend, and 85% lines and 80% branches on the frontend. At 100%, every new feature would fail CI before the agent could propose its tests.

## Example run: adding a feature

[PR #8](https://github.com/NikMG/taskly-app/pull/8) adds `GET /api/todos/stats` and a statistics block, with no tests of its own.

### 1. The feature: [run](https://github.com/NikMG/taskly-app/actions/runs/41742194428), [bot PR #9](https://github.com/NikMG/taskly-app/pull/9)

**Decisions:** tests for the stats calculation, the route, the API call, the query hook, the stats refresh in each mutation and the component. No tests for the plain schema or the constant key.

**Skills:** `analyze-test-impact`, `run-sandboxed-tests`, `write-backend-tests`, `write-frontend-tests`, `report-test-results`.

| Measured by `verify` | Backend | Frontend |
|---|---|---|
| Tests | 56 → 60 | 80 → 87 |
| Diff coverage | 66.67% → 100% | 27.27% → 100% |
| Repeat runs | Passed | Passed |
| Formatting and types | n/a | Passed |

Cost $0.28, about 2 minutes, no denied tool calls. It also tested that `/stats` is not swallowed by `/{todo_id}`. Of 11 manual mutations, the tests caught 10.

### 2. Merging the bot PR

The next run skipped with `agent_commit`, without calling the LLM. There was no loop, and the report stayed in place.

### 3. A questionable logic change: [run](https://github.com/NikMG/taskly-app/actions/runs/41743979315)

Overdue tasks stopped counting as active, which contradicts how the rest of the app uses "active". The agent refused to change the test and returned `needs_human` with a suspected bug. That is the intended behaviour for a change it cannot confirm.

### 4. A clear change: [run](https://github.com/NikMG/taskly-app/actions/runs/41746759754), [bot PR #10](https://github.com/NikMG/taskly-app/pull/10)

A label was renamed. The workflow gives the agent `last_update.diff`, which showed that the code changed after the test. So it changed one line of the test and explained it: `Expectation changed: ... expected the first label 'Total' and now expects 'All tasks'`.

### 5. Documentation only: [run](https://github.com/NikMG/taskly-app/actions/runs/41748008473)

`no_tests_needed`, an empty patch and no new bot PR. Cost $0.095, 10 seconds.

### 6. Bot PR lifecycle

Temporary PRs covered the rest. A second push replaced the tests in the [same open bot PR](https://github.com/NikMG/taskly-app/pull/12), with no duplicate, and closing a source PR [closed its bot PR](https://github.com/NikMG/taskly-app/pull/14) with a comment and deleted the branch.

### Backfill results

| | Backend | Frontend |
|---|---|---|
| Tests | 21 → 56 | 18 → 77, plus 3 from review |
| Cost | $0.23 | $0.62 |
| Manual mutations caught | 26 of 27 | 37 of 43 |

## Assumptions

- Contributors with write access are trusted. The main threats are forks, prompt injection through code, and mistakes by the agent.
- Unit tests without a database are enough here. The sandbox has no Postgres, so the backend tests mock the session.
- A person reads the bot PR. The workflow is built around that review and does not replace it.
- GitHub-hosted runners with Docker are available, and the API spend per run is acceptable: $0.10 to $0.30 in the runs above.

## Limitations

- **Backfill has a completeness check; PR mode does not.** PR mode reports diff coverage and runs one pass.
- **Same-repository writers can edit the workflow.** The protection is against forks and injection. CODEOWNERS only helps once branch protection requires it.
- **Docker socket access.** The agent can reach the socket, which is root-equivalent on the runner. Only the hook and the allowlist limit it to `run_tests.sh`.
- **Local backfill was run on macOS only.** The Linux Bash sandbox used in CI would need the same socket and dependency setup as the workflow.
- **Fork PRs get no agent run**, because they cannot have the secret.
- **Fixed size limits.** PRs over 50 files or 2000 lines are skipped. Patches over 30 files or 1500 lines are rejected. Each run has a $3 budget and a 25-minute limit. These suit this repository, and each is a setting that can be tuned per project.
- **An older bot PR can outlive a failed run.** If a later run fails or proposes nothing, the previous bot PR stays open. Check the latest PR comment before merging it.
- **Verification compares failure counts, not test names.** A patch that fixes one failing test and breaks another would not be caught by this comparison.

## For production

- Use a GitHub App instead of `GITHUB_TOKEN`, so bot PRs run CI without manual approval.
- Enable branch protection with required checks and CODEOWNERS reviews.
- Automate the sensitivity check: run mutation testing on the agent's new tests, as was done by hand here, and report the score.
- Track cost, decisions and acceptance rates over time, and set spending alerts.
- Tune the size limits and budgets per repository.
- Add a completeness check to PR mode, and use `last_update.diff` to skip runs when only documentation changed.
- Use an ephemeral runner or a rootless Docker proxy instead of exposing the socket.