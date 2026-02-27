#!/usr/bin/env python3
"""
Agent Orchestrator - Multi-model pipeline for feature development.

Uses OpenCode CLI (https://opencode.ai) to orchestrate across providers.

Pipeline:
  0. Clarifying questions (optional, interactive)
  1. Task decomposition → TODO.md
  2. Opus 4.6: Feature implementation (full context)
  3. Codex 5.3: Cold refactor (zero context, fresh eyes)
  4. Ship: checkout branch, commit, create PR
  5. Opus 4.6: Fresh session - PR review + test parity
  6. Human: final merge approval
"""

import subprocess
import sys
import os
import json
import yaml
import argparse
import re
import threading
from pathlib import Path
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console()

# ─── Configuration ───────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_CONFIG = SCRIPT_DIR / "config.yaml"


def load_config(config_path: str | None = None) -> dict:
    path = Path(config_path) if config_path else DEFAULT_CONFIG
    if not path.exists():
        console.print(_theme.s("error", f"Config not found: {path}", bold=True))
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


# ─── Theme ───────────────────────────────────────────────────────────────────

DEFAULT_THEME = {
    "accent": "blue",
    "success": "green",
    "error": "red",
    "warning": "yellow",
    "info": "cyan",
    "muted": "dim",
}

THEME_PRESETS = {
    "default": DEFAULT_THEME,
    "nord": {
        "accent": "#88C0D0",
        "success": "#A3BE8C",
        "error": "#BF616A",
        "warning": "#EBCB8B",
        "info": "#81A1C1",
        "muted": "#4C566A",
    },
}


class Theme:
    """Resolves theme colors from config with fallback defaults."""

    def __init__(self, config: dict) -> None:
        theme_cfg = config.get("theme", {}) or {}
        preset_name = str(theme_cfg.get("preset", "default")).lower()
        preset = THEME_PRESETS.get(preset_name, DEFAULT_THEME)
        merged = {**DEFAULT_THEME, **preset, **theme_cfg}

        self.preset = preset_name
        self.accent = merged.get("accent", DEFAULT_THEME["accent"])
        self.success = merged.get("success", DEFAULT_THEME["success"])
        self.error = merged.get("error", DEFAULT_THEME["error"])
        self.warning = merged.get("warning", DEFAULT_THEME["warning"])
        self.info = merged.get("info", DEFAULT_THEME["info"])
        self.muted = merged.get("muted", DEFAULT_THEME["muted"])

    def s(self, role: str, text: str, bold: bool = False) -> str:
        """Style text with a theme role. Returns rich markup string."""
        color = getattr(self, role, "white")
        b = " bold" if bold else ""
        return f"[{color}{b}]{text}[/{color}{b}]"


# ─── Utilities ───────────────────────────────────────────────────────────────


# Module-level theme, set when pipeline starts
_theme = Theme({})


def notify(message: str, config: dict):
    """Send notification via terminal bell + stdout."""
    if config["notifications"].get("terminal_bell"):
        console.print("\a", end="")
    if config["notifications"].get("stdout"):
        console.print()
        console.print(
            Panel(
                f"[bold]{message}[/bold]",
                title="NOTIFICATION",
                border_style=_theme.warning,
            )
        )
        console.print()


def slugify(text: str) -> str:
    """Convert task description to branch-safe slug."""
    slug = re.sub(r"[^a-zA-Z0-9\s-]", "", text.lower())
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    return slug[:50]


def _format_agent_event(event: dict) -> None:
    """Pretty-print a single JSON streaming event from opencode."""
    part = event.get("part", {})
    event_type = part.get("type", "")

    if event_type == "step-start":
        console.print(_theme.s("muted", "---"))

    elif event_type == "tool":
        tool_name = part.get("tool", "unknown")
        state = part.get("state", {})
        title = state.get("title", "")
        status = state.get("status", "")

        if title:
            console.print(
                f"  {_theme.s('info', tool_name, bold=True)} {_theme.s('muted', title)}"
            )
        else:
            tool_input = state.get("input", {})
            input_summary = ""
            if isinstance(tool_input, dict):
                for key in ("filePath", "command", "pattern", "content"):
                    if key in tool_input:
                        val = str(tool_input[key])
                        input_summary = val[:80] + ("..." if len(val) > 80 else "")
                        break
            console.print(
                f"  {_theme.s('info', tool_name, bold=True)} {_theme.s('muted', input_summary)}"
            )

        if status == "error":
            error = state.get("error", "unknown error")
            console.print(f"  {_theme.s('error', error)}")

    elif event_type == "text":
        text = part.get("text", "")
        if text.strip():
            console.print(text)

    elif event_type == "step-finish":
        tokens = part.get("tokens", {})
        total = tokens.get("total", 0)
        output = tokens.get("output", 0)
        if total:
            console.print(
                _theme.s("muted", f"  tokens: {total:,} total, {output:,} output")
            )


def run_agent(
    prompt: str, model: str, workdir: str, timeout: int = 600
) -> subprocess.CompletedProcess:
    """Run an OpenCode CLI session with real-time streaming output."""
    cmd = ["opencode", "run", "--format", "json", "--model", model, prompt]

    console.print(f"\n{_theme.s('muted', f'Running {model}...')}")
    prompt_preview = prompt[:100] + ("..." if len(prompt) > 100 else "")
    console.print(_theme.s("muted", f"Prompt: {prompt_preview}"))
    console.print(_theme.s("muted", f"Working dir: {workdir}") + "\n")

    process = subprocess.Popen(
        cmd,
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    text_parts: list[str] = []
    stderr_lines: list[str] = []

    assert process.stdout is not None
    assert process.stderr is not None

    proc_stdout = process.stdout
    proc_stderr = process.stderr

    # Read stderr in a background thread to avoid deadlocks
    def read_stderr():
        for line in proc_stderr:
            stderr_lines.append(line)

    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stderr_thread.start()

    # Stream JSON events and display them in real-time
    for line in proc_stdout:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            _format_agent_event(event)
            # Accumulate text parts for callers that need result.stdout
            part = event.get("part", {})
            if part.get("type") == "text":
                text = part.get("text", "")
                if text:
                    text_parts.append(text)
        except json.JSONDecodeError:
            # Non-JSON line, just print it
            console.print(line)

    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        console.print(
            f"\n{_theme.s('error', f'Agent timed out after {timeout}s', bold=True)}"
        )
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=-1,
            stdout="\n".join(text_parts),
            stderr="TIMEOUT",
        )

    stderr_thread.join(timeout=5)

    stdout_str = "\n".join(text_parts)
    stderr_str = "".join(stderr_lines)

    if process.returncode != 0:
        console.print(
            f"\n{_theme.s('error', f'Agent exited with code {process.returncode}')}"
        )
        if stderr_str:
            console.print(_theme.s("error", stderr_str[:500]))
    else:
        console.print(f"\n{_theme.s('success', 'Agent completed successfully.')}")

    return subprocess.CompletedProcess(
        args=cmd,
        returncode=process.returncode,
        stdout=stdout_str,
        stderr=stderr_str,
    )


def run_git(args: list[str], workdir: str) -> subprocess.CompletedProcess:
    """Run a git command."""
    cmd = ["git"] + args
    return subprocess.run(cmd, cwd=workdir, capture_output=True, text=True)


def run_gh(args: list[str], workdir: str) -> subprocess.CompletedProcess:
    """Run a GitHub CLI command."""
    cmd = ["gh"] + args
    return subprocess.run(cmd, cwd=workdir, capture_output=True, text=True)


def git_branch_exists(branch: str, workdir: str) -> bool:
    """Return True if a local branch exists."""
    if not branch:
        return False
    result = run_git(["show-ref", "--verify", f"refs/heads/{branch}"], workdir)
    return result.returncode == 0


def current_branch(workdir: str) -> str:
    """Get currently checked-out branch name."""
    result = run_git(["branch", "--show-current"], workdir)
    return result.stdout.strip() if result.returncode == 0 else ""


def resolve_base_branch(preferred: str, workdir: str) -> str:
    """Resolve a usable base branch for local operations."""
    if git_branch_exists(preferred, workdir):
        return preferred
    current = current_branch(workdir)
    if current:
        return current
    for fallback in ("main", "master"):
        if git_branch_exists(fallback, workdir):
            return fallback
    return preferred or "main"


# ─── Phase 0: Clarifying Questions ──────────────────────────────────────────


def phase_clarify(task: str, config: dict, workdir: str) -> str:
    """Optionally ask the user clarifying questions before starting the pipeline."""
    console.print()
    console.rule("[bold]Phase 0: Clarifying Questions[/bold]")

    # Ask the LLM to generate clarifying questions based on the task
    prompt = f"""You are a technical project planner about to implement a task. Before starting, look at the project structure and files to understand the existing codebase, then generate 2-4 short, specific clarifying questions that would help you implement it better.

Task: {task}

Output ONLY the numbered questions, one per line, like:
1. Question here?
2. Another question?

Do NOT include any other text, preamble, or explanation."""

    model = config["models"].get("clarify", config["models"]["feature"])
    result = run_agent(prompt, model, workdir, timeout=120)

    # Parse numbered questions from the output
    questions = re.findall(r"^\s*\d+[\.\)]\s*(.+)", result.stdout, re.MULTILINE)

    if not questions:
        console.print(
            _theme.s(
                "muted",
                "Could not generate clarifying questions. Proceeding with original task.",
            )
        )
        return task

    console.print(f"\n  Generated {len(questions)} clarifying question(s).")
    console.print(
        _theme.s("muted", '  (Press Enter to skip a question, "skip" to skip all,')
    )
    console.print(
        _theme.s("muted", '   or "own" to provide your own context instead.)') + "\n"
    )

    answers = []
    freeform_text = None

    for i, question in enumerate(questions, 1):
        console.print(f"  [bold]Q{i}:[/bold] {question}")
        try:
            response = input("  > ").strip()
        except EOFError:
            break

        if response.lower() == "skip":
            console.print("  " + _theme.s("muted", "Skipping remaining questions."))
            break
        elif response.lower() == "own":
            console.print(
                "\n  [bold]Provide your own context[/bold] "
                + _theme.s("muted", "(enter a blank line to finish):")
            )
            lines = []
            while True:
                try:
                    line = input("  > ")
                except EOFError:
                    break
                if line.strip() == "":
                    break
                lines.append(line)
            if lines:
                freeform_text = "\n".join(lines)
            break
        elif response:
            answers.append((question, response))
        # Empty response = skip this question, continue to next
        console.print()

    # Build enriched task string
    if not answers and not freeform_text:
        console.print(
            _theme.s(
                "muted",
                "No additional context provided. Proceeding with original task.",
            )
        )
        return task

    enriched = task

    if answers:
        enriched += "\n\nClarifications:"
        for q, a in answers:
            enriched += f"\n  Q: {q}\n  A: {a}"

    if freeform_text:
        enriched += f"\n\nAdditional context from user:\n{freeform_text}"

    console.print("\n" + _theme.s("success", "Task enriched with user context."))
    return enriched


# ─── Phase 1: Task Decomposition ────────────────────────────────────────────


def phase_decompose(task: str, config: dict, workdir: str) -> str:
    """Break down task into TODO.md using Opus."""
    console.print()
    console.rule("[bold]Phase 1: Task Decomposition[/bold]")

    prompt = f"""You are a technical project planner. Break down the following task into a clear, actionable TODO.md file.

Task: {task}

Write a TODO.md file in the project root with:
- A title describing the feature
- Checkboxes for each subtask (use - [ ] format)
- Subtasks should be ordered by dependency
- Each subtask should be small enough for a single implementation pass
- Include a final item for "Run tests and verify"

Write ONLY the TODO.md file, nothing else."""

    model = config["models"].get("planning", config["models"]["feature"])
    result = run_agent(prompt, model, workdir)

    todo_path = Path(workdir) / config["orchestrator"]["todo_file"]
    if todo_path.exists():
        console.print(_theme.s("success", f"TODO.md created at {todo_path}"))
        with open(todo_path) as f:
            content = f.read()
        console.print(content)
        return content
    else:
        # If claude didn't create the file, create it from output
        console.print(_theme.s("warning", "Creating TODO.md from agent output..."))
        with open(todo_path, "w") as f:
            f.write(result.stdout)
        return result.stdout


# ─── Phase 2: Feature Implementation ────────────────────────────────────────


def phase_implement(task: str, todo_content: str, config: dict, workdir: str):
    """Implement the feature using Opus with full context."""
    console.print()
    console.rule("[bold]Phase 2: Feature Implementation[/bold]")

    prompt = f"""You are implementing a feature. Here is the task and plan:

TASK: {task}

TODO PLAN:
{todo_content}

Implement all the subtasks in the TODO. For each completed subtask, update TODO.md by checking off the box (change - [ ] to - [x]).

Work through each item methodically. Write clean, well-structured code. Make sure everything compiles/runs correctly before finishing."""

    model = config["models"].get("implement", config["models"]["feature"])
    result = run_agent(prompt, model, workdir)

    if result.returncode != 0:
        raise RuntimeError(f"Feature implementation failed: {result.stderr[:300]}")

    console.print(_theme.s("success", "Phase 2 complete."))
    return result


# ─── Phase 3: Cold Refactor ──────────────────────────────────────────────────


def phase_refactor(config: dict, workdir: str):
    """Run Codex with zero prior context for unbiased refactoring."""
    console.print()
    console.rule("[bold]Phase 3: Cold Refactor[/bold]")

    # Intentionally minimal prompt - no task context leaked
    prompt = """Review the current state of this codebase. Refactor for:
- Code readability and clarity
- Consistent naming conventions
- DRY principles - remove duplication
- Proper error handling
- Performance improvements where obvious

Do NOT change functionality. Do NOT add new features. Only refactor existing code.
Focus on recently modified files (check git status for changed files).
Do not touch test files unless they have clear code quality issues."""

    model = config["models"]["refactor"]
    result = run_agent(prompt, model, workdir)

    if result.returncode != 0:
        raise RuntimeError(f"Refactor phase failed: {result.stderr[:300]}")

    console.print(_theme.s("success", "Phase 3 complete."))
    return result


# ─── Phase 4: Ship (Branch + PR) ────────────────────────────────────────────


def phase_ship(
    task: str, config: dict, workdir: str, local_mode: bool = False
) -> tuple[str | None, str, str]:
    """Checkout new branch, commit changes, and optionally push + create PR.

    Returns (pr_url_or_local_ref, branch_name, base_branch).
    pr_url_or_local_ref is None if no changes, "local:<branch>" in local mode,
    or a PR URL in remote mode.
    """
    console.print()
    console.rule("[bold]Phase 4: Ship[/bold]")

    slug = slugify(task)
    prefix = config["branching"]["prefix"]
    sep = config["branching"]["separator"]
    timestamp = datetime.now().strftime("%m%d")
    branch_name = f"{prefix}{sep}{slug}-{timestamp}"
    configured_base = config["pr"]["base_branch"]
    base_branch = resolve_base_branch(configured_base, workdir)
    if configured_base != base_branch:
        console.print(
            _theme.s(
                "warning",
                f"Configured base branch '{configured_base}' not found; using '{base_branch}'.",
            )
        )

    # Checkout new branch
    console.print(_theme.s("muted", f"Creating branch: {branch_name}"))
    result = run_git(["checkout", "-b", branch_name], workdir)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to create branch: {result.stderr}")

    # Stage all changes
    run_git(["add", "-A"], workdir)

    # Check if there are changes to commit
    status = run_git(["status", "--porcelain"], workdir)
    if not status.stdout.strip():
        console.print(_theme.s("warning", "No changes to commit."))
        return None, branch_name, base_branch

    # Commit
    commit_msg = f"feat: {task[:72]}"
    run_git(["commit", "-m", commit_msg], workdir)
    console.print(_theme.s("muted", f"Committed: {commit_msg}"))

    if local_mode:
        console.print(
            _theme.s(
                "muted",
                f"Branch {branch_name} created and committed locally.",
            )
        )
        return f"local:{branch_name}", branch_name, base_branch

    # Push
    result = run_git(["push", "-u", "origin", branch_name], workdir)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to push: {result.stderr}")
    console.print(_theme.s("muted", f"Pushed to origin/{branch_name}"))

    # Create PR
    draft_flag = ["--draft"] if config["pr"].get("draft") else []
    pr_result = run_gh(
        [
            "pr",
            "create",
            "--title",
            f"feat: {task[:72]}",
            "--body",
            f"## Summary\n\nImplements: {task}\n\n---\n_Created by agent-orchestrator_",
            "--base",
            base_branch,
        ]
        + draft_flag,
        workdir,
    )

    if pr_result.returncode != 0:
        raise RuntimeError(f"Failed to create PR: {pr_result.stderr}")

    pr_url = pr_result.stdout.strip()
    console.print(_theme.s("success", f"PR created: {pr_url}", bold=True))
    return pr_url, branch_name, base_branch


# ─── Phase 5: PR Review ─────────────────────────────────────────────────────


def phase_review(
    task: str, pr_url: str, config: dict, workdir: str, local_mode: bool = False
):
    """Fresh session reviews the PR (or local diff), runs tests, presents results."""
    console.print()
    console.rule("[bold]Phase 5: PR Review[/bold]")

    test_cmd = config["tests"]["command"]
    base_branch = resolve_base_branch(config["pr"]["base_branch"], workdir)

    if local_mode:
        prompt = f"""You are a code reviewer. Review the changes on the current branch.

TASK: {task}

Do the following:
1. Review the diff against {base_branch} using: git diff {base_branch}...HEAD
2. Run the test suite with: {test_cmd}
3. Check for test parity - are all existing tests still passing? Are there new tests for the new functionality?
4. Summarize your findings clearly:
   - List any issues or concerns
   - Test results (pass/fail count, coverage if available)
   - Whether the changes are safe to merge
5. Print a clear RECOMMENDATION: APPROVE or REQUEST_CHANGES with reasoning

Format your output clearly so it's easy to read in a terminal."""
    else:
        prompt = f"""You are a code reviewer. A PR has been created for the following task:

TASK: {task}
PR: {pr_url}

Do the following:
1. Review the PR diff using `gh pr diff`
2. Run the test suite with: {test_cmd}
3. Check for test parity - are all existing tests still passing? Are there new tests for the new functionality?
4. Summarize your findings clearly:
   - List any issues or concerns
   - Test results (pass/fail count, coverage if available)
   - Whether the changes are safe to merge
5. Print a clear RECOMMENDATION: APPROVE or REQUEST_CHANGES with reasoning

Format your output clearly so it's easy to read in a terminal."""

    model = config["models"]["review"]
    result = run_agent(prompt, model, workdir)

    console.print()
    console.print(
        Panel(
            result.stdout,
            title="[bold]PR Review Results[/bold]",
            border_style=_theme.info,
        )
    )

    return result


# ─── Phase 6: Human Approval ────────────────────────────────────────────────


def phase_approve(
    pr_url: str,
    config: dict,
    workdir: str,
    local_mode: bool = False,
    branch_name: str = "",
    base_branch: str = "main",
):
    """Notify human and wait for merge approval."""
    if local_mode:
        base_branch = resolve_base_branch(base_branch, workdir)

    console.print()
    console.rule("[bold]Phase 6: Human Approval[/bold]")

    if local_mode:
        notify(f"Branch ready for your review: {branch_name}", config)
        console.print(f"\nBranch: {branch_name}")
        console.print("\nOptions:")
        console.print(
            f"  {_theme.s('success', '[m]', bold=True)} Merge into {base_branch}"
        )
        console.print(
            f"  {_theme.s('warning', '[s]', bold=True)} Skip (leave branch as-is)"
        )
        console.print(f"  {_theme.s('error', '[a]', bold=True)} Abort (delete branch)")
    else:
        notify(f"PR ready for your review: {pr_url}", config)
        console.print(f"\nPR URL: [link={pr_url}]{pr_url}[/link]")
        console.print("\nOptions:")
        console.print(f"  {_theme.s('success', '[m]', bold=True)} Merge the PR")
        console.print(f"  {_theme.s('warning', '[s]', bold=True)} Skip (leave PR open)")
        console.print(f"  {_theme.s('error', '[a]', bold=True)} Abort (close PR)")

    while True:
        choice = input("\nYour choice (m/s/a): ").strip().lower()
        if choice == "m":
            if local_mode:
                result = run_git(["checkout", base_branch], workdir)
                if result.returncode != 0:
                    console.print(
                        _theme.s(
                            "error",
                            f"Failed to checkout {base_branch}: {result.stderr}",
                        )
                    )
                    break
                result = run_git(
                    [
                        "merge",
                        branch_name,
                        "--no-ff",
                        "-m",
                        f"Merge branch '{branch_name}'",
                    ],
                    workdir,
                )
                if result.returncode == 0:
                    console.print(
                        _theme.s(
                            "success",
                            f"Branch '{branch_name}' merged into {base_branch}.",
                            bold=True,
                        )
                    )
                    run_git(["branch", "-d", branch_name], workdir)
                else:
                    console.print(_theme.s("error", f"Merge failed: {result.stderr}"))
                    console.print(
                        _theme.s(
                            "muted",
                            "You may need to resolve conflicts manually.",
                        )
                    )
            else:
                result = run_gh(["pr", "merge", pr_url, "--merge"], workdir)
                if result.returncode == 0:
                    console.print(
                        _theme.s("success", "PR merged successfully.", bold=True)
                    )
                else:
                    console.print(_theme.s("error", f"Merge failed: {result.stderr}"))
                    console.print(_theme.s("muted", "You may need to merge manually."))
            break
        elif choice == "s":
            if local_mode:
                console.print(
                    _theme.s("warning", f"Branch '{branch_name}' left as-is.")
                )
            else:
                console.print(_theme.s("warning", "PR left open for manual review."))
            break
        elif choice == "a":
            if local_mode:
                run_git(["checkout", base_branch], workdir)
                result = run_git(["branch", "-D", branch_name], workdir)
                if result.returncode == 0:
                    console.print(_theme.s("error", f"Branch '{branch_name}' deleted."))
                else:
                    console.print(
                        _theme.s(
                            "error",
                            f"Failed to delete branch: {result.stderr}",
                        )
                    )
            else:
                run_gh(["pr", "close", pr_url], workdir)
                console.print(_theme.s("error", "PR closed."))
            break
        else:
            console.print(_theme.s("error", "Invalid choice. Enter m, s, or a."))


# ─── Main Pipeline ───────────────────────────────────────────────────────────


def _ensure_git_repo(workdir: str) -> bool:
    """Ensure the workdir exists and is a git repository with an origin remote.

    Returns True if operating in local mode (no remote), False otherwise.
    """
    path = Path(workdir)
    if not path.exists():
        path.mkdir(parents=True)
        console.print(_theme.s("muted", f"Created directory: {workdir}"))

    git_dir = path / ".git"
    if not git_dir.exists():
        result = subprocess.run(
            ["git", "init"], cwd=workdir, capture_output=True, text=True
        )
        if result.returncode == 0:
            console.print(_theme.s("muted", f"Initialized git repo in {workdir}"))
        else:
            console.print(
                _theme.s("error", f"Failed to init git repo: {result.stderr}")
            )
            return True

    # Check for origin remote
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=workdir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        console.print(_theme.s("warning", "No 'origin' remote configured.", bold=True))
        console.print(
            _theme.s(
                "muted",
                "A remote is needed for pushing branches and creating PRs.",
            )
        )
        console.print(
            _theme.s(
                "muted",
                "Example: https://github.com/user/repo.git or git@github.com:user/repo.git",
            )
        )
        try:
            url = input("  Remote URL (or Enter to work locally): ").strip()
        except (EOFError, KeyboardInterrupt):
            url = ""

        if url:
            add_result = subprocess.run(
                ["git", "remote", "add", "origin", url],
                cwd=workdir,
                capture_output=True,
                text=True,
            )
            if add_result.returncode == 0:
                console.print(_theme.s("success", f"Remote 'origin' set to {url}"))
                return False
            else:
                console.print(
                    _theme.s("error", f"Failed to add remote: {add_result.stderr}")
                )
                return True
        else:
            console.print(
                _theme.s(
                    "warning",
                    "Local mode — branches and reviews will happen locally.",
                )
            )
            return True

    return False


def run_pipeline(task: str, workdir: str, config_path: str | None = None):
    """Execute the full orchestrator pipeline."""
    global _theme
    config = load_config(config_path)
    _theme = Theme(config)
    workdir = os.path.abspath(workdir)
    local_mode = _ensure_git_repo(workdir)

    console.clear()
    mode_label = " [yellow](local mode)[/yellow]" if local_mode else ""
    console.print(
        Panel(
            f"[bold]Task:[/bold] {task}\n"
            f"[bold]Working dir:[/bold] {workdir}{mode_label}\n"
            f"[bold]Models:[/bold] {config['models']['feature']} / {config['models']['refactor']}",
            title=_theme.s("accent", "AGENT ORCHESTRATOR", bold=True),
            border_style=_theme.accent,
        )
    )

    try:
        # Phase 0: Clarifying questions (optional, interactive)
        task = phase_clarify(task, config, workdir)

        # Phase 1: Decompose
        todo_content = phase_decompose(task, config, workdir)

        # Phase 2: Implement
        phase_implement(task, todo_content, config, workdir)

        # Phase 3: Cold refactor
        phase_refactor(config, workdir)

        # Phase 4: Ship
        pr_url, branch_name, base_branch = phase_ship(task, config, workdir, local_mode)
        if pr_url is None:
            console.print(
                _theme.s("warning", "No changes produced. Pipeline complete.")
            )
            return

        # Phase 5: Review
        phase_review(task, pr_url, config, workdir, local_mode)

        # Phase 6: Human approval
        phase_approve(pr_url, config, workdir, local_mode, branch_name, base_branch)

    except RuntimeError as e:
        notify(f"Pipeline failed: {e}", config)
        console.print(f"\n{_theme.s('error', f'FATAL: {e}', bold=True)}")
        sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n" + _theme.s("warning", "Pipeline stopped by user."))
        sys.exit(130)
    finally:
        # Clean up the todo file
        todo_path = Path(workdir) / config["orchestrator"]["todo_file"]
        if todo_path.exists():
            todo_path.unlink()
            console.print(_theme.s("muted", f"Cleaned up {todo_path.name}"))

    console.print()
    console.print(
        Panel(
            _theme.s("success", "PIPELINE COMPLETE", bold=True),
            border_style=_theme.success,
        )
    )
    console.print()


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Agent Orchestrator - Multi-model development pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s "Add user authentication with JWT tokens"
  %(prog)s "Fix the pagination bug in /api/users" --workdir ./my-project
  %(prog)s "Refactor the database layer" --config ./custom-config.yaml
  %(prog)s --no-tui "Quick fix" --workdir ./my-project
        """,
    )
    parser.add_argument(
        "task", nargs="?", default=None, help="Description of the task to implement"
    )
    parser.add_argument(
        "--workdir",
        "-w",
        default=".",
        help="Project working directory (default: current directory)",
    )
    parser.add_argument(
        "--config",
        "-c",
        default=None,
        help="Path to config.yaml (default: orchestrator's config.yaml)",
    )
    parser.add_argument(
        "--no-tui",
        action="store_true",
        default=False,
        help="Run in headless mode (no interactive TUI)",
    )

    args = parser.parse_args()

    if args.no_tui:
        if not args.task:
            parser.error("task is required in --no-tui mode")
        run_pipeline(args.task, args.workdir, args.config)
    else:
        from agent_orchestrator.tui import run_tui

        run_tui(args.task, args.workdir, args.config)


if __name__ == "__main__":
    main()
