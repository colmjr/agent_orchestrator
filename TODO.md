# Agent Orchestrator — TODO

## Vim Mode Roadmap

### Implemented (Basic)
- [x] Normal / insert mode toggle
- [x] `i`, `a`, `A`, `I` — enter insert mode (at cursor, after cursor, end of line, start of line)
- [x] `o`, `O` — open line below / above
- [x] `Escape` — return to normal mode
- [x] `h`, `j`, `k`, `l` — cursor movement
- [x] `w`, `b` — word forward / backward
- [x] `0`, `$` — start / end of line
- [x] `x` — delete character under cursor
- [x] `dd` — delete entire line
- [x] `u` — undo
- [x] `Ctrl+R` — redo
- [x] `p` — paste from clipboard
- [x] `gg` — go to top of file
- [x] `G` — go to bottom of file
- [x] Vim mode indicator in config editor status bar
- [x] Works in both TextArea (config editor) and Input (prompt)

### Search
- [x] `/pattern` — forward search
- [x] `?pattern` — backward search
- [x] `n` — next match
- [x] `N` — previous match
- [ ] `*` — search for word under cursor

### Visual Mode
- [x] `v` — character-wise visual selection
- [x] `V` — line-wise visual selection
- [x] `d` — delete selection in visual mode
- [x] `y` — yank selection in visual mode

### Registers & Yank/Paste
- [ ] `yy` — yank current line
- [ ] `yw` — yank word
- [ ] `"ay` — yank into register a
- [ ] `"ap` — paste from register a

### Motions & Text Objects
- [x] `e` — end of word
- [x] `f{char}` — find character on line
- [x] `t{char}` — till character on line
- [x] `ci"` — change inside quotes
- [x] `di(` — delete inside parens
- [x] `dw` — delete word
- [x] `cw` — change word
- [x] `c$` / `C` — change to end of line
- [x] `d$` / `D` — delete to end of line

### Repeats & Counts
- [x] `3j` — move down 3 lines
- [x] `5dd` — delete 5 lines
- [x] `.` — repeat last change

### Command Mode
- [x] `:w` — save (in config editor)
- [x] `:q` — quit / close editor
- [x] `:wq` — save and quit
- [x] `:%s/old/new/g` — substitution

### Other
- [ ] Marks (`ma`, `'a`)
- [ ] Macros (`qa`, `@a`)
- [x] Scroll (`Ctrl+D`, `Ctrl+U`, `Ctrl+F`, `Ctrl+B`)
- [x] Join lines (`J`)
- [x] Indent/dedent (`>>`, `<<`)

## General Features
- [ ] End-to-end testing of TUI pipeline
- [ ] Configurable vim mode default (on/off in config.yaml)
- [ ] Persist vim mode preference between sessions
- [ ] Multi-line input mode for prompts (Shift+Enter for newline)
- [ ] Syntax highlighting for agent output (markdown)
- [ ] Resize sidebar / toggle sidebar visibility
