"""Read the cells of Fakturama's SWT NatTable canvases."""

from __future__ import annotations

import base64
import io
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from ctypes import windll
from _ctypes import COMError
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import win32gui
from PIL import Image, ImageGrab, ImageOps

from .control_locator import _text, control_right_of, visible_descendants, write_edit
from .fakturama_client import wait_until


def norm(text):
    return " ".join(str(text or "").casefold().split())


def identity(text):
    return norm(text).replace(" -", "-").replace("- ", "-").replace(" %", "%")


def amount(value):
    if isinstance(value, Decimal):
        return value
    text = "".join(c for c in str(value) if c in "0123456789-.,")
    if "," in text and "." in text:
        decimal, grouping = (",", ".") if text.rfind(",") > text.rfind(".") else (".", ",")
        text = text.replace(grouping, "").replace(decimal, ".")
    elif "," in text:
        text = text.replace(",", ".")
    return Decimal(text)


def money(value):
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def controls(root, kind, name=None):
    return [
        c for c in visible_descendants(root)
        if c.element_info.control_type == kind and (name is None or norm(_text(c)) == norm(name))
    ]


def one(values, label):
    if len(values) != 1:
        raise RuntimeError(f"Expected one {label}, found {len(values)}")
    return values[0]


def search(root, query):
    labels = controls(root, "Text", "Search:")
    label = one(labels, "Search label")
    edit = control_right_of(label, visible_descendants(root), {"Edit"}, max_dx=280)
    if edit is None:
        raise RuntimeError("Search input unavailable")
    expected = norm(query)

    def accepted():
        return norm(edit.get_value()) == expected

    if not accepted():
        write_edit(edit, query)
        # The SWT filter can already show matching rows while the UIA Value
        # provider still reports the previous text or an empty string.
        if not wait_until(accepted, timeout=1, interval=0.15):
            literal = "".join(f"{{{char}}}" if char in "+^%{}()" else char for char in query)
            edit.click_input()
            edit.type_keys("^a")
            edit.type_keys(literal or "{BACKSPACE}", with_spaces=True)
            if not wait_until(accepted, timeout=2, interval=0.2):
                raise RuntimeError(
                    f"Fakturama did not confirm search text {query!r}. Read {edit.get_value()!r}."
                )
    return edit


def grid_below(root, anchor):
    """Locate the table canvas under a search edit or beside an Items label."""
    a = anchor.rectangle()
    search_field = anchor.element_info.control_type == "Edit"
    min_top = a.bottom - 8 if search_field else a.top - 8

    def aligned(rect):
        if search_field:
            return rect.left < a.right and rect.right > a.left
        return rect.left >= a.right

    panes = []
    for pane in controls(root, "Pane"):
        r = pane.rectangle()
        if r.top >= min_top and r.left >= a.left - 1600 and r.bottom > a.bottom + 80:
            if r.width() >= 170 and r.top <= a.bottom + 45 and aligned(r):
                panes.append(pane)
    bars = controls(root, "ScrollBar", "Horizontal")
    for bar in bars:
        parent = bar.parent()
        # Documents also has a horizontal scrollbar on its category tree.
        if parent.element_info.control_type == "Tree":
            continue
        r = parent.rectangle()
        if aligned(r) and a.top - 8 <= r.top <= a.bottom + 45 and r.bottom > a.bottom + 80 and r.width() >= 170:
            return parent
    if panes:
        # Without a scrollbar, both the SWT canvas and its border pane can
        # match. Read the inner canvas for the Items grid.
        choose = max if search_field else min
        return choose(panes, key=lambda p: p.rectangle().width() * p.rectangle().height())
    raise GridLayoutError("Fakturama grid canvas not found")


def _words(image):
    data = io.BytesIO()
    image.save(data, format="PNG")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-File",
         str(Path(__file__).with_name("ocr_grid.ps1"))],
        input=base64.b64encode(data.getvalue()).decode("ascii"),
        capture_output=True, text=True, encoding="utf-8", timeout=18,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError(f"Windows OCR could not read the table: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _rulings(image, *, vertical=False, sparse=False):
    if vertical:
        image = image.transpose(Image.Transpose.TRANSPOSE)
    w, h = image.size
    start, end = max(2, w // 12), w - max(2, w // 12)
    pixels = image.load()
    lines = []
    for y in range(1, h - 1):
        segment = [pixels[x, y] for x in range(start, end)]
        color = segment[len(segment) // 2]
        if not (35 <= color <= 238 if sparse else 100 <= color <= 224):
            continue
        count = sum(
            abs(pixels[x, y] - color) <= 5
            and max(abs(pixels[x, y - 1] - color), abs(pixels[x, y + 1] - color)) > (10 if sparse else 13)
            for x in range(start, end)
        )
        if count >= (end - start) * 0.69 and (not lines or y - lines[-1] > 2):
            lines.append(y)
    return lines


class GridLayoutError(RuntimeError):
    pass


def _sparse_grid_layout(gray):
    """The address picker draws rules only around its header and actual rows."""
    width, height = gray.size
    rules = _rulings(gray, sparse=True)
    for i, header_bottom in enumerate(rules):
        if not 10 <= header_bottom <= 55:
            continue
        header = gray.crop((0, 0, width, header_bottom))
        # Thin lines at the canvas edges are borders, not data columns.
        columns = [x for x in _rulings(header, vertical=True, sparse=True) if 8 <= x <= width - 8]
        if len(columns) < 3:
            continue

        rows = rules[i:]
        gaps = [b - a for a, b in zip(rows, rows[1:])]
        if gaps and (min(gaps) < 11 or max(gaps) > 52 or max(gaps) - min(gaps) > 3):
            continue

        # A header alone is evidence of an empty result only when the area
        # underneath is blank. Unruled text could be an unrecognized row.
        tail = gray.crop((3, rows[-1] + 2, width - 3, height - 2))
        if tail.height < 8 or tail.getextrema()[0] < 230:
            continue
        return rows, [0, *columns, width]
    raise GridLayoutError("Could not verify the address grid header and rows")


def _order_row_layout(gray):
    """Recover row boundaries from the repeated rules below highlighted rows."""
    rules = _rulings(gray)
    gaps = [b - a for a, b in zip(rules, rules[1:])]
    pitches = [gap for gap in gaps if 11 <= gap <= 52]
    if not pitches:
        raise GridLayoutError("Could not measure the Order grid rows")
    nominal = max(pitches, key=lambda gap: sum(abs(gap - other) <= 1 for other in pitches))
    runs = []
    run = rules[:1]
    for gap, boundary in zip(gaps, rules[1:]):
        if abs(gap - nominal) > 1:
            runs.append(run)
            run = []
        run.append(boundary)
    runs.append(run)
    regular = max(runs, key=len)
    if len(regular) < 4:
        raise GridLayoutError("Could not verify repeated Order grid row boundaries")

    pitch = (regular[-1] - regular[0]) / (len(regular) - 1)
    # The canvas starts with one header row. Project back from the regular
    # run across highlighted rows whose separators are temporarily obscured.
    header = regular[0] - round((regular[0] - pitch) / pitch) * pitch
    if abs(header - pitch) > 0.35 * pitch:
        raise GridLayoutError("Could not locate the Order grid header")
    rows = [round(header + i * pitch) for i in range(round((regular[-1] - header) / pitch) + 1)]
    return rows, regular[0]


def _order_grid_layout(gray, bottom):
    body = gray.crop((0, 0, gray.width, bottom))
    rows, band_start = _order_row_layout(body)
    band = body.crop((0, band_start + 1, body.width, rows[-1]))
    cols = [0, *_rulings(band, vertical=True), body.width]
    if len(cols) < 4:
        raise GridLayoutError("Could not locate the Order grid columns")
    return rows, cols


def _scaled_canvas_grid(canvas, handle, rect, bottom):
    # HWND capture contains only the client area, in native bitmap pixels.
    # UIA reports the full control, including its border, in screen coordinates.
    left, top, right, lower = win32gui.GetClientRect(handle)
    x, y = win32gui.ClientToScreen(handle, (left, top))
    end_x, end_y = win32gui.ClientToScreen(handle, (right, lower))
    width, height = end_x - x, end_y - y
    if not (rect.left <= x < end_x <= rect.right and rect.top <= y < end_y <= rect.bottom):
        raise GridLayoutError("Canvas client area is outside the measured UIA bounds")
    scale_x = canvas.width / width
    scale_y = canvas.height / height
    if min(scale_x, scale_y) <= 0 or abs(scale_x - scale_y) * min(width, height) > 3:
        raise GridLayoutError(f"Canvas {canvas.size} does not match client proportions {(width, height)}")

    offset_x, offset_y = x - rect.left, y - rect.top
    native_bottom = min(canvas.height, round((bottom - offset_y) * scale_y))
    if native_bottom <= 0:
        raise GridLayoutError("Scrollbar leaves no visible canvas rows")
    rows, cols = _order_grid_layout(canvas.convert("L"), native_bottom)
    rows = [offset_y + round(value / scale_y) for value in rows]
    cols = [offset_x + round(value / scale_x) for value in cols]
    image = Image.new("RGB", (rect.right - rect.left, rect.bottom - rect.top), "white")
    image.paste(canvas.resize((width, height), Image.Resampling.LANCZOS), (offset_x, offset_y))
    return image, rows, cols


class Grid:
    def __init__(self, pane, *, sparse=False, order_grid=False):
        self.pane = pane
        self.rect = pane.rectangle()
        r = self.rect
        self.image = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True)
        gray = self.image.convert("L")
        self.columns = {}
        if order_grid:
            vertical = [bar for bar in controls(pane, "ScrollBar", "Vertical") if bar.is_enabled()]
            self.starts_at_top = not vertical
            if vertical:
                try:
                    self.starts_at_top = all(
                        bar.iface_range_value.CurrentValue == bar.iface_range_value.CurrentMinimum
                        for bar in vertical
                    )
                except (AttributeError, COMError):
                    pass
            bars = controls(pane, "ScrollBar", "Horizontal")
            bottom = gray.height
            if bars:
                bar_top = min(bar.rectangle().top for bar in bars) - r.top
                if 0 < bar_top <= gray.height:
                    bottom = bar_top
            try:
                self.rows, self.cols = _order_grid_layout(gray, bottom)
            except GridLayoutError as desktop_error:
                # Read the SWT canvas itself if its desktop crop was obscured.
                # Keep the same pixel geometry for cell reads and later clicks.
                if not pane.handle:
                    raise
                try:
                    canvas = ImageGrab.grab(window=pane.handle)
                except (OSError, TypeError):
                    # Older Pillow versions lack HWND capture.
                    raise desktop_error
                if canvas.size == self.image.size:
                    self.rows, self.cols = _order_grid_layout(canvas.convert("L"), bottom)
                    self.image = canvas
                else:
                    self.image, self.rows, self.cols = _scaled_canvas_grid(canvas, pane.handle, r, bottom)
            return
        if sparse:
            bars = controls(pane, "ScrollBar", "Horizontal")
            if len(bars) != 1:
                raise GridLayoutError("Could not locate the address grid's horizontal scrollbar")
            bottom = bars[0].rectangle().top - r.top
            if not 0 < bottom <= gray.height:
                raise GridLayoutError("The address grid body is outside its canvas")
            self.rows, self.cols = _sparse_grid_layout(gray.crop((0, 0, gray.width, bottom)))
            return
        self.rows = _rulings(gray)
        # Detect column rules below the header, where they span the blank rows.
        width, height = gray.size
        self.cols = [0, *_rulings(gray.crop((0, 0, width, self.rows[-1] if self.rows else height)),
                                  vertical=True), width]
        if len(self.rows) < 3 or len(self.cols) < 3:
            raise GridLayoutError("Could not verify the Fakturama grid layout")
        gaps = [b - a for a, b in zip(self.rows, self.rows[1:])]
        pitch = max(set(gaps), key=gaps.count)
        self.rows = [y for y in self.rows if (y - self.rows[0]) % pitch in (0, 1, pitch - 1)]
        if len(self.rows) < 3 or not 11 <= pitch <= 52:
            raise GridLayoutError("Could not verify Fakturama grid row height")

    def bounds(self, row, col):
        return self.cols[col], self.rows[row], self.cols[col + 1], self.rows[row + 1]

    def read(self, row, col):
        if col not in self.columns:
            self.columns[col] = self._read_column(col)
        tokens = self.columns[col].get(row, [])
        return " ".join(w["text"] for w in sorted(tokens, key=lambda w: w["x"]))

    def read_cell(self, row, col):
        """Read one cell at its original aspect ratio, with extra OCR padding."""
        left, top, right, bottom = self.bounds(row, col)
        cell = self.image.crop((left + 1, top + 2, right - 2, bottom - 2)).convert("L")
        enlarged = cell.resize((cell.width * 4, cell.height * 4))
        words = _words(ImageOps.expand(enlarged, border=16, fill=255))
        return " ".join(word["text"] for word in sorted(words, key=lambda word: word["x"]))

    def _read_column(self, col):
        # Remove grid rules and selected-row backgrounds before OCR. Keep each
        # cell in its own vertical slot so the words map back to the source row.
        slot = 74
        tokens = {}
        occupied = [r for r in range(len(self.rows) - 1) if self.ink(r, col)]
        for start in range(0, len(occupied), 48):
            group = occupied[start:start + 48]
            width = (self.cols[col + 1] - self.cols[col]) * 3 + 32
            sheet = Image.new("L", (width, len(group) * slot), 255)
            for i, row in enumerate(group):
                l, t, r, b = self.bounds(row, col)
                cell = self.image.crop((l + 1, t + 1, r, b)).convert("L")
                histogram = cell.histogram()
                if histogram.index(max(histogram)) < 150:
                    cell = ImageOps.invert(cell)
                cell = ImageOps.autocontrast(cell)
                w, h = cell.size
                height = min(h * 3, slot - 16)
                cell = cell.resize((w * 3, height))
                sheet.paste(cell, (15, i * slot + (slot - height) // 2))
            for word in _words(sheet):
                group_index = int((word["y"] + word["h"] / 2) // slot)
                if group_index < len(group):
                    tokens.setdefault(group[group_index], []).append(word)
        return tokens

    def ink(self, row, col):
        l, t, r, b = self.bounds(row, col)
        if r - l < 8 or b - t < 8:
            return False
        lo, hi = self.image.crop((l + 3, t + 3, r - 3, b - 3)).convert("L").getextrema()
        return hi - lo > 55

    def click(self, row, col, *, double=False, right=False):
        l, t, r, b = self.bounds(row, col)
        x = r - 7 if right else (l + r) // 2
        click = self.pane.double_click_input if double else self.pane.click_input
        click(coords=(x, (t + b) // 2))

    def copy_cell(self, row, col):
        """Try NatTable's selection copy when OCR cannot distinguish glyphs."""
        import pythoncom
        import win32clipboard as clipboard
        from pywintypes import com_error

        # UI Automation can initialize the main thread as MTA. OLE clipboard
        # access needs an STA, and the saved IDataObject must stay on that thread.
        previous = []

        def remember():
            pythoncom.OleInitialize()
            try:
                previous.append(pythoncom.OleGetClipboard())
            except BaseException:
                windll.ole32.OleUninitialize()
                raise

        def restore(copied_seq):
            try:
                data = previous.pop()
                if copied_seq is not None and clipboard.GetClipboardSequenceNumber() == copied_seq:
                    pythoncom.OleSetClipboard(data)
                    if data is not None:
                        pythoncom.OleFlushClipboard()
                del data
            finally:
                windll.ole32.OleUninitialize()

        with ThreadPoolExecutor(max_workers=1) as sta:
            try:
                sta.submit(remember).result()
            except com_error:
                return None
            copied_seq = None
            try:
                raw, copied_seq = self._copy_selected_text(row, col, clipboard)
            finally:
                try:
                    sta.submit(restore, copied_seq).result()
                except com_error as exc:
                    print(f"[UIA] Could not restore the clipboard after checking a grid cell: {exc}")

        if raw is None or "\n" in raw or "\r" in raw:
            return None
        cells = raw.split("\t")
        if len(cells) == 1:
            return cells[0]
        if len(cells) >= len(self.cols) - 2 and col < len(cells):
            return cells[col]
        return None

    def _copy_selected_text(self, row, col, clipboard):
        from pywintypes import error

        seq = clipboard.GetClipboardSequenceNumber()
        self.click(row, col)
        self.pane.type_keys("^c", set_foreground=False)
        if not wait_until(lambda: clipboard.GetClipboardSequenceNumber() != seq, timeout=0.8, interval=0.08):
            return None, None
        copied_seq = clipboard.GetClipboardSequenceNumber()
        try:
            clipboard.OpenClipboard()
            try:
                if clipboard.IsClipboardFormatAvailable(clipboard.CF_UNICODETEXT):
                    return clipboard.GetClipboardData(clipboard.CF_UNICODETEXT).rstrip("\r\n"), copied_seq
            finally:
                clipboard.CloseClipboard()
        except error:
            pass
        return None, copied_seq

    def matches(self, row, col, wanted):
        value = self.read(row, col)
        if identity(value) == identity(wanted):
            return True
        copied = self.copy_cell(row, col)
        return copied is not None and identity(copied) == identity(wanted)


def search_grid(root, query, *, sparse=False, refresh_root=None, order_grid=False):
    pane = None
    previous = None
    layout_error = None

    def stable():
        nonlocal pane, previous, layout_error
        try:
            if pane is None:
                scope = refresh_root() if refresh_root is not None else root
                if scope is None:
                    raise RuntimeError(f"The search view closed while filtering for {query!r}")
                edit = search(scope, query)
                pane = grid_below(scope, edit)
            current = Grid(pane, order_grid=True) if order_grid else Grid(pane, sparse=sparse)
            frame = current.image.tobytes()
            if frame == previous and any(c.is_enabled() for c in controls(pane, "ScrollBar", "Vertical")):
                raise RuntimeError("Filtered results span multiple screens. Review for conflicts.")
        except COMError as exc:
            if exc.hresult not in (-2147220991, -2147417848):
                raise
            # SWT can replace controls during a filter update. Reacquire the
            # modal, its Search edit, and the canvas before reading again.
            pane = None
            previous = None
            layout_error = GridLayoutError(f"Search controls became unavailable for {query!r}")
            return None
        except GridLayoutError as exc:
            previous = None
            layout_error = exc
            return None
        layout_error = None
        if frame == previous:
            return current
        previous = frame
        return None

    grid = None

    def ready():
        nonlocal grid
        grid = stable()
        return grid is not None

    if not wait_until(ready, timeout=6, interval=0.4):
        if layout_error is not None:
            raise layout_error
        raise RuntimeError("Filtered results did not settle")
    return grid


def filtered_grid(root, query, heading, col, *, refresh_root=None):
    grid = search_grid(root, query, refresh_root=refresh_root) if refresh_root else search_grid(root, query)
    # OCR the headings separately from the body. The first horizontal rule is
    # the bottom of the header, so only data cells are included by read().
    header = grid.image.crop((grid.cols[col], 0, grid.cols[col + 1], grid.rows[0]))
    w, h = header.size
    if h < 7:
        raise RuntimeError(f"Could not verify the {heading} column")
    header = ImageOps.autocontrast(header.convert("L"))
    header = ImageOps.expand(header.resize((w * 3, h * 3)), border=15, fill=255)
    title = " ".join(word["text"] for word in _words(header))
    if norm(title).replace(".", "") != norm(heading).replace(".", ""):
        raise RuntimeError(f"Expected the {heading} column, read {title!r}")
    hits = []
    for row in range(len(grid.rows) - 1):
        if not grid.ink(row, col):
            continue
        value = grid.read(row, col)
        if identity(value) == identity(query):
            hits.append(row)
        else:
            copied = grid.copy_cell(row, col)
            if copied:
                if identity(copied) == identity(query):
                    hits.append(row)
            else:
                raise RuntimeError(
                    f"Unreadable result for {query!r} (read {value!r}). Review before creating a record."
                )
    if len(hits) > 1:
        raise RuntimeError(f"Multiple exact matches for {query!r}. Review the list.")
    return grid, hits[0] if hits else None
