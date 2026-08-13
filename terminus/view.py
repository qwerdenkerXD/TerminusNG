import sublime
import sublime_plugin

import logging
import re


logger = logging.getLogger('Terminus')

# columns reserved on the right hand side of the viewport, they are covered by
# the vertical scrollbar and the right margin and hence not usable for text
RESERVED_COLUMNS = 3


def find_panel(view):
    # returns (window, panel name) of the output panel hosting `view`,
    # (None, None) if `view` is not an output panel of any window
    for w in sublime.windows():
        for panel in w.panels():
            name = panel.replace("output.", "")
            v = w.find_output_panel(name)
            if v and v.id() == view.id():
                return (w, name)
    return (None, None)


def get_panel_window(view):
    return find_panel(view)[0]


def get_panel_name(view):
    return find_panel(view)[1]


def panel_is_visible(view):
    window = get_panel_window(view)
    if not window:
        return False
    active_panel = window.active_panel()
    if not active_panel:
        return False
    active_view = window.find_output_panel(active_panel.replace("output.", ""))
    return active_view == view


def view_is_visible(view):
    window = view.window()
    if not window:
        return False
    group, _ = window.get_view_index(view)
    return window.active_view_in_group(group) == view


def view_size(view, default=None, force=None):
    if force:
        if all(force):
            return force
    settings = sublime.load_settings("Terminus.sublime-settings")
    min_rows = settings.get("min_rows", 4)
    min_columns = settings.get("min_columns", 20)
    max_columns = settings.get("max_columns", 500)

    pixel_width, pixel_height = view.viewport_extent()
    pixel_per_line = view.line_height()
    pixel_per_char = view.em_width()

    if pixel_width == 0 or pixel_height == 0 or pixel_per_line == 0 or pixel_per_char == 0:
        # the viewport is not measurable, the view is either not laid out yet or
        # not visible; any size derived from it would be degenerate
        if default:
            return default
        logger.debug("unmeasurable viewport, falling back to {} {}".format(min_rows, min_columns))
        return (min_rows, min_columns)

    nb_columns = int(pixel_width / pixel_per_char) - RESERVED_COLUMNS
    if nb_columns < 1:
        nb_columns = 1

    nb_rows = int(pixel_height / pixel_per_line)
    if nb_rows < 1:
        nb_rows = 1

    if nb_columns == 1 and default:
        return default

    if nb_columns < min_columns:
        nb_columns = min_columns
    elif nb_columns > max_columns:
        nb_columns = max_columns

    # a one row pty makes full screen programs such as vim, less or htop
    # unusable, keep a floor under the number of rows as well
    if nb_rows < min_rows:
        nb_rows = min_rows

    return (nb_rows, nb_columns)


class TerminusInsertCommand(sublime_plugin.TextCommand):

    def run(self, edit, point, character):
        self.view.insert(edit, point, character)


class TerminusTrimTrailingLinesCommand(sublime_plugin.TextCommand):

    def run(self, edit):
        view = self.view
        lastrow = view.rowcol(view.size())[0]
        if not self.is_empty(lastrow):
            view.insert(edit, view.size(), "\n")
            lastrow = lastrow + 1
        row = lastrow
        while row >= 1:
            if self.is_empty(row-1):
                R = view.line(view.text_point(row, 0))
                view.erase(edit, sublime.Region(R.a-1, R.b))
                row = row-1
            else:
                if self.is_empty(row):
                    R = view.line(view.text_point(row, 0))
                    view.erase(edit, sublime.Region(R.a, R.b))
                break

    def is_empty(self, row):
        view = self.view
        return re.match(r"^\s*$", view.substr(view.line(view.text_point(row, 0))))


class TerminusNukeCommand(sublime_plugin.TextCommand):

    def run(self, edit):
        view = self.view
        view.replace(edit, sublime.Region(0, view.size()), "")
