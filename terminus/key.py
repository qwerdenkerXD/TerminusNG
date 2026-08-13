# adopted from TerminalView

"""
Translation of Sublime key bindings into the byte sequences fed to the pty.

The cursor key class (up, down, right, left, home, end) is the part everybody
gets wrong, so here is the authoritative truth table, cross checked against the
xterm control sequence documentation and Microsoft's terminalInput.cpp:

                Normal (DECCKM reset)   Application (DECCKM set)   With modifier
  Home          ESC [ H                 ESC O H                    ESC [ 1 ; m H
  End           ESC [ F                 ESC O F                    ESC [ 1 ; m F
  Up            ESC [ A                 ESC O A                    ESC [ 1 ; m A
  Down          ESC [ B                 ESC O B                    ESC [ 1 ; m B
  Right         ESC [ C                 ESC O C                    ESC [ 1 ; m C
  Left          ESC [ D                 ESC O D                    ESC [ 1 ; m D

  m = 1 + (1 if shift) + (2 if alt) + (4 if ctrl)

Three rules that must not be broken:

  1. Any modifier forces the CSI form, never SS3.  Modified home in application
     mode is still ESC [ 1 ; 5 H, not ESC O ... anything.
  2. The leading "1" parameter is mandatory.  xterm's modifyCursorKeys defaults
     to 2, which means the sequence always carries both parameters.
  3. Home and End belong to the cursor key class, not to the VT220 tilde class.
     Never splice the cursor key "1 ; m" parameter shape onto the tilde final
     byte (ESC [ 1 ; 5 ~ and friends).  That is not a legal member of either
     family and no application will decode it.

The tilde class (insert, delete, pageup, pagedown, f5..f12) is separate and
takes ESC [ n ~ unmodified and ESC [ n ; m ~ modified.

This module is pure and dependency free on purpose: it reads no settings and
imports nothing.  The TERM=linux console codes for home and end are available
through the `linux_mode` flag, which the caller passes in.
"""

# cursor key class, key name -> final byte
_CURSOR_KEY_MAP = {
    "up": "A",
    "down": "B",
    "right": "C",
    "left": "D",
    "home": "H",
    "end": "F",
}

# VT220 tilde class, key name -> numeric parameter
_TILDE_KEY_MAP = {
    "insert": 2,
    "delete": 3,
    "pageup": 5,
    "pagedown": 6,
    "f5": 15,
    "f6": 17,
    "f7": 18,
    "f8": 19,
    "f9": 20,
    "f10": 21,
    "f11": 23,
    "f12": 24,
}

# SS3 function keys, key name -> final byte, modified form is ESC [ 1 ; m <final>
_SS3_KEY_MAP = {
    "f1": "P",
    "f2": "Q",
    "f3": "R",
    "f4": "S",
}

# TERM=linux console codes, opt-in only, see the `linux_mode` argument
_LINUX_KEY_MAP = {
    "home": "\x1b[1~",
    "end": "\x1b[4~",
}

_KEY_MAP = {
    "enter": "\r",
    "backspace": "\x7f",
    "tab": "\t",
    "space": " ",
    "escape": "\x1b",
    "bracketed_paste_mode_start": "\x1b[200~",
    "bracketed_paste_mode_end": "\x1b[201~",
}

_LMN_MODE_KEY_MAP = {
    "enter": "\r\n"
}

_CTRL_KEY_MAP = {
    "@": "\x00",
    "`": "\x00",
    "[": "\x1b",
    "{": "\x1b",
    "\\": "\x1c",
    "|": "\x1c",
    "]": "\x1d",
    "}": "\x1d",
    "^": "\x1e",
    "~": "\x1e",
    "_": "\x1f",
    "?": "\x7f",
}


def _modifier_parameter(ctrl, alt, shift):
    """
    The xterm modifier parameter, 1 means no modifier at all
    """
    modifier = 1
    if shift:
        modifier += 1
    if alt:
        modifier += 2
    if ctrl:
        modifier += 4
    return modifier


def _cursor_key_code(final, modifier, application_mode):
    # rule 1, any modifier wins over application mode and forces the CSI form,
    # this check has to come before the DECCKM branch
    if modifier > 1:
        # rule 2, the leading "1" parameter is not optional
        return "\x1b[1;{}{}".format(modifier, final)
    if application_mode:
        return "\x1bO" + final
    return "\x1b[" + final


def _tilde_key_code(number, modifier):
    if modifier > 1:
        return "\x1b[{};{}~".format(number, modifier)
    return "\x1b[{}~".format(number)


def _ss3_key_code(final, modifier):
    # f1 to f4 are SS3 unmodified but join the cursor key shape once modified
    if modifier > 1:
        return "\x1b[1;{}{}".format(modifier, final)
    return "\x1bO" + final


def _get_key_code(key, ctrl=False, shift=False, new_line_mode=False):
    """
    The base code of the keys which carry no modifier parameter of their own,
    alt is applied by the caller as an ESC prefix
    """
    key_lo = key.lower()

    if ctrl:
        if key_lo in _CTRL_KEY_MAP:
            return _CTRL_KEY_MAP[key_lo]
        if len(key_lo) == 1:
            c = ord(key_lo)
            if (c >= 97) and (c <= 122):
                return chr(c - ord('a') + 1)

    if new_line_mode and key_lo in _LMN_MODE_KEY_MAP:
        return _LMN_MODE_KEY_MAP[key_lo]

    if key_lo in _KEY_MAP:
        return _KEY_MAP[key_lo]

    if shift:
        return key.upper()

    return key


def get_key_code(
        key,
        ctrl=False, alt=False, shift=False,
        application_mode=False, new_line_mode=False, linux_mode=False):
    """
    Send keypress to the shell

    `linux_mode` swaps home and end back to the TERM=linux console codes and
    defaults to False, which is the xterm behaviour every terminfo entry we
    ship under assumes.
    """
    modifier = _modifier_parameter(ctrl, alt, shift)
    key_lo = key.lower()

    # the linux console knows no modified and no application mode home or end,
    # so the opt-in only covers the plain sequences and everything else keeps
    # the xterm form
    if linux_mode and modifier == 1 and key_lo in _LINUX_KEY_MAP:
        return _LINUX_KEY_MAP[key_lo]

    # the three classes below encode every modifier in the sequence itself,
    # they never take the ESC prefix and never take a ctrl control character
    if key_lo in _CURSOR_KEY_MAP:
        return _cursor_key_code(_CURSOR_KEY_MAP[key_lo], modifier, application_mode)

    if key_lo in _TILDE_KEY_MAP:
        return _tilde_key_code(_TILDE_KEY_MAP[key_lo], modifier)

    if key_lo in _SS3_KEY_MAP:
        return _ss3_key_code(_SS3_KEY_MAP[key_lo], modifier)

    if shift and key_lo == "tab":
        keycode = "\x1b[Z"
    else:
        keycode = _get_key_code(key, ctrl, shift, new_line_mode)

    # modifiers compose, alt is the ESC prefix on top of whatever ctrl produced
    if alt:
        keycode = "\x1b" + keycode

    return keycode
