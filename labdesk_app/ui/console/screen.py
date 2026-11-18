"""Terminal model of the LabDesk console: a pyte screen with its own scrollback, resize, wrap marker and
alternate screen.  Pure Python (pyte + wcwidth, no Qt).

Lines are handed to the view as compact tuples ``(text, runs, wrapped)``:

* ``text``     one character per column, trailing blanks stripped; the second cell of a wide character
               holds ``WIDE_STUB``
* ``runs``     ``(start, length, fg, bg, flags)`` for every stretch of cells whose attributes are not the
               default (fg / bg are pyte colour names or 'rrggbb')
* ``wrapped``  the line continues on the next one (autowrap), so a copy joins them without a newline

Scrollback never stores pyte cell rows, only these tuples.  Every line has an absolute id that survives
eviction: ``first_id`` grows when the scrollback drops lines from its front.
"""
from __future__ import annotations

from collections import defaultdict, deque

import pyte
from pyte import modes as mo
from pyte.screens import Char, Margins, StaticDefaultDict
from wcwidth import wcwidth

WIDE_STUB = "\x00"
BOLD, UNDERLINE, REVERSE, ITALIC, STRIKE, FAINT = 1, 2, 4, 8, 16, 32
DEFAULT_SCROLLBACK = 50_000
MIN_COLUMNS = 80
EMPTY = ("", (), False)

_ALT_MODES = (47, 1047, 1049)
_DECCOLM = 3
_DECCKM = 1 << 5             # private mode 1, shifted the way pyte stores private modes


class Row(StaticDefaultDict):
    """One screen row: pyte's sparse {column: Char} plus the wrap marker and the cached compact form."""
    __slots__ = ("wrapped", "cache")

    def __init__(self, default):
        super().__init__(default)
        self.wrapped = False
        self.cache = None


def _flags(c: Char) -> int:
    return ((BOLD if c.bold else 0) | (UNDERLINE if c.underscore else 0) | (REVERSE if c.reverse else 0)
            | (ITALIC if c.italics else 0) | (STRIKE if c.strikethrough else 0) | (FAINT if c.blink else 0))


def compact(row) -> tuple:
    """(text, runs, wrapped) of a pyte row."""
    if not row:
        return ("", (), bool(getattr(row, "wrapped", False))) if row is not None else EMPTY
    width = max(row) + 1
    chars = [" "] * width
    runs: list = []
    end = 0                                   # one past the last cell that is not a default blank
    cur = None                                # the open run: [start, length, fg, bg, flags]
    for x in sorted(row):
        c = row[x]
        data = c.data
        chars[x] = data[0] if data else WIDE_STUB
        fl = _flags(c)
        if c.fg == "default" and c.bg == "default" and not fl:
            cur = None
            if data != " ":
                end = x + 1
            continue
        if cur is not None and cur[0] + cur[1] == x and cur[2] == c.fg and cur[3] == c.bg and cur[4] == fl:
            cur[1] += 1
        else:
            cur = [x, 1, c.fg, c.bg, fl]
            runs.append(cur)
        # a styled blank only shows when it has a background, is reversed or underlined
        if data != " " or c.bg != "default" or fl & (REVERSE | UNDERLINE | STRIKE):
            end = x + 1
    out_runs = []
    for s, n, fg, bg, fl in runs:
        if s >= end:
            continue
        out_runs.append((s, min(n, end - s), fg, bg, fl))
    return ("".join(chars[:end]), tuple(out_runs), bool(getattr(row, "wrapped", False)))


def plain_text(line: tuple) -> str:
    """The characters of a compact line as the device sent them (no wide-character stubs)."""
    return line[0].replace(WIDE_STUB, "")


class TermScreen(pyte.Screen):
    """pyte.Screen + scrollback, wrap marker, alternate screen, faint (SGR 2) and a resize that keeps text."""

    def __init__(self, columns: int = 110, lines: int = 42, scrollback: int = DEFAULT_SCROLLBACK):
        self.history: deque = deque()
        self.max_history = max(0, int(scrollback))
        self.first_id = 0                    # absolute id of history[0] (or of screen row 0 when empty)
        self.alt = False
        self._main = None                    # (buffer, cursor state) while the alternate screen is shown
        self.bells = 0
        self.scrolls = 0                     # how many times the screen scrolled (the view repaints on it)
        self.on_reply = None                 # callable(str): DSR / DA answers for the child
        super().__init__(columns, lines)
        self.buffer = self._new_buffer()

    # ------------------------------------------------------------------ buffers / lines
    def _new_buffer(self):
        return defaultdict(lambda: Row(self.default_char))

    @property
    def total_lines(self) -> int:
        """History lines + screen rows."""
        return len(self.history) + self.lines

    def line(self, line_id: int) -> tuple:
        """The compact line with absolute id ``line_id`` (history first, then the screen rows)."""
        i = line_id - self.first_id
        if i < 0:
            return EMPTY
        n = len(self.history)
        if i < n:
            return self.history[i]
        return self.screen_line(i - n)

    def screen_line(self, y: int) -> tuple:
        row = self.buffer.get(y)
        if row is None:
            return EMPTY
        if y in self.dirty or row.cache is None:
            row.cache = compact(row)
        return row.cache

    def sync(self) -> None:
        """Refresh the compact caches of rows pyte marked dirty (call before reading lines for a paint)."""
        if self.dirty:
            buf = self.buffer
            for y in self.dirty:
                row = buf.get(y)
                if row is not None:
                    row.cache = None
            self.dirty.clear()

    def cursor_line_id(self) -> int:
        return self.first_id + len(self.history) + self.cursor.y

    def cursor_row_text(self) -> str:
        return compact(self.buffer.get(self.cursor.y))[0]

    def _push_history(self, row) -> None:
        if self.max_history <= 0:
            return
        self.history.append(compact(row) if row is not None else EMPTY)
        if len(self.history) > self.max_history:
            self.history.popleft()
            self.first_id += 1

    # ------------------------------------------------------------------ pyte overrides
    def write_process_input(self, data: str) -> None:
        if self.on_reply is not None:
            self.on_reply(data)

    def bell(self, *args) -> None:
        self.bells += 1

    def draw(self, data: str) -> None:
        """pyte's draw with the wrap marker (and without a width lookup for plain ASCII)."""
        data = data.translate(self.g1_charset if self.charset else self.g0_charset)
        cursor = self.cursor
        attrs = cursor.attrs
        columns = self.columns
        buffer = self.buffer
        irm = mo.IRM in self.mode
        for char in data:
            width = 1 if " " <= char <= "~" else wcwidth(char)
            if cursor.x == columns:
                if mo.DECAWM in self.mode:
                    buffer[cursor.y].wrapped = True
                    self.dirty.add(cursor.y)
                    self.carriage_return()
                    self.linefeed()
                elif width > 0:
                    cursor.x -= width
            if irm and width > 0:
                self.insert_characters(width)
            line = buffer[cursor.y]
            if width == 1:
                line[cursor.x] = attrs._replace(data=char)
            elif width == 2:
                line[cursor.x] = attrs._replace(data=char)
                if cursor.x + 1 < columns:
                    line[cursor.x + 1] = attrs._replace(data="")
            elif width == 0 and _combining(char):
                import unicodedata
                if cursor.x:
                    last = line[cursor.x - 1]
                    line[cursor.x - 1] = last._replace(data=unicodedata.normalize("NFC", last.data + char))
                elif cursor.y:
                    last = buffer[cursor.y - 1][columns - 1]
                    buffer[cursor.y - 1][columns - 1] = last._replace(
                        data=unicodedata.normalize("NFC", last.data + char))
            else:
                break
            if width > 0:
                cursor.x = min(cursor.x + width, columns)
        self.dirty.add(cursor.y)

    def index(self) -> None:
        """Scroll: the line leaving the top goes to the scrollback (not on the alternate screen, not
        inside a scrolling region that starts below the top)."""
        top, bottom = self.margins or Margins(0, self.lines - 1)
        if self.cursor.y != bottom:
            self.cursor_down()
            return
        buf = self.buffer
        self.sync()                 # rows move by reference and keep their compact cache: nothing gets dirty
        if top == 0 and not self.alt:
            self._push_history(buf.get(0))
        self.scrolls += 1
        for y in range(top, bottom):
            buf[y] = buf[y + 1]
        buf.pop(bottom, None)

    def erase_in_line(self, how: int = 0, private: bool = False) -> None:
        row = self.buffer[self.cursor.y]
        if how in (0, 2):
            row.wrapped = False
        attrs = self.cursor.attrs
        if attrs.bg == "default" and not attrs.reverse and how in (0, 2):
            # blank with default attributes = no cell at all (keeps rows sparse)
            self.dirty.add(self.cursor.y)
            start = self.cursor.x if how == 0 else 0
            for x in [x for x in row if x >= start]:
                del row[x]
            return
        super().erase_in_line(how, private)

    def select_graphic_rendition(self, *attrs: int, **kwargs) -> None:
        """pyte's SGR plus faint (2 on, 22 off).  Blink is ignored; pyte's unused ``blink`` cell field
        carries the faint flag.  A private 'CSI ? ... m' is not an SGR and is ignored."""
        if kwargs.get("private"):
            return
        if 2 not in attrs and 5 not in attrs and 22 not in attrs and 25 not in attrs:
            super().select_graphic_rendition(*attrs)
            return
        out: list[int] = []
        faint = None
        it = iter(attrs)
        for a in it:
            if a in (38, 48):                              # colour arguments are not attributes
                out.append(a)
                n = next(it, None)
                if n is None:
                    break
                out.append(n)
                for _ in range(1 if n == 5 else 3 if n == 2 else 0):
                    v = next(it, None)
                    if v is None:
                        break
                    out.append(v)
            elif a == 2:
                faint = True
            elif a == 22:
                faint = False
                out.append(a)
            elif a == 0:
                faint = False
                out.append(a)
            elif a in (5, 25):
                continue
            else:
                out.append(a)
        if out:
            super().select_graphic_rendition(*out)
        if faint is not None:
            self.cursor.attrs = self.cursor.attrs._replace(blink=faint)

    # ------------------------------------------------------------------ modes
    @property
    def app_cursor_keys(self) -> bool:
        """DECCKM: arrows send ESC O x."""
        return _DECCKM in self.mode

    @property
    def cursor_visible(self) -> bool:
        """DECTCEM."""
        return not self.cursor.hidden

    def set_mode(self, *modes: int, **kwargs) -> None:
        if kwargs.get("private"):
            for m in modes:
                if m in _ALT_MODES:
                    self._enter_alt(m)
            modes = tuple(m for m in modes if m not in _ALT_MODES and m != _DECCOLM)
            if not modes:
                return
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes: int, **kwargs) -> None:
        if kwargs.get("private"):
            for m in modes:
                if m in _ALT_MODES:
                    self._leave_alt(m)
            modes = tuple(m for m in modes if m not in _ALT_MODES and m != _DECCOLM)
            if not modes:
                return
        super().reset_mode(*modes, **kwargs)

    def _enter_alt(self, mode: int) -> None:
        if self.alt:
            return
        self.sync()
        c = self.cursor
        self._main = (self.buffer, (c.x, c.y, c.attrs, c.hidden), self.margins)
        self.buffer = self._new_buffer()
        self.alt = True
        self.margins = None
        self.dirty.update(range(self.lines))

    def _leave_alt(self, mode: int) -> None:
        if not self.alt:
            return
        self.sync()
        buf, (x, y, attrs, hidden), margins = self._main
        self._main = None
        self.buffer = buf
        self.alt = False
        self.margins = None
        if mode == 1049:                                   # 1049 also restores the cursor
            self.cursor.x, self.cursor.y, self.cursor.attrs = x, y, attrs
        self.cursor.hidden = False
        self.ensure_hbounds()
        self.cursor.y = max(0, min(self.cursor.y, self.lines - 1))
        self.dirty.update(range(self.lines))

    # ------------------------------------------------------------------ resize
    def resize(self, lines: int | None = None, columns: int | None = None) -> None:
        """Rows follow the view; columns only grow.  Before shrinking, the rows above the cursor that no
        longer fit go to the scrollback (pyte would delete them)."""
        lines = max(2, int(lines or self.lines))
        columns = max(self.columns, int(columns or self.columns))
        if lines == self.lines and columns == self.columns:
            return
        for buf, is_main in self._buffers():
            self._resize_buffer(buf, lines, is_main)
        if lines < self.lines:
            drop = max(0, self.cursor.y - (lines - 1))
            self.cursor.y -= drop
        self.lines, self.columns = lines, columns
        self.margins = None
        self.tabstops = set(range(8, columns, 8))
        self.cursor.y = max(0, min(self.cursor.y, lines - 1))
        self.dirty.update(range(lines))

    def _buffers(self):
        if self.alt and self._main is not None:
            return [(self.buffer, False), (self._main[0], True)]
        return [(self.buffer, True)]

    def _resize_buffer(self, buf, lines: int, is_main: bool) -> None:
        if lines >= self.lines:
            return
        if buf is self.buffer:
            cy = self.cursor.y
        else:                                              # the main screen behind the alternate one
            cy = self._main[1][1]
        drop = max(0, cy - (lines - 1))
        if drop:
            for y in range(drop):
                if is_main:
                    self._push_history(buf.get(y))
                buf.pop(y, None)
            for y in sorted(k for k in buf if k >= drop):
                buf[y - drop] = buf.pop(y)
            if buf is not self.buffer:
                b, (x, y, attrs, hidden), margins = self._main
                self._main = (b, (x, y - drop, attrs, hidden), None)
        for y in [k for k in buf if k >= lines]:
            buf.pop(y, None)

    # ------------------------------------------------------------------ text for copy
    def text_between(self, a: tuple, b: tuple) -> str:
        """Text from (line id, column) ``a`` to ``b`` (inclusive start, exclusive end), exactly as the
        device sent it: wrapped lines are joined, trailing blanks dropped, no highlighting artefacts."""
        if a > b:
            a, b = b, a
        (la, ca), (lb, cb) = a, b
        out: list[str] = []
        for lid in range(la, lb + 1):
            text, _runs, wrapped = self.line(lid)
            start = ca if lid == la else 0
            end = cb if lid == lb else None
            part = text[start:end].replace(WIDE_STUB, "")
            out.append(part)
            if lid != lb and not (wrapped and (end is None or end >= len(text))):
                out.append("\n")
        return "".join(out)


def _combining(char: str) -> bool:
    import unicodedata
    return bool(unicodedata.combining(char))


class Terminal:
    """A TermScreen plus the byte stream that feeds it."""

    def __init__(self, columns: int = 110, lines: int = 42, scrollback: int = DEFAULT_SCROLLBACK):
        self.screen = TermScreen(columns, lines, scrollback)
        self.stream = pyte.ByteStream(self.screen)

    def feed(self, data: bytes) -> None:
        try:
            self.stream.feed(data)
        except Exception:  # noqa: BLE001  (a sequence pyte cannot parse must never end the session)
            self.stream = pyte.ByteStream(self.screen)
