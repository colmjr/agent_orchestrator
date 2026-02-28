# Agent Orchestrator

A multi-model pipeline that automates feature development by coordinating different AI models across planning, implementation, refactoring, and code review — all inside an interactive TUI.

## How It Works

The orchestrator runs a 7-phase pipeline:

```
Task Description
      |
      v
 [Phase 0] Clarifying questions — LLM asks 2-4 questions, you answer or skip
      |
      v
 [Phase 1] Planning — generates plan, quality checks, approval gate, then TODO.md
      |
      v
 [Phase 2] Implementation — builds the feature with full context
      |
      v
 [Phase 3] Cold refactor — zero-context session for unbiased cleanup
      |
      v
 [Phase 4] Ship — branch, commit, push, open PR (or local commit)
      |
      v
 [Phase 5] Review — fresh session reviews diff and runs tests
      |
      v
 [Phase 6] Human approval — interactive decision menu
```

**Why multiple models?**

- Each phase can use a different model (configurable per-phase in `config.yaml`).
- The **refactor phase** resets the session so the model sees the code with completely fresh eyes.
- The **review phase** also uses a fresh session so it acts as an independent reviewer.
- When no git remote exists, the entire pipeline works **locally** — branches, commits, diffs, and merges all happen without pushing.

## Prerequisites

- Python 3.10+
- [OpenCode](https://opencode.ai) (`opencode` command available in PATH, with API keys configured for your providers)
- [GitHub CLI](https://cli.github.com/) (`gh` command, authenticated) — optional if working in local mode
- Git

## Installation

```bash
git clone https://github.com/colmjr/agent_orchestrator.git
cd agent_orchestrator
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install -e . --no-build-isolation
```

This installs the `agent-orchestrator` command in your venv.

## Usage

```bash
# Launch the interactive TUI (prompts for task and workdir)
agent-orchestrator

# Provide task upfront
agent-orchestrator "Add user authentication with JWT tokens" --workdir /path/to/project

# Headless mode (no TUI, requires task argument)
agent-orchestrator "Fix the pagination bug" -w ./my-project --no-tui

# Custom config file
agent-orchestrator "Refactor the database layer" -w ./my-project -c ./my-config.yaml
```

### Arguments

| Argument | Flag | Default | Description |
|----------|------|---------|-------------|
| `task` | (positional) | — | Task description. Optional in TUI mode (prompted interactively); required with `--no-tui`. |
| `--workdir` | `-w` | `.` | Path to the project to work on |
| `--config` | `-c` | built-in `config.yaml` | Path to a custom configuration file |
| `--no-tui` | — | `false` | Run in headless mode (no interactive TUI) |

## TUI

The default mode launches a full interactive terminal UI built with [Textual](https://textual.textualize.io/).

### Layout

- **Phase header** — current phase, status indicator, task summary, active models
- **Output pane** — streaming agent output with Rich formatting
- **Sidebar** (right) — working directory, git branch, input mode, token usage, cost estimate, TODO.md progress, modified files
- **Message input** (bottom) — multi-line text area for prompts and commands

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Enter` | Submit message |
| `Ctrl+J` | Insert newline (multi-line input) |
| `Escape` | Interrupt running agent |
| `Ctrl+C` | Quit |
| `PageUp` / `PageDown` | Scroll output pane |
| `Ctrl+Up` / `Ctrl+Down` | Scroll output 3 lines |

### Slash Commands

Type `/` in the input box to see a fuzzy-filtered popup menu.

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/config` | Open full-screen YAML config editor |
| `/configure` | Describe config changes in plain English (AI-powered) |
| `/theme` | Set or view theme preset |
| `/vim` | Toggle vim keybindings |
| `/status` | Show pipeline status, session, and models |
| `/task` | Show current task description |
| `/workdir` | Show working directory |
| `/skip` | Skip the current phase or question |
| `/exit` | Quit the application |

### Decision Menus

Interactive option lists appear at key decision points. Navigate with arrow keys (or `j`/`k` in vim mode), confirm with Enter, cancel with Escape.

**Plan approval (Phase 1):**
- Approve (Keep Context)
- Approve (Clear Context)
- Revise Plan
- Add More Context
- Skip

**Merge approval (Phase 6):**
- Merge (Keep Context)
- Merge (Clear Context)
- Add Change Before Merge — describe a change, the agent applies it, re-runs review, then returns to this menu
- Skip
- Abort

### Vim Mode

Enable with `/vim` or set `editor.vim_mode: true` in config. Persisted between sessions.

Starts in INSERT mode for comfortable typing. Press `Escape` for NORMAL mode.

**Supported in both the message input and config editor:**

| Category | Keys |
|----------|------|
| Mode | `i`, `a`, `A`, `I`, `o`, `O` (enter insert), `Escape` (normal), `v` (visual), `V` (visual line) |
| Movement | `h`/`j`/`k`/`l`, `w`/`b`/`e`, `0`/`$`, `gg`/`G`, `f{char}`/`t{char}` |
| Editing | `x`, `dd`, `dw`, `d$`/`D`, `cw`, `c$`/`C`, `ci"`/`di(`, `J` (join), `>>` / `<<` (indent) |
| Search | `/pattern`, `?pattern`, `n`/`N` |
| Visual | `v`/`V` then `d` (cut) or `y` (yank) |
| Other | `u` (undo), `Ctrl+R` (redo), `p` (paste), `.` (repeat), counts (`3j`, `5dd`) |
| Scroll | `Ctrl+D`/`Ctrl+U` (half-page), `Ctrl+F`/`Ctrl+B` (full page) |
| Command | `:w`, `:q`, `:wq`, `:%s/old/new/g` (config editor) |

## Configuration

Edit `config.yaml` directly, use `/config` in the TUI, or use `/configure` to describe changes in plain English.

```yaml
# Model assignments — each phase can use a different provider/model
models:
  feature: "anthropic/claude-opus-4-6"       # Default fallback for all phases
  clarify: "anthropic/claude-opus-4-6"       # Phase 0: clarifying questions
  planning: "anthropic/claude-opus-4-6"      # Phase 1: plan generation
  implement: "anthropic/claude-opus-4-6"     # Phase 2: feature implementation
  configure: "anthropic/claude-opus-4-6"     # /configure AI config editing
  refactor: "openai/codex-5.3"              # Phase 3: cold refactor
  review: "anthropic/claude-opus-4-6"        # Phase 5: PR review

# Branch naming (produces: feat/your-task-slug-0227)
branching:
  prefix: "feat"
  separator: "/"

# PR settings
pr:
  draft: false
  base_branch: "main"    # Falls back to current branch, then main/master

# Test commands — set per project
tests:
  command: "npm test"
  coverage: true

# Orchestrator behavior
orchestrator:
  todo_file: "TODO.md"
  max_retries: 2
  require_tests_pass: true
  plan_approval: true                  # Show plan approval gate in Phase 1
  plan_quality_check: true             # Validate plans for quality signals
  plan_quality_mode: "balanced"        # "strict" or "balanced"
  plan_quality_retries: 2              # Retry count for low-quality plans
  auto_clear_context_on_bad_plan: true
  plan_offtopic_keywords:              # Reject plans containing these
    - "essay"
    - "poem"
    - "trivia"

# Notifications
notifications:
  terminal_bell: true
  stdout: true

# Editor settings
editor:
  vim_mode: true          # Enable vim keybindings (persisted)

# UI settings
ui:
  input_height: 8         # Message input height in lines (3-15)

# Theme — preset + optional color overrides
theme:
  preset: "nord"          # default, nord, gruvbox, dracula, solarized, monokai, tokyo-night, catppuccin
  # Override individual colors (hex values):
  # accent: "#88C0D0"
  # success: "#A3BE8C"
  # error: "#BF616A"
  # warning: "#EBCB8B"
  # info: "#81A1C1"
  # muted: "#4C566A"
```

### Per-project configuration

```bash
cp /path/to/agent_orchestrator/agent_orchestrator/config.yaml .orchestrator.yaml
# Edit for your project, then:
agent-orchestrator "your task" -w . -c .orchestrator.yaml
```

## Pipeline Phases in Detail

### Phase 0 — Clarifying Questions
The LLM generates 2-4 targeted questions about your task. You can answer them, skip individual questions, or provide freeform context. This enriches the task description before planning begins.

### Phase 1 — Planning & Decomposition
Generates an implementation plan, validates it against quality signals (off-topic keywords, actionable steps, task-term overlap), and presents it for approval via the decision menu. After approval, converts the plan into a `TODO.md` with checkboxes.

### Phase 2 — Feature Implementation
Receives the full task context and TODO plan, then implements each subtask. Checks off items in `TODO.md` as it goes.

### Phase 3 — Cold Refactor
A **fresh session** (zero prior context) sees only the current file state and a generic refactor prompt. This ensures unbiased cleanup with no knowledge of implementation decisions.

### Phase 4 — Ship
- Creates a new branch (`feat/your-task-slug-0227`)
- Stages and commits all changes
- **Remote mode:** pushes to origin, creates a PR via `gh`
- **Local mode:** commits locally (no push, no PR)

### Phase 5 — PR Review
A **fresh session** reviews the changes:
- Reads the PR diff (or local `git diff` in local mode)
- Runs the test suite
- Checks test parity
- Outputs a recommendation: APPROVE or REQUEST_CHANGES

### Phase 6 — Human Approval
An interactive decision menu with options to merge, skip, abort, or request additional changes before merging. In local mode, merge happens locally via `git merge`.

## Local Mode

When no `origin` remote is detected, the orchestrator prompts you to add one or continue locally. In local mode:

- Phase 4 creates a branch and commits without pushing
- Phase 5 reviews against a local diff (`git diff base...HEAD`)
- Phase 6 offers local merge/skip/delete-branch

Base branch resolution: configured branch > current branch > `main` > `master`.

## License

MIT
