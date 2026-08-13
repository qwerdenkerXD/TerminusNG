import sublime
import sublime_plugin

import logging
import os

from colorsys import rgb_to_hls
from Terminus.tools.theme_generator import generate_theme_file, ANSI_COLORS, DEFAULT_BACKGROUND
from .utils import set_settings_on_change

logger = logging.getLogger('Terminus')

# the 256 color scheme carries a rule for every pair of 256 colors and its content
# depends on nothing but the background baked into it, so remember the background of
# the file on disk and skip the work whenever it would write the very same file. the
# settings may hold a null background, so the "nothing written yet" mark cannot be None
_UNSET = object()
_generated_256_background = _UNSET

# theme generations asked for by a settings change are coalesced, only the most
# recently scheduled one is allowed to run
_generate_theme_token = 0

# set while the theme quick panel is open, see schedule_generate_theme
_previewing_theme = False

# how long a settings driven generation waits, long enough to swallow the burst of
# settings changes the theme quick panel makes while the user browses it
GENERATE_THEME_DELAY = 250


def cancel_scheduled_generate_theme():
    """Drop the theme generation a settings change scheduled, if there is one."""
    global _generate_theme_token
    _generate_theme_token += 1


def schedule_generate_theme(delay=GENERATE_THEME_DELAY):
    """Generate the theme shortly, coalescing a burst of settings changes into one run."""
    global _generate_theme_token
    _generate_theme_token += 1
    token = _generate_theme_token
    # browsing the quick panel saves the settings on every highlight, and all a preview
    # needs is the main scheme. the 256 color file is several megabytes and writing it
    # per browsed theme would stall the window, so leave it to the confirmed choice
    skip256 = _previewing_theme

    def _generate():
        if token != _generate_theme_token:
            # a later settings change superseded this generation
            return
        window = sublime.active_window()
        if window:
            window.run_command("terminus_generate_theme", {"skip256": skip256})
        else:
            logger.error("no active window, the theme was not generated")

    sublime.set_timeout(_generate, delay)


class TerminusSelectThemeCommand(sublime_plugin.WindowCommand):
    themefiles = []

    def get_theme_files(self):
        for f in sublime.find_resources("*.json"):
            if f.startswith("Packages/Terminus/themes/"):
                yield f.replace("Packages/Terminus/themes/", "")

    def run(self):
        global _previewing_theme

        if not self.themefiles:
            self.themefiles = list(self.get_theme_files())

        settings = sublime.load_settings("Terminus.sublime-settings")

        self.themes = ["default", "adaptive", "user"] + \
            sorted([f.replace(".json", "") for f in self.themefiles])
        self.original_theme = settings.get("theme", "default")
        try:
            selected_index = self.themes.index(self.original_theme)
        except Exception:
            selected_index = 0
        _previewing_theme = True
        self.window.show_quick_panel(
            self.themes,
            self.on_selection,
            selected_index=selected_index,
            on_highlight=lambda x: sublime.set_timeout_async(
                lambda: self.on_selection(x, generate_theme=False)))

    def set_theme(self, theme):
        if theme not in ["default", "adaptive", "user"]:
            if theme + ".json" not in self.themefiles:
                raise IOError("Theme '{}' not found".format(theme))
        settings = sublime.load_settings("Terminus.sublime-settings")
        settings.set("theme", theme)
        sublime.save_settings("Terminus.sublime-settings")

    def on_selection(self, index, generate_theme=True):
        global _previewing_theme

        if index == -1:
            # the panel is gone, the restored theme gets a generation of its own again
            _previewing_theme = False
            self.set_theme(self.original_theme)
            return
        if generate_theme:
            _previewing_theme = False
        self.set_theme(self.themes[index])
        if generate_theme:
            # the settings change above schedules a generation of its own, run the
            # generation right away instead so the choice is applied without a delay
            cancel_scheduled_generate_theme()
            self.window.run_command("terminus_generate_theme", {'force': True})


class TerminusGenerateThemeCommand(sublime_plugin.WindowCommand):
    def run(self, theme=None, remove=False, force=False, skip256=False):
        global _generated_256_background

        settings = sublime.load_settings("Terminus.sublime-settings")

        if not theme:
            theme = settings.get("theme", "default")

        if sublime.version() < "4096" and theme == "adaptive":
            theme = "default"

        if theme == "user":
            variables = settings.get("user_theme_colors", {})

            if isinstance(variables, dict):
                # work on a copy, the settings are not ours to rewrite
                variables = dict(variables)
            else:
                logger.error("user_theme_colors is not an object, ignoring it")
                variables = {}

            if sublime.version() >= "4096":
                current_style = sublime.ui_info()['theme']['style']
                style_variables = settings.get("user_{}_theme_colors".format(current_style), None)

                if isinstance(style_variables, dict):
                    variables.update(style_variables)

            for key, value in list(variables.items()):
                if not isinstance(key, str) or not key.isdigit():
                    continue
                # a numeric key names an ansi color by its index, a key which is not a
                # usable index is dropped and reported instead of aborting the generation
                del variables[key]
                try:
                    # isdigit() accepts digits int() rejects, superscripts for example
                    index = int(key)
                except ValueError:
                    index = -1
                if 0 <= index < len(ANSI_COLORS):
                    variables[ANSI_COLORS[index]] = value
                else:
                    logger.error(
                        "ignoring user theme color {}, expected an index between 0 and {}".format(
                            key, len(ANSI_COLORS) - 1))

        elif theme == "default" or theme == "classic":
            variables = {}
        elif theme == "adaptive":
            ui_info = sublime.ui_info()
            palette = ui_info["color_scheme"]["palette"]
            gray = "#888888"
            window = sublime.active_window()
            if window:
                _panel = "terminus_color_scheme"
                view = window.create_output_panel(_panel, True)
                comment_foreground = view.style_for_scope("comment")["foreground"]
                r = int(comment_foreground[1:3], 16)
                g = int(comment_foreground[3:5], 16)
                b = int(comment_foreground[5:7], 16)
                _, _, s = rgb_to_hls(r/255, g/255, b/255)
                if s < 0.2:
                    gray = comment_foreground
                window.destroy_output_panel(_panel)
            light_color_template = "color({} l(+ 15%))"
            variables = {
                "background": palette["background"],
                "foreground": palette["foreground"],
                # no "selection" and no "selection_foreground": a hidden color scheme
                # overrides the variables of the scheme it merges into, and a scheme
                # which defines its selection as a variable, as most of them do, would
                # take ours instead of its own. the adaptive theme is supposed to look
                # like the editor around it, so selecting in a terminal has to look
                # exactly like selecting anywhere else, down to keeping the colors the
                # program chose for the text underneath
                "black": "#000000",
                "red": palette["redish"],
                "green": palette["greenish"],
                "brown": palette["yellowish"],
                "blue": palette["bluish"],
                "magenta": palette["pinkish"],
                "cyan": palette["cyanish"],
                "white": gray,
                "light_black": light_color_template.format(gray),
                "light_red": light_color_template.format(palette["redish"]),
                "light_green": light_color_template.format(palette["greenish"]),
                "light_brown": light_color_template.format(palette["yellowish"]),
                "light_blue": light_color_template.format(palette["bluish"]),
                "light_magenta": light_color_template.format(palette["pinkish"]),
                "light_cyan": light_color_template.format(palette["cyanish"]),
                "light_white": "#ffffff"
            }
        else:
            content = sublime.load_resource("Packages/Terminus/themes/{}.json".format(theme))
            theme_data = sublime.decode_value(content)
            variables = theme_data["theme_colors"]

        path = os.path.join(
            sublime.packages_path(),
            "User",
            "Terminus",
            "Terminus.hidden-color-scheme"
        )

        path256 = os.path.join(
            sublime.packages_path(),
            "User",
            "Terminus.hidden-color-scheme"
        )

        if remove:
            if os.path.isfile(path):
                os.unlink(path)
                print("Theme removed: {}".format(path))
            if os.path.isfile(path256):
                os.unlink(path256)
                print("Theme removed: {}".format(path256))
            _generated_256_background = _UNSET
            sublime.status_message("Theme {} removed".format(theme))
        else:
            if settings.get("256color", False):
                if "background" in variables:
                    background = variables["background"]
                else:
                    background = DEFAULT_BACKGROUND
                # the file bakes in the background, so a settings driven theme change has
                # to rewrite it as well, otherwise the 256 color cells keep rendering
                # against the background of the previous theme. a preview says skip256
                # and leaves it to the theme the user settles on
                if not skip256 and (force or not os.path.isfile(path256) or
                                    _generated_256_background != background):
                    generate_theme_file(
                        path256, ansi_scopes=True, color256_scopes=True, background=background,
                        pretty=False)
                    _generated_256_background = background
                    print("Theme {} generated: {}".format(theme, path256))
            else:
                if os.path.isfile(path256):
                    os.unlink(path256)
                _generated_256_background = _UNSET

            generate_theme_file(path, variables=variables, ansi_scopes=False, color256_scopes=False)
            print("Theme {} generated: {}".format(theme, path))

            sublime.status_message("Theme generated")


def plugin_loaded():
    # this is a hack to remove the deprecated sublime-color-scheme files
    deprecated_paths = [
        os.path.join(sublime.packages_path(), "User", "Console.sublime-color-scheme"),
        os.path.join(sublime.packages_path(), "User", "SublimelyTerminal.sublime-color-scheme"),
        os.path.join(sublime.packages_path(), "User", "Terminus.sublime-color-scheme"),
        os.path.join(sublime.packages_path(), "User", "Terminus", "Terminus.sublime-color-scheme")
    ]
    for deprecated_path in deprecated_paths:
        if os.path.isfile(deprecated_path):
            os.unlink(deprecated_path)

    settings = sublime.load_settings("Terminus.sublime-settings")
    preferences = sublime.load_settings("Preferences.sublime-settings")

    path = os.path.join(
        sublime.packages_path(),
        "User",
        "Terminus",
        "Terminus.hidden-color-scheme"
    )

    path256 = os.path.join(
        sublime.packages_path(),
        "User",
        "Terminus.hidden-color-scheme"
    )

    if (not os.path.isfile(path) or
            (settings.get("256color", False) and not os.path.isfile(path256))):
        schedule_generate_theme(100)

    set_settings_on_change(
        settings,
        ["256color", "user_theme_colors",
         "user_light_theme_colors", "user_dark_theme_colors", "theme"],
        lambda _: schedule_generate_theme())

    def check_update_theme(value):
        if settings.get("theme", "adaptive") == "adaptive":
            schedule_generate_theme()

    set_settings_on_change(preferences, "color_scheme", check_update_theme)


def plugin_unloaded():
    settings = sublime.load_settings("Terminus.sublime-settings")
    preferences = sublime.load_settings("Preferences.sublime-settings")
    set_settings_on_change(
        settings,
        ["256color", "user_theme_colors",
         "user_light_theme_colors", "user_dark_theme_colors", "theme"], None)
    set_settings_on_change(preferences, "color_scheme", None)
