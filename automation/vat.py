"""Verify or create the exact VAT master required by a product."""

from decimal import InvalidOperation

from .control_locator import _combo_value, _text, combo_for_label, select_combo
from .fakturama_client import wait_until
from .payment import _fill, _menu_items, click_toolbar_save
from .table import amount, controls, filtered_grid, norm, one


STANDARD = "S (Standard rate)"


def open_data_view(window, name):
    one(controls(window, "MenuItem", "Data"), "Data menu").click_input()
    items = [c for c in _menu_items(window) if norm(_text(c)) == norm(name)]
    if not items:
        raise RuntimeError(f"Data menu item {name!r} unavailable")
    min(items, key=lambda c: c.rectangle().top).click_input()
    if not wait_until(lambda: bool(controls(window, "Pane", name)), timeout=5):
        raise RuntimeError(f"Data view {name!r} did not open")
    return one(controls(window, "Pane", name), name)


def vat_name(rate):
    return f"VAT {format(rate.normalize(), 'f')}%"


def _enter_rate(edit, rate):
    # SWT validates the formatted percentage during keyboard entry. A bare
    # number set through UI Automation can revert to the previous value.
    current = edit.get_value() or ""
    number = format(rate.normalize(), "f")
    if "," in current and "." not in current:
        number = number.replace(".", ",")
    edit.type_keys("^a")
    edit.type_keys(number + "{%}")
    edit.type_keys("{TAB}")


def ensure_vat(window, rate):
    name = vat_name(rate)
    view = open_data_view(window, "VATs")
    grid, row = filtered_grid(view, name, "Name", 1)
    title = name
    if row is not None:
        grid.click(row, 1, double=True)
    else:
        title = "New TAX Rate"
        one(controls(window, "Button", "Create a new tax rate"), "New VAT button").click_input()
    if not wait_until(lambda: bool(controls(window, "Pane", title)), timeout=5):
        raise RuntimeError(f"VAT editor {title!r} did not open")
    editor = one(controls(window, "Pane", title), "VAT editor")
    code = combo_for_label(editor, ["VAT code (E-Invoice)"])
    value_edit = one(controls(editor, "Edit", "Value"), "VAT value")
    if row is None:
        _fill(editor, "Name", name)
        _fill(editor, "Description", name)
        select_combo(code, STANDARD, exact=True)
        _enter_rate(value_edit, rate)
    name_edit = one(controls(editor, "Edit", "Name"), "VAT name")
    actual_name = name_edit.get_value()
    actual_value = value_edit.get_value()
    actual_code = _combo_value(code)
    try:
        rate_matches = amount(actual_value) == rate
    except InvalidOperation:
        rate_matches = False
    if norm(actual_name) != norm(name) or not rate_matches or norm(actual_code) != norm(STANDARD):
        problem = "New VAT did not validate" if row is None else "Conflicting VAT definition"
        raise RuntimeError(
            f"{problem} for {name!r}. Expected name {name!r}, value {rate}%, code {STANDARD!r}. "
            f"Read name {actual_name!r}, value {actual_value!r}, code {actual_code!r}."
        )
    if row is None:
        click_toolbar_save(window)
    return name
