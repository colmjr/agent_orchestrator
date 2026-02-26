#!/usr/bin/env python3
"""
Agent Orchestrator - Multi-model pipeline for feature development.

Pipeline:
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
import yaml
import argparse
import re
from pathlib import Path
from datetime import datetime

# ─── Configuration ───────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_CONFIG = SCRIPT_DIR / "config.yaml"


def load_config(config_path: str | None = None) -> dict:
    path = Path(config_path) if config_path else DEFAULT_CONFIG
    if not path.exists():
        print(f"[ERROR] Config not found: {path}")
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


# ─── Utilities ───────────────────────────────────────────────────────────────


def notify(message: str, config: dict):
    """Send notification via terminal bell + stdout."""
    if config["notifications"].get("terminal_bell"):
        print("\a", end="", flush=True)
    if config["notifications"].get("stdout"):
        print(f"\n{'=' * 60}")
        print(f"  NOTIFICATION: {message}")
        print(f"{'=' * 60}\n")


def slugify(text: str) -> str:
    """Convert task description to branch-safe slug."""
    slug = re.sub(r"[^a-zA-Z0-9\s-]", "", text.lower())
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    return slug[:50]


def run_claude(
    prompt: str, model: str, workdir: str, print_mode: bool = True
) -> subprocess.CompletedProcess:
    """Run a Claude Code CLI session."""
    cmd = ["claude"]
    if print_mode:
        cmd.append("--print")
    cmd.extend(["--model", model, prompt])

    print(f"\n[AGENT] Running {model}...")
    print(f"[AGENT] Prompt: {prompt[:100]}{'...' if len(prompt) > 100 else ''}")
    print(f"[AGENT] Working dir: {workdir}\n")

    result = subprocess.run(
        cmd,
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=600,  # 10 min timeout per phase
    )

    if result.returncode != 0:
        print(f"[ERROR] Agent exited with code {result.returncode}")
        print(f"[STDERR] {result.stderr[:500]}")
    else:
        print(f"[AGENT] Completed successfully.")

    return result


def run_git(args: list[str], workdir: str) -> subprocess.CompletedProcess:
    """Run a git command."""
    cmd = ["git"] + args
    return subprocess.run(cmd, cwd=workdir, capture_output=True, text=True)


def run_gh(args: list[str], workdir: str) -> subprocess.CompletedProcess:
    """Run a GitHub CLI command."""
    cmd = ["gh"] + args
    return subprocess.run(cmd, cwd=workdir, capture_output=True, text=True)


# ─── Phase 1: Task Decomposition ────────────────────────────────────────────


def phase_decompose(task: str, config: dict, workdir: str) -> str:
    """Break down task into TODO.md using Opus."""
    print("\n" + "─" * 60)
    print("PHASE 1: Task Decomposition")
    print("─" * 60)

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
    result = run_claude(prompt, model, workdir)

    todo_path = Path(workdir) / config["orchestrator"]["todo_file"]
    if todo_path.exists():
        print(f"[PHASE 1] TODO.md created at {todo_path}")
        with open(todo_path) as f:
            content = f.read()
        print(content)
        return content
    else:
        # If claude didn't create the file, create it from output
        print("[PHASE 1] Creating TODO.md from agent output...")
        with open(todo_path, "w") as f:
            f.write(result.stdout)
        return result.stdout


# ─── Phase 2: Feature Implementation ────────────────────────────────────────


def phase_implement(task: str, todo_content: str, config: dict, workdir: str):
    """Implement the feature using Opus with full context."""
    print("\n" + "─" * 60)
    print("PHASE 2: Feature Implementation (Opus 4.6)")
    print("─" * 60)

    prompt = f"""You are implementing a feature. Here is the task and plan:

TASK: {task}

TODO PLAN:
{todo_content}

Implement all the subtasks in the TODO. For each completed subtask, update TODO.md by checking off the box (change - [ ] to - [x]).

Work through each item methodically. Write clean, well-structured code. Make sure everything compiles/runs correctly before finishing."""

    model = config["models"]["feature"]
    result = run_claude(prompt, model, workdir)

    if result.returncode != 0:
        raise RuntimeError(f"Feature implementation failed: {result.stderr[:300]}")

    print("[PHASE 2] Feature implementation complete.")
    return result


# ─── Phase 3: Cold Refactor ──────────────────────────────────────────────────


def phase_refactor(config: dict, workdir: str):
    """Run Codex with zero prior context for unbiased refactoring."""
    print("\n" + "─" * 60)
    print("PHASE 3: Cold Refactor (Codex 5.3 - Zero Context)")
    print("─" * 60)

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
    result = run_claude(prompt, model, workdir)

    if result.returncode != 0:
        raise RuntimeError(f"Refactor phase failed: {result.stderr[:300]}")

    print("[PHASE 3] Cold refactor complete.")
    return result


# ─── Phase 4: Ship (Branch + PR) ────────────────────────────────────────────


def phase_ship(task: str, config: dict, workdir: str) -> str | None:
    """Checkout new branch, commit changes, create PR."""
    print("\n" + "─" * 60)
    print("PHASE 4: Ship (Branch + Commit + PR)")
    print("─" * 60)

    slug = slugify(task)
    prefix = config["branching"]["prefix"]
    sep = config["branching"]["separator"]
    timestamp = datetime.now().strftime("%m%d")
    branch_name = f"{prefix}{sep}{slug}-{timestamp}"
    base_branch = config["pr"]["base_branch"]

    # Checkout new branch
    print(f"[SHIP] Creating branch: {branch_name}")
    result = run_git(["checkout", "-b", branch_name], workdir)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to create branch: {result.stderr}")

    # Stage all changes
    run_git(["add", "-A"], workdir)

    # Check if there are changes to commit
    status = run_git(["status", "--porcelain"], workdir)
    if not status.stdout.strip():
        print("[SHIP] No changes to commit.")
        return None

    # Commit
    commit_msg = f"feat: {task[:72]}"
    run_git(["commit", "-m", commit_msg], workdir)
    print(f"[SHIP] Committed: {commit_msg}")

    # Push
    result = run_git(["push", "-u", "origin", branch_name], workdir)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to push: {result.stderr}")
    print(f"[SHIP] Pushed to origin/{branch_name}")

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
    print(f"[SHIP] PR created: {pr_url}")
    return pr_url


# ─── Phase 5: PR Review ─────────────────────────────────────────────────────


def phase_review(task: str, pr_url: str, config: dict, workdir: str):
    """Fresh Opus session reviews the PR, runs tests, presents results."""
    print("\n" + "─" * 60)
    print("PHASE 5: PR Review (Fresh Opus 4.6 Session)")
    print("─" * 60)

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
    result = run_claude(prompt, model, workdir)

    print("\n" + "=" * 60)
    print("PR REVIEW RESULTS")
    print("=" * 60)
    print(result.stdout)

    return result


# ─── Phase 6: Human Approval ────────────────────────────────────────────────


def phase_approve(pr_url: str, config: dict, workdir: str):
    """Notify human and wait for merge approval."""
    print("\n" + "─" * 60)
    print("PHASE 6: Human Approval")
    print("─" * 60)

    notify(f"PR ready for your review: {pr_url}", config)

    print(f"\nPR URL: {pr_url}")
    print("\nOptions:")
    print("  [m] Merge the PR")
    print("  [s] Skip (leave PR open)")
    print("  [a] Abort (close PR)")

    while True:
        choice = input("\nYour choice (m/s/a): ").strip().lower()
        if choice == "m":
            result = run_gh(["pr", "merge", pr_url, "--merge"], workdir)
            if result.returncode == 0:
                print("[APPROVED] PR merged successfully.")
            else:
                print(f"[ERROR] Merge failed: {result.stderr}")
                print("You may need to merge manually.")
            break
        elif choice == "s":
            print("[SKIPPED] PR left open for manual review.")
            break
        elif choice == "a":
            result = run_gh(["pr", "close", pr_url], workdir)
            print("[ABORTED] PR closed.")
            break
        else:
            print("Invalid choice. Enter m, s, or a.")


# ─── Main Pipeline ───────────────────────────────────────────────────────────


def run_pipeline(task: str, workdir: str, config_path: str | None = None):
    """Execute the full orchestrator pipeline."""
    config = load_config(config_path)
    workdir = os.path.abspath(workdir)

    print(f"\n{'=' * 60}")
    print(f"  AGENT ORCHESTRATOR")
    print(f"  Task: {task}")
    print(f"  Working dir: {workdir}")
    print(f"  Models: {config['models']['feature']} / {config['models']['refactor']}")
    print(f"{'=' * 60}")

    try:
        # Phase 1: Decompose
        todo_content = phase_decompose(task, config, workdir)

        # Phase 2: Implement
        phase_implement(task, todo_content, config, workdir)

        # Phase 3: Cold refactor
        phase_refactor(config, workdir)

        # Phase 4: Ship
        pr_url = phase_ship(task, config, workdir)
        if pr_url is None:
            print("[DONE] No changes produced. Pipeline complete.")
            return

        # Phase 5: Review
        phase_review(task, pr_url, config, workdir)

        # Phase 6: Human approval
        phase_approve(pr_url, config, workdir)

    except RuntimeError as e:
        notify(f"Pipeline failed: {e}", config)
        print(f"\n[FATAL] {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[INTERRUPTED] Pipeline stopped by user.")
        sys.exit(130)

    print(f"\n{'=' * 60}")
    print("  PIPELINE COMPLETE")
    print(f"{'=' * 60}\n")


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
