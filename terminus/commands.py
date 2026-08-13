import sublime
import sublime_plugin

import os
import re
import sys
import logging

from .clipboard import g_clipboard_history
from .const import DEFAULT_PANEL, DEFAULT_TITLE, EXEC_PANEL, CONTINUATION
from .key import get_key_code
from .recency import RecencyManager
from .terminal import Terminal
from .utils import available_panel_name
from .utils import shlex_split
from .view import get_panel_window, get_panel_name, panel_is_visible, view_is_visible


KEYS = [
    "ctrl+k",
    "ctrl+p"
]

SUPPORTED_TERMS = [
    "linux",
    "xterm",
    "xterm-16color",
    "xterm-256color"
]

logger = logging.getLogger('Terminus')


def executable_name(arg):
    # the basename without extension, taken by hand because a windows path is not split
    # by posixpath when the plugin host runs elsewhere
    arg = arg.strip('"').replace("\\", "/").rstrip("/")
    arg = arg.rsplit("/", 1)[-1].lower()
    if arg.endswith(".exe"):
        arg = arg[:-4]
    return arg


def is_wsl_command(cmd):
    """
    whether the command launches a wsl distribution, it may be spelled "wsl", "wsl.exe"
    or an absolute path and cmd may be a string or a list
    """
    if not cmd:
        return False

    if isinstance(cmd, str):
        args = shlex_split(cmd)
    else:
        args = list(cmd)

    args = [arg for arg in args if isinstance(arg, str) and arg]
    if not args:
        return False

    # a shell_cmd is wrapped into "cmd.exe /c ...", the launcher is behind the wrapper
    if executable_name(args[0]) in ["cmd", "command"] and \
            len(args) > 2 and args[1].startswith("/"):
        args = args[2:]

    return executable_name(args[0]) == "wsl"


def reported_cwd(view):
    """
    The working directory a terminal view's shell last reported through OSC 7, if it
    is a directory this side of the pty can actually reach. A wsl shell reports paths
    from inside the distribution and a shell over ssh reports paths on the far host,
    neither of which exist here, so an unreachable path is simply not used.
    """
    if not view:
        return None
    settings = sublime.load_settings("Terminus.sublime-settings")
    if not settings.get("follow_shell_cwd", True):
        return None
    terminal = Terminal.from_id(view.id())
    # screen only exists once the process has been spawned
    screen = getattr(terminal, "screen", None) if terminal else None
    if not screen:
        return None
    cwd = screen.cwd
    if cwd and os.path.isdir(cwd):
        return cwd
    return None


def share_env_with_wsl(env):
    """
    wsl.exe hands nothing of the windows environment to the distribution unless the
    variable is named in WSLENV, so the variables Terminus promises have to be shared
    explicitly, otherwise TERM_PROGRAM is invisible inside wsl
    """
    if "TERM" not in env:
        env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["TERM_PROGRAM"] = "Terminus-Sublime"
    # read by wsl.exe itself on the windows side, it does not travel through WSLENV
    env["WSL_UTF8"] = "1"

    # keep whatever the user shares already, WSLENV is a colon separated list of
    # NAME/flags entries
    wslenv = env.get("WSLENV", os.environ.get("WSLENV", ""))
    entries = [entry for entry in wslenv.split(":") if entry]
    shared = [entry.split("/")[0] for entry in entries]
    for name in ["TERM", "COLORTERM", "TERM_PROGRAM"]:
        if name not in shared:
            # /u shares the value from win32 to wsl only, /p would treat it as a path
            # and translate, that is, corrupt it
            entries.append("{}/u".format(name))

    env["WSLENV"] = ":".join(entries)


def abandon_detached_terminal(terminal):
    """
    a terminal detached for a reset, a maximize or a minimize is hosted by no view, and
    should_be_reaped deliberately never fires for a detached terminal, so a round trip
    which cannot finish has to terminate it by hand. otherwise the process stays alive
    and its renderer thread keeps ticking at 30 Hz for the rest of the session
    """
    logger.error("no window to reattach the terminal to, terminating it")
    terminal.kill()
    if terminal in Terminal._detached_terminals:
        Terminal._detached_terminals.remove(terminal)


def unoccupied_panel_name(window, panel_name, terminal):
    """
    a panel already hosting another live terminal must not be taken over, that terminal
    would become unreachable by view id while its renderer keeps writing to the view
    """
    view = window.find_output_panel(panel_name)
    if not view:
        return panel_name

    other = Terminal.from_id(view.id())
    if not other or other is terminal:
        return panel_name

    return available_panel_name(window, panel_name)


class TerminusOpenCommand(sublime_plugin.WindowCommand):
    def run(self, **kwargs):
        sublime.set_timeout_async(lambda: self.run_async(**kwargs))

    def run_async(
            self,
            config_name=None,
            cmd=None,
            shell_cmd=None,
            cwd=None,
            working_dir=None,
            env={},
            title=None,
            show_in_panel=None,
            panel_name=None,
            focus=True,
            tag=None,
            file_regex=None,
            line_regex=None,
            pre_window_hooks=[],
            post_window_hooks=[],
            post_view_hooks=[],
            view_settings={},
            auto_close=True,
            cancellable=False,
            reactivable=True,
            timeit=False,
            paths=[],
    ):
        config = None

        st_vars = self.window.extract_variables()

        if config_name == "<ask>":
            self.show_configs()
            return

        if config_name:
            config = self.get_config_by_name(config_name)
        elif cmd or shell_cmd:
            config = {}
        else:
            config = self.get_config_by_name("Default")

        config_name = config["name"] if config else None

        if config_name:
            default_title = config_name
        else:
            default_title = DEFAULT_TITLE

        if config and "cmd" in config and "shell_cmd" in config:
            raise Exception(
                "both `cmd` are `shell_cmd` are specified in config {}".format(config_name))

        if cmd and shell_cmd:
            raise Exception("both `cmd` are `shell_cmd` are passed to terminus_open")

        if shell_cmd is not None or ("shell_cmd" in config and config["shell_cmd"]):
            if shell_cmd is None:
                shell_cmd = config["shell_cmd"]

            if not isinstance(shell_cmd, str):
                raise ValueError("shell_cmd should be a string")
            # mimic exec target
            if sys.platform.startswith("win"):
                comspec = os.environ.get("COMSPEC", "cmd.exe")
                cmd_to_run = [comspec, "/c"] + shlex_split(shell_cmd)
            elif sys.platform == "darwin":
                cmd_to_run = ["/usr/bin/env", "bash", "-l", "-c", shell_cmd]
            else:
                cmd_to_run = ["/usr/bin/env", "bash", "-c", shell_cmd]

        elif cmd is not None or ("cmd" in config and config["cmd"]):

            if cmd is None:
                cmd = config["cmd"]

            cmd_to_run = cmd

        else:
            raise Exception("both `cmd` are `shell_cmd` are empty")

        if cmd_to_run is None:
            raise ValueError("cannot determine command to run")

        if isinstance(cmd_to_run, str):
            cmd_to_run = [cmd_to_run]

        cmd_to_run = sublime.expand_variables(cmd_to_run, st_vars)

        # a copy of the config env, the passed env is merged on top of it instead of
        # replacing it and neither the config's nor the caller's dict is written to
        if config and "env" in config:
            _env = dict(config["env"])
        else:
            _env = {}

        _env["TERMINUS_SUBLIME"] = "1"  # for backward compatibility
        _env["TERM_PROGRAM"] = "Terminus-Sublime"

        _env.update(env)

        settings = sublime.load_settings("Terminus.sublime-settings")

        if sys.platform.startswith("win"):
            if is_wsl_command(cmd_to_run):
                share_env_with_wsl(_env)

        else:
            if "TERM" not in _env:
                _env["TERM"] = settings.get("unix_term", "linux")

            if "LANG" not in _env:
                if "LANG" in os.environ:
                    _env["LANG"] = os.environ["LANG"]
                else:
                    _env["LANG"] = settings.get("unix_lang", "en_US.UTF-8")

        # an explicitly provided TERM is validated on every platform, not only on unix
        if "TERM" in _env and _env["TERM"] not in SUPPORTED_TERMS:
            raise Exception("{} is not supported.".format(_env["TERM"]))

        #  force prompt-toolkit to use 256 color
        if "PROMPT_TOOLKIT_COLOR_DEPTH" not in os.environ \
                and "PROMPT_TOOLKIT_COLOR_DEPTH" not in _env:
            _env["PROMPT_TOOLKIT_COLOR_DEPTH"] = "DEPTH_8_BIT"

        # paths is passed if this was invoked from the side bar context menu
        if paths:
            cwd = paths[0]
            if not os.path.isdir(cwd):
                cwd = os.path.dirname(cwd)
        else:
            if not cwd and working_dir:
                cwd = working_dir

            if cwd:
                cwd = sublime.expand_variables(cwd, st_vars)

        if not cwd:
            # a terminal opened from a terminal starts where that shell is, which is
            # only known if the shell reports it through OSC 7, see the readme
            cwd = reported_cwd(self.window.active_view())

        if not cwd:
            if self.window.folders():
                cwd = self.window.folders()[0]
            else:
                cwd = os.path.expanduser("~")

        if not os.path.isdir(cwd):
            home = os.path.expanduser("~")
            if home:
                cwd = home

        if not os.path.isdir(cwd):
            raise Exception("{} does not exist".format(cwd))

        if default_title:
            default_title = sublime.expand_variables(default_title, st_vars)

        if title:
            title = sublime.expand_variables(title, st_vars)

        if show_in_panel is None and panel_name:
            show_in_panel = True

        terminal = None
        window = self.window
        view = None

        if tag:
            terminal = Terminal.from_tag(tag)

        if not terminal and show_in_panel and panel_name:
            view = window.find_output_panel(panel_name)
            if view:
                terminal = Terminal.from_id(view.id())

        if terminal:
            # cleanup existing terminal
            view = terminal.view
            # avoid terminus_cleanup
            view.settings().set("terminus_view.finished", True)
            # the renderer of the old terminal queues a deferred terminus_cleanup on its
            # view, it may still land after terminus_initialize_view has erased the
            # finished flag for the terminal taking the view over, and would then kill
            # that fresh process. detaching empties terminal.view first, which makes the
            # pending callback a no-op no matter when it lands.
            terminal.detach_view()
            terminal.kill()
            if terminal in Terminal._detached_terminals:
                # it was only detached to disarm its cleanup, it is not coming back
                Terminal._detached_terminals.remove(terminal)

        if not view:
            if not panel_name:
                panel_name = available_panel_name(window, DEFAULT_PANEL)

            if show_in_panel:
                view = window.get_output_panel(panel_name)
            else:
                view = window.new_file(syntax="Terminus View.sublime-syntax")

        # pre_window_hooks
        for hook in pre_window_hooks:
            window.run_command(*hook)

        view.run_command(
            "terminus_activate",
            {
                "cmd": cmd_to_run,
                "cwd": cwd,
                "env": _env,
                "default_title": default_title,
                "title": title,
                "show_in_panel": show_in_panel,
                "panel_name": panel_name,
                "tag": tag,
                "auto_close": auto_close,
                "cancellable": cancellable,
                "reactivable": reactivable,
                "timeit": timeit,
                "file_regex": file_regex,
                "line_regex": line_regex,
                "view_settings": view_settings,
            })

        if show_in_panel:
            window.run_command("show_panel", {"panel": "output.{}".format(panel_name)})

        if focus:
            window.focus_view(view)

        # post_window_hooks
        for hook in post_window_hooks:
            window.run_command(*hook)

        # post_view_hooks
        for hook in post_view_hooks:
            view.run_command(*hook)

    def show_configs(self):
        settings = sublime.load_settings("Terminus.sublime-settings")
        configs = settings.get("shell_configs", [])

        ok_configs = []
        has_default = False
        platform = sublime.platform()
        for config in configs:
            if "enable" in config and not config["enable"]:
                continue
            if "platforms" in config and platform not in config["platforms"]:
                continue
            if "default" in config and config["default"] and not has_default:
                has_default = True
                ok_configs = [config] + ok_configs
            else:
                ok_configs.append(config)

        if not has_default:
            default_config = self._default_config()
            ok_configs = [default_config] + ok_configs

        self.window.show_quick_panel(
            [[config["name"], self.config_details(config)] for config in ok_configs],
            lambda x: on_selection_shell(x)
        )

        def on_selection_shell(index):
            if index < 0:
                return
            config = ok_configs[index]
            config_name = config["name"]
            sublime.set_timeout(
                lambda: self.window.show_quick_panel(
                    ["Open in Tab", "Open in Panel"],
                    lambda x: on_selection_method(x, config_name)
                )
            )

        def on_selection_method(index, config_name):
            if index == 0:
                self.run(config_name=config_name)
            elif index == 1:
                self.run(config_name=config_name, panel_name=DEFAULT_PANEL)

    def config_details(self, config):
        # a config carries either cmd or shell_cmd and cmd may be a string, a list or,
        # in a malformed config, empty
        cmd = config.get("cmd") or config.get("shell_cmd") or ""
        if isinstance(cmd, str):
            return cmd
        return cmd[0] if cmd else ""

    def get_config_by_name(self, name):
        default_config = self.default_config()
        if name.lower() == "default":
            return default_config

        settings = sublime.load_settings("Terminus.sublime-settings")
        configs = settings.get("shell_configs", [])

        platform = sublime.platform()
        for config in configs:
            if "enable" in config and not config["enable"]:
                continue
            if "platforms" in config and platform not in config["platforms"]:
                continue
            if name.lower() == config["name"].lower():
                return config

        # last chance
        if name.lower() == default_config["name"].lower():
            return default_config
        raise Exception("Config {} not found".format(name))

    def default_config(self):
        settings = sublime.load_settings("Terminus.sublime-settings")
        configs = settings.get("shell_configs", [])

        platform = sublime.platform()
        for config in configs:
            if "enable" in config and not config["enable"]:
                continue
            if "platforms" in config and platform not in config["platforms"]:
                continue
            if "default" in config and config["default"]:
                return config

        default_config_name = settings.get("default_config", None)
        if isinstance(default_config_name, dict):
            if platform in default_config_name:
                default_config_name = default_config_name[platform]
            else:
                default_config_name = None

        if default_config_name:
            for config in configs:
                if "enable" in config and not config["enable"]:
                    continue
                if "platforms" in config and platform not in config["platforms"]:
                    continue
                if default_config_name.lower() == config["name"].lower():
                    return config

        return self._default_config()

    def _default_config(self):
        if sys.platform.startswith("win"):
            return {
                "name": "Command Prompt",
                "cmd": "cmd.exe",
                "env": {}
            }
        else:
            if "SHELL" in os.environ:
                shell = os.environ["SHELL"]
                if os.path.basename(shell) == "tcsh":
                    cmd = [shell, "-l"]
                else:
                    cmd = [shell, "-i", "-l"]
            else:
                cmd = ["/bin/bash", "-i", "-l"]

            return {
                "name": "Login Shell",
                "cmd": cmd,
                "env": {}
            }


class TerminusCloseCommand(sublime_plugin.TextCommand):

    def run(self, _):
        view = self.view
        if not view.settings().get("terminus_view"):
            return

        terminal = Terminal.from_id(view.id())
        if terminal:
            terminal.kill()

        panel_name = get_panel_name(view)
        if panel_name:
            window = get_panel_window(view)
            if window:
                window.destroy_output_panel(panel_name)
        else:
            view.close()


class TerminusCloseAllCommand(sublime_plugin.WindowCommand):

    def run(self):
        window = self.window
        views = []
        for view in window.views():
            if view.settings().get("terminus_view"):
                views.append(view)
        for panel in window.panels():
            view = window.find_output_panel(panel.replace("output.", ""))
            if view and view.settings().get("terminus_view"):
                views.append(view)
        for view in views:
            view.run_command("terminus_close")


# a drop in replacement of target `exec` in sublime-build
class TerminusExecCommand(sublime_plugin.WindowCommand):
    def run(self, **kwargs):
        if "kill" in kwargs and kwargs["kill"]:
            self.window.run_command("terminus_cancel_build")
            return

        if "cmd" not in kwargs and "shell_cmd" not in kwargs:
            raise Exception("'cmd' or 'shell_cmd' is required")
        if "show_in_panel" in kwargs and kwargs["show_in_panel"] is False:
            raise Exception("'show_in_panel must be True")
        if "panel_name" in kwargs:
            raise Exception("'panel_name' must not be specified")
        if "tag" in kwargs:
            raise Exception("'tag' must not be specified")
        kwargs["panel_name"] = EXEC_PANEL
        if "focus" not in kwargs:
            kwargs["focus"] = False
        if "auto_close" not in kwargs:
            kwargs["auto_close"] = False
        if "cancellable" not in kwargs:
            kwargs["cancellable"] = True
        if "reactivable" not in kwargs:
            kwargs["reactivable"] = False
        if "timeit" not in kwargs:
            kwargs["timeit"] = True
        for key in ["encoding", "quiet", "word_wrap", "syntax"]:
            if key in kwargs:
                del kwargs[key]
        self.window.run_command("terminus_open", kwargs)


class TerminusCancelBuildCommand(sublime_plugin.WindowCommand):
    def run(self, *args, exec_panel=EXEC_PANEL, **kwargs):
        window = self.window
        for panel_name in window.panels():
            panel_name = panel_name.replace("output.", "")
            if panel_name != exec_panel:
                continue
            view = window.find_output_panel(panel_name)
            if not view:
                continue
            terminal = Terminal.from_id(view.id())
            if not terminal:
                continue
            if terminal.cancellable:
                view.run_command("terminus_cleanup", {"by_user": True})


class TerminusInitializeViewCommand(sublime_plugin.TextCommand):
    def run(self, _, **kwargs):
        view = self.view
        view_settings = view.settings()

        if view_settings.get("terminus_view", False):
            # if it is an reused view
            view.run_command("terminus_nuke")
            view.settings().erase("terminus_view.finished")
            view.settings().erase("terminus_view.viewport_y")

        view_settings.set("terminus_view", True)
        view_settings.set("terminus_view.args", kwargs)

        if "tag" in kwargs:
            view_settings.set("terminus_view.tag", kwargs["tag"])
        if "cancellable" in kwargs:
            view_settings.set("terminus_view.cancellable", kwargs["cancellable"])
        if "reactivable" in kwargs:
            view_settings.set("terminus_view.reactivable", kwargs["reactivable"])

        terminus_settings = sublime.load_settings("Terminus.sublime-settings")
        view_settings.set(
            "terminus_view.natural_keyboard",
            terminus_settings.get("natural_keyboard", True))
        preserve_keys = terminus_settings.get("preserve_keys", {})
        if not preserve_keys:
            preserve_keys = terminus_settings.get("disable_keys", {})
        if not preserve_keys:
            preserve_keys = terminus_settings.get("ignore_keys", {})
        for key in KEYS:
            if key not in preserve_keys:
                view_settings.set("terminus_view.key.{}".format(key), True)
        view.set_scratch(True)
        view.set_read_only(False)
        view_settings.set("is_widget", True)
        view_settings.set("gutter", False)
        view_settings.set("highlight_line", False)
        view_settings.set("auto_complete_commit_on_tab", False)
        view_settings.set("draw_centered", False)
        view_settings.set("word_wrap", False)
        view_settings.set("auto_complete", False)
        view_settings.set("draw_white_space", "none")
        view_settings.set("draw_unicode_white_space", False)
        view_settings.set("draw_indent_guides", False)
        # view_settings.set("caret_style", "blink")
        view_settings.set("scroll_past_end", True)
        view_settings.set("color_scheme", "Terminus.hidden-color-scheme")

        max_columns = terminus_settings.get("max_columns")
        if max_columns:
            rulers = view_settings.get("rulers", [])
            if max_columns not in rulers:
                rulers.append(max_columns)
                view_settings.set("rulers", rulers)

        # search
        if "file_regex" in kwargs:
            view_settings.set("result_file_regex", kwargs["file_regex"])
        if "line_regex" in kwargs:
            view_settings.set("result_line_regex", kwargs["line_regex"])
        if "cwd" in kwargs:
            view_settings.set("result_base_dir", kwargs["cwd"])
        # disable bracket highligher (not working)
        view_settings.set("bracket_highlighter.ignore", True)
        view_settings.set("bracket_highlighter.clone_locations", {})
        # disable vintageous
        view_settings.set("__vi_external_disable", True)
        # honor settings specified in API right before user's
        for key, value in kwargs.get("view_settings", {}).items():
            view_settings.set(key, value)
        for key, value in terminus_settings.get("view_settings", {}).items():
            view_settings.set(key, value)
        # disable vintage
        view_settings.set("command_mode", False)


class TerminusActivateCommand(sublime_plugin.TextCommand):

    def run(self, _, **kwargs):
        view = self.view
        view.run_command("terminus_initialize_view", kwargs)
        Terminal.cull_terminals()
        terminal = Terminal(view)
        terminal.start(
            cmd=kwargs["cmd"],
            cwd=kwargs["cwd"],
            env=kwargs["env"],
            default_title=kwargs["default_title"],
            title=kwargs["title"],
            show_in_panel=kwargs["show_in_panel"],
            panel_name=kwargs["panel_name"],
            tag=kwargs["tag"],
            auto_close=kwargs["auto_close"],
            cancellable=kwargs["cancellable"],
            timeit=kwargs["timeit"]
        )
        recency_manager = RecencyManager.from_view(view)
        if recency_manager:
            RecencyManager.from_view(view).set_recent_terminal(view)


class TerminusClearUndoStackCommand(sublime_plugin.TextCommand):
    def run(self, _):
        if sublime.version() >= "4114":
            sublime.set_timeout(self.run_async)

    def run_async(self):
        view = self.view
        if view:
            view.clear_undo_stack()


class TerminusResetCommand(sublime_plugin.TextCommand):

    def run(self, _, soft=False, **kwargs):
        view = self.view
        terminal = Terminal.from_id(view.id())
        if not terminal:
            return

        view.run_command("terminus_clear_undo_stack")

        args = view.settings().get("terminus_view.args", {})

        if soft:
            view.run_command("terminus_nuke")
            view.settings().erase("terminus_view.viewport_y")
            terminal.set_offset()
            return

        def run_detach():
            terminal.detach_view()

            def run_sync():
                if terminal.show_in_panel:
                    panel_name = terminal.panel_name
                    window = get_panel_window(view)
                    if not window:
                        # the window went away over the detach hop, the terminal is
                        # detached and nothing would ever attach it again
                        abandon_detached_terminal(terminal)
                        return
                    window.destroy_output_panel(panel_name)  # do not reuse
                    new_view = window.get_output_panel(panel_name)

                    def run_attach():
                        new_view.run_command("terminus_initialize_view", args)
                        terminal.attach_view(new_view)
                        window.run_command("show_panel", {"panel": "output.{}".format(panel_name)})
                        window.focus_view(new_view)
                else:
                    window = view.window()
                    if not window:
                        abandon_detached_terminal(terminal)
                        return
                    has_focus = view == window.active_view()
                    layout = window.get_layout()
                    if not has_focus:
                        window.focus_view(view)
                    new_view = window.new_file(syntax="Terminus View.sublime-syntax")
                    view.close()

                    def run_attach():
                        new_view.run_command("terminus_initialize_view", args)
                        window.run_command("set_layout", layout)
                        if has_focus:
                            window.focus_view(new_view)
                        terminal.attach_view(new_view)

                sublime.set_timeout_async(run_attach)

            sublime.set_timeout(run_sync)

        sublime.set_timeout_async(run_detach)


class TerminusRenameTitleCommand(sublime_plugin.TextCommand):

    def run(self, _, title=None):
        view = self.view
        terminal = Terminal.from_id(view.id())

        terminal.title = title

        view.run_command("terminus_render")

    def input(self, _):
        return TerminusRenameTitleTextInputerHandler(self.view)

    def is_visible(self):
        return bool(Terminal.from_id(self.view.id()))


class TerminusRenameTitleTextInputerHandler(sublime_plugin.TextInputHandler):
    def __init__(self, view):
        self.view = view
        super().__init__()

    def name(self):
        return "title"

    def initial_text(self):
        terminal = Terminal.from_id(self.view.id())
        return terminal.title if terminal else ""

    def placeholder(self):
        return "new title"


class TerminusMaximizeCommand(sublime_plugin.TextCommand):

    def is_enabled(self):
        view = self.view
        terminal = Terminal.from_id(view.id())
        if terminal and terminal.show_in_panel:
            return True
        else:
            return False

    def run(self, _, **kwargs):
        view = self.view
        terminal = Terminal.from_id(view.id())

        # the original args have to survive the transition, otherwise the build settings,
        # the tag and the reactivability of the terminal are lost
        args = view.settings().get("terminus_view.args", {})

        def run_detach():
            all_text = view.substr(sublime.Region(0, view.size()))
            terminal.detach_view()

            def run_sync():
                offset = terminal.offset
                window = get_panel_window(view)
                if not window:
                    # the panel or its window went away over the detach hop, the
                    # terminal is detached and nothing would ever attach it again
                    abandon_detached_terminal(terminal)
                    return
                window.destroy_output_panel(terminal.panel_name)
                new_view = window.new_file(syntax="Terminus View.sublime-syntax")

                def run_attach():
                    view_args = dict(args)
                    view_args["show_in_panel"] = False
                    view_args["panel_name"] = None
                    new_view.run_command("terminus_initialize_view", view_args)
                    new_view.run_command(
                        "terminus_insert", {"point": 0, "character": all_text})
                    terminal.show_in_panel = False
                    terminal.attach_view(new_view, offset)

                sublime.set_timeout_async(run_attach)

            sublime.set_timeout(run_sync)

        sublime.set_timeout_async(run_detach)


def dont_close_windows_when_empty(func):
    def f(*args, **kwargs):
        s = sublime.load_settings('Preferences.sublime-settings')
        close_windows_when_empty = s.get('close_windows_when_empty')
        s.set('close_windows_when_empty', False)
        try:
            func(*args, **kwargs)
        finally:
            # an exception must not leave the preference disabled for the whole session
            if close_windows_when_empty:
                sublime.set_timeout(
                    lambda: s.set('close_windows_when_empty', close_windows_when_empty),
                    1000)
    return f


class TerminusMinimizeCommand(sublime_plugin.TextCommand):

    def is_enabled(self):
        view = self.view
        terminal = Terminal.from_id(view.id())
        if terminal and not terminal.show_in_panel:
            return True
        else:
            return False

    def run(self, _, **kwargs):
        view = self.view
        terminal = Terminal.from_id(view.id())

        # the original args have to survive the transition, otherwise the build settings,
        # the tag and the reactivability of the terminal are lost
        args = view.settings().get("terminus_view.args", {})

        def run_detach():
            all_text = view.substr(sublime.Region(0, view.size()))
            terminal.detach_view()

            @dont_close_windows_when_empty
            def run_sync():
                offset = terminal.offset
                window = view.window()
                if not window:
                    # the window went away over the detach hop, the terminal is
                    # detached and nothing would ever attach it again
                    abandon_detached_terminal(terminal)
                    return
                view.close()
                if "panel_name" in kwargs:
                    panel_name = kwargs["panel_name"]
                else:
                    panel_name = terminal.panel_name
                    if not panel_name:
                        panel_name = available_panel_name(window, DEFAULT_PANEL)

                # a tab terminal keeps carrying the default panel name, minimizing it
                # must not evict the terminal which already lives in that panel
                panel_name = unoccupied_panel_name(window, panel_name, terminal)

                new_view = window.get_output_panel(panel_name)

                def run_attach():
                    terminal.show_in_panel = True
                    terminal.panel_name = panel_name
                    view_args = dict(args)
                    view_args["show_in_panel"] = True
                    view_args["panel_name"] = panel_name
                    new_view.run_command("terminus_initialize_view", view_args)
                    new_view.run_command(
                        "terminus_insert", {"point": 0, "character": all_text})
                    window.run_command("show_panel", {"panel": "output.{}".format(panel_name)})
                    window.focus_view(new_view)
                    terminal.attach_view(new_view, offset)

                sublime.set_timeout_async(run_attach)

            sublime.set_timeout(run_sync)

        sublime.set_timeout_async(run_detach)


class TerminusKeypressCommand(sublime_plugin.TextCommand):

    def run(self, _, **kwargs):
        terminal = Terminal.from_id(self.view.id())
        if not terminal or not terminal.process.isalive():
            return
        # self.view.run_command("terminus_render")
        self.view.run_command("terminus_show_cursor")
        terminal.send_key(**kwargs)


class TerminusCopyCommand(sublime_plugin.TextCommand):
    """
    It does nothing special now, just `copy`.
    """

    def run(self, edit):
        view = self.view
        if not view.settings().get("terminus_view"):
            return
        text = ""
        for s in view.sel():
            if text:
                text += "\n"
            text += view.substr(s)

        # remove the continuation marker
        text = text.replace(CONTINUATION + "\n", "")
        text = text.replace(CONTINUATION, "")

        sublime.set_clipboard(text)


class TerminusPasteCommand(sublime_plugin.TextCommand):
    def run(self, edit, bracketed=True):
        copied = sublime.get_clipboard()
        self.view.run_command("terminus_paste_text", {"text": copied, "bracketed": bracketed})


class TerminusPasteFromHistoryCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        # provide paste choices
        paste_list = g_clipboard_history.get()
        keys = [x[0] for x in paste_list]
        self.view.show_popup_menu(keys, lambda choice_index: self.paste_choice(choice_index))

    def is_enabled(self):
        return not g_clipboard_history.empty()

    def paste_choice(self, choice_index):
        if choice_index == -1:
            return
        # use normal paste command
        text = g_clipboard_history.get()[choice_index][1]

        # rotate to top
        g_clipboard_history.push_text(text)

        sublime.set_clipboard(text)
        self.view.run_command("terminus_paste")


class TerminusPasteTextCommand(sublime_plugin.TextCommand):
    def run(self, edit, text, bracketed=True):
        view = self.view
        terminal = Terminal.from_id(view.id())
        if not terminal:
            return

        bracketed = bracketed and terminal.bracketed_paste_mode_enabled()
        if bracketed:
            terminal.send_key("bracketed_paste_mode_start")

        # self.view.run_command("terminus_render")
        self.view.run_command("terminus_show_cursor")

        terminal.send_string(text)

        if bracketed:
            terminal.send_key("bracketed_paste_mode_end")


class TerminusDeleteWordCommand(sublime_plugin.TextCommand):
    """
    On Windows, ctrl+backspace and ctrl+delete are used to delete words
    However, there is no standard key to delete word with ctrl+backspace
    a workaround is to repeatedly apply backspace to delete word
    """

    def run(self, edit, forward=False):
        view = self.view
        terminal = Terminal.from_id(view.id())
        if not terminal:
            return

        if len(view.sel()) != 1 or not view.sel()[0].empty():
            return

        if forward:
            pt = view.sel()[0].end()
            line = view.line(pt)
            text = view.substr(sublime.Region(pt, line.end()))
            match = re.search(r"(?<=\w)\b", text)
            if match:
                n = match.span()[0]
                n = n if n > 0 else 1
            else:
                n = 1
            delete_code = get_key_code("delete")

        else:
            pt = view.sel()[0].end()
            line = view.line(pt)
            text = view.substr(sublime.Region(line.begin(), pt))
            matches = list(re.finditer(r"\b(?=\w)", text))
            if matches:
                for match in matches:
                    pass
                n = view.rowcol(pt)[1] - match.span()[0]
                n if n > 0 else 1
            else:
                n = 1
            delete_code = get_key_code("backspace")

        # self.view.run_command("terminus_render")
        self.view.run_command("terminus_show_cursor")

        terminal.send_string(delete_code * n)


class ToggleTerminusPanelCommand(sublime_plugin.WindowCommand):
    def __init__(self, *args, **kwargs):
        self.cycled_panels = []
        super().__init__(*args, **kwargs)

    def run(self, panel_name=None, cycle=False, hide_active=None, **kwargs):
        window = self.window
        recency_manager = RecencyManager.from_window(window)
        if not recency_manager:
            return
        if cycle:
            if panel_name:
                raise ValueError("panel_name has to be None when cycle is True")

            if not recency_manager.cycling_panels:
                self.cycled_panels[:] = []
                recency_manager.cycling_panels = True

            panels = self.list_cycle_panels()
            if panels:
                panel_name = next((p for p in panels if p not in self.cycled_panels), None)
                if panel_name:
                    self.cycled_panels.append(panel_name)
                else:
                    self.cycled_panels[:] = []
            else:
                self.cycled_panels[:] = []

        if not panel_name:
            panel_name = recency_manager.recent_panel() or DEFAULT_PANEL

        terminus_view = window.find_output_panel(panel_name)
        if terminus_view:
            active_panel = window.active_panel()
            if hide_active and active_panel == "output.{}".format(panel_name):
                window.run_command("hide_panel")
            else:
                window.run_command(
                    "show_panel", {"panel": "output.{}".format(panel_name), "toggle": True})
                window.focus_view(terminus_view)
        else:
            kwargs["panel_name"] = panel_name
            window.run_command("terminus_open", kwargs)

    def list_cycle_panels(self):
        window = self.window
        recency_manager = RecencyManager.from_window(window)
        if not recency_manager:
            return

        panels = []
        active_panel = window.active_panel()
        active_index = -1

        for p in window.panels():
            panel_name = p.replace("output.", "")
            if panel_name == EXEC_PANEL:
                continue
            view = window.find_output_panel(panel_name)
            if view and view.settings().get("terminus_view"):
                if p == active_panel:
                    active_index = len(panels)
                panels.append(panel_name)

        if active_index != -1:
            panels = panels[active_index+1:] + panels[:active_index+1]
        else:
            self.cycled_panels[:] = []
            recent_panel_name = recency_manager.recent_panel()
            try:
                recent_index = panels.index(recent_panel_name)
            except ValueError:
                recent_index = -1
            if recent_index != -1:
                panels = panels[recent_index:] + panels[:recent_index]

        return panels


class TerminusFindTerminalMixin:

    def find_terminal(self, window, tag=None, panel_only=False, visible_only=False):

        if tag:
            terminal = Terminal.from_tag(tag)
            if terminal:
                return terminal

        view = None
        recency_manager = RecencyManager.from_window(window)
        if not recency_manager:
            return

        # The order of discovery is the following:
        # 1. the most recent view (including panel) if it is visible
        # 2. the most recent panel if it is visible
        # 3. any visible panbel
        # 4. any visible view
        # 5. the most recent view (including panel)
        # 6. the most recent panel
        # 7. any panel
        # 8. any view

        if not view:
            view = recency_manager.recent_view()
            if view:
                terminal = Terminal.from_id(view.id())
                if not terminal or (panel_only and not terminal.show_in_panel):
                    view = None
                if view:
                    if terminal.show_in_panel:
                        if not panel_is_visible(view):
                            view = None
                    elif not view_is_visible(view):
                        view = None

        if not view:
            panel_name = recency_manager.recent_panel()
            if panel_name:
                view = window.find_output_panel(panel_name)
                if view:
                    terminal = Terminal.from_id(view.id())
                    if not terminal:
                        view = None
                    if view:
                        if terminal.show_in_panel:
                            if not panel_is_visible(view):
                                view = None
                        elif not view_is_visible(view):
                            view = None

        if not view:
            view = self.get_terminus_panel(window, visible_only=True)

        if not view and not panel_only:
            view = self.get_terminus_view(window, visible_only=True)

        if not visible_only:
            if not view:
                view = recency_manager.recent_view()
                if view:
                    terminal = Terminal.from_id(view.id())
                    if not terminal or (panel_only and not terminal.show_in_panel):
                        view = None

            if not view:
                panel_name = recency_manager.recent_panel()
                if panel_name:
                    view = window.find_output_panel(panel_name)
                    if view:
                        terminal = Terminal.from_id(view.id())
                        if not terminal:
                            view = None

            if not view:
                view = self.get_terminus_panel(window, visible_only=False)
            if not view and not panel_only:
                view = self.get_terminus_view(window, visible_only=False)

        if view:
            terminal = Terminal.from_id(view.id())
        else:
            terminal = None

        return terminal

    def get_terminus_panel(self, window, visible_only=False):
        if visible_only:
            active_panel = window.active_panel()
            panels = [active_panel] if active_panel else []
        else:
            panels = window.panels()
        for panel in panels:
            panel_name = panel.replace("output.", "")
            if panel_name == EXEC_PANEL:
                continue
            panel_view = window.find_output_panel(panel_name)
            if panel_view:
                terminal = Terminal.from_id(panel_view.id())
                if terminal:
                    return panel_view
        return None

    def get_terminus_view(self, window, visible_only=False):
        for view in window.views():
            if visible_only:
                if not view_is_visible(view):
                    continue
            terminal = Terminal.from_id(view.id())
            if terminal:
                return view


class TerminusSendStringCommand(TerminusFindTerminalMixin, sublime_plugin.WindowCommand):
    """
    Send string to a (tagged) terminal
    """

    def run(self, string, tag=None, visible_only=False, bracketed=False):
        terminal = self.find_terminal(self.window, tag=tag, visible_only=visible_only)

        if not terminal:
            raise Exception("no terminal found")
        elif not terminal.process.isalive():
            raise Exception("process is terminated")

        if terminal.show_in_panel:
            self.window.run_command("show_panel", {
                "panel": "output.{}".format(terminal.panel_name)
            })
        else:
            self.bring_view_to_topmost(terminal.view)

        # terminal.view.run_command("terminus_render")
        # terminal.view.run_command("terminus_show_cursor")
        terminal.view.run_command(
            "terminus_paste_text", {"text": string, "bracketed": bracketed})

    def bring_view_to_topmost(self, view):
        # move the view to the top of the group
        if not view_is_visible(view):
            window = view.window()
            if window:
                window_active_view = window.active_view()
                window.focus_view(view)

                # do not refocus if view and active_view are of the same group
                group, _ = window.get_view_index(view)
                if window.get_view_index(window_active_view)[0] != group:
                    window.focus_view(window_active_view)
