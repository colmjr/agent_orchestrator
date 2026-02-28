# Agent Orchestrator — TODO

## Vim Mode Roadmap

### Implemented
- [x] Normal / insert mode toggle
- [x] `i`, `a`, `A`, `I` — enter insert mode
- [x] `o`, `O` — open line below / above
- [x] `Escape` — return to normal mode
- [x] `h`, `j`, `k`, `l` — cursor movement
- [x] `w`, `b` — word forward / backward
- [x] `e` — end of word
- [x] `0`, `$` — start / end of line
- [x] `x` — delete character under cursor
- [x] `dd` — delete entire line
- [x] `dw` — delete word
- [x] `d$` / `D` — delete to end of line
- [x] `cw` — change word
- [x] `c$` / `C` — change to end of line
- [x] `ci"` — change inside quotes
- [x] `di(` — delete inside parens
- [x] `f{char}` — find character on line
- [x] `t{char}` — till character on line
- [x] `u` — undo
- [x] `Ctrl+R` — redo
- [x] `p` — paste from clipboard
- [x] `gg` — go to top of file
- [x] `G` — go to bottom of file
- [x] `v` — character-wise visual selection
- [x] `V` — line-wise visual selection
- [x] `d` / `y` — delete / yank in visual mode
- [x] `/pattern` — forward search
- [x] `?pattern` — backward search
- [x] `n` / `N` — next / previous match
- [x] `3j`, `5dd` — count prefixes
- [x] `.` — repeat last change
- [x] `J` — join lines
- [x] `>>` / `<<` — indent / dedent
- [x] `Ctrl+D` / `Ctrl+U` / `Ctrl+F` / `Ctrl+B` — scroll
- [x] `:w` / `:q` / `:wq` — save / quit (config editor)
- [x] `:%s/old/new/g` — substitution (config editor)
- [x] Vim mode indicator in status bar and sidebar
- [x] Works in both TextArea (config editor) and message input
- [x] Vim mode persisted in config.yaml

### Not Planned
- Registers (`"ay`, `"ap`)
- Marks (`ma`, `'a`)
- Macros (`qa`, `@a`)

### Remaining
- [ ] `*` — search for word under cursor

## General Features

### Implemented
- [x] Interactive TUI (default mode)
- [x] Headless CLI mode (`--no-tui`)
- [x] 7-phase pipeline (Phase 0-6)
- [x] Clarifying questions (Phase 0)
- [x] Plan quality guardrails and approval gate (Phase 1)
- [x] Decision menus for plan approval and merge approval
- [x] Per-phase model configuration
- [x] Real-time streaming output via `opencode run --format json`
- [x] Session continuation across phases
- [x] Local mode (no git remote)
- [x] Base branch resolution fallback
- [x] Slash commands with fuzzy-filtered popup menu
- [x] `/config` — full-screen YAML editor
- [x] `/configure` — AI-powered plain-English config editing
- [x] Theme presets (`default`, `nord`) with `/theme` command
- [x] Multi-line input (Enter send, Ctrl+J newline)
- [x] Output pane scrolling without focus change (PageUp/Down, Ctrl+Up/Down, mouse wheel)
- [x] Sidebar (directory, branch, tokens, cost, TODO progress, modified files)
- [x] Configurable input height (`ui.input_height`)
- [x] Auto git init for new directories

### Remaining
- [ ] End-to-end testing of TUI pipeline
- [x] Additional theme presets (gruvbox, dracula, solarized, monokai, tokyo-night, catppuccin)
- [ ] Syntax highlighting for agent output (markdown)
- [ ] Resize / toggle sidebar visibility
