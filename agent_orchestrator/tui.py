#!/usr/bin/env python3
"""
Agent Orchestrator TUI - Interactive terminal interface.

Full-screen Textual app with:
- Scrolling output pane showing real-time agent events
- Input box for queuing messages / intervening between phases
- Escape to interrupt agent mid-response (keeps session context)
- Phase progression header
"""

import asyncio
import json
import os
import re
import signal
import subprocess
import yaml
from collections import deque
from pathlib import Path
from typing import Callable

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Footer,
    Header,
    Input,
    OptionList,
    RichLog,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option
from textual.worker import Worker, WorkerState

from agent_orchestrator.orchestrator import Theme

# ─── Configuration ───────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_CONFIG = SCRIPT_DIR / "config.yaml"

PHASE_NAMES = {
    0: "Clarifying Questions",
    1: "Planning & Decomposition",
    2: "Feature Implementation",
    3: "Cold Refactor",
    4: "Ship",
    5: "PR Review",
    6: "Human Approval",
}


def load_config(config_path: str | None = None) -> dict:
    path = Path(config_path) if config_path else DEFAULT_CONFIG
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with open(path) as f:
        return yaml.safe_load(f)


SLASH_COMMANDS = [
    ("/configure", "Describe config changes in plain text"),
    ("/help", "Show available commands"),
    ("/config", "Edit configuration"),
    ("/exit", "Quit the application"),
    ("/skip", "Skip the current phase or question"),
    ("/status", "Show current pipeline status"),
    ("/task", "Show the current task description"),
    ("/vim", "Toggle vim keybindings"),
    ("/workdir", "Show the working directory"),
]


class VimHandler:
    """Minimal vim emulation state machine for TextArea and Input widgets."""

    def __init__(self) -> None:
        self.mode = "normal"  # "normal" or "insert"
        self._pending = ""  # For multi-char commands like dd, dw

    @property
    def mode_label(self) -> str:
        return "-- INSERT --" if self.mode == "insert" else "NORMAL"

    def handle_key_textarea(self, key: str, char: str | None, editor: TextArea) -> bool:
        """Handle a key event for a TextArea. Returns True if the key was consumed."""
        if self.mode == "insert":
            if key == "escape":
                self.mode = "normal"
                return True
            return False  # Let TextArea handle it normally

        # ── Normal mode ──────────────────────────────────────────────
        # Multi-char commands
        if self._pending:
            return self._handle_pending(key, char, editor)

        if key == "i":
            self.mode = "insert"
            return True
        elif key == "a":
            self.mode = "insert"
            editor.action_cursor_right()
            return True
        elif char == "A":
            self.mode = "insert"
            editor.action_cursor_line_end()
            return True
        elif char == "I":
            self.mode = "insert"
            editor.action_cursor_line_start()
            return True
        elif key == "o":
            self.mode = "insert"
            editor.action_cursor_line_end()
            editor.insert("\n")
            return True
        elif char == "O":
            self.mode = "insert"
            editor.action_cursor_line_start()
            editor.insert("\n")
            editor.action_cursor_up()
            return True
        elif key == "h" or key == "left":
            editor.action_cursor_left()
            return True
        elif key == "j" or key == "down":
            editor.action_cursor_down()
            return True
        elif key == "k" or key == "up":
            editor.action_cursor_up()
            return True
        elif key == "l" or key == "right":
            editor.action_cursor_right()
            return True
        elif key == "w":
            editor.action_cursor_word_right()
            return True
        elif key == "b":
            editor.action_cursor_word_left()
            return True
        elif key == "0" or key == "home":
            editor.action_cursor_line_start()
            return True
        elif char == "$" or key == "end":
            editor.action_cursor_line_end()
            return True
        elif key == "x":
            editor.action_delete_right()
            return True
        elif key == "d":
            self._pending = "d"
            return True
        elif char == "G":
            # Go to end of file — move to last line
            editor.action_scroll_end()
            return True
        elif key == "g":
            self._pending = "g"
            return True
        elif key == "u":
            editor.action_undo()
            return True
        elif key == "ctrl+r":
            editor.action_redo()
            return True
        elif key == "p":
            editor.action_paste()
            return True

        # Consume all other keys in normal mode to prevent typing
        return True

    def _handle_pending(self, key: str, char: str | None, editor: TextArea) -> bool:
        """Handle the second character of a multi-char command."""
        pending = self._pending
        self._pending = ""

        if pending == "d" and key == "d":
            editor.action_delete_line()
            return True
        elif pending == "g" and key == "g":
            # Go to beginning of file
            editor.action_scroll_home()
            return True

        return True  # Consume unknown combos

    def handle_key_input(self, key: str, char: str | None, inp: Input) -> bool:
        """Handle a key event for an Input widget. Returns True if consumed."""
        if self.mode == "insert":
            if key == "escape":
                self.mode = "normal"
                return True
            return False

        # ── Normal mode ──────────────────────────────────────────────
        if key == "i":
            self.mode = "insert"
            return True
        elif key == "a":
            self.mode = "insert"
            inp.action_cursor_right()
            return True
        elif char == "A":
            self.mode = "insert"
            inp.action_end()
            return True
        elif char == "I":
            self.mode = "insert"
            inp.action_home()
            return True
        elif key == "h" or key == "left":
            inp.action_cursor_left()
            return True
        elif key == "l" or key == "right":
            inp.action_cursor_right()
            return True
        elif key == "w":
            inp.action_cursor_right_word()
            return True
        elif key == "b":
            inp.action_cursor_left_word()
            return True
        elif key == "0" or key == "home":
            inp.action_home()
            return True
        elif char == "$" or key == "end":
            inp.action_end()
            return True
        elif key == "x":
            inp.action_delete_right()
            return True

        # Consume all other keys in normal mode
        return True


class VimTextArea(TextArea):
    """TextArea subclass that intercepts keys for vim mode before processing."""

    def __init__(self, vim: VimHandler, **kwargs) -> None:
        super().__init__(**kwargs)
        self._vim = vim
        self.vim_enabled = False
        self._status_callback: Callable[[], None] = lambda: None
        self._command_callback: Callable[[str], None] = lambda command: None
        self.command_mode = False
        self.command_text = ""

    async def _on_key(self, event) -> None:
        if self.vim_enabled:
            # Vim command-line mode (e.g. :q, :w, :wq)
            if self.command_mode:
                if event.key == "escape":
                    self.command_mode = False
                    self.command_text = ""
                    self._status_callback()
                    event.prevent_default()
                    event.stop()
                    return
                if event.key == "enter":
                    cmd = self.command_text.strip()
                    self.command_mode = False
                    self.command_text = ""
                    self._command_callback(cmd)
                    self._status_callback()
                    event.prevent_default()
                    event.stop()
                    return
                if event.key == "backspace":
                    self.command_text = self.command_text[:-1]
                    self._status_callback()
                    event.prevent_default()
                    event.stop()
                    return
                if event.character and event.character.isprintable():
                    self.command_text += event.character
                    self._status_callback()
                    event.prevent_default()
                    event.stop()
                    return
                event.prevent_default()
                event.stop()
                return

            # Enter command-line mode from NORMAL using ':'
            if self._vim.mode == "normal" and event.character == ":":
                self.command_mode = True
                self.command_text = ""
                self._status_callback()
                event.prevent_default()
                event.stop()
                return

            consumed = self._vim.handle_key_textarea(event.key, event.character, self)
            if consumed:
                self._status_callback()
                event.prevent_default()
                event.stop()
                return
        await super()._on_key(event)


class VimInput(Input):
    """Input subclass that intercepts keys for vim mode before processing."""

    def __init__(self, vim: VimHandler, **kwargs) -> None:
        super().__init__(**kwargs)
        self._vim = vim
        self.vim_enabled = False
        self._status_callback: Callable[[], None] = lambda: None

    async def _on_key(self, event) -> None:
        if self.vim_enabled:
            # Always let Enter through for submitting
            if event.key == "enter":
                self._vim.mode = "insert"
                self._status_callback()
            elif event.key == "escape":
                if self._vim.mode == "insert":
                    self._vim.mode = "normal"
                    self._status_callback()
                event.prevent_default()
                event.stop()
                return
            else:
                consumed = self._vim.handle_key_input(event.key, event.character, self)
                if consumed:
                    self._status_callback()
                    event.prevent_default()
                    event.stop()
                    return
        await super()._on_key(event)


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9\\s-]", "", text.lower())
    slug = re.sub(r"[\\s_]+", "-", slug).strip("-")
    return slug[:50]


# ─── TUI App ─────────────────────────────────────────────────────────────────


class PhaseHeader(Static):
    """Displays current phase and status."""

    def __init__(self) -> None:
        super().__init__("")
        self._phase = -1
        self._status = "idle"
        self._task_summary = ""

    def set_phase(self, phase: int, status: str = "running") -> None:
        self._phase = phase
        self._status = status
        self._render_header()

    def set_status(self, status: str) -> None:
        self._status = status
        self._render_header()

    def set_task(self, task: str) -> None:
        self._task_summary = task[:60] + ("..." if len(task) > 60 else "")
        self._render_header()

    def _render_header(self) -> None:
        if self._phase < 0:
            phase_text = "Starting..."
        else:
            name = PHASE_NAMES.get(self._phase, f"Phase {self._phase}")
            phase_text = f"Phase {self._phase}: {name}"

        status_icon = {
            "running": "[yellow]●[/yellow]",
            "complete": "[green]✓[/green]",
            "error": "[red]✗[/red]",
            "idle": "[dim]○[/dim]",
            "interrupted": "[yellow]◌[/yellow]",
            "waiting": "[cyan]?[/cyan]",
        }.get(self._status, "[dim]○[/dim]")

        task_line = f"  [dim]{self._task_summary}[/dim]" if self._task_summary else ""
        self.update(f" {status_icon} {phase_text}  {task_line}")


class Sidebar(Static):
    """Right sidebar showing stats, TODO progress, and modified files."""

    def __init__(self) -> None:
        super().__init__("", id="sidebar")
        self._workdir = ""
        self._branch = ""
        self._total_tokens = 0
        self._output_tokens = 0
        self._cost_usd = 0.0
        self._todo_items: list[tuple[bool, str]] = []  # (checked, text)
        self._modified_files: list[str] = []
        self._input_mode = "plain"

    def set_input_mode(self, mode: str) -> None:
        """Set input mode label for sidebar (plain/insert/normal)."""
        self._input_mode = mode
        self._rebuild()

    def set_workdir(self, workdir: str) -> None:
        self._workdir = workdir
        self._refresh_git()
        self._refresh_todo()
        self._rebuild()

    def add_tokens(self, total: int, output: int) -> None:
        """Accumulate token usage and estimate cost."""
        self._total_tokens += total
        self._output_tokens += output
        # Rough cost estimate: ~$15/M input, ~$75/M output for Opus
        input_tokens = total - output
        self._cost_usd += (input_tokens / 1_000_000) * 15.0
        self._cost_usd += (output / 1_000_000) * 75.0
        self._rebuild()

    def refresh_data(self) -> None:
        """Refresh git and TODO data from disk."""
        self._refresh_git()
        self._refresh_todo()
        self._rebuild()

    def _refresh_git(self) -> None:
        if not self._workdir:
            return
        # Branch
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=self._workdir,
            capture_output=True,
            text=True,
        )
        self._branch = result.stdout.strip() if result.returncode == 0 else ""

        # Modified files
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=self._workdir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            self._modified_files = result.stdout.strip().splitlines()[:15]
        else:
            self._modified_files = []

    def _refresh_todo(self) -> None:
        if not self._workdir:
            return
        todo_path = Path(self._workdir) / "TODO.md"
        if not todo_path.exists():
            self._todo_items = []
            return
        try:
            content = todo_path.read_text()
            items = []
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("- [x]") or line.startswith("- [X]"):
                    items.append((True, line[5:].strip()))
                elif line.startswith("- [ ]"):
                    items.append((False, line[5:].strip()))
            self._todo_items = items
        except OSError:
            self._todo_items = []

    def _rebuild(self) -> None:
        parts: list[str] = []

        # ── Directory & Branch ───
        parts.append("[bold]Directory[/bold]")
        display_dir = self._workdir
        if len(display_dir) > 28:
            display_dir = "..." + display_dir[-25:]
        parts.append(f"  [dim]{display_dir}[/dim]")
        if self._branch:
            parts.append(f"  [cyan]{self._branch}[/cyan]")

        # Input mode
        if self._input_mode == "insert":
            parts.append("  Input: [bold green]VIM INSERT[/bold green]")
        elif self._input_mode == "normal":
            parts.append("  Input: [bold yellow]VIM NORMAL[/bold yellow]")
        else:
            parts.append("  Input: [dim]PLAIN[/dim]")
        parts.append("")

        # ── Tokens & Cost ────────
        parts.append("[bold]Usage[/bold]")
        if self._total_tokens:
            parts.append(f"  Tokens: [dim]{self._total_tokens:,}[/dim]")
            parts.append(f"  Output: [dim]{self._output_tokens:,}[/dim]")
            if self._total_tokens > 0:
                # Context estimate: rough % of 200k window used
                ctx_pct = min(100, (self._total_tokens / 200_000) * 100)
                parts.append(f"  Context: [dim]~{ctx_pct:.0f}%[/dim]")
            parts.append(f"  Cost: [dim]~${self._cost_usd:.2f}[/dim]")
        else:
            parts.append("  [dim]No usage yet[/dim]")
        parts.append("")

        # ── TODO Progress ────────
        if self._todo_items:
            done = sum(1 for c, _ in self._todo_items if c)
            total = len(self._todo_items)
            parts.append(f"[bold]TODO[/bold] [dim]{done}/{total}[/dim]")
            for checked, text in self._todo_items:
                icon = "[green]x[/green]" if checked else "[dim]o[/dim]"
                label = text[:26] + ("..." if len(text) > 26 else "")
                if checked:
                    parts.append(f"  {icon} [dim]{label}[/dim]")
                else:
                    parts.append(f"  {icon} {label}")
            parts.append("")

        # ── Modified Files ───────
        if self._modified_files:
            parts.append(
                f"[bold]Modified[/bold] [dim]{len(self._modified_files)}[/dim]"
            )
            for line in self._modified_files:
                # git status --short gives "XY filename"
                status = line[:2]
                fname = line[3:].strip()
                if len(fname) > 24:
                    fname = "..." + fname[-21:]
                color = "green" if "A" in status or "?" in status else "yellow"
                parts.append(f"  [{color}]{status}[/{color}] [dim]{fname}[/dim]")

        self.update("\n".join(parts))


class OrchestratorApp(App):
    """Interactive TUI for the Agent Orchestrator pipeline."""

    TITLE = "Agent Orchestrator"
    CSS = """
    PhaseHeader {
        dock: top;
        height: 1;
        background: $surface;
        color: $text;
        padding: 0 1;
    }

    #main-area {
        height: 1fr;
        layout: horizontal;
    }

    #left-pane {
        width: 1fr;
    }

    #output-log {
        height: 1fr;
        border: round $primary;
        scrollbar-size: 1 1;
    }

    #sidebar {
        width: 32;
        border: round $primary;
        padding: 1;
        overflow-y: auto;
    }

    #message-input {
        dock: bottom;
        height: 8;
        margin: 0 0;
    }

    #input-mode {
        dock: bottom;
        height: 1;
        display: none;
        background: $surface;
        color: $text;
        padding: 0 1;
    }

    #slash-menu {
        dock: bottom;
        height: auto;
        max-height: 10;
        margin: 0 0 3 0;
        display: none;
        border: round $accent;
        background: $surface;
    }

    #config-editor {
        height: 1fr;
        display: none;
        border: round $accent;
    }

    #config-status {
        dock: bottom;
        height: 1;
        display: none;
        background: $accent;
        color: $text;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "interrupt", "Interrupt agent", show=True),
        Binding("ctrl+c", "quit_app", "Quit", show=True),
    ]

    def __init__(
        self,
        task: str | None = None,
        workdir: str | None = None,
        config_path: str | None = None,
    ) -> None:
        super().__init__()
        self.user_task = task or ""
        self.workdir = os.path.abspath(workdir) if workdir else os.path.abspath(".")
        self.config = load_config(config_path)
        self.t = Theme(self.config)
        self.session_id: str | None = None
        self.current_process: subprocess.Popen | None = None
        self.message_queue: deque[str] = deque()
        self.agent_running = False
        self.current_phase = -1
        self.pipeline_cancelled = False
        self._needs_startup = not task  # True if we need to prompt for task
        self._local_mode = False  # Set to True if no origin remote
        self._editing_config = False
        self._config_path = str(Path(config_path) if config_path else DEFAULT_CONFIG)
        self._vim_enabled = self.config.get("editor", {}).get("vim_mode", False)
        self._vim = VimHandler()
        self._waiting_for_config_description = False
        self._input_height = self._clamp_input_height(
            self.config.get("ui", {}).get("input_height", 8)
        )

    def _clamp_input_height(self, value: object) -> int:
        """Clamp configured input height to a safe range."""
        try:
            n = int(str(value))
        except (TypeError, ValueError):
            n = 8
        return max(3, min(15, n))

    def _apply_ui_settings(self) -> None:
        """Apply UI layout settings from config."""
        self._input_height = self._clamp_input_height(
            self.config.get("ui", {}).get("input_height", 8)
        )
        inp = self.query_one("#message-input", VimInput)
        inp.styles.height = self._input_height
        menu = self.query_one("#slash-menu", OptionList)
        menu.styles.margin = (0, 0, self._input_height, 0)

    def _ensure_git_repo(self) -> None:
        """Ensure the workdir exists and is a git repository."""
        path = Path(self.workdir)
        if not path.exists():
            path.mkdir(parents=True)
        git_dir = path / ".git"
        if not git_dir.exists():
            subprocess.run(
                ["git", "init"], cwd=self.workdir, capture_output=True, text=True
            )

    def _has_origin_remote(self) -> bool:
        """Check if the git repo has an 'origin' remote."""
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=self.workdir,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def _git_branch_exists(self, branch: str) -> bool:
        """Return True if a local branch exists."""
        if not branch:
            return False
        result = subprocess.run(
            ["git", "show-ref", "--verify", f"refs/heads/{branch}"],
            cwd=self.workdir,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def _current_branch(self) -> str:
        """Get currently checked-out branch name."""
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=self.workdir,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def _resolve_base_branch(self, preferred: str) -> str:
        """Resolve a usable base branch for local operations."""
        if self._git_branch_exists(preferred):
            return preferred
        current = self._current_branch()
        if current:
            return current
        for fallback in ("main", "master"):
            if self._git_branch_exists(fallback):
                return fallback
        return preferred or "main"

    def compose(self) -> ComposeResult:
        yield Header()
        yield PhaseHeader()
        with Horizontal(id="main-area"):
            with Vertical(id="left-pane"):
                yield RichLog(id="output-log", highlight=True, markup=True, wrap=True)
                yield VimTextArea(
                    self._vim, id="config-editor", language="yaml", theme="monokai"
                )
                yield Static(
                    " Ctrl+S save  |  Ctrl+D discard  |  Escape cancel",
                    id="config-status",
                )
            yield Sidebar()
        yield OptionList(id="slash-menu")
        yield Static("", id="input-mode")
        yield VimInput(
            self._vim,
            placeholder="Type a message (Enter to send, Escape to interrupt)...",
            id="message-input",
        )
        yield Footer()

    def _sync_vim_state(self) -> None:
        """Sync vim enabled state to all vim-aware widgets."""
        editor = self.query_one("#config-editor", VimTextArea)
        editor.vim_enabled = self._vim_enabled
        editor._status_callback = self._update_config_status
        editor._command_callback = self._handle_vim_command
        editor.cursor_blink = not self._vim_enabled
        inp = self.query_one("#message-input", VimInput)
        inp.vim_enabled = self._vim_enabled
        inp._status_callback = self._update_input_mode_status
        self._update_input_mode_status()

    def _update_input_mode_status(self) -> None:
        """Show visual cue for vim mode on the message input."""
        status = self.query_one("#input-mode", Static)
        inp = self.query_one("#message-input", VimInput)
        sidebar = self.query_one(Sidebar)

        if not self._vim_enabled:
            status.styles.display = "block"
            inp.cursor_blink = True
            status.update(
                " [bold cyan]INPUT PLAIN[/bold cyan]  [dim]Type normally[/dim]"
            )
            sidebar.set_input_mode("plain")
            return

        status.styles.display = "block"
        inp.cursor_blink = self._vim.mode == "insert"

        if self._vim.mode == "insert":
            status.update(
                " [bold green]VIM INSERT[/bold green]  [dim]Esc for NORMAL[/dim]"
            )
            sidebar.set_input_mode("insert")
        else:
            status.update(
                " [bold yellow]VIM NORMAL[/bold yellow]  [dim]i/a to INSERT[/dim]"
            )
            sidebar.set_input_mode("normal")

    def on_mount(self) -> None:
        log = self.query_one("#output-log", RichLog)
        sidebar = self.query_one(Sidebar)
        sidebar.set_workdir(self.workdir)
        self._apply_ui_settings()
        self._sync_vim_state()
        if self._vim_enabled:
            # Default to INSERT mode for chat input on startup.
            self._vim.mode = "insert"
            self._update_input_mode_status()

        if self._needs_startup:
            # Interactive startup — prompt for task
            log.write(
                f"{self.t.s('accent', 'Agent Orchestrator', bold=True)}\n"
                f"{self.t.s('accent', 'Working dir:', bold=True)} {self.workdir}\n"
                f"{self.t.s('accent', 'Models:', bold=True)} "
                f"{self.config['models']['feature']} / {self.config['models']['refactor']}\n"
            )
            log.write(
                f"\n{self.t.s('info', 'What task would you like to implement?')}\n"
                "[dim]Type your task description below and press Enter.[/dim]\n"
                "[dim]Use /help for available commands.[/dim]\n"
            )
            inp = self.query_one("#message-input", Input)
            inp.placeholder = "Describe your task..."
            inp.focus()
            self._startup_phase = "task"  # Expecting task input
        else:
            self._startup_phase = None
            self._start_pipeline()

    def _start_pipeline(self) -> None:
        """Display task info and kick off the pipeline."""
        log = self.query_one("#output-log", RichLog)
        log.write(
            f"{self.t.s('accent', 'Task:', bold=True)} {self.user_task}\n"
            f"{self.t.s('accent', 'Working dir:', bold=True)} {self.workdir}\n"
            f"{self.t.s('accent', 'Models:', bold=True)} "
            f"{self.config['models']['feature']} / {self.config['models']['refactor']}\n"
        )
        header = self.query_one(PhaseHeader)
        header.set_task(self.user_task)
        self._ensure_git_repo()

        # Refresh sidebar with possibly new workdir
        sidebar = self.query_one(Sidebar)
        sidebar.set_workdir(self.workdir)

        inp = self.query_one("#message-input", Input)
        inp.placeholder = "Type a message (Enter to send, Escape to interrupt)..."
        inp.focus()

        self.run_pipeline()

    def action_interrupt(self) -> None:
        """Interrupt the currently running agent."""
        if self._editing_config:
            return
        if self.current_process and self.agent_running:
            log = self.query_one("#output-log", RichLog)
            log.write("\n[yellow bold]Interrupting agent...[/yellow bold]")
            try:
                self.current_process.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
            header = self.query_one(PhaseHeader)
            header.set_status("interrupted")

    def action_quit_app(self) -> None:
        """Clean quit — kill any running process and exit."""
        self.pipeline_cancelled = True
        if self.current_process:
            try:
                self.current_process.kill()
            except ProcessLookupError:
                pass
        # Clean up TODO.md
        todo_path = Path(self.workdir) / self.config["orchestrator"]["todo_file"]
        if todo_path.exists():
            todo_path.unlink()
        self.exit()

    # ─── Agent Runner ────────────────────────────────────────────────────

    @work(thread=True)
    def run_agent_worker(self, prompt: str, model: str, timeout: int = 600) -> dict:
        """Run opencode in a background thread, streaming events to the log."""
        cmd = ["opencode", "run", "--format", "json", "--model", model]

        # Continue session if we have one
        if self.session_id:
            cmd.extend(["--session", self.session_id])

        cmd.append(prompt)

        self.agent_running = True
        self.app.call_from_thread(self._log_write, f"\n[dim]Running {model}...[/dim]")
        self.app.call_from_thread(
            self._log_write,
            f"[dim]Prompt: {prompt[:100]}{'...' if len(prompt) > 100 else ''}[/dim]\n",
        )

        process = subprocess.Popen(
            cmd,
            cwd=self.workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.current_process = process

        text_parts: list[str] = []

        assert process.stdout is not None

        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)

                # Capture session ID
                if not self.session_id:
                    sid = event.get("sessionID")
                    if sid:
                        self.session_id = sid

                self._format_event_to_log(event, text_parts)

            except json.JSONDecodeError:
                self.app.call_from_thread(self._log_write, line)

        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            self.app.call_from_thread(
                self._log_write,
                f"\n[red bold]Agent timed out after {timeout}s[/red bold]",
            )

        self.current_process = None
        self.agent_running = False

        returncode = process.returncode
        stdout_text = "\n".join(text_parts)

        # Read any stderr
        stderr_text = ""
        if process.stderr:
            stderr_text = process.stderr.read()

        if returncode == 0:
            self.app.call_from_thread(
                self._log_write, "\n[green]Agent completed.[/green]"
            )
        elif returncode and returncode < 0:
            # Killed by signal (e.g., our SIGINT)
            self.app.call_from_thread(
                self._log_write, "\n[yellow]Agent interrupted.[/yellow]"
            )
        else:
            self.app.call_from_thread(
                self._log_write,
                f"\n[red]Agent exited with code {returncode}[/red]",
            )
            if stderr_text:
                self.app.call_from_thread(
                    self._log_write, f"[red]{stderr_text[:500]}[/red]"
                )

        # Refresh sidebar after every agent run (TODO progress, modified files)
        self.app.call_from_thread(self._refresh_sidebar)

        return {
            "returncode": returncode or 0,
            "stdout": stdout_text,
            "stderr": stderr_text,
        }

    def _format_event_to_log(self, event: dict, text_parts: list[str]) -> None:
        """Format a JSON event and write it to the log."""
        t = self.t
        part = event.get("part", {})
        event_type = part.get("type", "")

        if event_type == "step-start":
            self.app.call_from_thread(self._log_write, t.s("muted", "───"))

        elif event_type == "tool":
            tool_name = part.get("tool", "unknown")
            state = part.get("state", {})
            title = state.get("title", "")
            status = state.get("status", "")

            if title:
                self.app.call_from_thread(
                    self._log_write,
                    f"  {t.s('info', tool_name, bold=True)} {t.s('muted', title)}",
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
                self.app.call_from_thread(
                    self._log_write,
                    f"  {t.s('info', tool_name, bold=True)} {t.s('muted', input_summary)}",
                )

            if status == "error":
                error = state.get("error", "unknown error")
                self.app.call_from_thread(self._log_write, f"  {t.s('error', error)}")

        elif event_type == "text":
            text = part.get("text", "")
            if text.strip():
                text_parts.append(text)
                self.app.call_from_thread(self._log_write, text)

        elif event_type == "step-finish":
            tokens = part.get("tokens", {})
            total = tokens.get("total", 0)
            output = tokens.get("output", 0)
            if total:
                self.app.call_from_thread(
                    self._log_write,
                    t.s("muted", f"  tokens: {total:,} total, {output:,} output"),
                )
                self.app.call_from_thread(self._update_sidebar_tokens, total, output)

    def _log_write(self, text: str) -> None:
        """Write to the output log (must be called from main thread)."""
        log = self.query_one("#output-log", RichLog)
        log.write(text)

    def _update_sidebar_tokens(self, total: int, output: int) -> None:
        """Update sidebar with new token data (must be called from main thread)."""
        sidebar = self.query_one(Sidebar)
        sidebar.add_tokens(total, output)

    def _refresh_sidebar(self) -> None:
        """Refresh sidebar git/TODO data (must be called from main thread)."""
        sidebar = self.query_one(Sidebar)
        sidebar.refresh_data()

    def _process_queue(self) -> None:
        """Send queued messages to the agent."""
        if not self.message_queue or self.agent_running:
            return

        message = self.message_queue.popleft()
        model = self.config["models"]["feature"]
        self.run_agent_worker(message, model)

    # ─── Pipeline ────────────────────────────────────────────────────────

    @work(thread=False)
    async def run_pipeline(self) -> None:
        """Execute the full orchestrator pipeline as async tasks."""
        header = self.query_one(PhaseHeader)

        try:
            # Check for origin remote before starting
            if not self._has_origin_remote():
                self._log_write(
                    f"\n{self.t.s('warning', 'No origin remote configured.', bold=True)}\n"
                    "[dim]A remote is needed for pushing branches and creating PRs.[/dim]\n"
                    "[dim]Example: https://github.com/user/repo.git[/dim]\n"
                    "[dim]Enter a remote URL below, or type 'skip' to work locally.[/dim]\n"
                )
                url = await self._wait_for_input()
                if url and url.lower() != "skip":
                    result = subprocess.run(
                        ["git", "remote", "add", "origin", url],
                        cwd=self.workdir,
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode == 0:
                        self._log_write(
                            self.t.s("success", f"Remote 'origin' set to {url}")
                        )
                    else:
                        self._log_write(
                            f"[red]Failed to add remote: {result.stderr}[/red]"
                        )
                else:
                    self._local_mode = True
                    self._log_write(
                        "[yellow]Local mode — branches and reviews will happen locally.[/yellow]"
                    )

            # Phase 0: Clarifying questions
            self.current_phase = 0
            header.set_phase(0, "running")
            enriched_task = await self._phase_clarify()
            if self.pipeline_cancelled:
                return
            header.set_phase(0, "complete")

            # Phase 1: Decompose
            self.current_phase = 1
            header.set_phase(1, "running")
            todo_content = await self._phase_decompose(enriched_task)
            if self.pipeline_cancelled:
                return
            header.set_phase(1, "complete")

            # Phase 2: Implement
            self.current_phase = 2
            header.set_phase(2, "running")
            await self._phase_implement(enriched_task, todo_content)
            if self.pipeline_cancelled:
                return
            header.set_phase(2, "complete")

            # Process any queued messages between phases
            await self._drain_queue()

            # Phase 3: Refactor
            self.current_phase = 3
            header.set_phase(3, "running")
            await self._phase_refactor()
            if self.pipeline_cancelled:
                return
            header.set_phase(3, "complete")

            # Phase 4: Ship
            self.current_phase = 4
            header.set_phase(4, "running")
            pr_url = await self._phase_ship(enriched_task)
            if self.pipeline_cancelled:
                return
            if pr_url is None:
                self._log_write(
                    "[yellow]No changes produced. Pipeline complete.[/yellow]"
                )
                header.set_phase(4, "complete")
                return
            header.set_phase(4, "complete")

            # Phase 5: Review
            self.current_phase = 5
            header.set_phase(5, "running")
            await self._phase_review(enriched_task, pr_url)
            if self.pipeline_cancelled:
                return
            header.set_phase(5, "complete")

            # Phase 6: Human approval
            self.current_phase = 6
            header.set_phase(6, "waiting")
            if self._local_mode:
                branch = self._ship_branch
                base = self._base_branch
                self._log_write(
                    f"\n[bold]Branch ready for review:[/bold] {branch}\n"
                    f"Type [bold green]merge[/bold green] to merge into {base}, "
                    "[bold yellow]skip[/bold yellow] to leave branch as-is, or "
                    "[bold red]abort[/bold red] to delete the branch."
                )
            else:
                self._log_write(
                    f"\n[bold]PR ready for review:[/bold] {pr_url}\n"
                    "Type [bold green]merge[/bold green], "
                    "[bold yellow]skip[/bold yellow], or "
                    "[bold red]abort[/bold red] in the input box."
                )
            # Human approval is handled via message input — we wait here
            self._waiting_for_approval = True
            self._pr_url = pr_url

        except Exception as e:
            header.set_status("error")
            self._log_write(f"\n[red bold]Pipeline error: {e}[/red bold]")
        finally:
            # Clean up TODO.md
            todo_path = Path(self.workdir) / self.config["orchestrator"]["todo_file"]
            if todo_path.exists():
                todo_path.unlink()
                self._log_write(f"[dim]Cleaned up {todo_path.name}[/dim]")

    async def _run_agent_and_wait(
        self, prompt: str, model: str, timeout: int = 600
    ) -> dict:
        """Run an agent and wait for it to complete. Returns result dict."""
        worker = self.run_agent_worker(prompt, model, timeout)

        # Wait for the worker to complete
        while worker.state not in (
            WorkerState.SUCCESS,
            WorkerState.ERROR,
            WorkerState.CANCELLED,
        ):
            await asyncio.sleep(0.1)

        if worker.state == WorkerState.SUCCESS and worker.result is not None:
            return worker.result
        elif worker.state == WorkerState.CANCELLED:
            return {"returncode": -1, "stdout": "", "stderr": "CANCELLED"}
        else:
            return {"returncode": 1, "stdout": "", "stderr": "WORKER_ERROR"}

    async def _drain_queue(self) -> None:
        """Process all queued messages."""
        while self.message_queue and not self.pipeline_cancelled:
            message = self.message_queue.popleft()
            model = self.config["models"]["feature"]
            self._log_write(
                f"\n[bold green]Processing queued message:[/bold green] {message}"
            )
            await self._run_agent_and_wait(message, model)

    # ─── Phase Implementations ───────────────────────────────────────────

    async def _phase_clarify(self) -> str:
        """Phase 0: Generate and ask clarifying questions."""
        self._log_write("\n[bold]── Phase 0: Clarifying Questions ──[/bold]\n")

        prompt = (
            "You are a technical project planner about to implement a task. "
            "Before starting, look at the project structure and files to understand "
            "the existing codebase, then generate 2-4 short, specific clarifying "
            "questions that would help you implement it better.\n\n"
            f"Task: {self.user_task}\n\n"
            "Output ONLY the numbered questions, one per line, like:\n"
            "1. Question here?\n"
            "2. Another question?\n\n"
            "Do NOT include any other text, preamble, or explanation."
        )

        model = self.config["models"]["feature"]
        result = await self._run_agent_and_wait(prompt, model, timeout=120)

        # Parse questions from output
        questions = re.findall(r"^\s*\d+[\.\)]\s*(.+)", result["stdout"], re.MULTILINE)

        if not questions:
            self._log_write(
                "[dim]Could not generate clarifying questions. "
                "Proceeding with original task.[/dim]"
            )
            return self.user_task

        self._log_write(
            f"\n  Generated {len(questions)} clarifying question(s).\n"
            '  [dim]Answer in the input box below. Type "skip" to skip all,[/dim]\n'
            '  [dim]"own" for freeform context, or leave empty to skip a question.[/dim]\n'
        )

        answers = []
        freeform_text = None

        for i, question in enumerate(questions, 1):
            if self.pipeline_cancelled:
                break

            self._log_write(f"  [bold]Q{i}:[/bold] {question}")

            # Wait for user input via the Input widget
            response = await self._wait_for_input()

            if response.lower() == "skip":
                self._log_write("  [dim]Skipping remaining questions.[/dim]")
                break
            elif response.lower() == "own":
                self._log_write(
                    "\n  [bold]Provide your own context[/bold] "
                    '[dim](type your context, then send "done" to finish):[/dim]'
                )
                lines = []
                while True:
                    line = await self._wait_for_input()
                    if line.lower() == "done" or line == "":
                        break
                    lines.append(line)
                if lines:
                    freeform_text = "\n".join(lines)
                break
            elif response:
                answers.append((question, response))

        if not answers and not freeform_text:
            self._log_write(
                "[dim]No additional context provided. "
                "Proceeding with original task.[/dim]"
            )
            return self.user_task

        enriched = self.user_task
        if answers:
            enriched += "\n\nClarifications:"
            for q, a in answers:
                enriched += f"\n  Q: {q}\n  A: {a}"
        if freeform_text:
            enriched += f"\n\nAdditional context from user:\n{freeform_text}"

        self._log_write("\n[green]Task enriched with user context.[/green]")
        return enriched

    async def _phase_decompose(self, task: str) -> str:
        """Phase 1: Generate plan, get approval, then produce TODO.md."""
        self._log_write("\n[bold]── Phase 1: Planning & Decomposition ──[/bold]\n")

        model = self.config["models"]["feature"]
        orchestrator_cfg = self.config.get("orchestrator", {})
        require_plan_approval = self.config.get("orchestrator", {}).get(
            "plan_approval", True
        )
        quality_check = orchestrator_cfg.get("plan_quality_check", True)
        quality_mode = str(
            orchestrator_cfg.get("plan_quality_mode", "balanced")
        ).lower()
        quality_retries = max(0, int(orchestrator_cfg.get("plan_quality_retries", 2)))
        auto_clear_on_bad_plan = orchestrator_cfg.get(
            "auto_clear_context_on_bad_plan", True
        )

        # 1) Generate a human-readable plan for approval with quality guardrails.
        planning_prompt = (
            "You are a senior engineer creating an execution plan before coding.\n\n"
            f"Task: {task}\n\n"
            "Create a concise implementation plan with:\n"
            "- 5-9 numbered steps\n"
            "- Key risks/assumptions\n"
            "- Test/verification strategy\n\n"
            "Hard constraints:\n"
            "- Focus ONLY on software implementation for this task\n"
            "- Never include essays, history, trivia, or general-knowledge writing\n"
            "- Keep every step directly relevant to code changes in this repo\n\n"
            "Output plain text only (no markdown code fences)."
        )

        approved_plan = ""
        quality_ok = True
        quality_reasons: list[str] = []

        for attempt in range(quality_retries + 1):
            current_prompt = planning_prompt
            if attempt > 0:
                reason_text = "; ".join(quality_reasons) or "plan was low relevance"
                current_prompt = (
                    planning_prompt
                    + "\n\nPrevious attempt was rejected for quality reasons:\n"
                    + f"- {reason_text}\n"
                    + "Regenerate a stricter, task-relevant software plan."
                )

            plan_result = await self._run_agent_and_wait(
                current_prompt, model, timeout=180
            )
            approved_plan = plan_result.get("stdout", "").strip()

            quality_ok, quality_reasons = self._validate_plan_quality(
                task, approved_plan
            )

            if not quality_check or quality_ok:
                break

            self._log_write(
                "[yellow]Plan Quality: FAIL[/yellow] "
                + (
                    f"([dim]{'; '.join(quality_reasons)}[/dim])"
                    if quality_reasons
                    else ""
                )
            )

            if auto_clear_on_bad_plan and self.session_id:
                self.session_id = None
                self._log_write(
                    "[dim]Cleared session context before plan regeneration.[/dim]"
                )

            if attempt < quality_retries:
                self._log_write("[dim]Regenerating plan...[/dim]")

        if quality_check:
            if quality_ok:
                self._log_write("[green]Plan Quality: PASS[/green]")
            else:
                self._log_write(
                    "[yellow]Plan Quality: FAIL after retries[/yellow] "
                    + (
                        f"([dim]{'; '.join(quality_reasons)}[/dim])"
                        if quality_reasons
                        else ""
                    )
                )
                if quality_mode == "strict":
                    raise RuntimeError(
                        "Plan failed quality checks in strict mode. "
                        "Revise task/context and retry."
                    )

        # 2) Ask for approval/revision (Claude Code style)
        if require_plan_approval:
            self._log_write("\n[bold cyan]Proposed plan:[/bold cyan]")
            self._log_write(approved_plan or "[dim](No plan text returned)[/dim]")
            self._log_write(
                "\nType [bold green]approve[/bold green] to continue, "
                "[bold yellow]revise[/bold yellow] to improve the plan, "
                "or [bold red]skip[/bold red] to continue as-is."
            )

            while True:
                response = (await self._wait_for_input()).strip()
                low = response.lower()

                if low in {"approve", "a", "yes", "y"}:
                    self._log_write("[green]Plan approved.[/green]")
                    break

                if low in {"skip", "s"}:
                    self._log_write(
                        "[yellow]Skipping plan approval; proceeding.[/yellow]"
                    )
                    break

                if low.startswith("revise") or low in {"r", "edit"}:
                    feedback = ""
                    if low.startswith("revise "):
                        feedback = response[7:].strip()
                    if not feedback:
                        self._log_write("[dim]Enter plan feedback:[/dim]")
                        feedback = (await self._wait_for_input()).strip()
                    if not feedback:
                        self._log_write(
                            "[dim]No feedback provided. Plan unchanged.[/dim]"
                        )
                        continue

                    revise_prompt = (
                        "Revise this implementation plan based on user feedback.\n\n"
                        f"Task: {task}\n\n"
                        f"Current plan:\n{approved_plan}\n\n"
                        f"User feedback:\n{feedback}\n\n"
                        "Return only the revised plan as plain text."
                    )
                    revised = await self._run_agent_and_wait(
                        revise_prompt, model, timeout=180
                    )
                    approved_plan = revised.get("stdout", "").strip() or approved_plan
                    if quality_check:
                        revised_ok, revised_reasons = self._validate_plan_quality(
                            task, approved_plan
                        )
                        if revised_ok:
                            self._log_write("[green]Plan Quality: PASS[/green]")
                        else:
                            self._log_write(
                                "[yellow]Plan Quality: FAIL[/yellow] "
                                + (
                                    f"([dim]{'; '.join(revised_reasons)}[/dim])"
                                    if revised_reasons
                                    else ""
                                )
                            )
                    self._log_write("\n[bold cyan]Revised plan:[/bold cyan]")
                    self._log_write(approved_plan)
                    self._log_write(
                        "\nType [bold green]approve[/bold green], "
                        "[bold yellow]revise[/bold yellow], or [bold red]skip[/bold red]."
                    )
                    continue

                self._log_write("[dim]Please type approve, revise, or skip.[/dim]")

        # 3) Convert approved plan into TODO.md used by implementation phase
        todo_prompt = (
            "You are a technical project planner. Convert the approved plan into "
            "a clear, actionable TODO.md file.\n\n"
            f"Task: {task}\n\n"
            f"Approved Plan:\n{approved_plan}\n\n"
            "Write a TODO.md file in the project root with:\n"
            "- A title describing the feature\n"
            "- Checkboxes for each subtask (use - [ ] format)\n"
            "- Subtasks ordered by dependency\n"
            "- Each subtask small enough for a single implementation pass\n"
            '- Include a final item for "Run tests and verify"\n\n'
            "Write ONLY the TODO.md file, nothing else."
        )

        result = await self._run_agent_and_wait(todo_prompt, model)

        todo_path = Path(self.workdir) / self.config["orchestrator"]["todo_file"]
        if todo_path.exists():
            self._log_write(self.t.s("success", f"TODO.md created at {todo_path}"))
            with open(todo_path) as f:
                content = f.read()
            self._log_write(content)
            return content

        self._log_write("[yellow]Creating TODO.md from agent output...[/yellow]")
        with open(todo_path, "w") as f:
            f.write(result.get("stdout", ""))
        return result.get("stdout", "")

    def _validate_plan_quality(self, task: str, plan: str) -> tuple[bool, list[str]]:
        """Heuristic check for off-topic / low-quality plans."""
        cfg = self.config.get("orchestrator", {})
        off_topic = cfg.get(
            "plan_offtopic_keywords",
            [
                "history of chess",
                "500-word",
                "500 word",
                "essay",
                "poem",
                "biography",
                "trivia",
                "wikipedia",
                "middle ages",
                "roman empire",
            ],
        )

        text = (plan or "").strip().lower()
        reasons: list[str] = []

        if not text:
            return False, ["empty plan"]

        for keyword in off_topic:
            key = str(keyword).strip().lower()
            if key and key in text:
                reasons.append(f"off-topic keyword: {key}")

        if not re.search(r"^\s*\d+[\.)]\s+", plan or "", re.MULTILINE):
            reasons.append("missing numbered steps")

        actionable = [
            "implement",
            "update",
            "add",
            "refactor",
            "test",
            "verify",
            "fix",
            "integrate",
            "create",
            "modify",
        ]
        if not any(word in text for word in actionable):
            reasons.append("low actionable signal")

        task_terms = {
            token
            for token in re.findall(r"[a-zA-Z0-9_\-]{4,}", task.lower())
            if token not in {"with", "from", "that", "this", "into", "your"}
        }
        plan_terms = set(re.findall(r"[a-zA-Z0-9_\-]{4,}", text))
        overlap = len(task_terms & plan_terms)
        if task_terms and overlap < max(1, min(3, len(task_terms) // 5)):
            reasons.append("low overlap with task terms")

        return len(reasons) == 0, reasons

    async def _phase_implement(self, task: str, todo_content: str) -> None:
        """Phase 2: Feature implementation."""
        self._log_write("\n[bold]── Phase 2: Feature Implementation ──[/bold]\n")

        prompt = (
            "You are implementing a feature. Here is the task and plan:\n\n"
            f"TASK: {task}\n\n"
            f"TODO PLAN:\n{todo_content}\n\n"
            "Implement all the subtasks in the TODO. For each completed subtask, "
            "update TODO.md by checking off the box (change - [ ] to - [x]).\n\n"
            "Work through each item methodically. Write clean, well-structured code. "
            "Make sure everything compiles/runs correctly before finishing."
        )

        model = self.config["models"]["feature"]
        result = await self._run_agent_and_wait(prompt, model)

        if result["returncode"] != 0 and result["returncode"] != -1:
            raise RuntimeError(
                f"Feature implementation failed: {result['stderr'][:300]}"
            )

        self._log_write(self.t.s("success", "Phase 2 complete."))

    async def _phase_refactor(self) -> None:
        """Phase 3: Cold refactor with zero context."""
        self._log_write("\n[bold]── Phase 3: Cold Refactor ──[/bold]\n")

        # Reset session for cold refactor — fresh eyes
        old_session = self.session_id
        self.session_id = None

        prompt = (
            "Review the current state of this codebase. Refactor for:\n"
            "- Code readability and clarity\n"
            "- Consistent naming conventions\n"
            "- DRY principles - remove duplication\n"
            "- Proper error handling\n"
            "- Performance improvements where obvious\n\n"
            "Do NOT change functionality. Do NOT add new features. "
            "Only refactor existing code.\n"
            "Focus on recently modified files (check git status for changed files).\n"
            "Do not touch test files unless they have clear code quality issues."
        )

        model = self.config["models"]["refactor"]
        result = await self._run_agent_and_wait(prompt, model)

        if result["returncode"] != 0 and result["returncode"] != -1:
            raise RuntimeError(f"Refactor phase failed: {result['stderr'][:300]}")

        # Restore session for continuity
        self.session_id = old_session
        self._log_write(self.t.s("success", "Phase 3 complete."))

    async def _phase_ship(self, task: str) -> str | None:
        """Phase 4: Branch, commit, and optionally push + create PR."""
        from datetime import datetime

        self._log_write("\n[bold]── Phase 4: Ship ──[/bold]\n")

        slug = slugify(task)
        prefix = self.config["branching"]["prefix"]
        sep = self.config["branching"]["separator"]
        timestamp = datetime.now().strftime("%m%d")
        branch_name = f"{prefix}{sep}{slug}-{timestamp}"
        configured_base = self.config["pr"]["base_branch"]
        base_branch = self._resolve_base_branch(configured_base)
        if configured_base != base_branch:
            self._log_write(
                f"[yellow]Configured base branch '{configured_base}' not found; "
                f"using '{base_branch}' instead.[/yellow]"
            )

        # Store branch name for local merge later
        self._ship_branch = branch_name
        self._base_branch = base_branch

        # Create branch
        self._log_write(f"[dim]Creating branch: {branch_name}[/dim]")
        result = subprocess.run(
            ["git", "checkout", "-b", branch_name],
            cwd=self.workdir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create branch: {result.stderr}")

        # Stage all changes
        subprocess.run(
            ["git", "add", "-A"], cwd=self.workdir, capture_output=True, text=True
        )

        # Check for changes
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.workdir,
            capture_output=True,
            text=True,
        )
        if not status.stdout.strip():
            self._log_write("[yellow]No changes to commit.[/yellow]")
            return None

        # Commit
        commit_msg = f"feat: {task[:72]}"
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=self.workdir,
            capture_output=True,
            text=True,
        )
        self._log_write(f"[dim]Committed: {commit_msg}[/dim]")

        if self._local_mode:
            self._log_write(
                f"[dim]Branch {branch_name} created and committed locally.[/dim]"
            )
            return f"local:{branch_name}"

        # Push
        result = subprocess.run(
            ["git", "push", "-u", "origin", branch_name],
            cwd=self.workdir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to push: {result.stderr}")
        self._log_write(f"[dim]Pushed to origin/{branch_name}[/dim]")

        # Create PR
        draft_flag = ["--draft"] if self.config["pr"].get("draft") else []
        pr_result = subprocess.run(
            [
                "gh",
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
            cwd=self.workdir,
            capture_output=True,
            text=True,
        )

        if pr_result.returncode != 0:
            raise RuntimeError(f"Failed to create PR: {pr_result.stderr}")

        pr_url = pr_result.stdout.strip()
        self._log_write(self.t.s("success", f"PR created: {pr_url}", bold=True))
        return pr_url

    async def _phase_review(self, task: str, pr_url: str) -> None:
        """Phase 5: PR review (remote PR or local diff)."""
        self._log_write("\n[bold]── Phase 5: PR Review ──[/bold]\n")

        # Fresh session for review
        old_session = self.session_id
        self.session_id = None

        test_cmd = self.config["tests"]["command"]
        base_branch = getattr(self, "_base_branch", self.config["pr"]["base_branch"])

        if self._local_mode:
            prompt = (
                "You are a code reviewer. Review the changes on the current branch.\n\n"
                f"TASK: {task}\n\n"
                "Do the following:\n"
                f"1. Review the diff against {base_branch} using: git diff {base_branch}...HEAD\n"
                f"2. Run the test suite with: {test_cmd}\n"
                "3. Check for test parity — are all existing tests still passing? "
                "Are there new tests for new functionality?\n"
                "4. Summarize findings clearly\n"
                "5. Print a clear RECOMMENDATION: APPROVE or REQUEST_CHANGES\n\n"
                "Format your output clearly so it's easy to read."
            )
        else:
            prompt = (
                "You are a code reviewer. A PR has been created for the following task:\n\n"
                f"TASK: {task}\n"
                f"PR: {pr_url}\n\n"
                "Do the following:\n"
                "1. Review the PR diff using `gh pr diff`\n"
                f"2. Run the test suite with: {test_cmd}\n"
                "3. Check for test parity\n"
                "4. Summarize findings clearly\n"
                "5. Print a clear RECOMMENDATION: APPROVE or REQUEST_CHANGES\n\n"
                "Format your output clearly so it's easy to read."
            )

        model = self.config["models"]["review"]
        await self._run_agent_and_wait(prompt, model)

        self.session_id = old_session
        self._log_write(self.t.s("success", "Phase 5 complete."))

    # ─── Input Handling ──────────────────────────────────────────────────

    _input_future: asyncio.Future | None = None
    _waiting_for_approval: bool = False
    _pr_url: str | None = None

    async def _wait_for_input(self) -> str:
        """Block until the user submits input. Returns the input text."""
        self._input_future = asyncio.get_event_loop().create_future()
        result = await self._input_future
        self._input_future = None
        return result

    def _update_slash_menu(self, value: str) -> None:
        """Show/hide the slash command menu based on input value."""
        menu = self.query_one("#slash-menu", OptionList)

        if value.startswith("/"):
            query = value.lower()

            def score(cmd: str) -> tuple[int, int, int]:
                """Rank command similarity to query. Lower is better."""
                if cmd == query:
                    return (0, 0, len(cmd))
                if cmd.startswith(query):
                    # Prefer the shortest completion when multiple commands share prefix.
                    return (1, len(cmd) - len(query), len(cmd))
                if query in cmd:
                    return (2, cmd.index(query), len(cmd))
                return (99, 99, len(cmd))

            matches = [
                (cmd, desc) for cmd, desc in SLASH_COMMANDS if score(cmd)[0] < 99
            ]
            matches.sort(key=lambda item: score(item[0]))
            if matches:
                menu.clear_options()
                for cmd, desc in matches:
                    menu.add_option(Option(f"{cmd}  [dim]{desc}[/dim]", id=cmd))
                menu.styles.display = "block"
                # Highlight the first option
                if menu.option_count > 0:
                    menu.highlighted = 0
                return

        menu.styles.display = "none"

    @on(Input.Changed, "#message-input")
    def on_message_input_changed(self, event: Input.Changed) -> None:
        """Update slash menu as user types."""
        self._update_slash_menu(event.value.strip())

    def _is_slash_menu_visible(self) -> bool:
        menu = self.query_one("#slash-menu", OptionList)
        return str(menu.styles.display) != "none"

    def _update_config_status(self) -> None:
        """Update the config editor status bar text."""
        status = self.query_one("#config-status", Static)
        editor = self.query_one("#config-editor", VimTextArea)

        if editor.command_mode:
            status.update(f" [bold cyan]:{editor.command_text}[/bold cyan]")
            return

        if self._vim_enabled:
            vim_indicator = f"  [bold]{self._vim.mode_label}[/bold]  |"
            close_hint = ":q close  |  :w save  |  :wq save+close"
        else:
            vim_indicator = ""
            close_hint = "Escape cancel"
        status.update(
            f"{vim_indicator}  Ctrl+S save  |  Ctrl+D discard  |  {close_hint}"
        )

    def on_key(self, event) -> None:
        """Handle key events for config editor controls and slash menu."""
        # ── Config editor keys (Ctrl shortcuts + close) ──────────────
        if self._editing_config:
            if event.key == "ctrl+s":
                self._save_config()
                event.prevent_default()
                event.stop()
                return
            elif event.key == "ctrl+d":
                self._discard_config()
                event.prevent_default()
                event.stop()
                return
            elif event.key == "escape":
                if self._vim_enabled:
                    # Vim handles Escape in VimTextArea._on_key before it bubbles here.
                    # If it somehow reaches here, just ignore it — use :q to close.
                    event.prevent_default()
                    event.stop()
                else:
                    self._close_config_editor()
                    log = self.query_one("#output-log", RichLog)
                    log.write("[dim]Config edit cancelled.[/dim]")
                    event.prevent_default()
                    event.stop()
                return
            # All other keys are handled by VimTextArea._on_key
            return

        # ── Slash menu keys ──────────────────────────────────────────
        if not self._is_slash_menu_visible():
            return

        menu = self.query_one("#slash-menu", OptionList)

        if event.key == "up":
            if menu.highlighted is not None:
                if menu.highlighted > 0:
                    menu.highlighted -= 1
                else:
                    menu.highlighted = menu.option_count - 1
            event.prevent_default()
            event.stop()
        elif event.key == "down":
            if menu.highlighted is not None:
                if menu.highlighted < menu.option_count - 1:
                    menu.highlighted += 1
                else:
                    menu.highlighted = 0
            event.prevent_default()
            event.stop()
        elif event.key == "enter":
            if menu.highlighted is not None:
                option = menu.get_option_at_index(menu.highlighted)
                cmd = option.id
                if cmd:
                    inp = self.query_one("#message-input", Input)
                    inp.value = ""
                    menu.styles.display = "none"
                    log = self.query_one("#output-log", RichLog)
                    self._handle_slash_command(str(cmd), log)
                event.prevent_default()
                event.stop()
        elif event.key == "escape":
            menu.styles.display = "none"
            event.prevent_default()
            event.stop()

    @on(OptionList.OptionSelected, "#slash-menu")
    def on_slash_menu_selected(self, event: OptionList.OptionSelected) -> None:
        """When a slash command is selected from the menu, execute it."""
        cmd = event.option_id
        if cmd:
            inp = self.query_one("#message-input", Input)
            inp.value = ""
            menu = self.query_one("#slash-menu", OptionList)
            menu.styles.display = "none"
            log = self.query_one("#output-log", RichLog)
            self._handle_slash_command(cmd, log)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle user input from the text box."""
        message = event.value.strip()
        event.input.value = ""

        if not message:
            if self._waiting_for_config_description:
                log = self.query_one("#output-log", RichLog)
                log.write("[dim]Config request cancelled.[/dim]")
                self._waiting_for_config_description = False
                return
            # During workdir startup, empty Enter accepts the default
            if self._startup_phase == "workdir":
                log = self.query_one("#output-log", RichLog)
                log.write(f"\n[bold green]You:[/bold green] {self.workdir}")
                self._startup_phase = None
                self._start_pipeline()
                return
            # If we're waiting for phase input, empty means skip
            if self._input_future and not self._input_future.done():
                self._input_future.set_result("")
            return

        log = self.query_one("#output-log", RichLog)

        # ── Slash commands ───────────────────────────────────────────
        if message.startswith("/"):
            menu = self.query_one("#slash-menu", OptionList)
            menu.styles.display = "none"
            self._handle_slash_command(message, log)
            return

        log.write(f"\n[bold green]You:[/bold green] {message}")

        # ── Plain-text config flow ───────────────────────────────────
        if self._waiting_for_config_description:
            self._waiting_for_config_description = False
            self._configure_from_plain_text(message)
            return

        # ── Startup flow — collecting task / workdir ─────────────────
        if self._startup_phase == "task":
            self.user_task = message
            log.write(
                f"\n{self.t.s('info', 'Working directory:')}\n"
                f"[dim]Current: {self.workdir}[/dim]\n"
                "[dim]Press Enter to use current directory, or type a path.[/dim]\n"
            )
            self._startup_phase = "workdir"
            inp = self.query_one("#message-input", Input)
            inp.placeholder = f"Working directory (Enter for {self.workdir})..."
            return

        if self._startup_phase == "workdir":
            if message:
                self.workdir = os.path.abspath(message)
            self._startup_phase = None
            self._start_pipeline()
            return

        # ── Handle human approval phase ──────────────────────────────
        if self._waiting_for_approval and self._pr_url:
            self._handle_approval(message)
            return

        # If we're waiting for phase input (clarifying questions), resolve it
        if self._input_future and not self._input_future.done():
            self._input_future.set_result(message)
            return

        # Otherwise, queue for the agent
        if self.agent_running:
            self.message_queue.append(message)
            log.write("[dim]  (queued — will send after current step)[/dim]")
        else:
            self.message_queue.append(message)
            self._process_queue()

    # ─── Config Editor ─────────────────────────────────────────────

    def _open_config_editor(self) -> None:
        """Open the config file in the TextArea editor."""
        try:
            with open(self._config_path) as f:
                content = f.read()
        except FileNotFoundError:
            log = self.query_one("#output-log", RichLog)
            log.write(f"[red]Config file not found: {self._config_path}[/red]")
            return

        editor = self.query_one("#config-editor", VimTextArea)
        editor.load_text(content)
        status = self.query_one("#config-status", Static)

        # Hide main view, show editor
        self.query_one("#output-log", RichLog).styles.display = "none"
        self.query_one("#message-input", VimInput).styles.display = "none"
        self.query_one("#input-mode", Static).styles.display = "none"
        self.query_one("#slash-menu", OptionList).styles.display = "none"
        self.query_one(Sidebar).styles.display = "none"
        editor.styles.display = "block"
        status.styles.display = "block"
        editor.focus()

        self._editing_config = True
        # Reset vim to normal mode when opening editor
        if self._vim_enabled:
            self._vim.mode = "normal"
        self._update_config_status()

    def _close_config_editor(self) -> None:
        """Close the config editor and restore the main view."""
        editor = self.query_one("#config-editor", VimTextArea)
        status = self.query_one("#config-status", Static)

        editor.styles.display = "none"
        status.styles.display = "none"
        self.query_one("#output-log", RichLog).styles.display = "block"
        self.query_one("#message-input", Input).styles.display = "block"
        self.query_one(Sidebar).styles.display = "block"
        if self._vim_enabled:
            # Return chat input to INSERT mode by default.
            self._vim.mode = "insert"
        self._update_input_mode_status()

        self._editing_config = False
        self.query_one("#message-input", Input).focus()

    def _handle_vim_command(self, command: str) -> None:
        """Handle vim-style config commands (:q, :w, :wq)."""
        cmd = command.strip().lower()
        status = self.query_one("#config-status", Static)

        if cmd in {"q", "q!"}:
            self._close_config_editor()
            log = self.query_one("#output-log", RichLog)
            log.write("[dim]Config edit closed.[/dim]")
            return

        if cmd == "w":
            if self._save_config(close_editor=False):
                status.update(" [green]Config saved.[/green]")
            return

        if cmd == "wq":
            self._save_config(close_editor=True)
            return

        status.update(
            f" [red]Unknown command: :{command}[/red] [dim](use :q, :w, :wq)[/dim]"
        )

    def _save_config(self, close_editor: bool = True) -> bool:
        """Validate and save the config editor contents.

        Returns True on success.
        """
        editor = self.query_one("#config-editor", VimTextArea)
        content = editor.text
        status = self.query_one("#config-status", Static)

        # Validate YAML
        try:
            new_config = yaml.safe_load(content)
        except yaml.YAMLError as e:
            status.update(f" [bold red]Invalid YAML:[/bold red] {e}")
            return False

        if not isinstance(new_config, dict):
            status.update(
                " [bold red]Invalid config: must be a YAML mapping[/bold red]"
            )
            return False

        # Write to file
        try:
            with open(self._config_path, "w") as f:
                f.write(content)
        except OSError as e:
            status.update(f" [bold red]Save failed:[/bold red] {e}")
            return False

        # Reload config, theme, and editor settings
        self.config = new_config
        self.t = Theme(self.config)
        self._vim_enabled = self.config.get("editor", {}).get("vim_mode", False)
        self._apply_ui_settings()
        self._sync_vim_state()

        if close_editor:
            self._close_config_editor()
            log = self.query_one("#output-log", RichLog)
            log.write(self.t.s("success", "Configuration saved and reloaded."))
        else:
            self._update_config_status()

        return True

    def _persist_vim_setting(self, enabled: bool) -> None:
        """Save vim_mode setting to config file."""
        try:
            with open(self._config_path) as f:
                content = f.read()
            config = yaml.safe_load(content)
            if not isinstance(config, dict):
                return
            config.setdefault("editor", {})["vim_mode"] = enabled
            with open(self._config_path, "w") as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False)
            self.config = config
        except (OSError, yaml.YAMLError):
            pass  # Silently fail — not critical

    def _discard_config(self) -> None:
        """Reload config from disk, discarding all edits."""
        try:
            with open(self._config_path) as f:
                content = f.read()
        except FileNotFoundError:
            status = self.query_one("#config-status", Static)
            status.update(
                f" [bold red]Config file not found: {self._config_path}[/bold red]"
            )
            return

        editor = self.query_one("#config-editor", VimTextArea)
        editor.load_text(content)
        self._update_config_status()

        status = self.query_one("#config-status", Static)
        # Flash a message briefly — update status to show discard happened
        self._update_config_status()
        log = self.query_one("#output-log", RichLog)
        # We can't show this while editor is open, so update status bar
        status.update(" [yellow]Changes discarded — reloaded from disk.[/yellow]")

    def _extract_yaml_block(self, text: str) -> str:
        """Extract YAML text from fenced block or raw text."""
        match = re.search(
            r"```(?:yaml|yml)?\n(.*?)```", text, re.DOTALL | re.IGNORECASE
        )
        if match:
            return match.group(1).strip()
        return text.strip()

    @work(thread=False)
    async def _configure_from_plain_text(self, description: str) -> None:
        """Apply config changes from a plain-English request."""
        log = self.query_one("#output-log", RichLog)
        self._log_write("\n[bold]Applying config request...[/bold]")

        # Fast-path: simple natural language input height changes.
        desc = description.lower()
        mentions_input = any(
            phrase in desc
            for phrase in [
                "input",
                "text box",
                "textbox",
                "prompt box",
                "message box",
            ]
        ) and any(word in desc for word in ["height", "taller", "bigger", "lines"])
        height_match = re.search(r"\b(\d{1,2})\b", desc)
        if mentions_input and height_match:
            requested = self._clamp_input_height(height_match.group(1))
            cfg = dict(self.config)
            ui_cfg = dict(cfg.get("ui", {}))
            ui_cfg["input_height"] = requested
            cfg["ui"] = ui_cfg
            try:
                Path(self._config_path).write_text(
                    yaml.dump(cfg, default_flow_style=False, sort_keys=False)
                )
            except OSError as e:
                self._log_write(f"[red]Failed to write config: {e}[/red]")
                return

            self.config = cfg
            self.t = Theme(self.config)
            self._vim_enabled = self.config.get("editor", {}).get("vim_mode", False)
            self._apply_ui_settings()
            self._sync_vim_state()
            if self._vim_enabled:
                self._vim.mode = "insert"
                self._update_input_mode_status()
            self._log_write(
                self.t.s("success", f"Set input height to {requested} lines.")
            )
            return

        try:
            current = Path(self._config_path).read_text()
        except OSError as e:
            self._log_write(f"[red]Failed to read config: {e}[/red]")
            return

        prompt = (
            "You are editing a YAML config file. "
            "Return ONLY the full updated YAML in a fenced ```yaml code block.\n\n"
            "User request:\n"
            f"{description}\n\n"
            "Current YAML:\n"
            "```yaml\n"
            f"{current}\n"
            "```\n\n"
            "Rules:\n"
            "- Preserve existing keys unless user asked to change them\n"
            "- Keep values valid YAML\n"
            "- Include the full file, not a partial snippet"
        )

        model = self.config["models"]["feature"]
        result = await self._run_agent_and_wait(prompt, model)
        updated_text = self._extract_yaml_block(str(result.get("stdout", "")))

        try:
            new_config = yaml.safe_load(updated_text)
        except yaml.YAMLError as e:
            self._log_write(f"[red]Generated YAML is invalid: {e}[/red]")
            return

        if not isinstance(new_config, dict):
            self._log_write("[red]Generated config is not a YAML mapping.[/red]")
            return

        try:
            Path(self._config_path).write_text(updated_text + "\n")
        except OSError as e:
            self._log_write(f"[red]Failed to write config: {e}[/red]")
            return

        self.config = new_config
        self.t = Theme(self.config)
        self._vim_enabled = self.config.get("editor", {}).get("vim_mode", False)
        self._apply_ui_settings()
        self._sync_vim_state()
        if self._vim_enabled:
            self._vim.mode = "insert"
            self._update_input_mode_status()
        self._log_write(self.t.s("success", "Config updated from plain text request."))

    def _handle_slash_command(self, message: str, log: RichLog) -> None:
        """Handle /commands from the input box."""
        parts = message.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "/help":
            vim_state = "on" if self._vim_enabled else "off"
            log.write(
                "\n[bold]Available commands:[/bold]\n"
                "  [bold]/configure[/bold]     Describe config changes in plain text\n"
                "  [bold]/config[/bold]        Edit configuration\n"
                "  [bold]/help[/bold]          Show this help\n"
                "  [bold]/exit[/bold]          Quit the application\n"
                "  [bold]/skip[/bold]          Skip the current phase or question\n"
                "  [bold]/status[/bold]        Show current pipeline status\n"
                "  [bold]/task[/bold]          Show the current task description\n"
                f"  [bold]/vim[/bold]           Toggle vim mode ({vim_state})\n"
                "  [bold]/workdir[/bold]       Show the working directory\n"
            )
        elif cmd == "/configure":
            self._waiting_for_config_description = True
            log.write(
                "\n[bold]Describe the config change:[/bold]\n"
                '[dim]Example: "Use gpt-5.1 for review and turn on vim mode by default"[/dim]'
            )
        elif cmd == "/config":
            self._open_config_editor()
        elif cmd == "/exit":
            self.action_quit_app()
        elif cmd == "/skip":
            if self._input_future and not self._input_future.done():
                log.write("[dim]Skipping...[/dim]")
                self._input_future.set_result("skip")
            elif self._waiting_for_approval:
                log.write("[yellow]PR left open for manual review.[/yellow]")
                self._waiting_for_approval = False
                self._pr_url = None
                header = self.query_one(PhaseHeader)
                header.set_phase(6, "complete")
                log.write(
                    "\n[bold green]Pipeline complete![/bold green] "
                    "Press Ctrl+C to exit."
                )
            else:
                log.write("[dim]Nothing to skip right now.[/dim]")
        elif cmd == "/status":
            phase_name = PHASE_NAMES.get(
                self.current_phase, f"Phase {self.current_phase}"
            )
            status = "running" if self.agent_running else "idle"
            log.write(
                f"\n[bold]Status:[/bold] {phase_name} ({status})\n"
                f"  Session: {self.session_id or 'none'}\n"
                f"  Queued messages: {len(self.message_queue)}"
            )
        elif cmd == "/task":
            log.write(f"\n[bold]Task:[/bold] {self.user_task}")
        elif cmd == "/vim":
            self._vim_enabled = not self._vim_enabled
            self._persist_vim_setting(self._vim_enabled)
            if self._vim_enabled:
                self._vim.mode = "insert"  # Start in insert mode for comfort
                self._sync_vim_state()
                log.write(
                    "[bold]Vim mode enabled.[/bold] [dim](saved to config)[/dim]\n"
                    "  [dim]Escape to enter normal mode, i/a/o to insert.[/dim]\n"
                    "  [dim]hjkl to move, dd to delete line, u to undo.[/dim]\n"
                    "  [dim]Type /vim again to disable.[/dim]"
                )
            else:
                self._sync_vim_state()
                log.write(
                    "[bold]Vim mode disabled.[/bold] [dim](saved to config)[/dim]"
                )
        elif cmd == "/workdir":
            log.write(f"\n[bold]Working dir:[/bold] {self.workdir}")
        else:
            log.write(
                f"[dim]Unknown command: {cmd}. Type /help for available commands.[/dim]"
            )

    def _handle_approval(self, choice: str) -> None:
        """Handle merge/skip/abort for Phase 6 (remote PR or local branch)."""
        log = self.query_one("#output-log", RichLog)
        header = self.query_one(PhaseHeader)

        if self._local_mode:
            self._handle_local_approval(choice, log, header)
        else:
            self._handle_remote_approval(choice, log, header)

    def _handle_remote_approval(
        self, choice: str, log: RichLog, header: PhaseHeader
    ) -> None:
        """Handle merge/skip/abort for a remote PR."""
        pr_url = self._pr_url

        if not pr_url:
            log.write("[red]No PR URL available.[/red]")
            return

        if choice.lower() in ("m", "merge"):
            result = subprocess.run(
                ["gh", "pr", "merge", pr_url, "--merge"],
                cwd=self.workdir,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                log.write(self.t.s("success", "PR merged successfully.", bold=True))
            else:
                log.write(f"[red]Merge failed: {result.stderr}[/red]")
                log.write("[dim]You may need to merge manually.[/dim]")
            header.set_phase(6, "complete")
        elif choice.lower() in ("s", "skip"):
            log.write("[yellow]PR left open for manual review.[/yellow]")
            header.set_phase(6, "complete")
        elif choice.lower() in ("a", "abort"):
            subprocess.run(
                ["gh", "pr", "close", pr_url],
                cwd=self.workdir,
                capture_output=True,
                text=True,
            )
            log.write("[red]PR closed.[/red]")
            header.set_phase(6, "complete")
        else:
            log.write("[dim]Type merge, skip, or abort.[/dim]")
            return

        self._waiting_for_approval = False
        self._pr_url = None
        log.write("\n[bold green]Pipeline complete![/bold green] Press Ctrl+C to exit.")

    def _handle_local_approval(
        self, choice: str, log: RichLog, header: PhaseHeader
    ) -> None:
        """Handle merge/skip/abort for a local branch (no remote)."""
        branch: str = getattr(self, "_ship_branch", "")
        base: str = self._resolve_base_branch(getattr(self, "_base_branch", "main"))

        if not branch:
            log.write("[red]No branch name available.[/red]")
            return

        if choice.lower() in ("m", "merge"):
            # Checkout base branch and merge the feature branch
            result = subprocess.run(
                ["git", "checkout", base],
                cwd=self.workdir,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                log.write(f"[red]Failed to checkout {base}: {result.stderr}[/red]")
                return

            result = subprocess.run(
                ["git", "merge", branch, "--no-ff", "-m", f"Merge branch '{branch}'"],
                cwd=self.workdir,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                log.write(
                    self.t.s(
                        "success",
                        f"Branch '{branch}' merged into {base}.",
                        bold=True,
                    )
                )
                # Clean up the feature branch
                subprocess.run(
                    ["git", "branch", "-d", branch],
                    cwd=self.workdir,
                    capture_output=True,
                    text=True,
                )
            else:
                log.write(f"[red]Merge failed: {result.stderr}[/red]")
                log.write("[dim]You may need to resolve conflicts manually.[/dim]")
            header.set_phase(6, "complete")

        elif choice.lower() in ("s", "skip"):
            log.write(f"[yellow]Branch '{branch}' left as-is.[/yellow]")
            header.set_phase(6, "complete")

        elif choice.lower() in ("a", "abort"):
            # Switch back to base and delete feature branch
            subprocess.run(
                ["git", "checkout", base],
                cwd=self.workdir,
                capture_output=True,
                text=True,
            )
            result = subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=self.workdir,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                log.write(f"[red]Branch '{branch}' deleted.[/red]")
            else:
                log.write(f"[red]Failed to delete branch: {result.stderr}[/red]")
            header.set_phase(6, "complete")
        else:
            log.write("[dim]Type merge, skip, or abort.[/dim]")
            return

        self._waiting_for_approval = False
        self._pr_url = None
        log.write("\n[bold green]Pipeline complete![/bold green] Press Ctrl+C to exit.")


# ─── Entry Point ─────────────────────────────────────────────────────────────


def run_tui(
    task: str | None = None,
    workdir: str | None = None,
    config_path: str | None = None,
) -> None:
    """Launch the TUI app."""
    app = OrchestratorApp(task, workdir, config_path)
    app.run()
