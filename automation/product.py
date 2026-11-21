"""Populate and verify every source item on an unsaved Fakturama Order."""

from __future__ import annotations

from _ctypes import COMError
import re
from decimal import Decimal, InvalidOperation

from pywinauto import mouse

from .control_locator import _combo_value, _text, combo_for_label, select_combo, write_edit
from .fakturama_client import snapshot, wait_until
from .order import active_order_key, return_to_original_order
from .payment import click_toolbar_save
from .table import Grid, GridLayoutError, amount, controls, filtered_grid, grid_below, identity, money, norm, one
from .vat import ensure_vat


class LineVatMissing(RuntimeError):
    pass


class LineLookupError(RuntimeError):
    pass


def _active_order(window):
    return one(controls(window, "Pane", "New Order"), "active New Order")


def _select_dialog(window):
    dialogs = controls(window, "Window", "Select a product")
    return one(dialogs, "Select a product dialog") if dialogs else None


def _pick_product(window, sku):
    order = _active_order(window)
    before_net = _total_net(order)
    before_editor = _item_editor_key(_inline_item_editor(order))
    items = one(controls(order, "Text", "Items"), "Items label")
    r = items.rectangle()
    icons = []
    for icon in controls(order, "Image"):
        box = icon.rectangle()
        if abs(box.left - r.left) < 48 and r.bottom <= box.top <= r.bottom + 110 and box.width() < 40:
            icons.append(icon)
    if not icons:
        raise RuntimeError("Product-selection icon not found")
    min(icons, key=lambda i: i.rectangle().top).click_input()
    if not wait_until(lambda: _select_dialog(window) is not None, timeout=6):
        raise RuntimeError("Select a product dialog did not open")
    dialog = _select_dialog(window)

    def inserted_from_search():
        def line_appeared():
            try:
                active = _active_order(window)
                # Qty opens even when the new line is free. The net total also
                # proves the Order changed if the inline editor already closed.
                if _total_net(active) != before_net:
                    return True
                editor_key = _item_editor_key(_inline_item_editor(active))
                return editor_key is not None and editor_key != before_editor
            except (RuntimeError, COMError):
                return False

        if not wait_until(line_appeared, timeout=3, interval=0.2):
            return False
        print(f"[UIA] Product picker inserted {sku!r} directly from Search")
        return True

    try:
        grid, row = filtered_grid(dialog, sku, "Item No.", 0, refresh_root=lambda: _select_dialog(window))
    except (RuntimeError, COMError):
        # Fakturama accepts an exact SKU entered in Search as a scanned item.
        # The modal closes and the Order's Qty editor opens before UIA returns.
        if _select_dialog(window) is None and inserted_from_search():
            return True
        raise
    dialog = _select_dialog(window)
    if dialog is None:
        if inserted_from_search():
            return True
        raise RuntimeError(f"The product selector closed before confirming {sku!r}")
    if row is not None:
        grid.click(row, 0)
    one(controls(dialog, "Button", "OK" if row is not None else "Cancel"), "product selector button").click_input()
    if not wait_until(lambda: _select_dialog(window) is None, timeout=6):
        raise RuntimeError("Product selector did not close")
    return row is not None


def _product_edit(editor, label):
    named = controls(editor, "Edit", label)
    if named:
        return one(named, f"product {label} field")
    rect = one(controls(editor, "Text", label), f"product {label} label").rectangle()
    edits = []
    for edit in controls(editor, "Edit"):
        r = edit.rectangle()
        if rect.right - 4 <= r.left <= rect.right + 100 and r.top < rect.bottom and r.bottom > rect.top:
            edits.append((r.left, edit))
    if not edits:
        raise RuntimeError(f"Product {label} field was not found on its label's row")
    return min(edits, key=lambda pair: pair[0])[1]


def _enter_product_number(edit, value):
    current = edit.get_value() or ""
    decimal = "," if re.search(r",\d{1,2}(?:\D*)$", current) else "."
    text = format(value, ".2f").replace(".", decimal)
    # Focusing the SWT currency field changes the formatted text to its
    # editable number. Keep focus on the same control for select/type/commit.
    edit.click_input()
    edit.type_keys(f"^a{text}{{TAB}}", set_foreground=False)


def _number_matches(edit, expected):
    try:
        return amount(edit.get_value()) == expected
    except InvalidOperation:
        return False


def _create_product(window, item, vat, gross):
    one(controls(window, "Text", "New product"), "New product navigation entry").click_input()
    if not wait_until(lambda: len(controls(window, "Pane", "New product")) == 1, timeout=6):
        raise RuntimeError("New product editor did not open")
    editor = one(controls(window, "Pane", "New product"), "product editor")
    text_fields = {"Item Number": item["sku"], "Name": item["description"], "Description": item["description"]}
    numeric_fields = {"cost price (net)": Decimal("0.00"), "Stock": Decimal("0.00"), "Price (gross)": gross}
    for label, value in text_fields.items():
        write_edit(_product_edit(editor, label), str(value))
    vat_combo = combo_for_label(editor, ["VAT"])
    select_combo(vat_combo, vat, exact=True)
    if not wait_until(lambda: norm(_combo_value(vat_combo)) == norm(vat), timeout=3, interval=0.2):
        raise RuntimeError(f"Product {item['sku']!r} VAT did not settle at {vat!r}")
    for label, value in numeric_fields.items():
        edit = _product_edit(editor, label)
        if _number_matches(edit, value):
            continue
        for _ in range(2):
            _enter_product_number(edit, value)
            if wait_until(lambda: _number_matches(edit, value), timeout=1, interval=0.15):
                break
        else:
            actual = edit.get_value()
            raise RuntimeError(f"Product {item['sku']!r} {label} read back as {actual!r}, expected {value:.2f}")

    for label, expected in text_fields.items():
        actual = _product_edit(editor, label).get_value()
        if str(actual or "").strip() != str(expected).strip():
            raise RuntimeError(f"Product {item['sku']!r} {label} read back as {actual!r}, expected {expected!r}")
    for label, expected in numeric_fields.items():
        edit = _product_edit(editor, label)
        if not _number_matches(edit, expected):
            actual = edit.get_value()
            raise RuntimeError(f"Product {item['sku']!r} {label} read back as {actual!r}, expected {expected:.2f}")
    actual_vat = _combo_value(vat_combo)
    if norm(actual_vat) != norm(vat):
        raise RuntimeError(f"Product {item['sku']!r} VAT read back as {actual_vat!r}, expected {vat!r}")
    print(f"[UIA] Product {item['sku']!r} verified: gross {gross:.2f}, VAT {vat}, cost and stock 0.00")
    click_toolbar_save(window)


def _items_canvas(order):
    return grid_below(order, one(controls(order, "Text", "Items"), "Items label"))


def _items_grid(order):
    return Grid(_items_canvas(order), order_grid=True)


def _inline_item_editor(order):
    edits = controls(_items_canvas(order), "Edit")
    return edits[0] if len(edits) == 1 else None


def _item_editor_key(edit):
    if edit is None:
        return None
    return edit.handle or tuple(edit.element_info.runtime_id or ()) or None


def _commit_item_editor(order):
    edit = _inline_item_editor(order)
    if edit is not None:
        edit.type_keys("{ENTER}")


def require_empty_items(window):
    order = _active_order(window)
    last_error = None

    def empty_after_paint():
        nonlocal last_error
        try:
            grid = _items_grid(order)
        except GridLayoutError as exc:
            last_error = exc
            return False
        if any(grid.ink(row, 0) or grid.ink(row, 2) for row in range(len(grid.rows) - 1)):
            raise RuntimeError("The open Order already has items. Use an empty Order to start this flow.")
        return True

    if not wait_until(empty_after_paint, timeout=4, interval=0.2):
        raise GridLayoutError(f"Could not verify an empty Items grid: {last_error}") from last_error


def _line_row(grid, line, sku, *, allow_copy=True):
    """Locate the source line and verify its SKU before editing."""
    if len(grid.cols) < 4:
        raise LineLookupError("The Order grid does not show the Item No. column")
    wanted = identity(sku)
    ordinal = line - 1
    rows = range(len(grid.rows) - 1)
    if grid.starts_at_top:
        candidates = [ordinal] if ordinal in rows else []
    else:
        candidates = [row for row in rows if grid.read(row, 0).strip() == str(line)]
    if len(candidates) == 1:
        row = candidates[0]
        shown = identity(grid.read(row, 2))
        if shown == wanted:
            return row
        # The picker has just confirmed this SKU. Windows OCR can read its
        # numeric suffix as letters on the highlighted row (01 as OI).
        prefix, digits = re.fullmatch(r"(.*?)([0-9]*)", wanted).groups()
        if digits and shown.startswith(prefix) and shown[len(prefix):].translate(
            str.maketrans("oil", "011")
        ) == digits:
            return row
        if allow_copy and grid.matches(row, 2, sku):
            return row
        raise LineLookupError(f"Order line {line} shows Item No. {shown!r}, expected {sku!r}")
    raise LineLookupError(f"Could not locate Order line {line} for {sku!r} in the visible grid")


def _verified_item_grid(order, line, sku):
    # The pointer can remain on the product-picker icon after Search closes
    # the dialog. Its tooltip extends across the desktop pixels used for OCR.
    rect = order.rectangle()
    mouse.move(coords=(rect.left + 3, rect.top + 3))
    verified = None

    def read_line():
        nonlocal verified
        try:
            grid = _items_grid(order)
            verified = grid, _line_row(grid, line, sku, allow_copy=False)
        except (COMError, GridLayoutError, LineLookupError):
            return False
        return True

    if wait_until(read_line, timeout=4, interval=0.15):
        return verified

    # Use one fresh capture for the clipboard fallback and final diagnostic.
    grid = _items_grid(order)
    return grid, _line_row(grid, line, sku)


def _cell_editor(order, grid, row, col, *, right=False):
    l, t, r, b = grid.bounds(row, col)
    grid.click(row, col, double=not right, right=right)

    def edit():
        matches = []
        for c in controls(order, "Edit"):
            rect = c.rectangle()
            if (grid.rect.left + l - 4 <= rect.left < grid.rect.left + r
                    and abs(rect.top - (grid.rect.top + t)) <= b - t
                    and rect.bottom <= grid.rect.top + b + 5):
                matches.append(c)
        return matches[0] if len(matches) == 1 else None

    if not wait_until(lambda: edit() is not None, timeout=3, interval=0.15):
        raise RuntimeError(f"Order grid editor did not open for column {col + 1}")
    return edit()


def _set_cell(order, grid, row, col, value):
    edit = _cell_editor(order, grid, row, col)
    current = edit.get_value()
    decimal = "," if re.search(r",\d{1,2}\s*[%$€£]?\s*$", current or "") else "."
    edit.type_keys("^a")
    edit.type_keys(format(value, "f").replace(".", decimal), with_spaces=True)
    edit.type_keys("{ENTER}")


def _vat_percent(text):
    match = re.findall(r"(-?\d+(?:[.,]\d+)?)\s*%", text)
    return amount(match[-1]) if match else None


def line_discount_percent(value):
    """Read Fakturama's signed line discount as the source discount percentage."""
    return abs(amount(value))


def _set_line_vat(window, order, grid, row, rate, vat):
    edit = _cell_editor(order, grid, row, 6, right=True)
    current = _vat_percent(edit.get_value())
    if current == rate or (current is None and _vat_percent(grid.read_cell(row, 6)) == rate):
        edit.type_keys("{ESC}")
        return
    choices = []

    def available():
        choices[:] = [c for c in controls(window, "ListItem") if _vat_percent(_text(c)) == rate]
        return bool(choices)

    if not wait_until(available, timeout=3):
        edit.type_keys("{ESC}")
        raise LineVatMissing(f"No VAT choice at {rate}% on the Order line")
    if vat:
        choices = [c for c in choices if norm(_text(c)).startswith(norm(vat) + " (")]
    one(choices, f"Order VAT choice for {rate}%").click_input()
    # Reopen the editor briefly to verify the committed rate.
    edit = _cell_editor(order, grid, row, 6, right=True)
    actual = _vat_percent(edit.get_value())
    edit.type_keys("{ESC}")
    if actual is None:
        actual = _vat_percent(_items_grid(order).read_cell(row, 6))
    if actual != rate:
        raise RuntimeError(f"Order VAT read back as {actual}, expected {rate}%")


def _total_net(order):
    return amount(one(controls(order, "Edit", "Total Net"), "Order Total Net").get_value())


def prepare_items(items):
    """Validate all item fields before making any UI changes."""
    if not items:
        raise ValueError("The source order has no items")
    prepared = []
    for index, item in enumerate(items, 1):
        fields = ("sku", "description", "quantity", "unit_net_price", "vat_percent", "discount_percent")
        missing = [key for key in fields if item.get(key) is None or not str(item[key]).strip()]
        if missing:
            raise ValueError(f"Source line {index} is missing {', '.join(missing)}")
        qty, unit, vat, discount = (amount(item[key]) for key in fields[2:])
        if not all(v.is_finite() for v in (qty, unit, vat, discount)) or qty <= 0 or unit < 0 or not (
            0 <= vat <= 100 and 0 <= discount <= 100
        ):
            raise ValueError(f"Invalid quantity, price, VAT, or discount on source line {index}")
        gross = money(unit * (1 + vat / 100))
        net = money(qty * unit * (1 - discount / 100))
        source = item.get("source_line_net_total")
        if source is not None and amount(source) != net:
            raise ValueError(f"Source line {index} shows {source}, but the calculated price is {net}")
        prepared.append((item, qty, unit, vat, discount, gross, net))
    return prepared


def fill_order_items(window, prepared):
    order_key = active_order_key(window)
    for line, (item, qty, unit, rate, discount, gross, net) in enumerate(prepared, 1):
        sku = item["sku"]
        try:
            before = _total_net(_active_order(window))
            vat = None
            if not _pick_product(window, sku):
                vat = ensure_vat(window, rate)
                _create_product(window, item, vat, gross)
                return_to_original_order(window, order_key)
                if not _pick_product(window, sku):
                    raise RuntimeError(f"Saved product {sku!r} is missing from the selector")
            order = _active_order(window)
            # Product insertion puts focus in Qty. Commit the default and
            # capture the grid after this transient editor closes.
            _commit_item_editor(order)
            grid, row = _verified_item_grid(order, line, sku)
            try:
                _set_line_vat(window, order, grid, row, rate, vat)
            except LineVatMissing:
                if vat is not None:
                    raise
                vat = ensure_vat(window, rate)
                return_to_original_order(window, order_key)
                order = _active_order(window)
                grid, row = _verified_item_grid(order, line, sku)
                _set_line_vat(window, order, grid, row, rate, vat)
            for column, value in ((1, qty), (7, unit), (8, discount)):
                _set_cell(order, grid, row, column, value)
                readback = _cell_editor(order, grid, row, column)
                displayed = readback.get_value()
                actual = line_discount_percent(displayed) if column == 8 else amount(displayed)
                readback.type_keys("{ESC}")
                if actual != value:
                    raise RuntimeError(f"Order column {column + 1} read back as {actual}, expected {value}")
            if not wait_until(lambda: _total_net(order) - before == net, timeout=3):
                raise RuntimeError(f"Order line {line} has net {_total_net(order) - before}, expected {net}")
            print(f"[UIA] Order line {line}: {sku} net={net:.2f}")
        except RuntimeError:
            snapshot(window, f"order_item_{line}_review")
            raise
