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
        console.print(f"[red bold]Config not found: {path}[/red bold]")
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


# ─── Utilities ───────────────────────────────────────────────────────────────


def notify(message: str, config: dict):
    """Send notification via terminal bell + stdout."""
    if config["notifications"].get("terminal_bell"):
        console.print("\a", end="")
    if config["notifications"].get("stdout"):
        console.print()
        console.print(
            Panel(
                f"[bold]{message}[/bold]", title="NOTIFICATION", border_style="yellow"
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
        console.print("[dim]---[/dim]")

    elif event_type == "tool":
        tool_name = part.get("tool", "unknown")
        state = part.get("state", {})
        title = state.get("title", "")
        status = state.get("status", "")

        if title:
            console.print(f"  [cyan bold]{tool_name}[/cyan bold] [dim]{title}[/dim]")
        else:
            # Show the input for context
            tool_input = state.get("input", {})
            input_summary = ""
            if isinstance(tool_input, dict):
                # Show first meaningful value
                for key in ("filePath", "command", "pattern", "content"):
                    if key in tool_input:
                        val = str(tool_input[key])
                        input_summary = val[:80] + ("..." if len(val) > 80 else "")
                        break
            console.print(
                f"  [cyan bold]{tool_name}[/cyan bold] [dim]{input_summary}[/dim]"
            )

        if status == "error":
            error = state.get("error", "unknown error")
            console.print(f"  [red]{error}[/red]")

    elif event_type == "text":
        text = part.get("text", "")
        if text.strip():
            console.print(text)

    elif event_type == "step-finish":
        tokens = part.get("tokens", {})
        total = tokens.get("total", 0)
        output = tokens.get("output", 0)
        if total:
            console.print(f"[dim]  tokens: {total:,} total, {output:,} output[/dim]")


def run_agent(
    prompt: str, model: str, workdir: str, timeout: int = 600
) -> subprocess.CompletedProcess:
    """Run an OpenCode CLI session with real-time streaming output."""
    cmd = ["opencode", "run", "--format", "json", "--model", model, prompt]

    console.print(f"\n[dim]Running {model}...[/dim]")
    console.print(
        f"[dim]Prompt: {prompt[:100]}{'...' if len(prompt) > 100 else ''}[/dim]"
    )
    console.print(f"[dim]Working dir: {workdir}[/dim]\n")

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
        console.print(f"\n[red bold]Agent timed out after {timeout}s[/red bold]")
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
        console.print(f"\n[red]Agent exited with code {process.returncode}[/red]")
        if stderr_str:
            console.print(f"[red]{stderr_str[:500]}[/red]")
    else:
        console.print(f"\n[green]Agent completed successfully.[/green]")

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

    model = config["models"]["feature"]
    result = run_agent(prompt, model, workdir, timeout=120)

    # Parse numbered questions from the output
    questions = re.findall(r"^\s*\d+[\.\)]\s*(.+)", result.stdout, re.MULTILINE)

    if not questions:
        console.print(
            "[dim]Could not generate clarifying questions. Proceeding with original task.[/dim]"
        )
        return task

    console.print(f"\n  Generated {len(questions)} clarifying question(s).")
    console.print('  [dim](Press Enter to skip a question, "skip" to skip all,[/dim]')
    console.print('  [dim] or "own" to provide your own context instead.)[/dim]\n')

    answers = []
    freeform_text = None

    for i, question in enumerate(questions, 1):
        console.print(f"  [bold]Q{i}:[/bold] {question}")
        try:
            response = input("  > ").strip()
        except EOFError:
            break

        if response.lower() == "skip":
            console.print("  [dim]Skipping remaining questions.[/dim]")
            break
        elif response.lower() == "own":
            console.print(
                "\n  [bold]Provide your own context[/bold] [dim](enter a blank line to finish):[/dim]"
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
            "[dim]No additional context provided. Proceeding with original task.[/dim]"
        )
        return task

    enriched = task

    if answers:
        enriched += "\n\nClarifications:"
        for q, a in answers:
            enriched += f"\n  Q: {q}\n  A: {a}"

    if freeform_text:
        enriched += f"\n\nAdditional context from user:\n{freeform_text}"

    console.print("\n[green]Task enriched with user context.[/green]")
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

    model = config["models"]["feature"]
    result = run_agent(prompt, model, workdir)

    todo_path = Path(workdir) / config["orchestrator"]["todo_file"]
    if todo_path.exists():
        console.print(f"[green]TODO.md created at {todo_path}[/green]")
        with open(todo_path) as f:
            content = f.read()
        console.print(content)
        return content
    else:
        # If claude didn't create the file, create it from output
        console.print("[yellow]Creating TODO.md from agent output...[/yellow]")
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

    model = config["models"]["feature"]
    result = run_agent(prompt, model, workdir)

    if result.returncode != 0:
        raise RuntimeError(f"Feature implementation failed: {result.stderr[:300]}")

    console.print("[green]Phase 2 complete.[/green]")
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

    console.print("[green]Phase 3 complete.[/green]")
    return result


# ─── Phase 4: Ship (Branch + PR) ────────────────────────────────────────────


def phase_ship(task: str, config: dict, workdir: str) -> str | None:
    """Checkout new branch, commit changes, create PR."""
    console.print()
    console.rule("[bold]Phase 4: Ship[/bold]")

    slug = slugify(task)
    prefix = config["branching"]["prefix"]
    sep = config["branching"]["separator"]
    timestamp = datetime.now().strftime("%m%d")
    branch_name = f"{prefix}{sep}{slug}-{timestamp}"
    base_branch = config["pr"]["base_branch"]

    # Checkout new branch
    console.print(f"[dim]Creating branch: {branch_name}[/dim]")
    result = run_git(["checkout", "-b", branch_name], workdir)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to create branch: {result.stderr}")

    # Stage all changes
    run_git(["add", "-A"], workdir)

    # Check if there are changes to commit
    status = run_git(["status", "--porcelain"], workdir)
    if not status.stdout.strip():
        console.print("[yellow]No changes to commit.[/yellow]")
        return None

    # Commit
    commit_msg = f"feat: {task[:72]}"
    run_git(["commit", "-m", commit_msg], workdir)
    console.print(f"[dim]Committed: {commit_msg}[/dim]")

    # Push
    result = run_git(["push", "-u", "origin", branch_name], workdir)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to push: {result.stderr}")
    console.print(f"[dim]Pushed to origin/{branch_name}[/dim]")

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
    console.print(f"[green bold]PR created: {pr_url}[/green bold]")
    return pr_url


# ─── Phase 5: PR Review ─────────────────────────────────────────────────────


def phase_review(task: str, pr_url: str, config: dict, workdir: str):
    """Fresh Opus session reviews the PR, runs tests, presents results."""
    console.print()
    console.rule("[bold]Phase 5: PR Review[/bold]")

    test_cmd = config["tests"]["command"]

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
            result.stdout, title="[bold]PR Review Results[/bold]", border_style="cyan"
        )
    )

    return result


# ─── Phase 6: Human Approval ────────────────────────────────────────────────


def phase_approve(pr_url: str, config: dict, workdir: str):
    """Notify human and wait for merge approval."""
    console.print()
    console.rule("[bold]Phase 6: Human Approval[/bold]")

    notify(f"PR ready for your review: {pr_url}", config)

    console.print(f"\nPR URL: [link={pr_url}]{pr_url}[/link]")
    console.print("\nOptions:")
    console.print("  [bold green]\\[m][/bold green] Merge the PR")
    console.print("  [bold yellow]\\[s][/bold yellow] Skip (leave PR open)")
    console.print("  [bold red]\\[a][/bold red] Abort (close PR)")

    while True:
        choice = input("\nYour choice (m/s/a): ").strip().lower()
        if choice == "m":
            result = run_gh(["pr", "merge", pr_url, "--merge"], workdir)
            if result.returncode == 0:
                console.print("[green bold]PR merged successfully.[/green bold]")
            else:
                console.print(f"[red]Merge failed: {result.stderr}[/red]")
                console.print("[dim]You may need to merge manually.[/dim]")
            break
        elif choice == "s":
            console.print("[yellow]PR left open for manual review.[/yellow]")
            break
        elif choice == "a":
            result = run_gh(["pr", "close", pr_url], workdir)
            console.print("[red]PR closed.[/red]")
            break
        else:
            console.print("[red]Invalid choice. Enter m, s, or a.[/red]")


# ─── Main Pipeline ───────────────────────────────────────────────────────────


def run_pipeline(task: str, workdir: str, config_path: str | None = None):
    """Execute the full orchestrator pipeline."""
    config = load_config(config_path)
    workdir = os.path.abspath(workdir)

    console.clear()
    console.print(
        Panel(
            f"[bold]Task:[/bold] {task}\n"
            f"[bold]Working dir:[/bold] {workdir}\n"
            f"[bold]Models:[/bold] {config['models']['feature']} / {config['models']['refactor']}",
            title="[bold blue]AGENT ORCHESTRATOR[/bold blue]",
            border_style="blue",
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
        pr_url = phase_ship(task, config, workdir)
        if pr_url is None:
            console.print("[yellow]No changes produced. Pipeline complete.[/yellow]")
            return

        # Phase 5: Review
        phase_review(task, pr_url, config, workdir)

        # Phase 6: Human approval
        phase_approve(pr_url, config, workdir)

    except RuntimeError as e:
        notify(f"Pipeline failed: {e}", config)
        console.print(f"\n[red bold]FATAL: {e}[/red bold]")
        sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Pipeline stopped by user.[/yellow]")
        sys.exit(130)
    finally:
        # Clean up the todo file
        todo_path = Path(workdir) / config["orchestrator"]["todo_file"]
        if todo_path.exists():
            todo_path.unlink()
            console.print(f"[dim]Cleaned up {todo_path.name}[/dim]")

    console.print()
    console.print(
        Panel("[bold green]PIPELINE COMPLETE[/bold green]", border_style="green")
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
        """,
    )
    parser.add_argument("task", help="Description of the task to implement")
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

    args = parser.parse_args()
    run_pipeline(args.task, args.workdir, args.config)


if __name__ == "__main__":
    main()
