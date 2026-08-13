# Shell integration

Terminus only knows what the shell tells it. A shell reports its working directory
with **OSC 7**, and once it does, a new terminal opened from that one starts in the
same place instead of in the first project folder.

Nothing here is required — without it the terminal behaves exactly as before.
Set `"follow_shell_cwd": false` in the Terminus settings to ignore the reports.

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

## A note on WSL

`wsl.exe` passes nothing of the Windows environment into the distribution unless the
variable is named in `WSLENV`. Terminus does that for you: a `wsl.exe` command gets
`TERM`, `COLORTERM` and `TERM_PROGRAM` shared through `WSLENV`, so the
`$TERM_PROGRAM` guards above work inside WSL.

The path a WSL shell reports is a path *inside* the distribution — `/home/you/project`,
not `C:\Users\you\project`. Sublime, running on the Windows side, cannot open it, so
Terminus ignores it rather than guessing. Translating between the two is a separate
job and is not done yet.
