# Agent Orchestrator

A multi-model pipeline that automates feature development by coordinating different AI models for implementation, refactoring, and code review.

## How It Works

The orchestrator runs a 6-phase pipeline:

```
Task Description
      |
      v
 [Phase 1] Opus 4.6 decomposes the task into a TODO.md plan
      |
      v
 [Phase 2] Opus 4.6 implements the feature (full context)
      |
      v
 [Phase 3] Codex 5.3 refactors the code (zero context, fresh eyes)
      |
      v
 [Phase 4] Creates a new branch, commits, pushes, and opens a PR
      |
      v
 [Phase 5] Fresh Opus 4.6 session reviews the PR and runs tests
      |
      v
 [Phase 6] You review the results and decide: merge / skip / abort
```

**Why multiple models?**

- **Opus 4.6** handles complex reasoning: task planning, feature implementation, and code review.
- **Codex 5.3** is spawned cold with no prior context, so it refactors with completely fresh eyes - no anchoring to implementation decisions.
- The **review phase** uses a new Opus session (no carryover from implementation) so it acts as an independent reviewer.

## Prerequisites

- Python 3.10+
- [OpenCode](https://opencode.ai) (`opencode` command available in PATH, with API keys configured for your providers)
- [GitHub CLI](https://cli.github.com/) (`gh` command, authenticated)
- Git

## Installation

```bash
git clone https://github.com/colmjr/agent_orchestrator.git
cd agent_orchestrator
pip install -r requirements.txt
```

## Usage

```bash
# Basic usage - run from your project directory
python orchestrator.py "Add user authentication with JWT tokens" --workdir /path/to/your/project

# Short flags
python orchestrator.py "Fix the pagination bug in /api/users" -w ./my-project

# With a custom config file
python orchestrator.py "Refactor the database layer" -w ./my-project -c ./my-config.yaml
```

### Arguments

| Argument | Flag | Default | Description |
|----------|------|---------|-------------|
| `task` | (positional) | required | Description of the task to implement |
| `--workdir` | `-w` | `.` | Path to the project you want to work on |
| `--config` | `-c` | built-in `config.yaml` | Path to a custom configuration file |

## Configuration

Edit `config.yaml` to customize the pipeline:

```yaml
# Model assignments per phase (provider/model format for OpenCode)
models:
  feature: "anthropic/claude-opus-4-6"    # Task decomposition + implementation
  refactor: "openai/codex-5.3"            # Cold refactor
  review: "anthropic/claude-opus-4-6"     # PR review

# Branch naming (produces: feat/your-task-slug-0227)
branching:
  prefix: "feat"
  separator: "/"

# PR settings
pr:
  draft: false
  base_branch: "main"

# Notification settings
notifications:
  terminal_bell: true
  stdout: true

# Test commands - set this per project
tests:
  command: "npm test"
  coverage: true

# Orchestrator behavior
orchestrator:
  todo_file: "TODO.md"
  max_retries: 2
  require_tests_pass: true
```

### Per-project configuration

You can keep a custom config alongside each project:

```bash
# In your project repo
cp /path/to/agent_orchestrator/config.yaml .orchestrator.yaml

# Edit test command, base branch, etc.
# Then run with:
python /path/to/orchestrator.py "your task" -w . -c .orchestrator.yaml
```

## Pipeline Phases in Detail

### Phase 1 - Task Decomposition
Opus breaks down your task description into an actionable `TODO.md` with checkboxes. This becomes the implementation plan.

### Phase 2 - Feature Implementation
Opus receives the full task context and the TODO plan, then implements each subtask. It checks off items in `TODO.md` as it goes.

### Phase 3 - Cold Refactor
Codex is spawned with **zero prior context**. It only sees the current state of the files and a generic refactor prompt. This ensures unbiased refactoring with no knowledge of implementation decisions or intent.

### Phase 4 - Ship
Automatically:
- Creates a new branch (`feat/your-task-slug-0227`)
- Stages and commits all changes
- Pushes to origin
- Creates a pull request via `gh`

### Phase 5 - PR Review
A **fresh** Opus session (no carryover from Phase 2) reviews the PR:
- Reads the PR diff
- Runs the test suite
- Checks test parity (existing tests still pass, new tests exist)
- Outputs a clear recommendation: APPROVE or REQUEST_CHANGES

### Phase 6 - Human Approval
You get a terminal notification and three options:
- `[m]` Merge the PR
- `[s]` Skip (leave PR open for manual review)
- `[a]` Abort (close the PR)

## Example

```bash
$ python orchestrator.py "Add rate limiting middleware to the Express API" -w ~/projects/my-api

============================================================
  AGENT ORCHESTRATOR
  Task: Add rate limiting middleware to the Express API
  Working dir: /Users/you/projects/my-api
  Models: anthropic/claude-opus-4-6 / openai/codex-5.3
============================================================

────────────────────────────────────────────────────────────
PHASE 1: Task Decomposition
────────────────────────────────────────────────────────────
[AGENT] Running anthropic/claude-opus-4-6...
[PHASE 1] TODO.md created

────────────────────────────────────────────────────────────
PHASE 2: Feature Implementation (Opus 4.6)
────────────────────────────────────────────────────────────
[AGENT] Running anthropic/claude-opus-4-6...
[PHASE 2] Feature implementation complete.

────────────────────────────────────────────────────────────
PHASE 3: Cold Refactor (Codex 5.3 - Zero Context)
────────────────────────────────────────────────────────────
[AGENT] Running openai/codex-5.3...
[PHASE 3] Cold refactor complete.

────────────────────────────────────────────────────────────
PHASE 4: Ship (Branch + Commit + PR)
────────────────────────────────────────────────────────────
[SHIP] Creating branch: feat/add-rate-limiting-middleware-0227
[SHIP] Committed: feat: Add rate limiting middleware to the Express API
[SHIP] Pushed to origin/feat/add-rate-limiting-middleware-0227
[SHIP] PR created: https://github.com/you/my-api/pull/42

────────────────────────────────────────────────────────────
PHASE 5: PR Review (Fresh Opus 4.6 Session)
────────────────────────────────────────────────────────────
[AGENT] Running anthropic/claude-opus-4-6...

============================================================
PR REVIEW RESULTS
============================================================
...review output...

────────────────────────────────────────────────────────────
PHASE 6: Human Approval
────────────────────────────────────────────────────────────

============================================================
  NOTIFICATION: PR ready for your review: https://github.com/you/my-api/pull/42
============================================================

PR URL: https://github.com/you/my-api/pull/42

Options:
  [m] Merge the PR
  [s] Skip (leave PR open)
  [a] Abort (close PR)

Your choice (m/s/a):
```

## License

MIT
