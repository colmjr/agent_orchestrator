import unittest

from agent_orchestrator.tui import VimHandler


class _FakeDocument:
    def __init__(self, editor: "_FakeTextArea") -> None:
        self._editor = editor

    @property
    def lines(self) -> list[str]:
        return self._editor.text.split("\n")

    @property
    def line_count(self) -> int:
        return len(self.lines)

    def get_line(self, row: int) -> str:
        return self.lines[row]


class _FakeTextArea:
    def __init__(self, text: str, cursor_location: tuple[int, int] = (0, 0)) -> None:
        self.text = text
        self.cursor_location = cursor_location
        self.document = _FakeDocument(self)

        self.cursor_word_right_calls = 0
        self.delete_line_calls = 0
        self.delete_word_right_calls = 0

    def _to_index(self, location: tuple[int, int]) -> int:
        row, col = location
        lines = self.text.split("\n")
        row = max(0, min(row, len(lines) - 1))
        col = max(0, min(col, len(lines[row])))
        return sum(len(line) + 1 for line in lines[:row]) + col

    def _from_index(self, index: int) -> tuple[int, int]:
        lines = self.text.split("\n")
        text_len = sum(len(line) + 1 for line in lines[:-1]) + len(lines[-1])
        idx = max(0, min(index, text_len))
        running = 0
        for row, line in enumerate(lines):
            end = running + len(line)
            if idx <= end:
                return (row, idx - running)
            running = end + 1
        return (len(lines) - 1, len(lines[-1]))

    def move_cursor(self, target: tuple[int, int], center: bool = False) -> None:
        del center
        self.cursor_location = target

    def replace(
        self, new_text: str, start: tuple[int, int], end: tuple[int, int]
    ) -> None:
        start_idx = self._to_index(start)
        end_idx = self._to_index(end)
        self.text = self.text[:start_idx] + new_text + self.text[end_idx:]
        self.cursor_location = self._from_index(start_idx + len(new_text))

    def action_cursor_word_right(self) -> None:
        self.cursor_word_right_calls += 1

    def action_delete_line(self) -> None:
        self.delete_line_calls += 1

    def action_delete_word_right(self) -> None:
        self.delete_word_right_calls += 1

    def action_delete_to_end_of_line(self) -> None:
        row, col = self.cursor_location
        line = self.document.get_line(row)
        self.replace("", (row, col), (row, len(line)))

    def action_delete_right(self) -> None:
        row, col = self.cursor_location
        line = self.document.get_line(row)
        if col >= len(line):
            return
        self.replace("", (row, col), (row, col + 1))


class VimHandlerSequenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vim = VimHandler()
        self.editor = _FakeTextArea("hello world")

    def test_count_motion_3w(self) -> None:
        self.vim.handle_key_textarea("3", "3", self.editor)
        self.vim.handle_key_textarea("w", "w", self.editor)
        self.assertEqual(self.editor.cursor_word_right_calls, 3)

    def test_count_delete_5dd(self) -> None:
        self.vim.handle_key_textarea("5", "5", self.editor)
        self.vim.handle_key_textarea("d", "d", self.editor)
        self.vim.handle_key_textarea("d", "d", self.editor)
        self.assertEqual(self.editor.delete_line_calls, 5)

    def test_change_word_cw_enters_insert(self) -> None:
        self.vim.handle_key_textarea("c", "c", self.editor)
        self.vim.handle_key_textarea("w", "w", self.editor)
        self.assertEqual(self.editor.delete_word_right_calls, 1)
        self.assertEqual(self.vim.mode, "insert")

    def test_change_inside_quotes_ci_quote(self) -> None:
        editor = _FakeTextArea('say "hello" now', cursor_location=(0, 6))
        self.vim.handle_key_textarea("c", "c", editor)
        self.vim.handle_key_textarea("i", "i", editor)
        self.vim.handle_key_textarea('"', '"', editor)
        self.assertEqual(editor.text, 'say "" now')
        self.assertEqual(self.vim.mode, "insert")

    def test_repeat_dot_after_x(self) -> None:
        editor = _FakeTextArea("hello", cursor_location=(0, 0))
        self.vim.handle_key_textarea("x", "x", editor)
        self.assertEqual(editor.text, "ello")
        self.vim.handle_key_textarea(".", ".", editor)
        self.assertEqual(editor.text, "llo")


if __name__ == "__main__":
    unittest.main()
