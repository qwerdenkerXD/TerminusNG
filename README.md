# TerminusNG

A hardened fork of [Terminus](https://github.com/randy3k/Terminus), the cross-platform
terminal for Sublime Text, by [@randy3k](https://github.com/randy3k). MIT, same as
upstream. Forked from `master` at `0cccd3f`.

Everything upstream does, it still does — this is surgical hardening, not a rewrite.
The usage documentation below is upstream's and still applies.

## Why

Two bugs, both with small root causes:

**Home and End did nothing.** `key.py` sent `ESC[1~` / `ESC[4~`, the `TERM=linux`
console codes, while the shipped default is `xterm-256color`, whose terminfo declares
`khome=\EOH` / `kend=\EOF`. Readline waited for bytes that never arrived, so the keys
were silently unbound — dead in zsh, fish, `bash -o vi`, `less` and the Python REPL.
ConPTY accepts all three sequence families, so this never reproduced on Windows
native, only under WSL and Unix.

**Resizing the pane killed the terminal.** `view_size()` has two escape hatches for an
unmeasurable viewport, both keyed on its `default` argument — and the resize path
passed none. Mid-drag `viewport_extent()` reads `(0,0)`, the terminal was resized to
**one row**, which SIGWINCHes `vim`/`less`/`htop` into exiting, and `auto_close` then
closed the tab.

## What is different

### Input
- Home/End emit the correct sequences in all three forms: `ESC[H`/`ESC[F` normally,
  `ESC OH`/`ESC OF` under DECCKM, `ESC[1;{m}H` with a modifier. Any modifier forces
  CSI, never SS3.
- Modifiers compose. `get_key_code` branched `if ctrl / elif alt / elif shift`, so
  `ctrl+alt+f` sent only `\x06`. It uses the xterm modifier bitmask now.
- Modified Home/End no longer emit `ESC[1;5~`, which belongs to neither escape family.
- `f11` was missing from the key table and typed the literal string `f11`.

### Stability
- The renderer no longer tears a terminal down on a single transient "unhosted"
  reading; a view which is genuinely gone is still reaped promptly, so panel
  terminals, which never get an `on_pre_close`, do not leak.
- An exception in a dispatch handler no longer kills the render thread for good. The
  pyte stream generator is restarted after a handler raises — previously any raise
  closed it permanently and every later feed raised `StopIteration`, leaving the
  terminal blank for the rest of its life.
- A failed `spawn` no longer leaves a registered terminal with no process, which used
  to make the view impossible to close for the rest of the session.
- Resizing respects a `min_rows` floor and never sends a degenerate size.

### Terminal emulation
- Multi-digit OSC codes dispatch. `OSC 2` (window title) and `OSC 1337` (imgcat) were
  unreachable because the parser read the code one character at a time.
- `draw()` no longer discards the rest of a text run on a zero-width character, which
  silently truncated any line containing an emoji variation selector or ZWJ.
- Colon subparameters (`ESC[38:2::r:g:b`, used by neovim and delta) are consumed
  instead of leaking onto the screen as literal text.
- `resize()` clamps the cursor, restores the alternate buffer at the right geometry,
  and `reset()` clears alternate-buffer state — a `RIS` from the alt screen used to
  kill scrollback silently for the rest of the session.
- `index()` archives a line only when the scrolling region starts at the top, so a
  pager's pinned status line is no longer copied into the scrollback on every scroll.

### Shell integration
See [SHELL_INTEGRATION.md](SHELL_INTEGRATION.md) for the snippets.

- **OSC 7** — the shell reports its working directory, and a terminal opened from a
  terminal starts there.
- **OSC 133** — semantic prompt marking, powering *Jump to Previous/Next Prompt*,
  *Select Command Output* and *Copy Command Output*. Optional gutter markers, red for
  a command that exited non-zero (`prompt_markers`, off by default: a prompt that
  reports OSC 133 usually shows its own exit status).
- **OSC 8** — hyperlinks are underlined and clickable. Only `http`, `https` and `file`
  targets are ever opened; the scheme is checked at parse time, after stripping
  whitespace and control characters and after percent-decoding, so a refused target is
  never stored. The hover popup shows the **host** first and on its own, because
  `https://www.paypal.com@attacker.example/` reads as a bank for its first 22
  characters, and bidi and zero-width characters are spelled out as `<U+202E>`.

### Windows and WSL
- `wsl.exe` inherits nothing from the Windows environment unless the variable is named
  in `WSLENV`, so Terminus's own `TERM_PROGRAM == "Terminus-Sublime"` contract was
  invisible inside WSL. `TERM`, `COLORTERM` and `TERM_PROGRAM` are now shared through
  it, which is what makes the shell-integration snippets fire.
- Paths are translated across the boundary (`/mnt/c/...` ↔ `C:\...`,
  `/home/you` ↔ `\\wsl.localhost\<distro>\home\you`), so a reported cwd or a clicked
  file link works from the Windows side.
- Translation refuses rather than guesses. `--distribution-id` and `--system` name a
  distribution in a form that cannot be resolved, and since the same posix path exists
  in every installed distro, guessing would have opened a file from the wrong one.
  Components ending in `.` or a space are refused too — Windows silently rewrites
  them, so a link to `report.` would open `report`.
- **Terminus Utilities: Backend Info** reports the PTY backend, pywinpty version and
  ConPTY availability. Run it before reporting anything Windows-specific.

### Performance
- Paste is no longer capped at ~5 KiB/s by a fixed `sleep(0.1)` per 512 bytes; 1 MiB
  used to take over three minutes.
- Colour regions are batched by scope instead of one `add_regions` per segment.
- The 256-colour scheme is regenerated when the theme's background actually changes.
  It was only rewritten when forced, so switching dark↔light by editing settings left
  256-colour cells rendering against the previous background.
- The adaptive theme no longer overrides the editor's selection colours, so selecting
  in a terminal looks like selecting anywhere else.

## New settings

| setting | default | |
|---|---|---|
| `min_rows` | `4` | floor for the terminal height; a one-row pty makes `vim`, `less` and `htop` unusable |
| `follow_shell_cwd` | `true` | open a new terminal in the directory the current shell reported via OSC 7 |
| `hyperlinks` | `true` | underline and open OSC 8 hyperlinks |
| `prompt_markers` | `false` | gutter markers on prompt rows, red when the command failed |

## New commands

`Terminus: Jump to Previous Prompt` · `Terminus: Jump to Next Prompt` ·
`Terminus: Select Command Output` · `Terminus: Copy Command Output` ·
`Terminus: Open Shell Directory as Folder` · `Terminus Utilities: Backend Info`

No key bindings are shipped for these. In a terminal almost every keystroke belongs to
the shell, so binding them is left to you.

## Installation

Not on Package Control — clone it and shadow the installed package. An unpacked
`Packages/Terminus` takes precedence over `Installed Packages/Terminus.sublime-package`,
and the dependencies (`pyte`, `wcwidth`, `pywinpty`/`ptyprocess`) keep resolving
because the package name is unchanged.

Windows:

```cmd
mklink /J "%APPDATA%\Sublime Text\Packages\Terminus" "<path to this repo>"
```

Linux / macOS:

```sh
ln -s <path to this repo> "$HOME/.config/sublime-text/Packages/Terminus"
```

Delete the link to go back to the Package Control copy.

## Known limitations

- **No truecolour.** `ESC[38;2;r;g;b` is parsed correctly but mapped to the nearest of
  256 colours. Colours reach the view as scopes in a generated `.hidden-color-scheme`,
  so 24-bit would mean writing and reloading a scheme file per novel colour. This is a
  ceiling of Sublime's API, not of this fork — any in-editor terminal hits it.
- **Sublime has no resize event.** [sublimehq/sublime_text#12](https://github.com/sublimehq/sublime_text/issues/12)
  has been open since 2013; polling is the only available design.
- `Default.sublime-keymap` is still 36 KB of hand-enumerated bindings. That is the
  structural reason `f11` went missing for years. Generating it from a spec is the real
  fix and has not been done.
- Detached terminals still cannot be reattached after a tab closes. Run `tmux` inside
  Terminus if you want that; it is better at it.
- `reported_cwd` stats a UNC path on the UI thread, so a stopped WSL VM can stall
  Sublime briefly when opening a terminal.

---

The rest of this document is upstream's, and still applies.

## Shell configurations

Terminus comes with several shell configurations. The settings file should be quite self explanatory. 


## User Key Bindings

You may find these key bindings useful. To edit, run `Preferences: Terminus Key Bindings`.
Check the details for the arguments of `terminus_open` below.


- toggle terminal panel
```json
[
    { 
        "keys": ["alt+`"], "command": "toggle_terminus_panel"
    }
]
```

- open a terminal view at current file directory
```json
[
    { 
        "keys": ["ctrl+alt+t"], "command": "terminus_open", "args": {
            "cwd": "${file_path:${folder}}"
        }
    }
]
```
or by passing a custom `cmd`, say `ipython`
```json
[
    { 
        "keys": ["ctrl+alt+t"], "command": "terminus_open", "args": {
            "cmd": "ipython",
            "cwd": "${file_path:${folder}}"
        }
    }
]
```

- open terminal in a split view by using [Origami](https://github.com/SublimeText/Origami)'s `carry_file_to_pane`
```json
[
    {
        "keys": ["ctrl+alt+t"],
        "command": "terminus_open",
        "args": {
            "post_window_hooks": [
                ["carry_file_to_pane", {"direction": "down"}]
            ]
        }
    }
]
```

- <kbd>ctrl-w</kbd> to close terminal

Following keybinding can be considered if one wants to use `ctrl+w` to close terminals.

```json
{ 
    "keys": ["ctrl+w"], "command": "terminus_close", "context": [{ "key": "terminus_view"}]
}
```

## User Commands in Palette

- run `Preferences: Terminus Command Palette`. Check the details for the arguments of `terminus_open` below

```json
[
    {
        "caption": "Terminus: Open Default Shell at Current Location",
        "command": "terminus_open",
        "args"   : {
            "cwd": "${file_path:${folder}}"
        }
    }
]
```
or by passing custom `cmd`, say `ipython`

```json
[
    {
        "caption": "Terminus: Open iPython",
        "command": "terminus_open",
        "args"   : {
            "cmd": "ipython",
            "cwd": "${file_path:${folder}}",
            "title": "iPython"
        }
    }
]
```

- open terminal in a split tab by using [Origami](https://github.com/SublimeText/Origami)'s `carry_file_to_pane`

```json
[
    {
        "caption": "Terminus: Open Default Shell in Split Tab",
        "command": "terminus_open",
        "args": {
            "post_window_hooks": [
                ["carry_file_to_pane", {"direction": "down"}]
            ]
        }
    }
]
```

## Terminus Build System

It is possible to use `Terminus` as a build system. The target `terminus_exec` is a drop in replacement of the default target `exec`. It takes exact same arguments as `terminus_open` except that their default values are set differently.

`terminus_cancel_build` is used to cancel the build when user runs `cancel_build` triggered by <kbd>ctrl+c</kbd> (macOS) or <kbd>ctrl+break</kbd> (Windows / Linux).

The following is an example of build system define in project settings that run a python script

```json
{
    "build_systems":
    [
        {
            "name": "Hello World",
            "target": "terminus_exec",
            "cancel": "terminus_cancel_build",
            "cmd": [
                "python", "helloworld.py"
            ],
            "working_dir": "$folder"
        }
    ]
}
```

The same Hello World example could be specified via a `.sublime-build` file.

```json
{
    "target": "terminus_exec",
    "cancel": "terminus_cancel_build",
    "cmd": [
        "python", "helloworld.py"
    ],
    "working_dir": "$folder"
}
```

Instead of `cmd`, user could also specify `shell_cmd`. In macOS and linux, a bash shell will be invoked; and in Windows, cmd.exe will be invoked.

```json
{
    "target": "terminus_exec",
    "cancel": "terminus_cancel_build",
    "shell_cmd": "python helloworld.py",
    // to directly invoke bash command
    // "shell_cmd": "echo helloworld",
    "working_dir": "$folder"
}
```

## Alt-Left/Right to move between words (Unix)

- Bash: add the following in `.bash_profile` or `.bashrc`

    ```sh
    if [ "$TERM_PROGRAM" == "Terminus-Sublime" ]; then
        bind '"\e[1;3C": forward-word'
        bind '"\e[1;3D": backward-word'
    fi
    ```

- Zsh: add the following in `.zshrc`

    ```sh
    if [ "$TERM_PROGRAM" = "Terminus-Sublime" ]; then
        bindkey "\e[1;3C" forward-word
        bindkey "\e[1;3D" backward-word
    fi
    ```

Some programs, such as julia, do not recognize the standard keycodes for `alt+left` and `alt+right`. You could
bind them to `alt+b` and `alt+f` respectively
```json
[
    { "keys": ["alt+left"], "command": "terminus_keypress", "args": {"key": "b", "alt": true}, "context": [{"key": "terminus_view"}] },
    { "keys": ["alt+right"], "command": "terminus_keypress", "args": {"key": "f", "alt": true}, "context": [{"key": "terminus_view"}] }
]
```

## Terminus API

- A terminal could be opened using the command `terminus_open` with

```py
window.run_command(
    "terminus_open", {
        "config_name": None,     # the shell config name, use `None` for the default config
        "cmd": None,             # the cmd to execute
        "shell_cmd": None,       # a script to execute in a shell
                                 # bash on Unix and cmd.exe on Windows
        "cwd": None,             # the working directory
        "working_dir": None,     # alias of "cwd"
        "env": {},               # extra environmental variables
        "title": None,           # title of the view, let terminal configures it if leave empty
        "panel_name": None,      # the name of the panel if terminal should be opened in panel
        "focus": True,           # focus to the panel
        "tag": None,             # a tag to identify the terminal
        "file_regex": None,      # the `file_regex` pattern in sublime build system
                                 # see https://www.sublimetext.com/docs/3/build_systems.html
        "line_regex": None,      # the `file_regex` pattern in sublime build system
        "pre_window_hooks": [],  # a list of window hooks before opening terminal
        "post_window_hooks": [], # a list of window hooks after opening terminal
        "post_view_hooks": [],   # a list of view hooks after opening terminal
        "view_settings": {},     # extra view settings which are passed to the terminus_view
        "auto_close": False,     # auto close terminal, possible values are "always" (True), "on_success", and False.
        "cancellable": False,    # allow `cancel_build` command to terminate process, only relevent to panels
        "timeit": False,         # display elapsed time when the process terminates
    }
)
```

The fields `cmd` and `cwd` understand Sublime Text build system [variables](https://www.sublimetext.com/docs/3/build_systems.html#variables).


- the setting `view.settings().get("terminus_view.tag")` can be used to identify the terminal and 

- keybind can be binded with specific tagged terminal

```json
    {
        "keys": ["ctrl+alt+w"], "command": "terminus_close", "context": [
            { "key": "terminus_view.tag", "operator": "equal", "operand": "YOUR_TAG" }
        ]
    }
```

- text can be sent to the terminal with

```py
window.run_command(
    "terminus_send_string", 
    {
        "string": "ls\n",
        "tag": "<YOUR_TAG>",       # ignore this or set it to None to send text to the first terminal found
        "visible_only": False,     # send to visible terminal only, default is `False`. Only relevent when `tag` is None
    }
)
```

If `tag` is not provided or is `None`, the text will be sent to the first terminal found in the current window.


## FAQ

### Memory issue

It is known that Terminus sometimes consumes a lot of memory after extensive use. It is because Sublime Text keeps an infinite undo stack. There is virtually no fix unless upstream provides an API to work with the undo stack. Meanwhile, users could execute `Terminus: Reset` to release the memory.

This issue has been fixed in Sublime Text >= 4114 and Terminus v0.3.20.

### Color issue when maximizing and minimizing terminal

It is known that the color of the scrollback history will be lost when a terminal is maximized or minimized from or to the panel. There is no fix for this issue.


### Terminal panel background issue

If you are using DA UI and your terminal panel has weird background color,
try playing with the setting `panel_background_color` or `panel_text_output_background_color` in `DA UI: Theme
Settings`.

<img src="https://user-images.githubusercontent.com/1690993/41728204-31a9a2a2-7544-11e8-9fb6-a37b59da852a.png" width="50%" />

```json
{
    "panel_background_color": "$background_color"
}
```
Or, to keep the Find and Replace panels unchanged:
```json
"panel_text_output_background_color": "$background_color"
```


### Cmd.exe rendering issue in panel

Due to a upstream bug (may winpty or cmd.exe?), there may be arbitrary empty lines inserted between prompts if the panel is too short. It seems that cmder and powershell are not affected by this bug.


### Acknowledgments

This package won't be possible without [pyte](https://github.com/selectel/pyte), [pywinpty](https://github.com/spyder-ide/pywinpty) and [ptyprocess](https://github.com/pexpect/ptyprocess).
