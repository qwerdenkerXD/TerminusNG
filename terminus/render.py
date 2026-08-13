import sublime
import sublime_plugin

import time
import math
import logging
import pyte
from functools import lru_cache
from wcwidth import wcswidth


from .const import CONTINUATION
from .ptty import XTERM_256_COLORS, EMPTY_LINKS, line_marks, line_links
from .terminal import Terminal
from .utils import rev_wcwidth, get_highlight_key

logger = logging.getLogger('Terminus')


# the scope the underline of an OSC 8 hyperlink is drawn in. it is the scope color
# schemes already give links, so a terminal link picks up the same color the rest of
# the editor uses, and a user who wants another one only has to add a rule for it
LINK_SCOPE = "markup.underline.link.terminus"
# only the underline is drawn, no fill and no outline, so the foreground and the
# background the escape sequences asked for stay exactly as they are
LINK_FLAGS = sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE | sublime.DRAW_SOLID_UNDERLINE


@lru_cache(maxsize=10000)
def is_supported_color(c):
    return c in ['default', 'reverse_default'] or c in XTERM_256_COLORS


RGB256 = {}
for c in pyte.graphics.FG_BG_256:
    RGB256[c] = tuple(int(c[i:i+2], 16) for i in (0, 2, 4))


# https://en.wikipedia.org/wiki/Color_difference#sRGB
@lru_cache(maxsize=10000)
def get_closest_color(c):
    r, g, b = tuple(int(c[i:i+2], 16) for i in (0, 2, 4))
    dmin = 1000000
    closest_color = "000000"
    for c, (r2, g2, b2) in RGB256.items():
        redmean = (r + r2) / 2
        d = (2 + redmean / 256) * (r - r2) ** 2 + 4 * \
            (g - g2)**2 + (2 + (255-redmean) / 256) * (b - b2)**2
        if d < dmin:
            dmin = d
            closest_color = c
    return closest_color


def reverse_fg_bg(fg, bg):
    fg, bg = bg, fg
    if fg == "default":
        fg = "reverse_default"
    if bg == "default":
        bg = "reverse_default"
    return fg, bg


def segment_buffer_line(buffer_line):
    """
    segment a buffer line based on bg and fg colors
    """
    is_wide_char = False
    text = ""
    start = 0
    counter = 0
    fg = "default"
    bg = "default"
    bold = False
    reverse = False

    if buffer_line:
        last_index = max(buffer_line.keys()) + 1
    else:
        last_index = 0

    for i in range(last_index):
        if is_wide_char:
            is_wide_char = False
            continue
        char = buffer_line[i]
        is_wide_char = wcswidth(char.data) >= 2

        if counter == 0:
            counter = i
            text = " " * i

        if fg != char.fg or bg != char.bg or bold != char.bold or reverse != char.reverse:
            if reverse:
                fg, bg = reverse_fg_bg(fg, bg)
            yield text, start, counter, fg, bg, bold
            fg = char.fg
            bg = char.bg
            bold = char.bold
            reverse = char.reverse
            text = char.data
            start = counter
        else:
            text += char.data

        counter += 1

    if reverse:
        fg, bg = reverse_fg_bg(fg, bg)
    yield text, start, counter, fg, bg, bold


def column_offsets(buffer_line):
    """
    map the cell columns of a buffer line onto the columns of the text `update_line`
    writes for it, and return that map together with the width of the text.

    the two only agree while the line holds no wide character: `segment_buffer_line`
    emits one character per cell and skips the second half of a wide one, so every
    wide character moves the text one column left of the cell grid. a hyperlink span
    names cells, a region names text, this is the step in between
    """
    offsets = {}
    index = 0
    is_wide_char = False

    if buffer_line:
        last_index = max(buffer_line.keys()) + 1
    else:
        last_index = 0

    for i in range(last_index):
        offsets[i] = index
        if is_wide_char:
            # the second half of a wide character carries no character of its own
            is_wide_char = False
            continue
        is_wide_char = wcswidth(buffer_line[i].data) >= 2
        index += 1

    # the exclusive end of the last cell, i.e. the column just past the text
    offsets[last_index] = index
    return offsets, index


def text_column(offsets, width, column):
    """
    the text column a cell column lands on, the end of the text for a cell which was
    never written. a span may name a cell past the last one the line kept
    """
    if column in offsets:
        return offsets[column]
    return width


class RowLink:
    """
    a run of one view row which sits inside an OSC 8 hyperlink. start is inclusive,
    end is exclusive and both are columns of the row's text, the same columns
    `colorize_line` colors by. link is the shared Hyperlink ptty.py interned, so
    `one.link is other.link` still answers whether two runs are one link.

    a RowLink is never modified once it is built, `relink_line` replaces the whole
    tuple of a row instead
    """
    __slots__ = ("start", "end", "link")

    def __init__(self, start, end, link):
        self.start = start
        self.end = end
        self.link = link

    @property
    def uri(self):
        return self.link.uri

    @property
    def link_id(self):
        return self.link.link_id

    def __repr__(self):
        return "RowLink({}, {}, {})".format(self.start, self.end, self.link)


class LinkIndex:
    """
    the OSC 8 hyperlinks of the rows of one view: row -> (region key, tuple of
    RowLink). it is the counterpart of `colored_lines`, a row is established by the
    same pass which writes that row's text and dropped the moment the text goes.

    the runs are kept next to the key because a view region carries no target: the
    underline says there is a link, this says which one. all the runs of a row share
    a single key, so a build log full of links costs one `add_regions` per row and
    not one per link
    """

    def __init__(self):
        self.rows = {}

    def set_line(self, view, line, links, regions):
        self.drop_line(view, line)
        if not links:
            return
        key = get_highlight_key(view)
        view.add_regions(key, regions, LINK_SCOPE, flags=LINK_FLAGS)
        self.rows[line] = (key, links)

    def drop_line(self, view, line):
        entry = self.rows.pop(line, None)
        if entry:
            view.erase_regions(entry[0])

    def line_links(self, line):
        entry = self.rows.get(line)
        if not entry:
            return EMPTY_LINKS
        return entry[1]

    def clear(self, view):
        for line in list(self.rows.keys()):
            self.drop_line(view, line)

    def shift(self, view, m):
        """
        the top m rows are about to be erased: their keys are dropped outright, a
        clamped one would underline text which has nothing to do with the link, and
        the rest moves up with the text exactly the way `colored_lines` does
        """
        for line in list(self.rows.keys()):
            if line < m:
                self.drop_line(view, line)
        self.rows = {k - m: v for (k, v) in self.rows.items()}


# the link index of every view which has one, view id -> LinkIndex. the index of a
# view belongs to the render command of that view, this map only makes it reachable
# from mouse.py, which has a point and a view and nothing else. a view which stops
# being a terminal is forgotten, see `register_link_index`
_link_indexes = {}


def register_link_index(view, index):
    """
    publish the index of a view, so that the read side below can find it
    """
    vid = view.id()
    if _link_indexes.get(vid) is not index:
        # a new view is joining the map, take the chance to forget the views which
        # are not terminals any more. an entry is only ever rebuilt by a render of
        # the view it belongs to, so dropping one can never lose a live row
        for other in list(_link_indexes.keys()):
            if Terminal.from_id(other) is None:
                del _link_indexes[other]
        _link_indexes[vid] = index


def row_links(view, row):
    """
    the RowLink runs of a view row, ordered by column and non overlapping. it is the
    shared EMPTY_LINKS tuple when the row holds no link. treat it as immutable
    """
    index = _link_indexes.get(view.id())
    if index is None:
        return EMPTY_LINKS
    return index.line_links(row)


def link_at_point(view, point):
    """
    the Hyperlink the text under a point belongs to, or None. read `.uri` for the
    target and `.link_id` for the id= the shell sent, if any
    """
    row_link = row_link_at_point(view, point)
    if row_link is None:
        return None
    return row_link.link


def row_link_at_point(view, point):
    """
    the RowLink under a point, or None
    """
    row, col = view.rowcol(point)
    for row_link in row_links(view, row):
        if row_link.start <= col < row_link.end:
            return row_link
    return None


# there is deliberately nothing here which joins the runs of a wrapped link back
# together for the sake of a highlight. `relink_line` underlines every run of every
# row as it writes it, so the halves of a wrapped link are already drawn as links and
# a walk over the neighbouring rows would add no pixel to what is on screen. the one
# thing such a walk could do is take one run's target and paint it over another run,
# which is precisely what an id= out of a hostile stream would be for


class TerminusViewMixin:

    def ensure_position(self, edit, row, col=0):
        view = self.view
        lastrow = view.rowcol(view.size())[0]
        if lastrow < row:
            view.insert(edit, view.size(), "\n" * (row - lastrow))
        line_region = view.line(view.text_point(row, 0))
        lastcol = view.rowcol(line_region.end())[1]
        if lastcol < col:
            view.insert(edit, line_region.end(), " " * (col - lastcol))


class TerminusRenderCommand(sublime_plugin.TextCommand, TerminusViewMixin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # it keeps all the highlight keys
        self.colored_lines = {}
        # and this keeps the OSC 8 hyperlinks of the rows, the same way, see LinkIndex
        self.links = LinkIndex()
        settings = sublime.load_settings("Terminus.sublime-settings")
        self.scrollback_history_size = settings.get("scrollback_history_size", 10000)
        self.brighten_bold_text = settings.get("brighten_bold_text", False)
        # with this off no row is ever put in the index, so nothing is underlined and
        # mouse.py finds no link to open either, which is the whole of the opt out
        self.hyperlinks = settings.get("hyperlinks", True)

    def run(self, edit):
        view = self.view
        startt = time.time()
        terminal = Terminal.from_id(view.id())
        if not terminal:
            return

        screen = terminal.screen

        # mouse.py has a view and a point and no way to reach this instance, hand it
        # the index of this view. it is the same object across renders, so this is a
        # lookup and nothing else once the view is known
        register_link_index(view, self.links)

        if terminal._pending_to_clear_scrollback[0]:
            view.replace(edit, sublime.Region(0, view.size()), "")  # nuke everything
            # the text is gone, so all the color regions are empty now, drop them and
            # rewind the highlight counter, otherwise the next `get_highlight_key` has
            # to descend the whole counter one step at a time
            for line in list(self.colored_lines.keys()):
                self.decolorize_line(line)
            # the link underlines are keys out of the very same counter, they have to
            # go before it is rewound or `get_highlight_key` hands out a key which is
            # still in use here
            self.links.clear(view)
            view.settings().set("terminus.highlight_counter", 0)
            # the rows the marks name are gone with the text
            terminal.marks.clear()
            terminal.offset = 0
            terminal.clean_images()
            terminal._pending_to_clear_scrollback[0] = False

        if terminal._pending_to_reset[0]:
            def _reset():
                logger.debug("reset terminal")
                view.run_command("terminus_reset", {"soft": True})
                terminal._pending_to_reset[0] = False

            sublime.set_timeout(_reset)

        self.update_lines(edit, terminal)
        viewport_y = view.settings().get("terminus_view.viewport_y", 0)
        if viewport_y < view.viewport_position()[1] + view.line_height():
            self.trim_trailing_spaces(edit, terminal)
            self.trim_history(edit, terminal)
            view.run_command("terminus_show_cursor")

        current_title = view.name()
        if terminal.title:
            if current_title != terminal.title:
                view.set_name(terminal.title)
        else:
            if screen.title:
                if current_title != screen.title:
                    view.set_name(screen.title)
            else:
                if current_title != terminal.default_title:
                    view.set_name(terminal.default_title)

        # we should not clear dirty lines here, it shoud be done in the eventloop
        # screen.dirty.clear()
        logger.debug("updating lines takes {}s".format(str(time.time() - startt)))
        logger.debug("mode: {}, cursor: {}.{}".format(
            [m >> 5 for m in screen.mode], screen.cursor.x, screen.cursor.y))

    def update_lines(self, edit, terminal):
        # cursor = screen.cursor
        screen = terminal.screen
        columns = screen.columns
        dirty_lines = sorted(screen.dirty)
        if dirty_lines:
            # replay history
            history = screen.history
            terminal.offset += len(history)
            offset = terminal.offset
            logger.debug("add {} line(s) to scroll back history".format(len(history)))

            for line in range(len(history)):
                buffer_line = history.pop()
                lf = buffer_line[columns - 1].linefeed
                row = offset - line - 1
                self.update_line(edit, row, buffer_line, lf)
                self.remark_line(terminal, row, buffer_line)
                self.relink_line(row, buffer_line)

            # update dirty line¡s
            logger.debug("screen is dirty: {}".format(str(dirty_lines)))
            for line in dirty_lines:
                buffer_line = screen.buffer[line]
                lf = buffer_line[columns - 1].linefeed
                row = line + offset
                self.update_line(edit, row, buffer_line, lf)
                self.remark_line(terminal, row, buffer_line)
                self.relink_line(row, buffer_line)

    def remark_line(self, terminal, line, buffer_line):
        """
        mirror the OSC 133 marks of a buffer line onto the view row it was just
        written to, that write is the only place a mark becomes a row
        """
        marks = line_marks(buffer_line)
        if marks:
            terminal.marks[line] = marks
        else:
            # whatever line occupies the row now carries no mark, so the row does
            # not either, an entry left behind would name text which is gone
            terminal.marks.pop(line, None)

    def relink_line(self, line, buffer_line):
        """
        mirror the OSC 8 hyperlinks of a buffer line onto the view row it was just
        written to and underline them, that write is the only place a link becomes a
        row. same as the marks above: whatever line occupies the row now decides,
        an entry left behind would point at text which is gone
        """
        view = self.view
        spans = line_links(buffer_line) if self.hyperlinks else EMPTY_LINKS
        if not spans:
            self.links.drop_line(view, line)
            return

        offsets, width = column_offsets(buffer_line)
        line_region = view.line(view.text_point(line, 0))
        begin = line_region.begin()
        # the text was rstripped and may be shorter than the cells the spans name
        last = line_region.end() - begin

        links = []
        regions = []
        for span in spans:
            a = min(text_column(offsets, width, span.start), last)
            b = min(text_column(offsets, width, span.end), last)
            if b <= a:
                # the run is entirely inside the trailing space which was stripped
                continue
            links.append(RowLink(a, b, span.link))
            regions.append(sublime.Region(begin + a, begin + b))

        self.links.set_line(view, line, tuple(links), regions)

    def update_line(self, edit, line, buffer_line, lf):
        view = self.view
        # make sure the view has enough lines
        self.ensure_position(edit, line)
        line_region = view.line(view.text_point(line, 0))
        segments = list(segment_buffer_line(buffer_line))

        text = "".join(s[0] for s in segments)
        if lf:
            # append a zero width space if the the line ends with a linefeed
            # we will use it to do non-break copying and searching
            # this hack is much easier than rewraping the lines
            text += CONTINUATION

        text = text.rstrip()
        self.decolorize_line(line)
        view.replace(edit, line_region, text)
        self.colorize_line(edit, line, segments)

    def colorize_line(self, edit, line, segments):
        view = self.view
        if segments:
            # ensure the last segement's position exists
            self.ensure_position(edit, line, segments[-1][2])
            if line not in self.colored_lines:
                self.colored_lines[line] = []
        # segments of a line sharing the same scope are batched together, so that they
        # take one `add_regions` call instead of one call each. the batches are kept
        # within a line, hence a key never spans two lines and `decolorize_line` and
        # `trim_history` keep working on a per line basis
        regions_of_scope = {}
        for s in segments:
            fg, bg, bold = s[3:]
            if not is_supported_color(fg):
                fg = get_closest_color(fg)
            if not is_supported_color(bg):
                bg = get_closest_color(bg)
            if fg != "default" or bg != "default":
                if bold and self.brighten_bold_text:
                    if fg != "default" and fg != "reverse_default" and not fg.startswith("light_"):
                        fg = "light_" + fg
                    if bg != "default" and bg != "reverse_default" and not bg.startswith("light_"):
                        bg = "light_" + bg
                a = view.text_point(line, s[1])
                b = view.text_point(line, s[2])
                scope = "terminus.{}.{}".format(fg, bg)
                if scope not in regions_of_scope:
                    regions_of_scope[scope] = []
                regions_of_scope[scope].append(sublime.Region(a, b))

        for scope, regions in regions_of_scope.items():
            key = get_highlight_key(view)
            view.add_regions(key, regions, scope)
            self.colored_lines[line].append(key)

    def decolorize_line(self, line):
        if line in self.colored_lines:
            for key in self.colored_lines[line]:
                self.view.erase_regions(key)
            del self.colored_lines[line]

    def trim_trailing_spaces(self, edit, terminal):
        view = self.view
        screen = terminal.screen
        cursor = screen.cursor
        cursor_row = terminal.offset + screen.cursor.y
        lastrow = view.rowcol(view.size())[0]
        row = lastrow
        while row > cursor_row:
            line_region = view.line(view.text_point(row, 0))
            text = view.substr(line_region)
            if len(text.strip()) == 0 and \
                    (row not in self.colored_lines or len(self.colored_lines[row]) == 0):
                region = view.line(view.text_point(row, 0))
                view.erase(edit, sublime.Region(region.begin() - 1, region.end()))
                # the row goes, so the mark on it goes too. a mark never keeps text
                # alive, it only ever follows it. the buffer line still carries it
                # and the next write of that row brings it back
                terminal.marks.pop(row, None)
                # the same goes for the links of the row, the underline would sit on
                # whatever text ends up on that row next
                self.links.drop_line(view, row)
                row = row - 1
            else:
                break
        if row == cursor_row:
            line_region = view.line(view.text_point(row, 0))
            text = view.substr(line_region)
            trailing_region = sublime.Region(
                line_region.begin() + rev_wcwidth(text, cursor.x) + 1,
                line_region.end())
            if not trailing_region.empty() and len(view.substr(trailing_region).strip()) == 0:
                view.erase(edit, trailing_region)

    def trim_history(self, edit, terminal):
        """
        If number of lines in view > n, remove n / 10 lines from the top
        """
        view = self.view

        screen = terminal.screen
        lastrow = view.rowcol(view.size())[0]
        n = self.scrollback_history_size
        if lastrow + 1 > n:
            m = max(lastrow + 1 - n, math.ceil(n / 10))
            logger.debug("removing {} lines from the top".format(m))
            for line in range(m):
                self.decolorize_line(line)
            # shift colored_lines indexes
            self.colored_lines = {k - m: v for (k, v) in self.colored_lines.items()}
            # the marks of the rows which are about to go are dropped outright, a
            # clamped one would be a phantom prompt sitting on row 0 forever. the
            # rest shifts with the text, the same way colored_lines does above
            terminal.marks = {k - m: v for (k, v) in terminal.marks.items() if k >= m}
            # and the link rows, which own view regions, so they are erased and not
            # only forgotten, see LinkIndex.shift
            self.links.shift(view, m)
            top_region = sublime.Region(0, view.line(view.text_point(m - 1, 0)).end() + 1)
            view.erase(edit, top_region)
            terminal.offset -= m
            lastrow -= m

            # delete outdated images
            terminal.clean_images()

        if lastrow > terminal.offset + screen.lines:
            tail_region = sublime.Region(
                view.text_point(terminal.offset + screen.lines, 0),
                view.size()
            )
            for line in view.lines(tail_region):
                row = view.rowcol(line.begin())[0]
                self.decolorize_line(row)
                terminal.marks.pop(row, None)
                self.links.drop_line(view, row)
            view.erase(edit, tail_region)


class TerminusShowCursorCommand(sublime_plugin.TextCommand, TerminusViewMixin):

    def run(self, edit, focus=True, scroll=True):
        view = self.view
        terminal = Terminal.from_id(view.id())
        if not terminal:
            return

        if focus:
            self.focus_cursor(edit, terminal)
        if scroll:
            sublime.set_timeout(lambda: self.scroll_to_cursor(terminal))

    def focus_cursor(self, edit, terminal):
        view = self.view

        sel = view.sel()
        sel.clear()

        screen = terminal.screen
        if screen.cursor.hidden:
            return

        cursor = screen.cursor
        offset = terminal.offset

        if len(view.sel()) > 0 and view.sel()[0].empty():
            row, col = view.rowcol(view.sel()[0].end())
            if row == offset + cursor.y and col == cursor.x:
                return

        # make sure the view has enough lines
        self.ensure_position(edit, cursor.y + offset)

        line_region = view.line(view.text_point(cursor.y + offset, 0))
        text = view.substr(line_region)
        col = rev_wcwidth(text, cursor.x) + 1

        self.ensure_position(edit, cursor.y + offset, col)
        pt = view.text_point(cursor.y + offset, col)

        sel.add(sublime.Region(pt, pt))

    def scroll_to_cursor(self, terminal):
        view = self.view
        last_y = view.text_to_layout(view.size())[1]
        viewport_y = last_y - view.viewport_extent()[1] + view.line_height()
        offset_y = view.text_to_layout(view.text_point(terminal.offset, 0))[1]
        y = max(offset_y, viewport_y)
        view.settings().set("terminus_view.viewport_y", y)
        view.set_viewport_position((0, y), False)


class TerminusCleanupCommand(sublime_plugin.TextCommand):
    def run(self, edit, by_user=False):
        logger.debug("cleanup")
        view = self.view
        terminal = Terminal.from_id(view.id())
        if not terminal:
            return

        if view.settings().get("terminus_view.finished"):
            return

        # to avoid double cancel
        view.settings().set("terminus_view.finished", True)

        view.run_command("terminus_render")

        # process might became orphan, make sure the process is terminated
        terminal.kill()
        process = terminal.process

        if terminal.auto_close is True or terminal.auto_close == "always" or \
                (process.exitstatus == 0 and terminal.auto_close == "on_success"):
            view.run_command("terminus_close")

        view.run_command("terminus_trim_trailing_lines")

        if by_user:
            view.run_command("append", {"characters": "[Cancelled]"})

        elif terminal.timeit:
            if process.exitstatus == 0:
                view.run_command(
                    "append",
                    {"characters": "[Finished in {:0.2f}s]".format(
                        time.time() - terminal.start_time)})
            else:
                view.run_command(
                    "append",
                    {"characters": "[Finished in {:0.2f}s with exit code {}]".format(
                        time.time() - terminal.start_time, process.exitstatus)})
        elif process.exitstatus is not None:
            view.run_command(
                "append",
                {"characters": "process is terminated with return code {}.".format(
                    process.exitstatus)})

        view.sel().clear()

        if not terminal.show_in_panel and view.settings().get("result_file_regex"):
            # if it is a tab based build, we will to refocus to enable next_result
            window = view.window()
            if window:
                active_view = window.active_view()
                view.window().focus_view(view)
                if active_view:
                    view.window().focus_view(active_view)
