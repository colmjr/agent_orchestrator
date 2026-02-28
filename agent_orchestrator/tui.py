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
from textual.document._document import Selection
from textual.message import Message
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

from agent_orchestrator.orchestrator import THEME_PRESETS, Theme

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


# Map our config preset names to Textual's built-in theme names.
# Textual ships with: textual-dark, textual-light, nord, gruvbox, dracula,
# solarized-light, solarized-dark, catppuccin-mocha, tokyo-night, monokai, etc.
TEXTUAL_THEME_MAP = {
    "default": "textual-dark",
    "nord": "nord",
    "gruvbox": "gruvbox",
    "dracula": "dracula",
    "solarized": "solarized-dark",
    "monokai": "monokai",
    "tokyo-night": "tokyo-night",
    "catppuccin": "catppuccin-mocha",
}

SLASH_COMMANDS = [
    ("/configure", "Describe config changes in plain text"),
    ("/theme", "Set or view theme preset"),
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
    """Lightweight vim emulation state machine for TextArea and Input widgets."""

    def __init__(self) -> None:
        self.mode = "normal"  # insert | normal | visual | visual_line
        self._pending = ""  # Multi-char commands like dd, gg, ci"
        self._pending_count = 1
        self._count_buffer = ""
        self._visual_anchor: tuple[int, int] | None = None
        self._last_search_pattern = ""
        self._last_search_forward = True
        self._last_change_textarea: Callable[[TextArea], None] | None = None
        self._last_change_input: Callable[[Input], None] | None = None

    @property
    def mode_label(self) -> str:
        if self.mode == "insert":
            return "-- INSERT --"
        if self.mode == "visual":
            return "-- VISUAL --"
        if self.mode == "visual_line":
            return "-- VISUAL LINE --"
        return "NORMAL"

    def _consume_count(self) -> int:
        count = int(self._count_buffer) if self._count_buffer else 1
        self._count_buffer = ""
        return count

    def _to_index(self, editor: TextArea, location: tuple[int, int]) -> int:
        row, col = location
        lines = editor.document.lines
        row = max(0, min(row, len(lines) - 1))
        col = max(0, min(col, len(lines[row])))
        return sum(len(line) + 1 for line in lines[:row]) + col

    def _from_index(self, editor: TextArea, index: int) -> tuple[int, int]:
        lines = editor.document.lines
        text_length = sum(len(line) + 1 for line in lines[:-1]) + len(lines[-1])
        index = max(0, min(index, text_length))
        running = 0
        for row, line in enumerate(lines):
            line_len = len(line)
            end = running + line_len
            if index <= end:
                return (row, index - running)
            running = end + 1
        return (len(lines) - 1, len(lines[-1]))

    def _set_visual_selection(self, editor: TextArea, target: tuple[int, int]) -> None:
        if self._visual_anchor is None:
            self._visual_anchor = editor.cursor_location
        editor.selection = Selection(self._visual_anchor, target)

    def _clear_selection(self, editor: TextArea) -> None:
        loc = editor.cursor_location
        editor.selection = Selection(loc, loc)

    def _run_times(self, count: int, fn: Callable[[], None]) -> None:
        for _ in range(max(1, count)):
            fn()

    def _run_editor_action(self, count: int, action: Callable[[], None]) -> None:
        """Run an editor action repeatedly with vim-style count semantics."""
        self._run_times(count, action)

    def _run_input_find(self, inp: Input, ch: str, count: int, till: bool) -> None:
        """Move input cursor to f/t match on the current line."""
        text = inp.value
        pos = inp.cursor_position
        idx = -1
        start = pos + 1
        for _ in range(max(1, count)):
            idx = text.find(ch, start)
            if idx == -1:
                return
            start = idx + 1
        inp.cursor_position = max(0, idx - 1) if till else idx

    def _handle_pending_input(
        self, pending: str, key: str, char: str | None, inp: Input, count: int
    ) -> bool:
        """Handle pending multi-key commands in Input normal mode."""
        if pending == "d" and key == "w":
            self._run_editor_action(count, inp.action_delete_right_word)
            self._last_change_input = lambda i, n=max(1, count): self._run_times(
                n, i.action_delete_right_word
            )
            return True
        if pending == "c" and key == "w":
            self._run_editor_action(count, inp.action_delete_right_word)
            self._last_change_input = lambda i, n=max(1, count): self._run_times(
                n, i.action_delete_right_word
            )
            self.mode = "insert"
            return True
        if pending == "d" and (char == "$" or key == "end"):
            inp.action_delete_right_all()
            self._last_change_input = lambda i: i.action_delete_right_all()
            return True
        if pending == "c" and (char == "$" or key == "end"):
            inp.action_delete_right_all()
            self._last_change_input = lambda i: i.action_delete_right_all()
            self.mode = "insert"
            return True
        if pending == "f" and char:
            self._run_input_find(inp, char, count, till=False)
            return True
        if pending == "t" and char:
            self._run_input_find(inp, char, count, till=True)
            return True
        return True

    def _repeat_change_textarea(self, editor: TextArea) -> None:
        if self._last_change_textarea:
            self._last_change_textarea(editor)

    def _repeat_change_input(self, inp: Input) -> None:
        if self._last_change_input:
            self._last_change_input(inp)

    def _find_char_on_line(
        self, editor: TextArea, ch: str, till: bool, count: int
    ) -> tuple[int, int] | None:
        row, col = editor.cursor_location
        line = editor.document.get_line(row)
        search_start = min(col + 1, len(line))
        idx = -1
        start = search_start
        for _ in range(count):
            idx = line.find(ch, start)
            if idx == -1:
                return None
            start = idx + 1
        if till:
            idx -= 1
        idx = max(0, min(idx, len(line)))
        return (row, idx)

    def _find_word_end(self, editor: TextArea, count: int) -> tuple[int, int]:
        idx = self._to_index(editor, editor.cursor_location)
        text = editor.text
        n = len(text)
        i = idx
        for _ in range(count):
            while i < n and not text[i].isalnum() and text[i] != "_":
                i += 1
            while i < n and (text[i].isalnum() or text[i] == "_"):
                i += 1
            i = max(0, i - 1)
            if i < n - 1:
                i += 1
        i = max(0, min(i - 1, n))
        return self._from_index(editor, i)

    def _join_next_line(self, editor: TextArea) -> None:
        row, _ = editor.cursor_location
        if row >= editor.document.line_count - 1:
            return
        current = editor.document.get_line(row).rstrip()
        nxt = editor.document.get_line(row + 1).lstrip()
        joined = f"{current} {nxt}".rstrip()
        start = (row, 0)
        end = (row + 1, len(editor.document.get_line(row + 1)))
        editor.replace(joined, start, end)
        editor.move_cursor((row, min(len(joined), len(current) + 1)))

    def _indent_lines(self, editor: TextArea, count: int, dedent: bool = False) -> None:
        row, _ = editor.cursor_location
        end_row = min(editor.document.line_count - 1, row + max(0, count - 1))
        for line_no in range(row, end_row + 1):
            line = editor.document.get_line(line_no)
            if dedent:
                if line.startswith("    "):
                    new_line = line[4:]
                elif line.startswith("\t"):
                    new_line = line[1:]
                else:
                    new_line = line.lstrip(" ")
                    if len(line) - len(new_line) > 4:
                        new_line = line[4:]
            else:
                new_line = f"    {line}"
            editor.replace(new_line, (line_no, 0), (line_no, len(line)))

    def _inside_pair_range(
        self, editor: TextArea, left: str, right: str
    ) -> tuple[tuple[int, int], tuple[int, int]] | None:
        row, col = editor.cursor_location
        line = editor.document.get_line(row)
        if left == right:
            start = line.rfind(left, 0, col + 1)
            end = line.find(right, col)
            if start == -1 or end == -1 or end <= start:
                return None
            return (row, start + 1), (row, end)

        start = line.rfind(left, 0, col + 1)
        end = line.find(right, col)
        if start == -1 or end == -1 or end <= start:
            return None
        return (row, start + 1), (row, end)

    def _apply_search(self, editor: TextArea, pattern: str, forward: bool) -> bool:
        if not pattern:
            return False
        text = editor.text
        cursor_index = self._to_index(editor, editor.cursor_location)
        try:
            regex = re.compile(pattern)
        except re.error:
            regex = re.compile(re.escape(pattern))

        target: tuple[int, int] | None = None
        if forward:
            match = regex.search(text, cursor_index + 1)
            if not match:
                match = regex.search(text, 0)
            if match:
                target = self._from_index(editor, match.start())
        else:
            matches = list(regex.finditer(text))
            previous = [m for m in matches if m.start() < cursor_index]
            match = previous[-1] if previous else (matches[-1] if matches else None)
            if match:
                target = self._from_index(editor, match.start())

        if target is None:
            return False

        editor.move_cursor(target, center=True)
        self._clear_selection(editor)
        self._last_search_pattern = pattern
        self._last_search_forward = forward
        return True

    def search_textarea(self, editor: TextArea, pattern: str, forward: bool) -> bool:
        return self._apply_search(editor, pattern, forward)

    def repeat_search_textarea(self, editor: TextArea, same_direction: bool) -> bool:
        if not self._last_search_pattern:
            return False
        direction = (
            self._last_search_forward
            if same_direction
            else not self._last_search_forward
        )
        return self._apply_search(editor, self._last_search_pattern, direction)

    def handle_key_textarea(self, key: str, char: str | None, editor: TextArea) -> bool:
        """Handle a key event for a TextArea. Returns True if the key was consumed."""
        if self.mode == "insert":
            if key == "escape":
                self.mode = "normal"
                return True
            return False  # Let TextArea handle it normally

        if self.mode in {"visual", "visual_line"}:
            if key == "escape":
                self.mode = "normal"
                self._visual_anchor = None
                self._clear_selection(editor)
                return True
            if key == "d":
                editor.action_cut()
                self._last_change_textarea = lambda ta: ta.action_cut()
                self.mode = "normal"
                self._visual_anchor = None
                return True
            if key == "y":
                editor.action_copy()
                self.mode = "normal"
                self._visual_anchor = None
                self._clear_selection(editor)
                return True
            if key in {"h", "left"}:
                editor.action_cursor_left(select=True)
                return True
            if key in {"j", "down"}:
                editor.action_cursor_down(select=True)
                return True
            if key in {"k", "up"}:
                editor.action_cursor_up(select=True)
                return True
            if key in {"l", "right"}:
                editor.action_cursor_right(select=True)
                return True
            if key == "w":
                editor.action_cursor_word_right(select=True)
                return True
            if key == "b":
                editor.action_cursor_word_left(select=True)
                return True
            if key == "0" or key == "home":
                editor.action_cursor_line_start(select=True)
                return True
            if char == "$" or key == "end":
                editor.action_cursor_line_end(select=True)
                return True
            return True

        # ── Normal mode ──────────────────────────────────────────────
        # Multi-char commands
        if self._pending:
            return self._handle_pending(key, char, editor)

        if key.isdigit() and (self._count_buffer or key != "0"):
            self._count_buffer += key
            return True

        count = self._consume_count()

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
            self._run_editor_action(count, editor.action_cursor_left)
            return True
        elif key == "j" or key == "down":
            self._run_editor_action(count, editor.action_cursor_down)
            return True
        elif key == "k" or key == "up":
            self._run_editor_action(count, editor.action_cursor_up)
            return True
        elif key == "l" or key == "right":
            self._run_editor_action(count, editor.action_cursor_right)
            return True
        elif key == "w":
            self._run_editor_action(count, editor.action_cursor_word_right)
            return True
        elif key == "b":
            self._run_editor_action(count, editor.action_cursor_word_left)
            return True
        elif key == "e":
            target = self._find_word_end(editor, count)
            editor.move_cursor(target)
            return True
        elif key == "0" or key == "home":
            editor.action_cursor_line_start()
            return True
        elif char == "$" or key == "end":
            editor.action_cursor_line_end()
            return True
        elif key == "x":
            self._run_editor_action(count, editor.action_delete_right)
            self._last_change_textarea = lambda ta, n=count: self._run_times(
                n, ta.action_delete_right
            )
            return True
        elif key == "v":
            self.mode = "visual"
            self._visual_anchor = editor.cursor_location
            self._set_visual_selection(editor, editor.cursor_location)
            return True
        elif char == "V":
            self.mode = "visual_line"
            row, _ = editor.cursor_location
            self._visual_anchor = (row, 0)
            end_col = len(editor.document.get_line(row))
            editor.selection = Selection((row, 0), (row, end_col))
            return True
        elif key == "d":
            self._pending = "d"
            self._pending_count = count
            return True
        elif key == "c":
            self._pending = "c"
            self._pending_count = count
            return True
        elif key == "f":
            self._pending = "f"
            self._pending_count = count
            return True
        elif key == "t":
            self._pending = "t"
            self._pending_count = count
            return True
        elif char == "G":
            # Go to end of file — move to last line
            editor.action_scroll_end()
            return True
        elif key == "g":
            self._pending = "g"
            self._pending_count = count
            return True
        elif key == ">":
            self._pending = ">"
            self._pending_count = count
            return True
        elif key == "<":
            self._pending = "<"
            self._pending_count = count
            return True
        elif char == "D":
            editor.action_delete_to_end_of_line()
            self._last_change_textarea = lambda ta: ta.action_delete_to_end_of_line()
            return True
        elif char == "C":
            editor.action_delete_to_end_of_line()
            self._last_change_textarea = lambda ta: ta.action_delete_to_end_of_line()
            self.mode = "insert"
            return True
        elif char == "J":
            self._run_editor_action(count, lambda: self._join_next_line(editor))
            self._last_change_textarea = lambda ta, n=max(1, count): self._run_times(
                n, lambda: self._join_next_line(ta)
            )
            return True
        elif key == "n":
            self.repeat_search_textarea(editor, same_direction=True)
            return True
        elif char == "N":
            self.repeat_search_textarea(editor, same_direction=False)
            return True
        elif key == "ctrl+d":
            editor.action_cursor_page_down()
            return True
        elif key == "ctrl+u":
            editor.action_cursor_page_up()
            return True
        elif key == "ctrl+f":
            editor.action_page_down()
            return True
        elif key == "ctrl+b":
            editor.action_page_up()
            return True
        elif key == "u":
            editor.action_undo()
            return True
        elif key == "ctrl+r":
            editor.action_redo()
            return True
        elif key == "p":
            editor.action_paste()
            self._last_change_textarea = lambda ta: ta.action_paste()
            return True
        elif key == ".":
            self._repeat_change_textarea(editor)
            return True

        # Consume all other keys in normal mode to prevent typing
        return True

    def _handle_pending_range_op(
        self, pending: str, char: str | None, editor: TextArea
    ) -> bool:
        """Handle ci?/di? pending range operations for TextArea."""
        if pending not in {"ci", "di"} or not char:
            return False

        pair_map = {
            '"': ('"', '"'),
            "'": ("'", "'"),
            "(": ("(", ")"),
            ")": ("(", ")"),
            "[": ("[", "]"),
            "]": ("[", "]"),
            "{": ("{", "}"),
            "}": ("{", "}"),
        }
        pair = pair_map.get(char)
        if not pair:
            return True
        match = self._inside_pair_range(editor, pair[0], pair[1])
        if not match:
            return True

        start, end = match
        editor.replace("", start, end)
        self._last_change_textarea = (
            lambda ta, s=start, e=end: ta.replace("", s, e) and None
        )
        if pending == "ci":
            self.mode = "insert"
        return True

    def _handle_pending(self, key: str, char: str | None, editor: TextArea) -> bool:
        """Handle the second character of a multi-char command."""
        pending = self._pending
        count = self._pending_count
        self._pending = ""
        self._pending_count = 1

        if pending == "d" and key == "d":
            self._run_editor_action(count, editor.action_delete_line)
            self._last_change_textarea = lambda ta, n=max(1, count): self._run_times(
                n, ta.action_delete_line
            )
            return True
        if pending == "d" and key == "w":
            self._run_editor_action(count, editor.action_delete_word_right)
            self._last_change_textarea = lambda ta, n=max(1, count): self._run_times(
                n, ta.action_delete_word_right
            )
            return True
        if pending == "d" and (char == "$" or key == "end"):
            editor.action_delete_to_end_of_line()
            self._last_change_textarea = lambda ta: ta.action_delete_to_end_of_line()
            return True
        if pending == "d" and key == "i":
            self._pending = "di"
            self._pending_count = count
            return True
        if pending == "c" and key == "w":
            self._run_editor_action(count, editor.action_delete_word_right)
            self._last_change_textarea = lambda ta, n=max(1, count): self._run_times(
                n, ta.action_delete_word_right
            )
            self.mode = "insert"
            return True
        if pending == "c" and (char == "$" or key == "end"):
            editor.action_delete_to_end_of_line()
            self._last_change_textarea = lambda ta: ta.action_delete_to_end_of_line()
            self.mode = "insert"
            return True
        if pending == "c" and key == "i":
            self._pending = "ci"
            self._pending_count = count
            return True
        if pending == "f" and char:
            target = self._find_char_on_line(
                editor, char, till=False, count=max(1, count)
            )
            if target:
                editor.move_cursor(target)
            return True
        if pending == "t" and char:
            target = self._find_char_on_line(
                editor, char, till=True, count=max(1, count)
            )
            if target:
                editor.move_cursor(target)
            return True
        if pending == "g" and key == "g":
            if count <= 1:
                editor.action_scroll_home()
            else:
                line_no = min(editor.document.line_count - 1, count - 1)
                editor.move_cursor((line_no, 0), center=True)
            return True
        if pending == ">" and key == ">":
            self._indent_lines(editor, max(1, count), dedent=False)
            self._last_change_textarea = lambda ta, n=max(1, count): self._indent_lines(
                ta, n, dedent=False
            )
            return True
        if pending == "<" and key == "<":
            self._indent_lines(editor, max(1, count), dedent=True)
            self._last_change_textarea = lambda ta, n=max(1, count): self._indent_lines(
                ta, n, dedent=True
            )
            return True
        if self._handle_pending_range_op(pending, char, editor):
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
        if self._pending:
            pending = self._pending
            count = self._pending_count
            self._pending = ""
            self._pending_count = 1
            return self._handle_pending_input(pending, key, char, inp, count)

        if key.isdigit() and (self._count_buffer or key != "0"):
            self._count_buffer += key
            return True
        count = self._consume_count()

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
            self._run_editor_action(count, inp.action_cursor_left)
            return True
        elif key == "l" or key == "right":
            self._run_editor_action(count, inp.action_cursor_right)
            return True
        elif key == "w":
            self._run_editor_action(count, inp.action_cursor_right_word)
            return True
        elif key == "b":
            self._run_editor_action(count, inp.action_cursor_left_word)
            return True
        elif key == "e":
            text = inp.value
            pos = inp.cursor_position
            i = pos
            for _ in range(count):
                while i < len(text) and not (text[i].isalnum() or text[i] == "_"):
                    i += 1
                while i < len(text) and (text[i].isalnum() or text[i] == "_"):
                    i += 1
                i = max(0, i - 1)
                if i < len(text) - 1:
                    i += 1
            inp.cursor_position = max(0, i - 1)
            return True
        elif key == "0" or key == "home":
            inp.action_home()
            return True
        elif char == "$" or key == "end":
            inp.action_end()
            return True
        elif key == "x":
            self._run_editor_action(count, inp.action_delete_right)
            self._last_change_input = lambda i, n=count: self._run_times(
                n, i.action_delete_right
            )
            return True
        elif key == "d":
            self._pending = "d"
            self._pending_count = count
            return True
        elif key == "c":
            self._pending = "c"
            self._pending_count = count
            return True
        elif key == "f":
            self._pending = "f"
            self._pending_count = count
            return True
        elif key == "t":
            self._pending = "t"
            self._pending_count = count
            return True
        elif char == "D":
            inp.action_delete_right_all()
            self._last_change_input = lambda i: i.action_delete_right_all()
            return True
        elif char == "C":
            inp.action_delete_right_all()
            self._last_change_input = lambda i: i.action_delete_right_all()
            self.mode = "insert"
            return True
        elif key == "ctrl+d":
            inp.action_scroll_end()
            return True
        elif key == "ctrl+u":
            inp.action_scroll_home()
            return True
        elif key == "p":
            inp.action_paste()
            self._last_change_input = lambda i: i.action_paste()
            return True
        elif key == ".":
            self._repeat_change_input(inp)
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
        self.command_prefix = ":"
        self.command_text = ""

    async def _on_key(self, event) -> None:
        if self.vim_enabled:
            # Vim command-line mode (e.g. :q, :w, :wq)
            if self.command_mode:
                if event.key == "escape":
                    self.command_mode = False
                    self.command_prefix = ":"
                    self.command_text = ""
                    self._status_callback()
                    event.prevent_default()
                    event.stop()
                    return
                if event.key == "enter":
                    cmd = self.command_text.strip()
                    prefix = self.command_prefix
                    self.command_mode = False
                    self.command_prefix = ":"
                    self.command_text = ""
                    if prefix == ":":
                        self._command_callback(cmd)
                    else:
                        self._vim.search_textarea(self, cmd, forward=(prefix == "/"))
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

            # Enter prompt mode from NORMAL using ':', '/' or '?'
            if self._vim.mode == "normal" and event.character in {":", "/", "?"}:
                self.command_mode = True
                self.command_prefix = event.character
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


class VimMessageArea(TextArea):
    """Message input area with vim interception and submit key handling."""

    class Submitted(Message):
        """Posted when the user submits from the message area."""

        def __init__(self, text_area: "VimMessageArea", value: str) -> None:
            super().__init__()
            self.text_area = text_area
            self.value = value

        @property
        def control(self) -> "VimMessageArea":
            return self.text_area

    def __init__(self, vim: VimHandler, **kwargs) -> None:
        super().__init__(**kwargs)
        self._vim = vim
        self.vim_enabled = False
        self._status_callback: Callable[[], None] = lambda: None
        self._scroll_callback: Callable[[str], None] = lambda _direction: None
        self._slash_menu_visible: Callable[[], bool] = lambda: False
        self._decision_active: Callable[[], bool] = lambda: False

    async def _on_key(self, event) -> None:
        # When decision menu is active, suppress TextArea handling but
        # do NOT stop the event so it bubbles to App.on_key for menu routing.
        if self._decision_active():
            event.prevent_default()
            return

        # When slash menu is showing, let navigation keys bubble to App.on_key.
        if event.key in {"enter", "escape", "up", "down"} and (
            self._slash_menu_visible()
        ):
            event.prevent_default()
            return

        if event.key in {"enter", "ctrl+enter"}:
            self.post_message(self.Submitted(self, self.text))
            self.clear()
            self._vim.mode = "insert"
            self._status_callback()
            event.prevent_default()
            event.stop()
            return

        if event.key == "ctrl+j":
            if not self.vim_enabled or self._vim.mode == "insert":
                self.insert("\n")
                event.prevent_default()
                event.stop()
                return

        if event.key in {"pageup", "pagedown", "ctrl+up", "ctrl+down"}:
            self._scroll_callback(event.key)
            event.prevent_default()
            event.stop()
            return

        if self.vim_enabled:
            if event.key == "escape":
                if self._vim.mode == "insert":
                    self._vim.mode = "normal"
                    self._status_callback()
                event.prevent_default()
                event.stop()
                return
            else:
                consumed = self._vim.handle_key_textarea(
                    event.key, event.character, self
                )
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
        self._models_summary = ""

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

    def set_models(self, models_summary: str) -> None:
        self._models_summary = models_summary
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
        models_line = (
            f"\n [dim]Models: {self._models_summary}[/dim]"
            if self._models_summary
            else ""
        )
        self.update(f" {status_icon} {phase_text}  {task_line}{models_line}")


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
        display_dir = self._workdir or "[dim]—[/dim]"
        if len(display_dir) > 28:
            display_dir = "..." + display_dir[-25:]
        parts.append(f"  [dim]{display_dir}[/dim]")
        if self._branch:
            parts.append(f"  [cyan]{self._branch}[/cyan]")
        else:
            parts.append("  [dim]—[/dim]")

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
                ctx_pct = min(100, (self._total_tokens / 200_000) * 100)
                parts.append(f"  Context: [dim]~{ctx_pct:.0f}%[/dim]")
            parts.append(f"  Cost: [dim]~${self._cost_usd:.2f}[/dim]")
        else:
            parts.append("  Tokens: [dim]0[/dim]")
            parts.append("  Cost: [dim]$0.00[/dim]")
        parts.append("")

        # ── TODO Progress ────────
        parts.append("[bold]TODO[/bold]")
        if self._todo_items:
            done = sum(1 for c, _ in self._todo_items if c)
            total = len(self._todo_items)
            parts[-1] = f"[bold]TODO[/bold] [dim]{done}/{total}[/dim]"
            for checked, text in self._todo_items:
                icon = "[green]x[/green]" if checked else "[dim]o[/dim]"
                label = text[:26] + ("..." if len(text) > 26 else "")
                if checked:
                    parts.append(f"  {icon} [dim]{label}[/dim]")
                else:
                    parts.append(f"  {icon} {label}")
        else:
            parts.append("  [dim]—[/dim]")
        parts.append("")

        # ── Modified Files ───────
        parts.append("[bold]Modified[/bold]")
        if self._modified_files:
            parts[-1] = f"[bold]Modified[/bold] [dim]{len(self._modified_files)}[/dim]"
            for line in self._modified_files:
                # git status --short gives "XY filename"
                raw_status = line[:2]
                fname = line[3:].strip()
                if len(fname) > 24:
                    fname = "..." + fname[-21:]
                # Map raw git codes to readable labels
                status_map = {
                    "??": ("new", "green"),
                    "A ": ("add", "green"),
                    "AM": ("add", "green"),
                    "M ": ("mod", "yellow"),
                    " M": ("mod", "yellow"),
                    "MM": ("mod", "yellow"),
                    "D ": ("del", "red"),
                    " D": ("del", "red"),
                    "R ": ("ren", "cyan"),
                    "C ": ("cpy", "cyan"),
                }
                label, color = status_map.get(raw_status, ("mod", "yellow"))
                parts.append(f"  [{color}]{label:>3}[/{color}] [dim]{fname}[/dim]")
        else:
            parts.append("  [dim]—[/dim]")

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

    #decision-prompt {
        dock: bottom;
        height: auto;
        display: none;
        margin: 0 0 1 0;
        border: round $accent;
        padding: 0 1;
        background: $surface;
    }

    #decision-menu {
        dock: bottom;
        height: auto;
        max-height: 10;
        display: none;
        margin: 0 0 3 0;
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
        self._decision_future: asyncio.Future | None = None
        self._decision_active = False
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
        inp = self.query_one("#message-input", VimMessageArea)
        inp.styles.height = self._input_height
        menu = self.query_one("#slash-menu", OptionList)
        menu.styles.margin = (0, 0, self._input_height, 0)
        decision_menu = self.query_one("#decision-menu", OptionList)
        decision_menu.styles.margin = (0, 0, self._input_height, 0)
        decision_prompt = self.query_one("#decision-prompt", Static)
        decision_prompt.styles.margin = (0, 0, self._input_height + 1, 0)

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
        yield Static("", id="decision-prompt")
        yield OptionList(id="decision-menu")
        yield OptionList(id="slash-menu")
        yield Static("", id="input-mode")
        yield VimMessageArea(
            self._vim,
            placeholder=(
                "Type a message (Enter send, Ctrl+J newline, Escape interrupt)..."
            ),
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
        inp = self.query_one("#message-input", VimMessageArea)
        inp.vim_enabled = self._vim_enabled
        inp._status_callback = self._update_input_mode_status
        inp._scroll_callback = self._scroll_output
        inp._slash_menu_visible = self._is_slash_menu_visible
        inp._decision_active = lambda: self._decision_active
        self._update_input_mode_status()

    def _update_input_mode_status(self) -> None:
        """Show visual cue for vim mode on the message input."""
        status = self.query_one("#input-mode", Static)
        inp = self.query_one("#message-input", VimMessageArea)
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

    def _apply_textual_theme(self) -> None:
        """Set the Textual app theme to match the config preset."""
        preset = self.config.get("theme", {}).get("preset", "default")
        textual_name = TEXTUAL_THEME_MAP.get(preset, "textual-dark")
        try:
            self.theme = textual_name
        except Exception:
            self.theme = "textual-dark"

    def _scroll_output(self, direction: str) -> None:
        """Scroll output pane while keeping message input focused."""
        log = self.query_one("#output-log", RichLog)
        if direction == "pageup":
            log.scroll_page_up(animate=False)
            return
        if direction == "pagedown":
            log.scroll_page_down(animate=False)
            return
        if direction == "ctrl+up":
            for _ in range(3):
                log.scroll_up(animate=False)
            return
        if direction == "ctrl+down":
            for _ in range(3):
                log.scroll_down(animate=False)
            return

    def on_mount(self) -> None:
        self._apply_textual_theme()
        log = self.query_one("#output-log", RichLog)
        log.can_focus = False
        sidebar = self.query_one(Sidebar)
        header = self.query_one(PhaseHeader)
        header.set_models(self._models_summary())
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
                f"{self._models_summary()}\n"
            )
            log.write(
                f"\n{self.t.s('info', 'What task would you like to implement?')}\n"
                "[dim]Type your task description below and press Enter.[/dim]\n"
                "[dim]Use /help for available commands.[/dim]\n"
            )
            inp = self.query_one("#message-input", VimMessageArea)
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
            f"{self._models_summary()}\n"
        )
        header = self.query_one(PhaseHeader)
        header.set_task(self.user_task)
        header.set_models(self._models_summary())
        self._ensure_git_repo()

        # Refresh sidebar with possibly new workdir
        sidebar = self.query_one(Sidebar)
        sidebar.set_workdir(self.workdir)

        inp = self.query_one("#message-input", VimMessageArea)
        inp.placeholder = (
            "Type a message (Enter send, Ctrl+J newline, Escape interrupt)..."
        )
        inp.focus()

        self.run_pipeline()

    def _model_for(self, phase_key: str) -> str:
        """Resolve the model for a specific phase key with feature fallback."""
        models = self.config.get("models", {})
        feature = models.get("feature", "")
        return str(models.get(phase_key, feature))

    def _models_summary(self) -> str:
        """Build a compact unique-model summary for UI/log display."""
        phase_keys = [
            "feature",
            "clarify",
            "planning",
            "implement",
            "configure",
            "refactor",
            "review",
        ]
        seen: set[str] = set()
        unique_models: list[str] = []
        for key in phase_keys:
            model = self._model_for(key)
            if model and model not in seen:
                seen.add(model)
                unique_models.append(model)

        if not unique_models:
            return "none"
        if len(unique_models) == 1:
            return unique_models[0]
        return ", ".join(unique_models)

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

            # Mark bash/shell commands with ! for visibility
            bash_tools = {"bash", "shell", "execute", "run", "terminal", "command"}
            is_bash = tool_name.lower() in bash_tools or "bash" in tool_name.lower()
            indicator = "[bold red]![/bold red] " if is_bash else "  "

            if title:
                self.app.call_from_thread(
                    self._log_write,
                    f"{indicator}{t.s('info', tool_name, bold=True)} {t.s('muted', title)}",
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
                    f"{indicator}{t.s('info', tool_name, bold=True)} {t.s('muted', input_summary)}",
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
            await self._phase_human_approval_menu(pr_url, enriched_task)

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

        model = self.config["models"].get("clarify", self.config["models"]["feature"])
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

        model = self.config["models"].get("planning", self.config["models"]["feature"])
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

            while True:
                decision = await self._wait_for_decision(
                    "Plan Decision",
                    [
                        ("approve_keep", "Approve (Keep Context)"),
                        ("approve_clear", "Approve (Clear Context)"),
                        ("revise", "Revise Plan"),
                        ("add_context", "Add More Context"),
                        ("skip", "Skip"),
                    ],
                )

                if decision == "approve_keep":
                    self._log_write("[green]Plan approved (keeping context).[/green]")
                    break

                if decision == "approve_clear":
                    self.session_id = None
                    self._log_write(
                        "[green]Plan approved.[/green] [dim]Context cleared.[/dim]"
                    )
                    break

                if decision in {"skip", "cancel"}:
                    self._log_write(
                        "[yellow]Skipping plan approval; proceeding.[/yellow]"
                    )
                    break

                if decision in {"revise", "add_context"}:
                    prompt_text = (
                        "[dim]Enter plan feedback:[/dim]"
                        if decision == "revise"
                        else "[dim]Add additional context for planning:[/dim]"
                    )
                    self._log_write(prompt_text)
                    feedback = (await self._wait_for_input()).strip()
                    if not feedback:
                        self._log_write("[dim]No input provided. Plan unchanged.[/dim]")
                        continue

                    revise_prompt = (
                        "Revise this implementation plan based on user input.\n\n"
                        f"Task: {task}\n\n"
                        f"Current plan:\n{approved_plan}\n\n"
                        f"User input:\n{feedback}\n\n"
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
                    continue

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

        model = self.config["models"].get("implement", self.config["models"]["feature"])
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

    async def _phase_human_approval_menu(self, pr_url: str, task: str) -> None:
        """Phase 6: Decision menu for merge/skip/abort and optional changes."""
        if self._local_mode:
            branch = getattr(self, "_ship_branch", "")
            base = getattr(self, "_base_branch", "main")
            self._log_write(
                f"\n[bold]Branch ready for review:[/bold] {branch} [dim](base: {base})[/dim]"
            )
        else:
            self._log_write(f"\n[bold]PR ready for review:[/bold] {pr_url}")

        while True:
            decision = await self._wait_for_decision(
                "Final Decision",
                [
                    ("merge_keep", "Merge (Keep Context)"),
                    ("merge_clear", "Merge (Clear Context)"),
                    ("add_change", "Add Change Before Merge"),
                    ("skip", "Skip"),
                    ("abort", "Abort"),
                ],
            )

            if decision in {"merge_keep", "merge_clear", "skip", "abort"}:
                if decision == "merge_clear":
                    self.session_id = None
                    self._log_write("[dim]Context cleared before merge.[/dim]")

                self._waiting_for_approval = True
                self._pr_url = pr_url
                mapped = {
                    "merge_keep": "merge",
                    "merge_clear": "merge",
                    "skip": "skip",
                    "abort": "abort",
                }[decision]
                self._handle_approval(mapped)
                break

            if decision in {"add_change", "cancel"}:
                if decision == "cancel":
                    self._log_write("[dim]Decision menu cancelled. Re-opening.[/dim]")
                    continue

                self._log_write("[dim]Describe the change to make before merge:[/dim]")
                request = (await self._wait_for_input()).strip()
                if not request:
                    self._log_write("[dim]No change request provided.[/dim]")
                    continue

                self._log_write("[bold]Applying requested change...[/bold]")
                model = self.config["models"].get(
                    "implement", self.config["models"]["feature"]
                )
                prompt = (
                    "Apply the following requested update before merge.\n\n"
                    f"Task: {task}\n"
                    f"Requested change: {request}\n\n"
                    "Make only the necessary code changes, keep behavior stable, "
                    "and run any relevant verification commands."
                )
                result = await self._run_agent_and_wait(prompt, model)
                if result["returncode"] not in (0, -1):
                    self._log_write(
                        f"[red]Requested change failed: {result['stderr'][:300]}[/red]"
                    )
                    continue

                self._log_write(
                    "[green]Requested change applied. Re-running review...[/green]"
                )
                header = self.query_one(PhaseHeader)
                self.current_phase = 5
                header.set_phase(5, "running")
                await self._phase_review(task, pr_url)
                header.set_phase(5, "complete")
                self.current_phase = 6
                header.set_phase(6, "waiting")

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

    async def _wait_for_decision(
        self, prompt: str, options: list[tuple[str, str]]
    ) -> str:
        """Show a decision menu and wait for a selected option id."""
        menu = self.query_one("#decision-menu", OptionList)
        prompt_widget = self.query_one("#decision-prompt", Static)

        menu.clear_options()
        for option_id, label in options:
            menu.add_option(Option(label, id=option_id))

        prompt_widget.update(prompt)
        prompt_widget.styles.display = "block"
        menu.styles.display = "block"
        self._decision_active = True
        if menu.option_count > 0:
            menu.highlighted = 0

        self._decision_future = asyncio.get_event_loop().create_future()
        result = await self._decision_future
        self._decision_future = None
        self._decision_active = False
        prompt_widget.styles.display = "none"
        menu.styles.display = "none"
        self.query_one("#message-input", VimMessageArea).focus()
        return result

    def _commit_decision(self) -> None:
        """Resolve the pending decision from the highlighted menu option."""
        if not self._decision_future or self._decision_future.done():
            return
        menu = self.query_one("#decision-menu", OptionList)
        if menu.highlighted is None:
            return
        option = menu.get_option_at_index(menu.highlighted)
        option_id = option.id
        if option_id:
            self._decision_future.set_result(str(option_id))

    def _cancel_decision(self) -> None:
        """Cancel the pending decision interaction."""
        if self._decision_future and not self._decision_future.done():
            self._decision_future.set_result("cancel")

    def _update_slash_menu(self, value: str) -> None:
        """Show/hide the slash command menu based on input value."""
        menu = self.query_one("#slash-menu", OptionList)

        if "\n" in value:
            menu.styles.display = "none"
            return

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

    @on(TextArea.Changed, "#message-input")
    def on_message_input_changed(self, event: TextArea.Changed) -> None:
        """Update slash menu as user types."""
        self._update_slash_menu(event.control.text.strip())

    def _is_slash_menu_visible(self) -> bool:
        menu = self.query_one("#slash-menu", OptionList)
        return str(menu.styles.display) != "none"

    def _update_config_status(self) -> None:
        """Update the config editor status bar text."""
        status = self.query_one("#config-status", Static)
        editor = self.query_one("#config-editor", VimTextArea)

        if editor.command_mode:
            status.update(
                f" [bold cyan]{editor.command_prefix}{editor.command_text}[/bold cyan]"
            )
            return

        if self._vim_enabled:
            vim_indicator = f"  [bold]{self._vim.mode_label}[/bold]  |"
            close_hint = ":q close  |  :w save  |  :wq save+close  |  :%s/old/new/g"
        else:
            vim_indicator = ""
            close_hint = "Escape cancel"
        status.update(
            f"{vim_indicator}  Ctrl+S save  |  Ctrl+D discard  |  {close_hint}"
        )

    def on_key(self, event) -> None:
        """Handle key events for config controls, decision menu, and slash menu."""
        # ── Decision menu keys ───────────────────────────────────────
        if self._decision_active:
            menu = self.query_one("#decision-menu", OptionList)
            down_keys = {"down"}
            up_keys = {"up"}
            if self._vim_enabled:
                down_keys.add("j")
                up_keys.add("k")

            if event.key in up_keys:
                if menu.highlighted is not None:
                    if menu.highlighted > 0:
                        menu.highlighted -= 1
                    else:
                        menu.highlighted = menu.option_count - 1
                event.prevent_default()
                event.stop()
                return
            if event.key in down_keys:
                if menu.highlighted is not None:
                    if menu.highlighted < menu.option_count - 1:
                        menu.highlighted += 1
                    else:
                        menu.highlighted = 0
                event.prevent_default()
                event.stop()
                return
            if event.key == "enter":
                self._commit_decision()
                event.prevent_default()
                event.stop()
                return
            if event.key == "escape":
                self._cancel_decision()
                event.prevent_default()
                event.stop()
                return
            return

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
                    inp = self.query_one("#message-input", VimMessageArea)
                    inp.clear()
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
            inp = self.query_one("#message-input", VimMessageArea)
            inp.clear()
            menu = self.query_one("#slash-menu", OptionList)
            menu.styles.display = "none"
            log = self.query_one("#output-log", RichLog)
            self._handle_slash_command(cmd, log)

    @on(OptionList.OptionSelected, "#decision-menu")
    def on_decision_menu_selected(self, event: OptionList.OptionSelected) -> None:
        """Resolve decision selection from mouse/enter on menu."""
        if (
            self._decision_future
            and not self._decision_future.done()
            and event.option_id
        ):
            self._decision_future.set_result(str(event.option_id))

    @on(VimMessageArea.Submitted, "#message-input")
    def on_input_submitted(self, event: VimMessageArea.Submitted) -> None:
        """Handle user input from the text box."""
        message = event.value.strip()

        if not message:
            if self._waiting_for_config_description:
                log = self.query_one("#output-log", RichLog)
                log.write("[dim]Config request cancelled.[/dim]")
                self._waiting_for_config_description = False
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

        # ── Shell escape (!command) ──────────────────────────────────
        if message.startswith("!"):
            shell_cmd = message[1:].strip()
            if not shell_cmd:
                log.write("[dim]Usage: !<command>  (e.g. !ls, !cd .., !pwd)[/dim]")
                return
            # Handle !cd specially — change workdir
            if shell_cmd.startswith("cd "):
                target = shell_cmd[3:].strip()
                new_dir = os.path.abspath(os.path.join(self.workdir, target))
                if os.path.isdir(new_dir):
                    self.workdir = new_dir
                    self.query_one(Sidebar).set_workdir(self.workdir)
                    log.write(f"[dim]Changed directory to: {self.workdir}[/dim]")
                else:
                    log.write(f"[red]Not a directory: {new_dir}[/red]")
                return
            if shell_cmd == "cd":
                log.write(f"[dim]Current directory: {self.workdir}[/dim]")
                return
            # Run other shell commands
            log.write(f"[bold red]![/bold red] {shell_cmd}")
            try:
                result = subprocess.run(
                    shell_cmd,
                    shell=True,
                    cwd=self.workdir,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.stdout.strip():
                    log.write(f"[dim]{result.stdout.strip()}[/dim]")
                if result.stderr.strip():
                    log.write(f"[red]{result.stderr.strip()}[/red]")
                if result.returncode != 0:
                    log.write(f"[red]Exit code: {result.returncode}[/red]")
            except subprocess.TimeoutExpired:
                log.write("[red]Command timed out (30s limit).[/red]")
            except Exception as e:
                log.write(f"[red]Error: {e}[/red]")
            return

        log.write(f"\n[bold green]You:[/bold green] {message}")

        # ── Plain-text config flow ───────────────────────────────────
        if self._waiting_for_config_description:
            self._waiting_for_config_description = False
            self._configure_from_plain_text(message)
            return

        # ── Startup flow — collecting task ───────────────────────────
        if self._startup_phase == "task":
            self.user_task = message
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
        self.query_one("#message-input", VimMessageArea).styles.display = "none"
        self.query_one("#input-mode", Static).styles.display = "none"
        self.query_one("#decision-prompt", Static).styles.display = "none"
        self.query_one("#decision-menu", OptionList).styles.display = "none"
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
        self.query_one("#message-input", VimMessageArea).styles.display = "block"
        self.query_one(Sidebar).styles.display = "block"
        if self._vim_enabled:
            # Return chat input to INSERT mode by default.
            self._vim.mode = "insert"
        self._update_input_mode_status()

        self._editing_config = False
        self.query_one("#message-input", VimMessageArea).focus()

    def _handle_vim_command(self, command: str) -> None:
        """Handle vim-style config commands (:q, :w, :wq, :%s/old/new/g)."""
        raw = command.strip()
        cmd = raw.lower()
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

        sub_match = re.match(r"^%s/(.*?)/(.*?)/(g?)$", raw)
        if sub_match:
            pattern, replacement, flag = sub_match.groups()
            editor = self.query_one("#config-editor", VimTextArea)
            text = editor.text
            try:
                if flag == "g":
                    new_text, replacements = re.subn(pattern, replacement, text)
                else:
                    replacements = 0
                    updated_lines: list[str] = []
                    for line in text.splitlines(keepends=True):
                        new_line, count = re.subn(pattern, replacement, line, count=1)
                        replacements += count
                        updated_lines.append(new_line)
                    new_text = "".join(updated_lines)
            except re.error as e:
                status.update(f" [red]Substitution error:[/red] {e}")
                return

            if replacements == 0:
                status.update(" [yellow]No matches found.[/yellow]")
                return

            editor.load_text(new_text)
            status.update(f" [green]{replacements} substitution(s) applied.[/green]")
            return

        status.update(
            " [red]Unknown command:[/red] "
            f":{command} [dim](use :q, :w, :wq, :%s/old/new/g)[/dim]"
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

        self._apply_loaded_config(new_config)

        if close_editor:
            self._close_config_editor()
            log = self.query_one("#output-log", RichLog)
            log.write(self.t.s("success", "Configuration saved and reloaded."))
            log.write(f"[dim]Models: {self._models_summary()}[/dim]")
        else:
            self._update_config_status()

        return True

    def _apply_loaded_config(
        self, new_config: dict, reset_vim_insert: bool = False
    ) -> None:
        """Apply a newly loaded config to runtime state and UI widgets."""
        self.config = new_config
        self.t = Theme(self.config)
        self._apply_textual_theme()
        self._vim_enabled = self.config.get("editor", {}).get("vim_mode", False)
        self._apply_ui_settings()
        self._sync_vim_state()
        self.query_one(PhaseHeader).set_models(self._models_summary())
        if reset_vim_insert and self._vim_enabled:
            self._vim.mode = "insert"
            self._update_input_mode_status()

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

    def _persist_theme_preset(self, preset: str) -> bool:
        """Save theme preset (and resolved colors) to config file."""
        preset_name = preset.lower().strip()
        if preset_name not in THEME_PRESETS:
            return False

        try:
            with open(self._config_path) as f:
                content = f.read()
            config = yaml.safe_load(content)
            if not isinstance(config, dict):
                return False

            resolved = dict(THEME_PRESETS[preset_name])
            resolved["preset"] = preset_name
            config["theme"] = resolved

            with open(self._config_path, "w") as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False)

            self._apply_loaded_config(config)
            return True
        except (OSError, yaml.YAMLError):
            return False

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

            self._apply_loaded_config(cfg, reset_vim_insert=True)
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

        model = self.config["models"].get("configure", self.config["models"]["feature"])
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

        self._apply_loaded_config(new_config, reset_vim_insert=True)
        self._log_write(self.t.s("success", "Config updated from plain text request."))
        self._log_write(f"[dim]Models: {self._models_summary()}[/dim]")

    def _handle_slash_command(self, message: str, log: RichLog) -> None:
        """Handle /commands from the input box."""
        parts = message.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "/help":
            vim_state = "on" if self._vim_enabled else "off"
            current_preset = self.config.get("theme", {}).get("preset", "default")
            log.write(
                "\n[bold]Available commands:[/bold]\n"
                "  [bold]/configure[/bold]     Describe config changes in plain text\n"
                "  [bold]/config[/bold]        Edit configuration\n"
                "  [bold]/help[/bold]          Show this help\n"
                "  [bold]/exit[/bold]          Quit the application\n"
                "  [bold]/skip[/bold]          Skip the current phase or question\n"
                "  [bold]/status[/bold]        Show current pipeline status\n"
                "  [bold]/task[/bold]          Show the current task description\n"
                f"  [bold]/theme[/bold]         Set/view theme preset (current: {current_preset})\n"
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
                f"  Queued messages: {len(self.message_queue)}\n"
                f"  Models: {self._models_summary()}"
            )
        elif cmd == "/task":
            log.write(f"\n[bold]Task:[/bold] {self.user_task}")
        elif cmd == "/theme":
            arg = arg.strip().lower()
            if not arg:
                current = self.config.get("theme", {}).get("preset", "default")
                presets = ", ".join(sorted(THEME_PRESETS.keys()))
                log.write(
                    f"\n[bold]Theme preset:[/bold] {current}\n"
                    f"[dim]Available: {presets}[/dim]\n"
                    "[dim]Usage: /theme <preset>  (example: /theme nord)[/dim]"
                )
            elif arg not in THEME_PRESETS:
                presets = ", ".join(sorted(THEME_PRESETS.keys()))
                log.write(
                    f"[red]Unknown theme preset: {arg}[/red] "
                    f"[dim](available: {presets})[/dim]"
                )
            else:
                if self._persist_theme_preset(arg):
                    log.write(
                        self.t.s(
                            "success",
                            f"Theme set to '{arg}' and saved to config.",
                            bold=True,
                        )
                    )
                else:
                    log.write("[red]Failed to save theme preset to config.[/red]")
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
