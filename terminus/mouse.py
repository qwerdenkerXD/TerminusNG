import sublime
import sublime_plugin

import os
import re
import sys
import html
import bisect
import logging
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

from .commands import is_wsl_command, terminal_command
from .const import CONTINUATION
from .ptty import safe_link_uri, uri_to_path
from .render import link_at_point
from .terminal import Terminal
from .utils import get_highlight_key
from .view import get_panel_window
from .wsl import wsl_to_windows_for_cmd

logger = logging.getLogger('Terminus')

rex = re.compile(
    r'''(?x)
    \b(?:
        https?://(?:(?:[a-zA-Z0-9\-._]+)|localhost)|  # http://
        www\.[a-zA-Z0-9\-_]+(?:\.[a-zA-Z0-9\-._]+)+   # www.
    )
    /?[a-zA-Z0-9\-._?,!'(){}\[\]/+&@%$#=:"|~;]*       # url path and query string
    [a-zA-Z0-9\-_~:/#@$*+=]                           # allowed end chars
    ''')

# how far either side of the hovered point the plain url scan looks. a url may wrap
# over several view rows and the scan has to see all of them at once
URL_SCAN_RADIUS = 1024

# how much of a target is put in front of the user, in the popup and in the context
# menu. what is behind an OSC 8 link is whatever the shell sent and may be very long
MAX_TARGET_SHOWN = 72

URL_POPUP = """
<style>
body {
    margin: 0px;
}
div {
    border: 1px;
    border-style: solid;
    border-color: grey;
}
</style>
<body>
<div>
<a href="open">
<img width="20%" height="20%" src="res://Packages/Terminus/images/link.png" />
</a>
</div>
</body>
"""

# the text of an OSC 8 hyperlink says nothing about where it goes, it may read
# "click here" or name a completely different site, so the popup shows the target
# itself and never the text under the mouse.
#
# the host is shown on its own and first, because that is the only part of a uri
# which decides who is on the other end, and reading it out of the middle of a long
# string is exactly what a target is written to make hard, e.g.
# "https://www.paypal.com@attacker.example/login" begins with a bank and ends
# somewhere else entirely
LINK_POPUP = """
<style>
body {{
    margin: 0px;
}}
div.link {{
    border: 1px;
    border-style: solid;
    border-color: grey;
    padding: 2px;
}}
span.host {{
    padding-left: 4px;
    font-weight: bold;
}}
span.rest {{
    color: color(var(--foreground) alpha(0.7));
}}
div.warning {{
    padding: 2px 4px;
    color: var(--redish);
}}
</style>
<body>
<div class="link">
<a href="open">
<img width="20%" height="20%" src="res://Packages/Terminus/images/link.png" />
</a>
<span class="host">{host}</span><span class="rest">{rest}</span>
{warning}
</div>
</body>
"""

# characters which do not draw as themselves: a bidi override reverses the text
# after it, and a zero width or soft hyphen character simply is not there to read.
# a target carrying one of them shows one thing and opens another, so the popup
# spells each one out instead of rendering it. safe_link_uri does not refuse them,
# they are legal in a uri and only the display is at risk
INVISIBLE_IN_TARGET = re.compile(
    "[\u00ad\u061c\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")


def shorten(text, limit=MAX_TARGET_SHOWN):
    if len(text) > limit:
        return text[0:limit] + "..."
    return text


def visible(text):
    """
    a string with every character which would rewrite or hide part of the text it is
    shown in replaced by its code point, so what the user reads is what is there
    """
    return INVISIBLE_IN_TARGET.sub(lambda m: "<U+{:04X}>".format(ord(m.group())), text)


def describe_target(uri):
    """
    A target split into the parts the popup shows: (host, rest, warning).

    host is what decides where the click ends up, and it is shown by itself so it
    cannot be buried in the middle of a long string. warning is a sentence when the
    uri is one whose target is not what its first characters read like, and "" when
    there is nothing to say.
    """
    scheme = uri.partition(":")[0].lower()
    if scheme == "file":
        # a file target names no host worth showing, uri_to_path drops it. what is
        # shown is the path which would actually be opened, percent decoding included
        return "file://", shorten(uri_to_path(uri) or uri), ""

    try:
        split = urlsplit(uri)
        netloc = split.netloc
        host = split.hostname or ""
    except ValueError:
        # a malformed authority, e.g. an unbalanced "[" of an ipv6 literal. there is
        # no host to name, so the whole target is shown and flagged
        return "", shorten(uri), "this target cannot be read, do not open it"

    warning = ""
    if "@" in netloc:
        # everything in front of the "@" is userinfo and names nobody, it is only
        # there to be read as the host by whoever is looking at the link
        warning = "the part before the @ is not the host, this link opens {}".format(
            host or "somewhere else")
    if not host:
        warning = warning or "this target names no host, do not open it"

    rest = uri
    if netloc:
        rest = uri.split(netloc, 1)[-1]
    return host or scheme + ":", shorten(rest), warning


def link_popup(uri):
    """
    the popup shown for an OSC 8 hyperlink. everything in it came out of the pty, so
    every part is made visible first and escaped second, and neither step may be
    skipped: the escaping keeps a target from writing its own minihtml, and the
    visibility keeps it from rewriting what the rest of the popup reads like
    """
    host, rest, warning = describe_target(uri)
    if warning:
        warning = '<div class="warning">{}</div>'.format(html.escape(warning))
    return LINK_POPUP.format(
        host=html.escape(visible(host)),
        rest=html.escape(visible(rest)),
        warning=warning)


def joined_text(view, pt, radius=URL_SCAN_RADIUS):
    """
    The text around a point with the soft wrap markers taken out, and the buffer
    offset of every character which survived.

    A wrapped line is written as several view rows joined by CONTINUATION + "\\n", so
    a url has to be matched against the text without them, and every offset such a
    match reports then has to be turned back into a buffer offset through `offsets`.
    The two do not line up by themselves: an offset in the joined text runs behind
    the buffer by one marker for every wrap in front of it, which is what used to
    move the underline and make the hit test miss.

    `offsets` has one entry per character of the returned text, plus a sentinel, so
    that a match ending at the very end of the text still maps.
    """
    region = sublime.Region(max(0, pt - radius), min(view.size(), pt + radius))
    original = view.substr(region)
    marker = CONTINUATION + "\n"
    chars = []
    offsets = []
    i = 0
    n = len(original)
    while i < n:
        if original.startswith(marker, i):
            i += len(marker)
            continue
        chars.append(original[i])
        offsets.append(region.a + i)
        i += 1
    offsets.append(region.a + n)
    return "".join(chars), offsets


def find_url_match(view, pt):
    """
    the match of the plain url under a point, with the buffer offsets of the text it
    was matched in, or (None, None)
    """
    text, offsets = joined_text(view, pt)
    # where the point sits in the joined text. a point on a marker itself lands on
    # the character right behind it, which is where the text continues
    index = bisect.bisect_left(offsets, pt)
    for match in rex.finditer(text):
        if match.start() <= index <= match.end():
            return match, offsets
    return None, None


def find_url(view, event=None, pt=None):
    if event:
        pt = view.window_to_text((event["x"], event["y"]))
    match, _ = find_url_match(view, pt)
    if not match:
        return None
    url = match.group()
    if url[0:3] == "www":
        return "http://" + url
    return url


def find_url_region(view, event=None, pt=None):
    if event:
        pt = view.window_to_text((event["x"], event["y"]))
    match, offsets = find_url_match(view, pt)
    if not match or match.end() == match.start():
        return None
    # the last character of the match, plus one, and not the offset of the character
    # behind it: that one may sit past a marker and would drag the underline over the
    # wrap of the next row
    return (offsets[match.start()], offsets[match.end() - 1] + 1)


def find_link(view, event=None, pt=None):
    """
    The target of the OSC 8 hyperlink under a point, or None.

    The rows are looked up in the index render.py builds as it writes them, which is
    also the only thing which still knows about a row that scrolled into the view's
    history, so a link keeps working for as long as its text is on screen.

    What is returned is the uri the shell sent and never the text the user sees: the
    text of a hyperlink is anything the shell felt like, up to and including a
    different url entirely.
    """
    if event:
        pt = view.window_to_text((event["x"], event["y"]))
    link = link_at_point(view, pt)
    if link is None:
        return None
    # the parser checked this before it stored it. it is checked again on the way out
    # because no single layer should be the only thing standing between the pty and
    # the browser
    return safe_link_uri(link.uri)


def window_of(view):
    # a terminal in a panel is not hosted by the window's view list
    return view.window() or get_panel_window(view)


def path_to_file_uri(path):
    """
    the file uri of a path this side of the pty owns, "file://" in front of a windows
    path is not one
    """
    try:
        return Path(path).as_uri()
    except Exception as e:
        logger.debug("no file uri for {}: {}".format(path, e))
        return None


def local_path(view, path):
    """
    A path a link named, spelled the way this side of the pty can reach it, or None.

    A shell inside wsl names the paths of its distribution. Windows can reach those,
    under a drive letter or under the distribution's unc path, but only once it is
    known which distribution they belong to, and that is the one the terminal's
    command names and never a guessed one: the same posix path exists in every
    installed distribution and picking the wrong one would open a stranger's file. A
    path which cannot be translated opens nothing at all.
    """
    if not sys.platform.startswith("win") or not path.startswith("/"):
        return path
    cmd = terminal_command(view)
    if not is_wsl_command(cmd):
        # a posix path out of a terminal which is not running wsl, e.g. one from a
        # shell over ssh, names nothing on this machine
        logger.debug("not translating the posix path of a non wsl terminal")
        return None
    windows_path = wsl_to_windows_for_cmd(path, cmd)
    if not windows_path:
        logger.debug("no distribution to translate a link's path against")
        sublime.status_message("cannot tell which wsl distribution that path is in")
        return None
    return windows_path


def open_file_uri(view, uri):
    """
    Open the target of a file:// link. It is only ever handed to Sublime or to the
    os, it is never run and never goes near a shell, whatever it names and wherever
    it points.
    """
    path = uri_to_path(uri)
    if not path:
        logger.debug("not a usable file uri: {}".format(repr(uri)))
        return False
    path = local_path(view, path)
    if not path:
        return False
    # a link may name anything at all, including a path on the far side of an ssh
    # session, so what cannot be reached from here is not handed on to anybody
    is_file = os.path.isfile(path)
    if not is_file and not os.path.isdir(path):
        sublime.status_message("{} is not reachable from here".format(shorten(path)))
        return False
    window = window_of(view)
    if not window:
        return False
    if is_file:
        # opened by name and nothing else, a ":" in a hostile file name is part of
        # the name and must not be read as a row and column to jump to
        window.open_file(path)
    else:
        window.run_command("open_dir", {"dir": path})
    return True


def open_target(view, uri):
    """
    Open the target of a link. Everything here came out of the pty, so it may be a
    hostile file name, a log line or a git branch name, and the scheme decides what
    happens to it: an unknown one is refused outright and never repaired.
    """
    checked = safe_link_uri(uri)
    if not checked:
        logger.debug("refusing to open {}".format(repr(uri)[0:128]))
        sublime.status_message("refusing to open that link")
        return False
    scheme = checked.split(":", 1)[0].lower()
    if scheme in ("http", "https"):
        webbrowser.open_new_tab(checked)
        return True
    if scheme == "file":
        return open_file_uri(view, checked)
    # safe_link_uri only ever returns one of the schemes above, this catches one
    # being added there without being given a handler here
    logger.debug("no handler for the scheme of {}".format(repr(checked)[0:128]))
    return False


class TerminusMouseEventListener(sublime_plugin.EventListener):

    def on_text_command(self, view, command_name, args):
        terminal = Terminal.from_id(view.id())
        if not terminal:
            return
        if command_name == "drag_select":
            if len(args) == 1 and args["event"]["button"] == 1:  # simple click
                return ("terminus_click", args)

    def on_hover(self, view, point, hover_zone):
        terminal = Terminal.from_id(view.id())
        if not terminal:
            return
        if hover_zone != sublime.HOVER_TEXT:
            return

        # a link is opened by hovering it and clicking the popup, the way a plain url
        # already was. a plain click is left alone on purpose: it is what selects
        # text, and terminus_click below hands it straight to drag_select
        #
        # an OSC 8 hyperlink wins over the text under it, the text of one may itself
        # look like a url while pointing somewhere else entirely
        target = find_link(view, pt=point)
        if target:
            # the renderer underlines the runs of a hyperlink as it writes them, so
            # there is nothing to highlight here, only the target to show
            regions = []
            popup = link_popup(target)
        else:
            target = find_url(view, pt=point)
            if not target:
                return
            url_region = find_url_region(view, pt=point)
            regions = [sublime.Region(*url_region)] if url_region else []
            popup = URL_POPUP

        def on_navigate(action):
            if action == "open":
                open_target(view, target)

        def on_hide():
            if link_key:
                view.erase_regions(link_key)

        link_key = None
        if regions:
            link_key = get_highlight_key(view)
            view.add_regions(
                link_key,
                regions,
                "meta",
                flags=sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE | sublime.DRAW_SOLID_UNDERLINE)

        view.show_popup(
            popup,
            sublime.HIDE_ON_MOUSE_MOVE_AWAY,
            location=point,
            on_navigate=on_navigate, on_hide=on_hide)


class TerminusOpenContextUrlCommand(sublime_plugin.TextCommand):
    """
    Open whatever the click landed on: the target of an OSC 8 hyperlink first, and a
    plain url in the text otherwise.
    """

    def target(self, event):
        return find_link(self.view, event) or find_url(self.view, event)

    def run(self, edit, event):
        target = self.target(event)
        if not target:
            return
        open_target(self.view, target)

    def is_enabled(self, *args, **kwargs):
        terminal = Terminal.from_id(self.view.id())
        return terminal is not None

    def is_visible(self, event):
        terminal = Terminal.from_id(self.view.id())
        return terminal is not None and self.target(event) is not None

    def description(self, event):
        target = self.target(event)
        if not target:
            # is_visible said there is nothing there, the caption is asked for anyway
            # by whoever built the menu
            return "Open Link"
        # a caption is plain text and cannot be escaped, but it can still be reordered
        # by a bidi override in the target, so the target is made visible here too
        return "Open " + visible(shorten(target))

    def want_event(self):
        return True


class TerminusClickCommand(sublime_plugin.TextCommand):
    """Reset cursor position if the click is occured below the last row."""

    def run_(self, edit, args):
        view = self.view
        window = view.window()
        if not window:
            return

        event = args["event"]
        pt = view.window_to_text((event["x"], event["y"]))
        if pt == view.size():
            if view.text_to_window(view.size())[1] + view.line_height() < event["y"]:
                logger.debug("reset cursor")
                window.focus_group(window.active_group())
                window.focus_view(view)
                view.run_command("terminus_show_cursor", {"scroll": False})
                return

        if any(s.contains(pt) for s in view.sel()):
            # disable dragging
            view.sel().clear()

        view.run_command("drag_select", args)


class TerminusOpenImageCommand(sublime_plugin.TextCommand):
    def want_event(self):
        return True

    def is_enabled(self, *args, **kwargs):
        terminal = Terminal.from_id(self.view.id())
        return terminal is not None

    def is_visible(self, event):
        terminal = Terminal.from_id(self.view.id())
        return terminal is not None and self.find_phantom(event) is not None

    def find_phantom(self, event):
        view = self.view
        terminal = Terminal.from_id(view.id())
        if not terminal:
            return None
        pt = view.window_to_text((event["x"], event["y"]))
        cord = view.text_to_window(pt)
        if cord[1] >= event["y"]:
            # the right click happens at the lower half of the images
            row = view.rowcol(pt)[0]
            cord = view.text_to_window(view.text_point(row - 1, 0))
            pt = view.window_to_text((event["x"], cord[1]))
        # a snapshot, the render thread drops an image whose row is gone while this
        # iterates and a dict which changes size during iteration raises
        for pid in list(terminal.images.keys()):
            regions = view.query_phantom(pid)
            if not regions:
                # the phantom is gone, the image it belongs to is on its way out
                continue
            if regions[0].end() == pt:
                return pid
        return None

    def run(self, edit, event):
        view = self.view
        terminal = Terminal.from_id(view.id())
        if not terminal:
            return
        pid = self.find_phantom(event)
        if pid is None:
            return
        # the phantom may have been dropped between the menu being built and this
        # running, and the command may be run without a menu having asked at all
        image_path = terminal.images.get(pid)
        if not image_path:
            return
        uri = path_to_file_uri(image_path)
        if not uri:
            return
        webbrowser.open_new_tab(uri)

    def description(self, event):
        return "Open Image"
