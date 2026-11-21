"""Debtor on open Order: select, create, pay, save, re-select. Spec §2.1–2.13."""

from __future__ import annotations

import re
import time

from pywinauto import Desktop

from .control_locator import (
    combo_for_label,
    control_right_of,
    edit_for_label,
    find_exact,
    select_combo,
    visible_descendants,
    write_edit,
    _text,
)
from .fakturama_client import names_of, snapshot, wait_until
from .order import active_order_key, return_to_original_order
from .payment import (
    click_toolbar_save,
    ensure_payment_method,
    set_payment_on_debtor,
)
from .table import GridLayoutError, controls as table_controls, search_grid

_LABEL_TYPES = {"Text", "Static"}


class UIAMappingError(RuntimeError):
    pass


# --- Primitives ----------------------------------------------------------


def _norm(value) -> str:
    return ("" if value is None else str(value)).strip().casefold()


def _ctype(control) -> str:
    return control.element_info.control_type


def _one(items: list, what: str):
    if len(items) != 1:
        raise UIAMappingError(f"Expected one {what}, found {len(items)}")
    return items[0]


def _static(root, text: str) -> list:
    return [c for c in visible_descendants(root) if _ctype(c) in _LABEL_TYPES and _text(c) == text]


def _static_contains(root, hint: str) -> list:
    parts = hint.casefold().replace("-", " ").split()
    return [
        c
        for c in visible_descendants(root)
        if _ctype(c) in _LABEL_TYPES
        and all(p in _text(c).casefold().replace("-", " ") for p in parts)
    ]


def _tab(root, name: str) -> None:
    _one(find_exact(visible_descendants(root), name, "TabItem"), f"{name} tab").click_input()


# --- Field helpers -------------------------------------------------------


def _fill(root, labels: list[str], value) -> None:
    """Fill using edit_for_label (handles partial label matching)."""
    if value is None or not str(value).strip():
        return
    write_edit(edit_for_label(root, labels), str(value))
    print(f"[UIA] {labels[0]} ← {value!r}")


def _fill_exact(root, label: str, value) -> None:
    """Exact label match only. Use for ambiguous labels (E-Mail, Telephone, Alias name)."""
    if value is None or not str(value).strip():
        return
    edit = _exact_edit(root, label)
    write_edit(edit, str(value))
    print(f"[UIA] {label} ← {value!r}")


def _exact_edit(root, label):
    controls = visible_descendants(root)
    named = [c for c in controls if _ctype(c) == "Edit" and _norm(_text(c)) == _norm(label)]
    if len(named) == 1:
        return named[0]
    static = _one(
        [c for c in controls if _ctype(c) in _LABEL_TYPES and _norm(_text(c)) == _norm(label)],
        f"{label} label",
    )
    edit = control_right_of(static, controls, {"Edit"}, max_dx=520)
    if edit is None:
        raise UIAMappingError(f"No edit beside {label!r}")
    return edit


def _fill_pair(root, hint: str, left_val, right_val) -> None:
    """Two edits beside a combined label (e.g. 'First Name Last Name', 'ZIP - City')."""
    label = _one(_static_contains(root, hint), f"{hint} label")
    lr = label.rectangle()
    slack = min(16, max(8, lr.height() // 2 + 4))
    edits = [
        c
        for c in visible_descendants(root)
        if _ctype(c) == "Edit"
        and abs(c.rectangle().top - lr.top) <= slack
        and lr.right - 20 <= c.rectangle().left <= lr.right + 360
    ]
    edits.sort(key=lambda c: c.rectangle().left)
    if len(edits) < 2:
        raise UIAMappingError(f"Expected 2 edits beside {hint!r}, found {len(edits)}")
    for edit, val, side in ((edits[0], left_val, "left"), (edits[1], right_val, "right")):
        if val:
            write_edit(edit, str(val))
            print(f"[UIA] {hint} ({side}) ← {val!r}")


# --- Select existing debtor (§2.1–2.3) -----------------------------------


def _address_icons(window) -> list:
    addr = _one(_static(window, "Addresses"), "Addresses label")
    lr = addr.rectangle()
    icons = [
        c
        for c in visible_descendants(window)
        if _ctype(c) == "Image"
        and lr.left - 80 <= c.rectangle().left <= lr.left + 80
        and (abs(c.rectangle().top - lr.top) < 80 or lr.top - 10 <= c.rectangle().top <= lr.bottom + 70)
        and c.rectangle().height() < 40
        and c.rectangle().width() < 40
    ]
    icons.sort(key=lambda c: c.rectangle().top)
    if len(icons) < 2:
        snapshot(window, "address_icons_missing")
        raise UIAMappingError(f"Expected two address icons, found {len(icons)}")
    return icons


def _select_address_dialog(owner):
    found = [
        c
        for c in owner.descendants()
        if _ctype(c) == "Window"
        and c.is_visible()
        and "select" in _text(c).casefold()
        and "address" in _text(c).casefold()
    ]
    pick = [c for c in found if "select the address" in _text(c).casefold()]
    return (pick or found or [None])[0]


def _dialog_button(dialog, name: str) -> None:
    hits = [c for c in visible_descendants(dialog) if _ctype(c) == "Button" and _norm(_text(c)) == _norm(name)]
    _one(hits, f"{name} button").click_input()


def _search_query(debtor: dict) -> str:
    """Search by first name; fall back to company, then last name."""
    for key in ("first_name", "company", "last_name"):
        value = (debtor.get(key) or "").strip()
        if value:
            return value
    raise ValueError("Debtor has nothing to search by")


def _customer_number_matches(displayed, customer_id):
    """Handle OCR confusing zero and one with letters in a numeric ID suffix."""
    displayed = re.sub(r"\s+", "", displayed).upper()
    wanted = customer_id.upper()
    if displayed == wanted:
        return True
    match = re.fullmatch(r"([^0-9]+)([0-9]+)", wanted)
    if not match:
        return False
    prefix, digits = match.groups()
    suffix = displayed[len(prefix):]
    return displayed.startswith(prefix) and suffix.translate(str.maketrans("OIL", "011")) == digits


def _read_address_grid(dialog, query):
    try:
        return search_grid(dialog, query, sparse=True)
    except GridLayoutError as exc:
        snapshot(dialog, "debtor_grid_review")
        raise UIAMappingError(f"{exc}. Review the open address selector.") from exc


def _confirm_address(window, dialog):
    _dialog_button(dialog, "OK")
    if not wait_until(lambda: _select_address_dialog(window) is None, timeout=6):
        raise UIAMappingError("Debtor selector did not close after OK")
    return "selected"


def select_existing_debtor(window, debtor: dict, *, customer_id=None) -> str:
    dialog = _select_address_dialog(window)
    if dialog is None:
        print("[UIA] Upper address icon (not green +)")
        _address_icons(window)[0].click_input()
        if not wait_until(lambda: _select_address_dialog(window) is not None, timeout=8):
            snapshot(window, "select_address_missing")
            raise UIAMappingError("Select the address did not open")
        dialog = _select_address_dialog(window)

    if customer_id:
        # Fakturama supports filtering by the saved Customer No. Restrict the
        # search first, then verify that the only returned row has that ID.
        print(f"[UIA] Search saved Customer No. {customer_id!r}")
        grid = _read_address_grid(dialog, customer_id)
        shown = ""
        if len(grid.rows) == 2 and len(grid.cols) >= 3:
            shown = grid.read(0, 1)
            if not _customer_number_matches(shown, customer_id):
                shown = grid.read_cell(0, 1)
        if not _customer_number_matches(shown, customer_id):
            snapshot(dialog, "debtor_match_review")
            raise UIAMappingError(f"Could not verify one address row for Customer No. {customer_id!r}. Read {shown!r}.")
        grid.click(0, 1)
        return _confirm_address(window, dialog)

    query = _search_query(debtor)
    print(f"[UIA] Search {query!r}")
    grid = _read_address_grid(dialog, query)
    count = len(grid.rows) - 1
    print(f"[UIA] Address search results={count}")
    if count > 1:
        snapshot(dialog, "debtor_ambiguous")
        raise UIAMappingError(f"{count} rows returned for {query!r}. Choose the debtor in the open selector.")
    if count == 1:
        grid.click(0, 1)  # Select the row by its Customer No. cell.
        return _confirm_address(window, dialog)

    # 0 → close dialog, caller creates the debtor
    _dialog_button(dialog, "Cancel")
    if not wait_until(lambda: _select_address_dialog(window) is None, timeout=12):
        snapshot(window, "select_address_cancel_stuck")
        raise UIAMappingError("Select the address still open after Cancel")
    return "not_found"


# --- New debtor form helpers --------------------------------------------


def _debtor_editor(window):
    panes = [
        c
        for c in window.descendants()
        if _ctype(c) == "Pane" and _norm(_text(c)) in {"new debtor", "*new debtor"} and c.is_visible()
    ]
    panes.sort(key=lambda c: c.rectangle().width() * c.rectangle().height(), reverse=True)
    return _one(panes, "New Debtor pane")


def _commit_company(form, company: str) -> None:
    """Company is on the debtor header; re-apply and TAB so SWT commits before Save."""
    if not company or not str(company).strip():
        return
    _fill_exact(form, "Company", company)
    edits = [c for c in visible_descendants(form) if _ctype(c) == "Edit" and _text(c) == "Company"]
    if len(edits) == 1:
        try:
            edits[0].set_focus()
            edits[0].type_keys("{TAB}")
        except Exception:
            pass


def _control_key(control) -> object:
    try:
        return tuple(control.element_info.runtime_id)
    except Exception:
        return id(control)


_ADDRESS_ROLES = ("invoice addr.", "delivery addr.")


def _address_role_boxes(main, opener):
    """Read the nearby SWT popup from Fakturama's own process."""
    anchor = opener.rectangle()
    boxes = {name: {} for name in _ADDRESS_ROLES}
    roots = Desktop(backend="uia").windows(process=main.element_info.process_id)
    for root in roots:
        for box in root.descendants(control_type="CheckBox"):
            name = _norm(_text(box))
            if name in boxes and box.is_visible():
                rect = box.rectangle()
                if abs(rect.top - anchor.top) <= 90 and abs(rect.left - anchor.left) <= 320:
                    boxes[name][_control_key(box)] = box
    return {name: next(iter(matches.values())) for name, matches in boxes.items() if len(matches) == 1}


def _assign_address_roles(scope, invoice: bool, delivery: bool, *, main=None) -> None:
    """Invoice/Delivery live under the address type picker."""
    main = main or scope
    controls = visible_descendants(scope)
    labels = _static(scope, "address type")
    if not labels:
        raise UIAMappingError("address type label not found")
    addr_lbl = max(labels, key=lambda c: c.rectangle().top)
    type_edit = control_right_of(addr_lbl, controls, {"Edit"}, max_dx=450)
    if type_edit is None:
        raise UIAMappingError("address type edit not found")
    er = type_edit.rectangle()
    opener = _one(
        [
            c
            for c in controls
            if _ctype(c) == "Button"
            and abs(c.rectangle().top - er.top) < 16
            and 0 <= c.rectangle().left - er.right < 40
            and c.rectangle().width() < 50
        ],
        "address type button",
    )

    boxes = {}

    def picker_ready():
        boxes.clear()
        boxes.update(_address_role_boxes(main, opener))
        return len(boxes) == len(_ADDRESS_ROLES)

    def roles_match():
        current = _norm(type_edit.get_value())
        return all((role in current) == wanted for role, wanted in zip(_ADDRESS_ROLES, (invoice, delivery)))

    if not roles_match():
        opener.click_input()
        if not wait_until(picker_ready, timeout=3, interval=0.2):
            snapshot(main, "address_type_popup_missing")
            raise UIAMappingError("Address-type popup did not show both address roles")
        for role, wanted in zip(_ADDRESS_ROLES, (invoice, delivery)):
            if (role in _norm(type_edit.get_value())) != wanted:
                boxes[role].click_input()
        addr_lbl.click_input()
        if not wait_until(lambda: not _address_role_boxes(main, opener), timeout=2, interval=0.2):
            raise UIAMappingError("Address-type picker is still open")
        if not roles_match():
            raise UIAMappingError(f"Address type read back as {type_edit.get_value()!r}. Review before saving.")
    print(f"[UIA] Address type: {type_edit.get_value()}")


def _net_or_gross_combo(root):
    """Locate the Miscellaneous label without opening every combo via texts()."""
    return combo_for_label(root, ["Net or Gross"], anchor="bottom")


def _add_address_button(window):
    """The '+' inside the Addresses tab control."""
    controls = visible_descendants(window)
    candidates = [c for c in controls if _ctype(c) == "Button" and _text(c) == "+" and c.rectangle().width() < 40]
    if not candidates:
        raise UIAMappingError("'+' add-address button not found")
    tabs = [c for c in controls if _ctype(c) == "Tab" and _norm(_text(c)) == "addresses"]
    if tabs:
        tr = tabs[0].rectangle()
        inside = [
            c
            for c in candidates
            if tr.left - 10 <= c.rectangle().left and c.rectangle().right <= tr.right + 10
            and tr.top - 10 <= c.rectangle().top <= tr.bottom + 10
        ]
        if inside:
            return inside[0]
    return min(candidates, key=lambda c: c.rectangle().top)


def _same_address(a: dict, b: dict | None) -> bool:
    if not a or not b:
        return False
    required = ("street", "zip_code", "city", "country")
    return all(_norm(a.get(k)) for k in required) and all(
        _norm(a.get(k)) == _norm(b.get(k))
        for k in (*required, "additional_name", "address_specification", "district")
    )


def _address_text(value):
    return " ".join(re.findall(r"\w+", _norm(value)))


def verify_document_addresses(window, document, debtor):
    billing = debtor.get("billing_address") or {}
    for role, source in (("invoice", billing), ("delivery", debtor.get("delivery_address"))):
        if not source:
            continue
        tabs = [
            c for c in table_controls(document, "TabItem")
            if _norm(_text(c)).startswith(role) and "addr" in _norm(_text(c))
        ]
        _one(tabs, f"{role} address tab").click_input()

        def correct():
            tab = [c for c in table_controls(document, "Tab") if _norm(_text(c)).startswith(role)]
            if len(tab) != 1:
                return False
            edits = table_controls(tab[0], "Edit")
            if len(edits) != 1:
                return False
            displayed = f" {_address_text(edits[0].get_value())} "
            # Fakturama keeps Additional name on the debtor address, but
            # omits it from the formatted address in the Order.
            expected = [source.get(key) for key in ("street", "zip_code", "city", "country",
                                                    "address_specification", "district")]
            if role == "invoice":
                expected.extend(debtor.get(key) for key in ("company", "first_name", "last_name"))
            return all(
                f" {_address_text(value)} " in displayed for value in expected if _address_text(value)
            )

        if not wait_until(correct, timeout=3, interval=0.2):
            snapshot(window, f"{role}_address_review")
            raise UIAMappingError(f"Document {role} address differs from the source. Review the displayed address.")
        print(f"[UIA] Confirmed {role} address against the source")


def _verify_order_addresses(window, debtor):
    order = _one(table_controls(window, "Pane", "New Order"), "active New Order")
    verify_document_addresses(window, order, debtor)


def _fill_main_address(form, addr: dict) -> None:
    for key, label in (
        ("street", "Street"),
        ("additional_name", "additional name"),
        ("address_specification", "Address specification"),
        ("district", "district"),
    ):
        _fill_exact(form, label, addr.get(key))
    _fill_pair(form, "ZIP City", addr.get("zip_code"), addr.get("city"))
    if addr.get("country"):
        select_combo(combo_for_label(form, ["Country"]), str(addr["country"]))
    _fill_exact(form, "E-Mail", addr.get("email"))
    _fill_exact(form, "Telephone", addr.get("telephone"))
    for key, label in (("street", "Street"), ("additional_name", "additional name")):
        if addr.get(key) and _address_text(_exact_edit(form, label).get_value()) != _address_text(addr[key]):
            raise UIAMappingError(f"{label} did not read back in the address editor. Review before saving.")


def _add_delivery_address(form, delivery: dict, *, main) -> None:
    print("[UIA] '+' → add delivery address")
    button = _add_address_button(form)
    folder = button.parent()
    count = len(table_controls(folder, "TabItem"))
    selected = _norm(_text(folder))
    button.click_input()

    def delivery_tab_ready():
        return (len(table_controls(folder, "TabItem")) > count
                and _norm(_text(folder)) not in {"", selected, "main address"})

    if not wait_until(delivery_tab_ready, timeout=4, interval=0.2):
        snapshot(main, "additional_address_review")
        raise UIAMappingError("The additional address tab did not open. Main address was left untouched.")
    _fill_main_address(form, delivery)
    _assign_address_roles(form, invoice=False, delivery=True, main=main)


# --- Create debtor (§2.5–2.11) + re-select (§2.12–2.13) ------------------


def create_debtor(window, debtor: dict, payment: dict | None = None) -> str:
    contact = _one(
        [c for c in visible_descendants(window) if _ctype(c) == "Text" and _norm(_text(c)) == "new contact"],
        "New Contact",
    )
    print("[UIA] New Contact (Order stays open)")
    contact.click_input()
    if not wait_until(lambda: "new debtor" in " ".join(names_of(window)).casefold(), timeout=12):
        snapshot(window, "new_debtor_missing")
        raise UIAMappingError("New Debtor editor did not open")

    form = _debtor_editor(window)
    customer_id = _exact_edit(form, "Customer ID").get_value().strip()
    snapshot(window, "new_debtor")
    bill = debtor.get("billing_address") or {}
    delivery = debtor.get("delivery_address")

    # Scope header fields to the New Debtor pane (not the whole workbench).
    _fill_exact(form, "Company", debtor.get("company"))
    _fill_pair(form, "First Name Last Name", debtor.get("first_name"), debtor.get("last_name"))

    _tab(form, "Addresses")
    _tab(form, "Main address")
    time.sleep(0.3)
    _fill_main_address(form, bill)

    same = _same_address(bill, delivery)
    _assign_address_roles(form, invoice=True, delivery=same, main=window)
    if delivery and not same:
        _add_delivery_address(form, delivery, main=window)

    _tab(form, "Miscellaneous")
    time.sleep(0.4)
    misc = _debtor_editor(window)
    snapshot(window, "new_debtor_misc")
    _fill_exact(misc, "Alias name", debtor.get("alias"))
    _fill(misc, ["Discount"], "0")
    select_combo(_net_or_gross_combo(misc), "Net")

    method = (payment or {}).get("method")
    if method:
        set_payment_on_debtor(window, method, _debtor_editor)

    # §2.11 Commit Company and save the debtor once.
    form = _debtor_editor(window)
    _commit_company(form, debtor.get("company"))
    click_toolbar_save(window)
    return customer_id


def resolve_debtor(window, order: dict) -> str:
    debtor = order.get("debtor") or {}
    if not debtor.get("company") and not (debtor.get("first_name") and debtor.get("last_name")):
        raise ValueError("Order has no debtor identity")

    outcome = select_existing_debtor(window, debtor)
    if outcome == "selected":
        _verify_order_addresses(window, debtor)
        return "selected"
    if outcome != "not_found":
        raise RuntimeError(f"Unexpected debtor outcome: {outcome!r}")

    order_key = active_order_key(window)
    payment = order.get("payment") or {}
    method = payment.get("method")
    if method:
        print(f"[UIA] Checking Terms of Payment for {method!r} before New Debtor")
        ensure_payment_method(window, method, return_to_order=False)
        return_to_original_order(window, order_key)
    customer_id = create_debtor(window, debtor, payment)

    print("[UIA] Returning to Order; reopening Select the address")
    return_to_original_order(window, order_key)
    second = select_existing_debtor(window, debtor, customer_id=customer_id)
    if second != "selected":
        raise UIAMappingError(
            f"Saved debtor not visible on second pass (outcome={second!r}). "
            "Check that Save succeeded and Company was persisted."
        )
    _verify_order_addresses(window, debtor)
    print("[UIA] Debtor save confirmed in the Order")
    return "created"


