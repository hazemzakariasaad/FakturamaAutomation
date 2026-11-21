"""Payment method on Debtor: select existing, or create via Data > terms of payment (§2.10)."""

from __future__ import annotations

import time

from pywinauto import Desktop

from .control_locator import ComboOptionMissing, _combo_value, control_right_of, select_combo, visible_descendants, write_edit, _text
from .fakturama_client import snapshot, wait_until
from .table import filtered_grid

__all__ = [
    "PaymentError",
    "click_toolbar_save",
    "ensure_payment_method",
    "set_payment_on_debtor",
    "switch_to_order_tab",
]

_PAYMENT_CODE = {
    "bank transfer": "Credit transfer",
    "credit card": "Credit card",
    "sepa direct debit": "SEPA direct debit",
}
_LABEL_TYPES = {"Text", "Static"}
_CREATE_BTN = "create a new term of payment"


class PaymentError(RuntimeError):
    pass


# ---------- small helpers ----------

def _norm(value) -> str:
    return ("" if value is None else str(value)).strip().casefold()


def _ctype(c) -> str:
    return c.element_info.control_type


def _find(root, ctype, pred=lambda c: True) -> list:
    """Visible descendants of a given control type matching pred."""
    types = {ctype} if isinstance(ctype, str) else set(ctype)
    return [c for c in visible_descendants(root) if _ctype(c) in types and pred(c)]


def _one(items: list, what: str):
    if len(items) != 1:
        raise PaymentError(f"Expected one {what}, found {len(items)}")
    return items[0]


def _fill(root, label: str, value) -> None:
    """Write value into the Edit named `label`, or the Edit right of its label."""
    edits = _find(root, "Edit", lambda c: _text(c) == label)
    if len(edits) == 1:
        edit = edits[0]
    else:
        static = _one(_find(root, _LABEL_TYPES, lambda c: _text(c) == label), f"{label} label")
        edit = control_right_of(static, visible_descendants(root), {"Edit"}, max_dx=520)
        if edit is None:
            raise PaymentError(f"No edit beside {label!r}")
    write_edit(edit, str(value))
    print(f"[UIA] {label} ← {value!r}")


def _combo_right_of_label(root, label: str):
    """Combo named `label`, else the combo to the right of its (lowest) label."""
    named = _find(root, "ComboBox", lambda c: _norm(_text(c)) == label)
    if len(named) == 1:
        return named[0]
    labels = _find(root, _LABEL_TYPES, lambda c: _norm(_text(c)) == label)
    if not labels:
        return None
    labels.sort(key=lambda c: c.rectangle().top, reverse=True)
    return control_right_of(labels[0], visible_descendants(root), {"ComboBox"}, max_dx=520)


# ---------- toolbar / tabs ----------

def click_toolbar_save(window) -> None:
    buttons = _find(window, "Button")
    hits = [c for c in buttons if "save" in _norm(_text(c)) and "current" in _norm(_text(c))]
    hits = hits or [c for c in buttons if _norm(_text(c)) == "save"]
    _one(hits, "Save toolbar button").click_input()
    time.sleep(0.6)
    print("[UIA] Toolbar Save clicked")


def _click_tab(window, text: str, *, exclude: str = "") -> bool:
    tabs = _find(
        window, "TabItem",
        lambda c: text in _norm(_text(c)) and not (exclude and exclude in _norm(_text(c))),
    )
    if not tabs:
        return False
    starred = [c for c in tabs if _text(c).startswith("*")]
    try:
        (starred or tabs)[0].click_input()
        time.sleep(0.4)
        return True
    except Exception:
        return False


def switch_to_order_tab(window) -> bool:
    return _click_tab(window, "order", exclude="delivery")


# ---------- terms of payment view ----------

def _menu_items(window) -> list:
    """Menu items from all visible top-level windows plus the main window."""
    items = list(visible_descendants(window))
    try:
        for top in Desktop(backend="uia").windows():
            try:
                if top.is_visible():
                    items.extend(c for c in top.descendants() if c.is_visible())
            except Exception:
                continue
    except Exception:
        pass
    return [c for c in items if _ctype(c) == "MenuItem"]


def _open_terms_of_payment(window) -> None:
    _one(_find(window, "MenuItem", lambda c: _norm(_text(c)) == "data"), "Data menu").click_input()
    subs = [c for c in _menu_items(window) if _norm(_text(c)) == "terms of payment"]
    if not subs:
        raise PaymentError("'terms of payment' menu item not found")
    subs.sort(key=lambda c: c.rectangle().top)  # top-most if duplicated
    subs[0].click_input()


def _terms_panes(window) -> list:
    """Panes titled 'terms of payment', largest first."""
    def area(p):
        r = p.rectangle()
        return (r.right - r.left) * (r.bottom - r.top)
    return sorted(_find(window, "Pane", lambda c: "terms of payment" in _norm(_text(c))), key=area, reverse=True)


def _new_term_editor(window):
    """Scope of the 'New Term of Payment' editor, or None if it is not open."""
    for ctype in ("Pane", "Tab"):
        hits = _find(window, ctype, lambda c: _text(c) == "New Term of Payment")
        if hits:
            return hits[0]
    return None


def _edit_value(edit) -> str:
    """Read an edit's value, rather than its accessible label ('Name')."""
    try:
        return str(edit.get_value() or "")
    except Exception:
        try:
            return str(edit.legacy_properties().get("Value") or "")
        except Exception:
            return ""


def _existing_term_matches(window, method: str) -> bool:
    """An existing payment editor exposes the Name hidden by the table canvas."""
    wanted = _norm(method)
    for pane in _find(window, "Pane", lambda c: _norm(_text(c)) == wanted):
        edits = _find(pane, "Edit", lambda c: _norm(_text(c)) == "name")
        if len(edits) == 1 and _norm(_edit_value(edits[0])) == wanted:
            return True
    return False


def _return_to_order(window) -> None:
    close_terms_of_payment_view(window)
    if not switch_to_order_tab(window):
        raise PaymentError("Could not return to Order after terms-of-payment check")
    if not wait_until(
        lambda: bool(_find(window, "Pane", lambda c: _norm(_text(c)) == "new order")),
        timeout=5,
    ):
        raise PaymentError("New Order editor not visible after terms-of-payment check")


def _conflict_dialog_open(window) -> bool:
    phrases = ("conflicting definition", "already exist", "duplicate")
    try:
        tops = Desktop(backend="uia").windows(process=window.element_info.process_id)
    except Exception:
        return False
    for top in tops:
        try:
            r = top.rectangle()
            if top.handle == window.handle or not top.is_visible() or r.width() > 900 or r.height() > 500:
                continue
            if any(p in _norm(_text(top)) for p in phrases):
                return True
            if any(
                c.is_visible() and _ctype(c) in _LABEL_TYPES | {"Pane"}
                and any(p in _norm(_text(c)) for p in phrases)
                for c in top.descendants()
            ):
                return True
        except Exception:
            continue
    return False


def _payment_code_combo(scope):
    combos = _find(scope, "ComboBox")
    for c in combos:
        raw = _norm(_text(c)).replace("!", "").replace(" ", "")
        if "paymentcode" in raw:
            return c
    # label + combo to its right
    for label in _find(scope, _LABEL_TYPES, lambda c: "paymentcode" in _norm(_text(c)).replace("!", "").replace(" ", "")):
        combo = control_right_of(label, visible_descendants(scope), {"ComboBox"}, max_dx=520)
        if combo is not None:
            return combo
    others = [c for c in combos if _norm(_text(c)) not in {"payment", "account"}]
    return others[0] if len(others) == 1 else None


def _terms_editor_active(window) -> bool:
    return any(
        p.rectangle().width() > 200 and p.rectangle().height() > 120
        for p in _find(window, "Pane", lambda c: _norm(_text(c)) == "terms of payment")
    )


def close_terms_of_payment_view(window) -> bool:
    switch_to_order_tab(window)
    if _terms_editor_active(window):
        tabs = _find(window, "TabItem", lambda c: _norm(_text(c)) == "terms of payment")
        if tabs:
            try:
                tabs[0].click_input()
                window.set_focus()
                window.type_keys("^w")  # close the tab
                time.sleep(0.35)
            except Exception:
                pass
            switch_to_order_tab(window)
    left = not _terms_editor_active(window)
    print("[UIA] Left terms of payment view" if left else "[UIA] Still in terms of payment view")
    return left


def ensure_payment_method(window, method: str, *, return_to_order=True) -> None:
    """§2.10.1–2.10.6: open terms of payment; create method if missing."""
    _open_terms_of_payment(window)

    if not wait_until(lambda: bool(_terms_panes(window)), timeout=5):
        raise PaymentError("Terms of payment did not open")
    try:
        grid, row = filtered_grid(_terms_panes(window)[0], method, "Name", 1)
    except RuntimeError:
        snapshot(window, "terms_lookup_unreadable")
        raise
    if row is not None:
        grid.click(row, 1, double=True)
        if not wait_until(lambda: _existing_term_matches(window, method), timeout=5):
            raise PaymentError(f"Could not verify the exact payment record {method!r}")
        expected = _PAYMENT_CODE.get(_norm(method))
        pane = _one(_find(window, "Pane", lambda c: _norm(_text(c)) == _norm(method)), "payment editor")
        combo = _payment_code_combo(pane)
        if expected and (combo is None or _norm(_combo_value(combo)) != _norm(expected)):
            raise PaymentError(f"Conflicting payment code for {method!r}. Review the open payment editor.")
        print(f"[UIA] Payment method {method!r} found via Search; skipping create")
        if return_to_order:
            _return_to_order(window)
        return

    code = _PAYMENT_CODE.get(_norm(method))
    if code is None:
        raise PaymentError(f"No payment-code mapping for {method!r}")

    print(f"[UIA] Creating payment method {method!r}")
    plus = _find(window, "Button", lambda c: _CREATE_BTN in _norm(_text(c)))
    _one(plus, "Create a new term of payment button").click_input()

    wait_until(lambda: _new_term_editor(window) is not None or _conflict_dialog_open(window), timeout=5)
    if _conflict_dialog_open(window):
        snapshot(window, "payment_conflict")
        raise PaymentError(f"Conflicting definition for {method!r}; stop for manual review")

    editor = _new_term_editor(window)
    if editor is None:
        raise PaymentError("New Term of Payment editor did not open")
    snapshot(window, "payment_new")

    _fill(editor, "Name", method)
    _fill(editor, "Description", method)

    combo = _payment_code_combo(editor)
    if combo is None:
        raise PaymentError(f"Payment-code combo not found for {code!r}")
    select_combo(combo, code)
    print(f"[UIA] Payment code ← {code!r}")

    for label in ("Cash discount", "Discount Days", "Net Days"):
        _fill(editor, label, "0")

    click_toolbar_save(window)
    if return_to_order:
        _return_to_order(window)


# ---------- debtor ----------

def set_payment_on_debtor(
    window,
    method: str,
    debtor_editor_fn,
) -> None:
    """Select the exact term verified before opening New Debtor."""
    combo = _combo_right_of_label(debtor_editor_fn(window), "payment")
    if combo is None:
        raise PaymentError("Payment combo not found on Miscellaneous")

    try:
        select_combo(combo, method, exact=True)
    except ComboOptionMissing as exc:
        raise PaymentError(
            f"Payment {method!r} is missing from New Debtor after the Terms of Payment check."
        ) from exc
    print(f"[UIA] Payment ← {method!r}")
