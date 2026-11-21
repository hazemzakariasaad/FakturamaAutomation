"""Complete a verified Order and its linked Fakturama Invoice (steps 4 and 5)."""

from .control_locator import _combo_value, combo_for_label, select_combo
from .debtor import verify_document_addresses
from .documents import display_date, document_editor, header_edit, header_value, verify_saved_row
from .fakturama_client import snapshot, wait_until
from .order import _format_date, _price_mode_combo
from .payment import click_toolbar_save
from .product import _cell_editor, _enter_product_number, _items_grid, _line_row, _vat_percent, line_discount_percent
from .table import amount, controls, norm, one
from .vat import _enter_rate


def validate_document_source(source):
    totals = source.get("totals") or {}
    if any(totals.get(key) is None for key in ("net", "vat", "gross")):
        raise ValueError("The source must provide Total Net, VAT, and Total before saving")
    net, vat, gross = (amount(totals[key]) for key in ("net", "vat", "gross"))
    if not all(v.is_finite() for v in (net, vat, gross)) or net + vat != gross:
        raise ValueError("The source document totals do not reconcile")
    payment = source.get("payment") or {}
    if not str(payment.get("method") or "").strip():
        raise ValueError("The source has no Invoice payment method")
    if norm(payment.get("status")) == "paid":
        display_date(payment.get("payment_date"))
    display_date(source.get("order_date"))
    if not str(source.get("external_reference") or "").strip():
        raise ValueError("The source has no Cust.Ref.")
    order_discount = amount(source.get("order_discount_percent") or 0)
    shipping = amount(source.get("shipping_value") or 0)
    if not order_discount.is_finite() or not 0 <= order_discount <= 100:
        raise ValueError("The source order-level discount is invalid")
    if not shipping.is_finite() or shipping < 0:
        raise ValueError("The source shipping value is invalid")


def _shipping_fields(editor):
    combo = combo_for_label(editor, ["Shipping"])
    anchor = combo.rectangle()
    edits = []
    for c in controls(editor, "Edit"):
        rect = c.rectangle()
        if anchor.right - 3 <= rect.left <= anchor.right + 35 and rect.top < anchor.bottom and rect.bottom > anchor.top:
            edits.append(c)
    return combo, one(edits, "Shipping amount field")


def _check_order_settings(editor, source, *, apply=False):
    discount = amount(source.get("order_discount_percent") or 0)
    shipping_value = amount(source.get("shipping_value") or 0)
    shipping_method = (source.get("shipping_method") or "").strip()
    if not shipping_method and not shipping_value:
        shipping_method = "Free of shipping costs"
    discount_edit = one(controls(editor, "Edit", "Discount"), "overall Discount")
    shipping, shipping_edit = _shipping_fields(editor)
    if apply:
        if amount(discount_edit.get_value()) != discount:
            _enter_rate(discount_edit, discount)
        if shipping_method and norm(_combo_value(shipping)) != norm(shipping_method):
            select_combo(shipping, shipping_method, exact=True)
        if amount(shipping_edit.get_value()) != shipping_value:
            _enter_product_number(shipping_edit, shipping_value)
    if amount(discount_edit.get_value()) != discount or amount(shipping_edit.get_value()) != shipping_value:
        raise RuntimeError("Overall Discount or Shipping amount differs from the source")
    if shipping_method and norm(_combo_value(shipping)) != norm(shipping_method):
        raise RuntimeError(f"Shipping method differs from {shipping_method!r}")


def _verify_header(editor, source, date_label):
    reference = str(source["external_reference"]).strip()
    if header_value(editor, "Cust.Ref.") != reference:
        raise RuntimeError(f"Cust.Ref. differs from the source value {reference!r}")
    expected_date = display_date(source["order_date"])
    if display_date(header_value(editor, date_label)) != expected_date:
        raise RuntimeError(f"{date_label} differs from the source Order date {expected_date}")
    if norm(_combo_value(combo_for_label(editor, ["VAT"]))) != "with vat":
        raise RuntimeError("VAT mode must be With VAT")
    date_edit = header_edit(editor, "Date")
    if norm(_combo_value(_price_mode_combo(editor, date_edit))) != "net":
        raise RuntimeError("Document price mode must be Net")
    for label, key in (("Total Net", "net"), ("VAT", "vat"), ("Total", "gross")):
        actual = amount(header_value(editor, label))
        expected = amount(source["totals"][key])
        if actual != expected:
            raise RuntimeError(f"{label} is {actual:.2f}, expected {expected:.2f}")


def _verify_linked_lines(editor, prepared):
    """Verify copied SKUs and read the actual values from each Invoice row."""
    for line, (item, qty, unit, rate, discount, _, _) in enumerate(prepared, 1):
        for col, label, expected in ((1, "Qty.", qty), (7, "U.Price", unit),
                                     (8, "Discount", discount), (6, "VAT", rate)):
            grid = _items_grid(editor)
            row = _line_row(grid, line, item["sku"])
            edit = _cell_editor(editor, grid, row, col, right=(col == 6))
            try:
                displayed = edit.get_value()
                parse_value = {6: _vat_percent, 8: line_discount_percent}.get(col, amount)
                actual = parse_value(displayed)
            finally:
                edit.type_keys("{ESC}")
            if col == 6 and actual is None:
                actual = _vat_percent(grid.read_cell(row, col))
            if actual != expected:
                raise RuntimeError(f"Invoice line {line} {label} reads {actual}, expected {expected}")
    grid = _items_grid(editor)
    if grid.starts_at_top and any(grid.ink(row, 2) for row in range(len(prepared), len(grid.rows) - 1)):
        raise RuntimeError("The linked Invoice has an extra item line")


def _paid_controls(editor):
    paid = one(controls(editor, "CheckBox", "paid"), "paid checkbox")
    p = paid.rectangle()
    combos = []
    for c in controls(editor, "ComboBox"):
        rect = c.rectangle()
        if p.right - 8 <= rect.left <= p.right + 80 and rect.top < p.bottom and rect.bottom > p.top:
            combos.append(c)
    return paid, one(combos, "Invoice payment method")


def _payment_date_edit(editor):
    return header_edit(editor, "at")


def _verify_payment(editor, payment, total):
    paid, method = _paid_controls(editor)
    expected_paid = norm(payment.get("status")) == "paid"
    if norm(_combo_value(method)) != norm(payment["method"]):
        raise RuntimeError(f"Invoice payment method differs from {payment['method']!r}")
    if paid.get_toggle_state() != int(expected_paid):
        raise RuntimeError("Invoice paid state differs from the source")
    if expected_paid:
        actual_date = _payment_date_edit(editor).get_value()
        if display_date(actual_date) != display_date(payment["payment_date"]):
            raise RuntimeError(f"Invoice payment date reads {actual_date!r}, expected {payment['payment_date']}")
        if amount(header_value(editor, "Value")) != total:
            raise RuntimeError(f"Invoice payment Value differs from {total:.2f}")


def _apply_payment(editor, payment, total):
    paid, method = _paid_controls(editor)
    select_combo(method, payment["method"], exact=True)
    expected = norm(payment.get("status")) == "paid"
    if paid.get_toggle_state() != int(expected):
        paid.click_input()
    if expected:
        if not wait_until(lambda: len(controls(editor, "Edit", "Value")) == 1, timeout=4):
            raise RuntimeError("The paid Invoice did not expose its payment date and Value fields")
        edit = _payment_date_edit(editor)
        value = str(edit.get_value() or "")
        expected_date = display_date(payment["payment_date"])
        value_edit = one(controls(editor, "Edit", "Value"), "Invoice payment Value")
        try:
            current_date = display_date(value)
        except ValueError:
            current_date = None
        if current_date != expected_date:
            # This masked SWT field retains its active segment across focus
            # changes. Start in the first segment and commit by leaving it.
            edit.click_input(coords=(4, edit.rectangle().height() // 2))
            formatted = _format_date(expected_date.isoformat(), value)
            edit.type_keys("{HOME}^a" + formatted + "{TAB}", with_spaces=True, pause=0.05)
            value_edit.click_input()
        current_value = str(value_edit.get_value() or "").strip()
        if not current_value or amount(current_value) != total:
            _enter_product_number(value_edit, total)
    _verify_payment(editor, payment, total)


def _save_document(window, editor, number):
    try:
        # Fakturama 2.2.1 runs the selected address tab's binding during
        # doSave. The delivery tab can have no registered binding.
        if not controls(editor, "Tab", "Invoice addr."):
            one(controls(editor, "TabItem", "Invoice addr."), "Invoice address tab").click_input()
            if not wait_until(lambda: bool(controls(editor, "Tab", "Invoice addr.")), timeout=3):
                raise RuntimeError(f"Could not select Invoice addr. before saving {number!r}")
        click_toolbar_save(window)
        # The active editor is renamed from New Order/New Invoice on save.
        # Stay on that editor until the save has completed.
        saved = document_editor(window, number)
        if header_value(saved, "No.") != number:
            raise RuntimeError(f"The active saved editor did not confirm document number {number!r}")
    except RuntimeError:
        snapshot(window, "document_save_review")
        raise


def complete_order_and_invoice(window, source, prepared, *, saved_order_no=None):
    """Verify an existing or newly saved Order and complete its linked Invoice."""
    validate_document_source(source)
    reference = str(source["external_reference"]).strip()
    order_date = display_date(source["order_date"])
    total = amount(source["totals"]["gross"])
    payment = source["payment"]

    order_no = saved_order_no
    if saved_order_no is None:
        order = document_editor(window, "New Order")
        order_no = header_value(order, "No.")
        if not order_no:
            raise RuntimeError("The Order has no proposed number")
        verify_document_addresses(window, order, source["debtor"])
        _check_order_settings(order, source, apply=True)
        _verify_header(order, source, "Date")
        _save_document(window, order, order_no)
    saved_order = verify_saved_row(window, order_no, order_date, reference, total, {"open"}, category="Orders")

    group = one(controls(saved_order, "Group", "Create a follow-up document"), "Order follow-up actions")
    invoice_button = one(controls(group, "Button", "Invoice"), "linked Invoice action")
    if not invoice_button.is_enabled():
        raise RuntimeError(f"Linked Invoice action is disabled on Order {order_no}")
    invoice_button.click_input()

    try:
        invoice = document_editor(window, "New Invoice")
        proposed = {label: header_value(invoice, label) for label in ("No.", "Date", "Service date")}
        invoice_no = proposed["No."]
        if not invoice_no:
            raise RuntimeError("The linked Invoice has no proposed number")
        invoice_date = display_date(proposed["Date"])
        _verify_header(invoice, source, "Order Date")
        verify_document_addresses(window, invoice, source["debtor"])
        _check_order_settings(invoice, source)
        _verify_linked_lines(invoice, prepared)
        _apply_payment(invoice, payment, total)
        if any(header_value(invoice, key) != value for key, value in proposed.items()):
            raise RuntimeError("The proposed Invoice number or dates changed before save")
        _save_document(window, invoice, invoice_no)
        paid = norm(payment.get("status")) == "paid"
        saved_invoice = verify_saved_row(window, invoice_no, invoice_date, reference, total,
                                         {"paid"} if paid else {"open", "unpaid"}, category="Invoices")
        _verify_payment(saved_invoice, payment, total)
        verify_saved_row(window, order_no, order_date, reference, total, {"open"}, category="Orders")
    except (RuntimeError, ValueError):
        snapshot(window, "invoice_review")
        raise
    return order_no, invoice_no
