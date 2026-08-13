# Shell integration

Terminus only knows what the shell tells it. A shell reports its working directory
with **OSC 7**, and once it does, a new terminal opened from that one starts in the
same place instead of in the first project folder. A shell reports where its prompts,
its input and its command output begin and end with **OSC 133**, and once it does,
Terminus can tell the three apart instead of seeing one undifferentiated stream of
characters.

Nothing here is required — without it the terminal behaves exactly as before.
Set `"follow_shell_cwd": false` in the Terminus settings to ignore the OSC 7 reports.
OSC 133 has no setting of its own: the marks are only ever recorded, never drawn, and
a shell which does not report them simply leaves the commands below with nothing to
go on.

The two are independent. Emitting OSC 7 does not require OSC 133 and the other way
round, and the snippets below are written so both sets can live in the same rc file.

## What OSC 7 looks like

    ESC ] 7 ; file://HOSTNAME/PATH BEL

The path is percent-encoded and absolute. The hostname is ignored: a shell in a
container, over ssh, or inside WSL reports a path this side of the pty usually
cannot reach, and an unreachable directory is simply not used.

## bash

Add to `~/.bashrc`:

```bash
if [ -n "$TERM_PROGRAM" ] && [ "$TERM_PROGRAM" = "Terminus-Sublime" ]; then
    __terminus_osc7() {
        printf '\033]7;file://%s%s\007' "$HOSTNAME" "$PWD"
    }
    PROMPT_COMMAND="__terminus_osc7${PROMPT_COMMAND:+; $PROMPT_COMMAND}"
fi
```

`PROMPT_COMMAND` runs before each prompt, so the report follows every `cd`.

## zsh

Add to `~/.zshrc`:

```zsh
if [[ $TERM_PROGRAM == "Terminus-Sublime" ]]; then
    __terminus_osc7() {
        printf '\033]7;file://%s%s\007' "$HOST" "$PWD"
    }
    autoload -Uz add-zsh-hook
    add-zsh-hook chpwd __terminus_osc7
    __terminus_osc7
fi
```

`chpwd` fires on directory change; the trailing call reports the starting directory.

## fish

Add to `~/.config/fish/config.fish`:

```fish
if test "$TERM_PROGRAM" = "Terminus-Sublime"
    function __terminus_osc7 --on-variable PWD
        printf '\033]7;file://%s%s\007' (hostname) "$PWD"
    end
    __terminus_osc7
end
```

## PowerShell

Add to your `$PROFILE`:

```powershell
if ($env:TERM_PROGRAM -eq "Terminus-Sublime") {
    function global:prompt {
        $p = (Get-Location).Path
        Write-Host -NoNewline "$([char]27)]7;file://$env:COMPUTERNAME/$($p -replace '\\','/')$([char]7)"
        "PS $p> "
    }
}
```

## Semantic prompt marking (OSC 133)

OSC 7 answers *where am I*. OSC 133 answers *what is this text* — it splits the
scrollback into prompts, typed input and command output, and tags each finished
command with its exit status.

    ESC ] 133 ; A ST            fresh line, a prompt begins here
    ESC ] 133 ; B ST            the prompt ends, what follows is what you typed
    ESC ] 133 ; C ST            your input ends, what follows is command output
    ESC ] 133 ; D ST            the command finished, exit status unknown
    ESC ] 133 ; D ; 0 ST        the command finished with exit status 0
    ESC ] 133 ; P ; key=value   a semantic property; Terminus consumes and ignores it

`ST` is `ESC \`. Terminus also accepts `BEL` (`\007`) as the terminator, which is
easier to quote in a shell, and any subcommand it does not know is swallowed
silently — an unfamiliar shell integration will never print stray text or break the
terminal.

With the marks in place Terminus knows the first row of each prompt, the row where
output starts and where the command ended, which is what these command palette
entries work from:

* **Terminus: Jump to Previous Prompt** and **Terminus: Jump to Next Prompt**
  (`terminus_jump_to_prompt`, with `{"forward": true}` for the second) scroll from
  prompt to prompt. They only move the viewport — the terminal cursor stays where the
  shell put it, so scrolling back to the bottom resumes typing as usual.
* **Terminus: Select Command Output** (`terminus_select_command_output`) selects the
  output of one command, without its prompt and without the line you typed.
* **Terminus: Copy Command Output** (`terminus_copy_command_output`) puts that same
  text on the clipboard directly.

The four marks are useful individually: `A` and `B` alone already delimit the prompt,
and the commands above fall back to `B` when a shell reports no `A`.

Which command each of them acts on follows what is on screen: the command around the
terminal cursor while the cursor is visible, and the command at the top of the
viewport once you have scrolled or jumped away from it. At an idle prompt that means
the command you just ran.

### The ordering trap

`D` reports the status of the command that *just finished*, and a shell learns that
status only when it is about to draw the next prompt. So `D` is emitted at the
**start of the next prompt cycle**, immediately before that cycle's `A` — not at the
end of the previous one. There is no hook that runs after a command but before the
shell regains control, and trying to emit `D` from the command itself would report
the wrong status.

The corollary is that `$?` must be captured as the very first thing the pre-prompt
hook does. Any command in front of it — an `echo`, an OSC 7 report, a `local`
declaration that is not itself the capture — replaces `$?` with its own status and
every command afterwards looks successful.

### bash

Add to `~/.bashrc`:

```bash
if [ -n "$TERM_PROGRAM" ] && [ "$TERM_PROGRAM" = "Terminus-Sublime" ]; then
    __terminus_osc133_precmd() {
        local __terminus_status=$?      # must stay the first line of this function
        if [ -n "$__terminus_running" ]; then
            printf '\033]133;D;%s\007' "$__terminus_status"
            unset __terminus_running
        fi
        __terminus_in_prompt=1
        __terminus_started=1
    }
    __terminus_osc133_done() {
        unset __terminus_in_prompt
    }
    __terminus_osc133_preexec() {
        [ -n "$__terminus_started" ] || return   # nothing before the first prompt
        [ -z "$__terminus_in_prompt" ] || return # PROMPT_COMMAND is not a command line
        case "$BASH_COMMAND" in
            __terminus_osc133_*) return ;;
        esac
        [ -n "$__terminus_running" ] && return   # one C per command line, not per word
        __terminus_running=1
        printf '\033]133;C\007'
    }
    PS1='\[\033]133;A\007\]'"$PS1"'\[\033]133;B\007\]'
    trap '__terminus_osc133_preexec' DEBUG
    PROMPT_COMMAND="__terminus_osc133_precmd${PROMPT_COMMAND:+; $PROMPT_COMMAND}"
    PROMPT_COMMAND="$PROMPT_COMMAND; __terminus_osc133_done"
fi
```

bash has no `preexec`, so `C` has to come out of the `DEBUG` trap, and that trap
fires for a great deal more than command lines: for the rest of `~/.bashrc`, for each
element of a pipeline, and for every function in `PROMPT_COMMAND`. Each guard above
rules one of those out, and all of them are load-bearing:

* `__terminus_started` — the trap is installed while `~/.bashrc` is still running, so
  without it the remaining lines of the rc file report themselves as a command. That
  produces a `C` and a `D;0` in front of the very first prompt, i.e. a command that
  never ran, reported as started and finished successfully.
* `__terminus_in_prompt` — set while `PROMPT_COMMAND` runs and cleared by the entry
  appended to the end of it. Without it any *other* pre-prompt hook, the OSC 7 one
  above included, marks itself as the start of command output.
* The `case` guard keeps this snippet's own three functions from marking themselves.
  It is what makes pressing Enter on an empty line produce nothing at all — no
  command runs, `__terminus_running` is never set, and no `D` follows.
* `__terminus_running` reduces a pipeline to one `C`, and is what tells the next
  pre-prompt hook that a command actually ran.

Two more details:

* `A` and `B` go in `PS1` wrapped in `\[` … `\]`. Those brackets tell readline the
  bytes between them take up no screen columns. Without them bash miscounts the
  prompt width and long command lines wrap in the wrong place or overwrite the
  prompt.
* `local __terminus_status=$?` is the first statement of `__terminus_osc133_precmd`.
  `$?` is expanded before `local` runs, so the assignment captures the real status;
  a `local` on the line above would not. The pre-prompt hook is prepended to
  `PROMPT_COMMAND` for the same reason — it still runs first if the OSC 7 snippet is
  also installed. Keep it that way, and keep `__terminus_osc133_done` last.

One thing bash cannot do: a command line which is a bare subshell, `(cmd)`, does not
run the `DEBUG` trap in the parent shell, so it gets neither a `C` nor a `D`. Its
prompt is still marked, and every other form — pipelines, `{ … }` groups, loops,
conditionals, `sh -c …` — is reported normally. zsh and fish have real hooks and do
not have this hole.

### zsh

Add to `~/.zshrc`:

```zsh
if [[ $TERM_PROGRAM == "Terminus-Sublime" ]]; then
    __terminus_osc133_precmd() {
        local __terminus_status=$?      # must stay the first line of this function
        if [[ -n $__terminus_running ]]; then
            printf '\033]133;D;%s\007' $__terminus_status
            unset __terminus_running
        fi
    }
    __terminus_osc133_preexec() {
        __terminus_running=1
        printf '\033]133;C\007'
    }
    autoload -Uz add-zsh-hook
    add-zsh-hook precmd __terminus_osc133_precmd
    add-zsh-hook preexec __terminus_osc133_preexec
    PS1=$'%{\033]133;A\007%}'$PS1$'%{\033]133;B\007%}'
fi
```

zsh has the hooks bash has to fake: `preexec` runs once per command line, so no
`BASH_COMMAND` filtering is needed, and `precmd` runs once per prompt. `add-zsh-hook`
appends, so registering `__terminus_osc133_precmd` before any other `precmd` hook —
including anything a prompt framework installs later — keeps the `$?` capture first.

The prompt escapes are **not** bash's. zsh marks zero-width regions with `%{` … `%}`,
not `\[` … `\]`, and `\[` in a zsh `PS1` is a literal bracket. The `$'…'` quoting is
what turns `\033` and `\007` into real bytes; inside a plain `'…'` string they would
stay as five and four literal characters. If `PS1` is rebuilt later — `p10k`, `starship`
and friends do — apply these two lines after that, not before.

### fish

Add to `~/.config/fish/config.fish`:

```fish
if test "$TERM_PROGRAM" = "Terminus-Sublime"
    function __terminus_osc133_preexec --on-event fish_preexec
        printf '\033]133;C\007'
    end
    function __terminus_osc133_postexec --on-event fish_postexec
        set -g __terminus_status $status   # must stay the first line of this function
    end
    functions -q fish_prompt; and functions -c fish_prompt __terminus_orig_prompt
    function fish_prompt
        if set -q __terminus_status
            printf '\033]133;D;%s\007' $__terminus_status
            set -e __terminus_status
        end
        printf '\033]133;A\007'
        __terminus_orig_prompt
        printf '\033]133;B\007'
    end
end
```

fish has no `PS1`: the prompt *is* the `fish_prompt` function, so the marks are
printed around a copy of whatever prompt was already defined. `functions -c` takes
that copy once, before the redefinition — sourcing this block twice in one session
would otherwise make the copy point at the wrapper and recurse.

`fish_postexec` is the earliest place the finished command's status is readable, but
the status still belongs to the *previous* command by the time a prompt is drawn, so
it is stashed in a variable and emitted at the top of the next `fish_prompt`, exactly
as in bash and zsh. Store it first: in fish too, any command in front of the capture —
a `test`, a `set -q`, a `printf` — leaves its own status behind in `$status`.

fish strips escape sequences when it measures prompt width, so no zero-width markers
are needed — this is the one shell where the marks can simply be printed.

## A note on WSL

`wsl.exe` passes nothing of the Windows environment into the distribution unless the
variable is named in `WSLENV`. Terminus does that for you: a `wsl.exe` command gets
`TERM`, `COLORTERM` and `TERM_PROGRAM` shared through `WSLENV`, so the
`$TERM_PROGRAM` guards above work inside WSL.

The path a WSL shell reports is a path *inside* the distribution — `/home/you/project`,
not `C:\Users\you\project`. Sublime, running on the Windows side, cannot open it, so
Terminus ignores it rather than guessing. Translating between the two is a separate
job and is not done yet.
