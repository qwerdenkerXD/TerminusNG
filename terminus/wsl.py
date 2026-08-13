r"""
Translating a path across the WSL boundary.

Sublime runs on the windows side and a shell inside a distribution reports paths
from inside that distribution, so neither side can open the other's spelling of a
path. This module is that translation and nothing else. It imports no sublime,
spawns nothing, and holds no state beyond one cached lookup of the default
distribution name.

    /mnt/c/Users/franz/x   <->  C:\Users\franz\x
    /home/franz/project    <->  \\wsl.localhost\Ubuntu\home\franz\project
                                \\wsl$\Ubuntu\home\franz\project   (older builds)

A path here is untrusted input, it is whatever the shell printed, so every entry
point validates before it translates and returns None rather than a guess. Nothing
in this module opens, executes or even stats a path, it only rewrites a string.
"""

import logging
import os
import re
import shlex


logger = logging.getLogger('Terminus')

# the windows side reaches a distribution's own file system through a unc path,
# \\wsl$\ is the spelling older builds hand out and both are still in circulation
UNC_PREFIX = "\\\\wsl.localhost\\"
LEGACY_UNC_PREFIX = "\\\\wsl$\\"
# the hosts of the two spellings above, lower case, that is what is compared
UNC_HOSTS = ("wsl.localhost", "wsl$")

# where a distribution mounts the windows drives, the default automount root of
# /etc/wsl.conf. a distribution configured with another one has to say so
MOUNT_ROOT = "/mnt"

# the registry key wsl keeps its installed distributions under
LXSS_KEY = "Software\\Microsoft\\Windows\\CurrentVersion\\Lxss"

# a string longer than this is not a path, it is something trying to grow our memory
MAX_PATH_LENGTH = 4096
# a distribution name is a registry key name, nothing legitimate comes near this
MAX_DISTRO_LENGTH = 128

# NUL, newline and every other control character. a real path carries none of them
# and a string that does was assembled to be misread
CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
# what may never appear in a distribution name. the name is pasted into a unc path
# and a separator or a colon in there would quietly name a different host or share
BAD_DISTRO_PATTERN = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
# wsl's automount only ever creates lower case mount points and /mnt is an ordinary
# case sensitive directory inside the distribution, so /mnt/C is a directory of the
# distribution which happens to be called C and not the windows drive C
DRIVE_LETTER_PATTERN = re.compile(r"^[a-z]$")
# a drive rooted windows path. "C:\" is the root of the drive, while bare "C:" and
# "C:foo" are both relative to whatever the current directory on C happens to be,
# which is not ours to know, so neither is a path we can translate
DRIVE_PATH_PATTERN = re.compile(r"^([a-zA-Z]):(\\.*)$")
# what windows silently rewrites or reinterprets in a path component. a trailing dot
# or space is stripped by win32 path normalisation, so "report." names "report" over
# there, and a ":" opens an alternate data stream of a file instead of naming a file
# of its own. the rest are simply reserved. all of them are legal in a posix name, so
# such a name has no windows spelling at all and the only honest answer is None
UNSPELLABLE_IN_COMPONENT = re.compile(r'[:*?"<>|]')

# the options of wsl.exe which take a value, so the value is never mistaken for the
# command to run or for an option of its own
VALUE_OPTIONS = (
    "-d", "--distribution", "--distribution-id", "-u", "--user", "--cd", "--shell-type")
# options which pick a distribution this module cannot turn into a name: a guid names
# one of the installed distributions and only the registry knows which, and --system
# names the hidden system distribution. a command carrying one of these is running
# somewhere other than the default distribution, so falling back to the default would
# be exactly the guess this module exists not to make
UNRESOLVABLE_OPTIONS = ("--distribution-id", "--system")
# from here on the rest of the command line belongs to the distribution, a -d in
# there is an option of the command being run, not of wsl.exe
STOP_OPTIONS = ("--", "-e", "--exec")


def valid_distro_name(name):
    """
    whether a string can be used as a distribution name. it ends up as one component
    of a unc path, so a separator, a colon, a wildcard or a dot component in it would
    point the path at something else entirely
    """
    if not name or not isinstance(name, str):
        return False
    if len(name) > MAX_DISTRO_LENGTH:
        return False
    if name != name.strip():
        return False
    if BAD_DISTRO_PATTERN.search(name):
        return False
    if name in (".", ".."):
        return False
    return True


def _clean(path):
    """the string if it could be a path at all, otherwise None"""
    if not path or not isinstance(path, str):
        return None
    if len(path) > MAX_PATH_LENGTH:
        return None
    if CONTROL_PATTERN.search(path):
        return None
    return path


def _split(text, separators):
    """
    the components of a path, empty ones and "." dropped. that is what makes a
    trailing separator, a doubled separator and mixed separators all the same path
    """
    parts = [text]
    for separator in separators:
        split = []
        for part in parts:
            split.extend(part.split(separator))
        parts = split
    return [part for part in parts if part and part != "."]


def _resolve(parts):
    """
    ".." applied to already rooted components, clamped at the root. resolving it
    lexically is not what the kernel would do if a component is a symlink, but the
    alternative is handing ".." on to the other side, where it can walk out of the
    distribution and name a completely different place. a directory named ".." does
    not exist, so nothing is lost
    """
    resolved = []
    for part in parts:
        if part == "..":
            if resolved:
                resolved.pop()
        else:
            resolved.append(part)
    return resolved


def _spellable_on_windows(components):
    """
    whether every component of a path can be written down on the windows side and
    still name the very same file.

    this is the same refusal a backslash already gets in wsl_to_windows and for the
    same reason: win32 strips a trailing dot and a trailing space off a component
    before it opens anything, so "report." is handed to the file system as "report",
    and a ":" names an alternate data stream of another file. all of them are ordinary
    characters in a posix name, so a directory may well hold both "report" and
    "report.", and translating the second one anyway hands the user the first
    """
    for part in components:
        if part.endswith(".") or part.endswith(" "):
            return False
        if UNSPELLABLE_IN_COMPONENT.search(part):
            return False
    return True


def _mount_components(mount_root):
    """the automount root as components, /mnt unless the caller knows better"""
    root = _clean(mount_root) or MOUNT_ROOT
    if not root.startswith("/"):
        root = MOUNT_ROOT
    return _split(root, "/")


def wsl_to_windows(path, distro=None, unc_prefix=UNC_PREFIX, mount_root=MOUNT_ROOT):
    r"""
    The windows spelling of a path a wsl shell reported, or None.

    /mnt/c/... is a windows drive the distribution mounted and needs no distro.
    Every other absolute posix path lives inside the distribution and is only
    reachable through that distribution's unc path, which cannot be invented, so
    without a distro name the answer is None rather than a guess.
    """
    path = _clean(path)
    if path is None:
        return None

    # a backslash is a legal character in a posix file name and a separator on the
    # windows side. a name carrying one cannot be spelled over there at all and
    # translating it anyway would produce a path pointing somewhere else
    if "\\" in path:
        return None

    # OSC 7 and every other source of interest report an absolute path, a relative
    # one has no meaning without the shell's own cwd, which is not ours to assume
    if not path.startswith("/"):
        return None

    components = _resolve(_split(path, "/"))
    # a name windows cannot spell is refused rather than translated into one which
    # names a different file, exactly as the backslash above
    if not _spellable_on_windows(components):
        logger.debug("no windows spelling for the path {}".format(path))
        return None

    mount = _mount_components(mount_root)

    if len(components) > len(mount) and components[:len(mount)] == mount:
        drive = components[len(mount)]
        if DRIVE_LETTER_PATTERN.match(drive):
            rest = components[len(mount) + 1:]
            # the trailing separator of C:\ is not decoration, C: alone means the
            # current directory on that drive
            return "{}:\\{}".format(drive.upper(), "\\".join(rest))

    if not valid_distro_name(distro):
        return None

    if unc_prefix not in (UNC_PREFIX, LEGACY_UNC_PREFIX):
        unc_prefix = UNC_PREFIX

    windows = unc_prefix + distro
    if components:
        windows += "\\" + "\\".join(components)
    return windows


def windows_to_wsl(path, mount_root=MOUNT_ROOT):
    r"""
    The wsl spelling of a windows path, or None.

    A drive letter becomes a mount point and the unc path of a distribution becomes
    the path inside it, in either spelling of the prefix. Anything a distribution
    cannot see, a network share among it, and anything not absolute, is None.

    WHICH distribution a unc path belonged to is not part of the result and cannot be
    recovered from it: \\wsl$\Debian\home and \\wsl.localhost\Ubuntu\home both become
    "/home". The answer is therefore only meaningful to a caller which already knows
    the distribution it is going to use the path in, and handing it to some other
    distribution names a different file of the same name. Use distro_from_unc when
    the identity matters.
    """
    path = _clean(path)
    if path is None:
        return None

    # windows takes both separators, a distribution takes only one
    text = path.replace("/", "\\")

    if text.startswith("\\\\?\\"):
        # path parsing turned off for a long path, \\?\UNC\host\share is how the
        # same prefix spells a unc path
        text = text[4:]
        if text[:4].upper() == "UNC\\":
            text = "\\\\" + text[4:]

    if text.startswith("\\\\.\\"):
        # the device namespace names a device, not a file, there is nothing here.
        # checked after the rewrite above as well as before it, "\\?\UNC\.\..."
        # spells the very same thing and must not slip past
        return None

    if text.startswith("\\\\"):
        # split by hand and keep a "." component: dropping it here would move the
        # host and the distribution out of the first two positions, and "\\wsl$\.\U"
        # would be read as the distribution "U" of the host "wsl$"
        parts = [part for part in text.split("\\") if part]
        # the host and the distribution are the root of this path, ".." is resolved
        # only below them, otherwise a "\\wsl.localhost\Ubuntu\..\.." would shift
        # the distribution name onto some other component
        if len(parts) < 2 or parts[0].lower() not in UNC_HOSTS:
            return None
        if not valid_distro_name(parts[1]):
            return None
        rest = [part for part in parts[2:] if part != "."]
        return "/" + "/".join(_resolve(rest))

    match = DRIVE_PATH_PATTERN.match(text)
    if match:
        drive = match.group(1).lower()
        rest = _resolve(_split(match.group(2), "\\"))
        return "/" + "/".join(_mount_components(mount_root) + [drive] + rest)

    # a bare "\foo" is rooted on whichever drive is current, "C:" and "C:foo" are
    # relative to the current directory on C, a posix path is not a windows path, and
    # a relative path is not translatable
    return None


def distro_from_unc(path):
    r"""
    The distribution a \\wsl.localhost\ or \\wsl$\ path names, or None. This is the
    half windows_to_wsl throws away, so a caller which has to know whether a path
    belongs to the distribution it is about to use can ask for it separately.
    """
    path = _clean(path)
    if path is None:
        return None

    text = path.replace("/", "\\")
    if text.startswith("\\\\?\\"):
        text = text[4:]
        if text[:4].upper() == "UNC\\":
            text = "\\\\" + text[4:]
    if not text.startswith("\\\\") or text.startswith("\\\\.\\"):
        return None

    parts = [part for part in text.split("\\") if part]
    if len(parts) < 2 or parts[0].lower() not in UNC_HOSTS:
        return None
    if not valid_distro_name(parts[1]):
        return None
    return parts[1]


def _executable_name(arg):
    # the basename without extension, taken by hand because a windows path is not
    # split by posixpath when the plugin host runs elsewhere. this is a deliberate
    # copy of commands.executable_name, importing that module pulls in sublime
    arg = arg.strip('"').replace("\\", "/").rstrip("/")
    arg = arg.rsplit("/", 1)[-1].lower()
    if arg.endswith(".exe"):
        arg = arg[:-4]
    return arg


def _argv(cmd):
    """the command as a list of non empty arguments, quotes trimmed, or None"""
    if not cmd:
        return None

    if isinstance(cmd, str):
        try:
            args = shlex.split(cmd, posix=False)
        except ValueError:
            # an unbalanced quote, there is nothing to parse
            return None
    elif isinstance(cmd, (list, tuple)):
        args = list(cmd)
    else:
        return None

    cleaned = []
    for arg in args:
        if not isinstance(arg, str) or not arg:
            continue
        if len(arg) > 1 and arg[0] == arg[-1] and arg[0] in "\"'":
            arg = arg[1:-1]
        if arg:
            cleaned.append(arg)
    return cleaned


def _wsl_argv(cmd):
    """the argv of the wsl launcher itself, None if this command is not one"""
    args = _argv(cmd)
    if not args:
        return None

    # a shell_cmd is wrapped into "cmd.exe /c ...", the launcher is behind the wrapper
    if _executable_name(args[0]) in ["cmd", "command"] and \
            len(args) > 2 and args[1].startswith("/"):
        args = args[2:]

    if not args or _executable_name(args[0]) != "wsl":
        return None
    return args


def _scan_distro(cmd):
    """
    Which distribution a wsl command runs in, as (name, unresolvable).

    Telling "this command uses the default distribution" apart from "this command
    picks another one in a way I cannot read" is the whole point of this function.
    Both look like "no name found", and treating the second as the first is how a
    path ends up translated against a distribution the terminal is not in at all,
    which then opens a file of the same name belonging to somebody else.

    name is the distribution when the command names one this module can use.
    unresolvable is True when the command picks a distribution which cannot be turned
    into a name here: --distribution-id names one by guid, --system names the hidden
    system distribution, a -d whose value is not usable as a name names one we refuse
    to spell, and an option this module has never heard of may be a newer spelling of
    any of them. In all of those the caller must not fall back to the default.
    """
    args = _wsl_argv(cmd)
    if not args:
        return None, False

    rest = args[1:]
    i = 0
    while i < len(rest):
        arg = rest[i]
        lowered = arg.lower()

        if lowered in ("-d", "--distribution"):
            if i + 1 >= len(rest):
                # the option is there without its value, this command line names a
                # distribution and is simply cut off, it is not the default one
                return None, True
            name = rest[i + 1].strip()
            if valid_distro_name(name):
                return name, False
            return None, True

        if lowered.startswith("--distribution="):
            name = arg.split("=", 1)[1].strip()
            if valid_distro_name(name):
                return name, False
            return None, True

        if lowered in UNRESOLVABLE_OPTIONS or lowered.startswith("--distribution-id="):
            return None, True

        if lowered in STOP_OPTIONS:
            # everything behind this belongs to the command being run in the
            # distribution, and no distribution was named, so it is the default one
            return None, False

        if lowered in VALUE_OPTIONS:
            # skip the option and the value it takes
            i += 2
            continue

        if not arg.startswith("-"):
            # the first argument which is not an option starts the command to run
            return None, False

        # an option this module does not model. it may well be a newer way of
        # picking a distribution, so this command line is treated as one whose
        # distribution cannot be told rather than as one using the default
        logger.debug("unknown wsl option {}, not assuming the default distribution"
                     .format(arg))
        return None, True

    return None, False


def distro_from_cmd(cmd):
    """
    The distribution a wsl command names through -d or --distribution, or None when
    the command is not a wsl launcher, names no distribution, or names one that
    cannot be a distribution name. cmd is a string or a list and may sit behind a
    "cmd.exe /c" wrapper, the same shapes commands.is_wsl_command accepts.

    None does not mean "the default distribution", see _scan_distro, which is what
    wsl_to_windows_for_cmd uses for exactly that reason.
    """
    return _scan_distro(cmd)[0]


_default_distro = None
_default_distro_read = False


def default_distro():
    """
    The name of the default distribution, or None when it cannot be told cheaply.

    It is read from the registry key wsl keeps its distributions in, once, on first
    use, and cached, the failure included. Nothing is spawned to find out: wsl.exe
    -l -q answers in its own time and a subprocess started on sublime's ui thread
    is a frozen editor for as long as it takes, which is worse than a feature that
    quietly does nothing. Off windows, or with no wsl installed, this is None.
    """
    global _default_distro, _default_distro_read
    if not _default_distro_read:
        _default_distro_read = True
        _default_distro = _read_default_distro()
    return _default_distro


def clear_default_distro_cache():
    """forget the cached lookup, a distribution installed mid session is not seen"""
    global _default_distro, _default_distro_read
    _default_distro = None
    _default_distro_read = False


def _read_default_distro():
    if os.name != "nt":
        return None

    try:
        # imported here and never at module scope, this module stays importable
        # everywhere the plugin host runs
        import winreg
    except ImportError:
        return None

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LXSS_KEY) as lxss:
            guid = winreg.QueryValueEx(lxss, "DefaultDistribution")[0]
            # the guid names a subkey and is checked before it is used as one, a
            # separator in there would open some entirely different key
            if not isinstance(guid, str) or not guid or BAD_DISTRO_PATTERN.search(guid):
                return None
            with winreg.OpenKey(lxss, guid) as entry:
                name = winreg.QueryValueEx(entry, "DistributionName")[0]
    except OSError:
        # no wsl installed, or the key moved, either way there is no default
        return None
    except Exception as e:
        logger.debug("could not read the default wsl distribution: {}".format(e))
        return None

    if isinstance(name, str):
        name = name.strip()
    if valid_distro_name(name):
        return name
    return None


def wsl_to_windows_for_cmd(path, cmd):
    """
    The windows spelling of a path reported by the shell that cmd launched. A drive
    path needs nothing, anything else uses the distribution the command names and
    falls back to the default one. Returns None when it cannot be told.

    The fallback is only taken when the command really is running in the default
    distribution. A command which picks another one in a spelling this module cannot
    read gets None instead, because the same path exists in every installed
    distribution and the default one is then the wrong answer that still resolves.
    """
    windows = wsl_to_windows(path)
    if windows:
        return windows

    distro, unresolvable = _scan_distro(cmd)
    if not distro:
        if unresolvable:
            logger.debug("the command names a wsl distribution which cannot be resolved")
            return None
        distro = default_distro()
    if not distro:
        return None

    return wsl_to_windows(path, distro)
