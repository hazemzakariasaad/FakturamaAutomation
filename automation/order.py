"""Open a Fakturama New Order and fill the header from extracted order data.

Does not save the document and does not change the proposed document number.
"""

from __future__ import annotations

from datetime import date, datetime

from .control_locator import (
    _text,
    combo_for_label,
    edit_for_label,
    find_button,
    select_combo,
    visible_descendants,
    write_edit,
    write_date,
)
from .fakturama_client import names_of, snapshot, wait_until
from .table import controls as table_controls, norm, one


def active_order_key(window):
    pane = one(table_controls(window, "Pane", "New Order"), "active New Order")
    return tuple(pane.element_info.runtime_id)


def return_to_original_order(window, order_key):
    def original_is_active():
        panes = table_controls(window, "Pane", "New Order")
        return len(panes) == 1 and tuple(panes[0].element_info.runtime_id) == order_key

    if original_is_active():
        return

    # Opening and saving master data can shift the SWT tab strip.
    tabs = [
        tab for tab in table_controls(window, "TabItem")
        if norm(_text(tab).lstrip("*")) == "new order"
    ]
    for tab in reversed(tabs):
        tab.click_input()
        if wait_until(original_is_active, timeout=2, interval=0.2):
            print("[UIA] Returned to the original Order")
            return
    snapshot(window, "order_return_review")
    raise RuntimeError("Could not return to the original Order tab")


def order_editor_open(window) -> bool:
    names = [name.casefold() for name in names_of(window)]
    has_tab = any("new order" in name for name in names)
    has_ref = any(name == "cust.ref." for name in names)
    return has_tab and has_ref


def open_new_order(window) -> None:
    """Click the top-toolbar Create: New Order button. Do not click the editor's 'Order' label."""
    if order_editor_open(window):
        print("[UIA] New Order editor is already open")
        return
    controls = visible_descendants(window)
    buttons = find_button(controls, "Create: New Order")
    if len(buttons) != 1:
        raise RuntimeError(
            f"Found {len(buttons)} 'Create: New Order' toolbar buttons. "
            "No click performed. Inspect output/uia/before_order.json."
        )
    print("[UIA] Clicking toolbar 'Create: New Order'")
    buttons[0].click_input()
    if not wait_until(lambda: order_editor_open(window), timeout=12):
        snapshot(window, "order_open_failed")
        raise RuntimeError("New Order editor did not expose Cust.Ref. within 12s")
    print("[UIA] New Order editor is open")


def _format_date(order_date: str, current: str) -> str:
    parsed = datetime.strptime(order_date[:10], "%Y-%m-%d").date()
    if "." in current:
        return parsed.strftime("%d.%m.%Y")
    if "/" in current:
        return parsed.strftime("%m/%d/%Y")
    return f"{parsed.month} {parsed.day}, {parsed.year}"


def _price_mode_combo(window, date_edit):
    """Header Net/Gross combo sits on the Date row and has an empty accessible name."""
    date_rect = date_edit.rectangle()
    on_row = []
    for control in visible_descendants(window):
        if control.element_info.control_type != "ComboBox" or not control.is_visible():
            continue
        rect = control.rectangle()
        if abs(rect.top - date_rect.top) > 28 or rect.left < date_rect.right - 10:
            continue
        on_row.append(control)
    if len(on_row) == 1:
        return on_row[0]
    named = []
    for control in on_row:
        blob = control.window_text() or ""
        try:
            blob += " " + " ".join(child.window_text() or "" for child in control.descendants())
        except Exception:
            pass
        if "net" in blob.casefold() or "gross" in blob.casefold():
            named.append(control)
    if len(named) == 1:
        return named[0]
    raise RuntimeError(f"Expected one Net/Gross combo on the Date row, found {len(on_row)}")


def fill_order_header(window, order: dict) -> None:
    """Set Date, Cust.Ref., Net, and With VAT. Leave No. untouched."""
    if not order_editor_open(window):
        raise RuntimeError("New Order editor is not open")

    order_date = order.get("order_date")
    if isinstance(order_date, date):
        order_date = order_date.isoformat()
    if not order_date:
        raise RuntimeError("order.json has no order_date")
    reference = order.get("external_reference")
    if not reference:
        raise RuntimeError("order.json has no external_reference")

    date_edit = edit_for_label(window, ["Date"])
    try:
        current_date = date_edit.window_text() or ""
    except Exception:
        current_date = ""
    formatted = _format_date(str(order_date), current_date)
    print(f"[UIA] Date {current_date!r} -> {formatted!r} (No. left unchanged)")
    write_date(date_edit, formatted)

    ref_edit = edit_for_label(window, ["Cust.Ref."])
    print(f"[UIA] Cust.Ref. -> {reference!r}")
    write_edit(ref_edit, reference)

    select_combo(_price_mode_combo(window, date_edit), "Net")
    select_combo(combo_for_label(window, ["VAT"]), "With VAT")
    print("[UIA] Header set. Order tab stays open. Document was not saved.")
