from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from automation import documents, invoice


def source_order():
    return {
        "order_date": "2026-07-14",
        "external_reference": "WEB-2026-0714-A17",
        "debtor": {"company": "Northstar Office GmbH"},
        "payment": {"method": "Bank Transfer", "status": "PAID", "payment_date": "2026-07-18"},
        "totals": {"net": "570.00", "vat": "108.30", "gross": "678.30"},
    }


def test_document_source_rejects_inconsistent_totals_and_missing_paid_date():
    record = source_order()
    record["totals"]["gross"] = "678.31"
    with pytest.raises(ValueError, match="do not reconcile"):
        invoice.validate_document_source(record)
    record = source_order()
    record["payment"].pop("payment_date")
    with pytest.raises(ValueError, match="document date"):
        invoice.validate_document_source(record)


@pytest.mark.parametrize("status, paid, initially_checked", [
    ("PAID", True, False), ("UNPAID", False, True), ("UNPAID", False, False),
])
def test_invoice_only_enters_payment_date_and_value_for_paid(monkeypatch, status, paid, initially_checked):
    editor, method = object(), object()
    checkbox, value, date_field = Mock(), Mock(), Mock()
    checkbox.get_toggle_state.return_value = int(initially_checked)
    date_field.get_value.return_value = "Nov 21, 2026"
    value.get_value.return_value = "$0.00"
    date_field.rectangle.return_value.height.return_value = 22
    write_value, select = Mock(), Mock()
    monkeypatch.setattr(invoice, "_paid_controls", lambda _: (checkbox, method))
    monkeypatch.setattr(invoice, "_payment_date_edit", lambda _: date_field)
    monkeypatch.setattr(invoice, "controls", lambda *_: [value])
    monkeypatch.setattr(invoice, "_enter_product_number", write_value)
    monkeypatch.setattr(invoice, "_verify_payment", Mock())
    monkeypatch.setattr(invoice, "select_combo", select)
    payment = {"status": status, "method": "Bank Transfer"}
    if paid:
        payment["payment_date"] = "2026-07-18"

    invoice._apply_payment(editor, payment, Decimal("678.30"))

    select.assert_called_once_with(method, "Bank Transfer", exact=True)
    assert checkbox.click_input.call_count == int(initially_checked != paid)
    if paid:
        date_field.click_input.assert_called_once_with(coords=(4, 11))
        date_field.type_keys.assert_called_once_with("{HOME}^a7 18, 2026{TAB}", with_spaces=True, pause=0.05)
        value.click_input.assert_called_once_with()
        write_value.assert_called_once_with(value, Decimal("678.30"))
    else:
        date_field.type_keys.assert_not_called()
        write_value.assert_not_called()


def test_linked_invoice_accepts_fakturamas_signed_line_discount(monkeypatch):
    grid = SimpleNamespace(starts_at_top=True, rows=[20, 40])
    fields = {1: "2.00", 7: "$250.00", 8: "-10.00 %", 6: "VAT 19% (19.0%)"}
    monkeypatch.setattr(invoice, "_items_grid", lambda _: grid)
    monkeypatch.setattr(invoice, "_line_row", lambda *_: 0)
    monkeypatch.setattr(invoice, "_cell_editor", lambda editor, grid, row, col, **kw:
                        Mock(get_value=Mock(return_value=fields[col])))
    prepared = [({"sku": "CHR-ERG-01"}, *map(Decimal, ["2", "250", "19", "10", "297.50", "450"]))]
    invoice._verify_linked_lines(object(), prepared)


@pytest.mark.parametrize("actual, padded, succeeds", [
    ("open", "", True), ("closed", "", False), ("", "open", True), ("", "", False),
])
def test_documents_row_checks_state_and_verifies_untruncated_reference(monkeypatch, actual, padded, succeeds):
    window, view, editor = object(), object(), object()
    data = ["", "P0000003", "Jul 14, 2026", "Northstar", "WEB-2026-071 ...", actual, "$678.30"]
    grid = SimpleNamespace(rows=[25, 50], cols=list(range(9)), read=lambda row, col: data[col],
                           read_cell=Mock(return_value=padded), click=Mock())
    reopened = Mock(return_value=editor)
    orders, contextual, search = Mock(), Mock(), Mock(return_value=grid)
    ui = Mock()
    monkeypatch.setattr(documents, "_wide_documents", lambda _: view)
    ui.attach_mock(orders, "orders")
    ui.attach_mock(search, "search")
    monkeypatch.setattr(documents, "controls", lambda _, kind, name: {
        ("TreeItem", "Orders"): [orders],
        ("TreeItem", "---"): [contextual],
    }.get((kind, name), []))
    monkeypatch.setattr(documents, "search_grid", search)
    monkeypatch.setattr(documents, "document_editor", reopened)
    monkeypatch.setattr(documents, "snapshot", Mock())
    monkeypatch.setattr(documents, "header_value", lambda _, label: {
        "No.": "PO000003", "Cust.Ref.": "WEB-2026-0714-A17", "Date": "Jul 14, 2026", "Total": "$678.30",
    }[label])
    args = window, "PO000003", date(2026, 7, 14), "WEB-2026-0714-A17", Decimal("678.30"), {"open"}
    if succeeds:
        assert documents.verify_saved_row(*args, category="Orders") is editor
        reopened.assert_called_once_with(window, "PO000003")
        grid.click.assert_called_once_with(0, 1, double=True)
    else:
        with pytest.raises(RuntimeError, match="differs"):
            documents.verify_saved_row(*args, category="Orders")
        reopened.assert_not_called()
        grid.click.assert_not_called()
    assert ui.mock_calls == [call.orders.click_input(), call.search(view, "PO000003", order_grid=True)]
    contextual.click_input.assert_not_called()
    assert grid.read_cell.call_count == int(not actual)


@pytest.mark.parametrize("failure, save_count,resume", [
    ("totals", 0, False), ("save confirmation", 1, False), ("saved row", 1, False),
    ("payment", 1, False), (None, 2, False), (None, 1, True),
])
def test_order_and_invoice_save_only_after_their_checks(monkeypatch, failure, save_count, resume):
    source = source_order()
    window, order, saved_order, draft, saved_invoice = [object() for _ in range(5)]
    group, prepared = object(), [object(), object()]
    actions = Mock()
    actions.row.side_effect = [saved_order, saved_invoice, saved_order]
    button = SimpleNamespace(is_enabled=lambda: True, click_input=actions.follow_up)

    def document_editor(_, title):
        if title == "PO000001" and failure == "save confirmation":
            raise RuntimeError(failure)
        return {"New Order": order, "New Invoice": draft,
                "PO000001": saved_order, "INV000001": saved_invoice}[title]

    monkeypatch.setattr(invoice, "document_editor", document_editor)
    monkeypatch.setattr(invoice, "header_value", lambda pane, label: {
        "No.": "PO000001" if pane in (order, saved_order) else "INV000001",
        "Date": "Nov 21, 2026", "Service date": "<choose date>",
    }[label])
    addr_tab = Mock()
    selected = set()

    def lookup(scope, kind, name):
        if kind == "TabItem" and name == "Invoice addr.":
            addr_tab.click_input.side_effect = lambda: selected.add(scope)
            return [addr_tab]
        if kind == "Tab" and name == "Invoice addr.":
            return [addr_tab] if scope in selected else []
        return {
            (saved_order, "Group", "Create a follow-up document"): [group],
            (group, "Button", "Invoice"): [button],
        }[scope, kind, name]

    monkeypatch.setattr(invoice, "controls", lookup)
    monkeypatch.setattr(invoice, "verify_document_addresses", actions.addresses)
    monkeypatch.setattr(invoice, "_check_order_settings", actions.settings)
    monkeypatch.setattr(invoice, "_verify_header", actions.header)
    monkeypatch.setattr(invoice, "_verify_linked_lines", actions.lines)
    monkeypatch.setattr(invoice, "_apply_payment", actions.payment)
    monkeypatch.setattr(invoice, "click_toolbar_save", actions.save)
    monkeypatch.setattr(invoice, "verify_saved_row", actions.row)
    monkeypatch.setattr(invoice, "_verify_payment", actions.persisted_payment)
    monkeypatch.setattr(invoice, "snapshot", Mock())
    if failure is not None:
        check = {"totals": actions.header, "saved row": actions.row, "payment": actions.payment}.get(failure)
        if check is not None:
            check.side_effect = RuntimeError(failure)
        with pytest.raises(RuntimeError, match=failure):
            invoice.complete_order_and_invoice(window, source, prepared)
        if failure != "payment":
            actions.follow_up.assert_not_called()
    else:
        assert invoice.complete_order_and_invoice(window, source, prepared,
                                                saved_order_no="PO000001" if resume else None) == ("PO000001", "INV000001")
        flow = [entry[0] for entry in actions.mock_calls]
        initial = [] if resume else ["addresses", "settings", "header", "save"]
        assert flow == initial + ["row", "follow_up", "header", "addresses", "settings", "lines",
                                  "payment", "save", "row", "persisted_payment", "row"]
        actions.lines.assert_called_once_with(draft, prepared)
        assert [entry.args[-1] for entry in actions.row.call_args_list] == [{"open"}, {"paid"}, {"open"}]
        assert [entry.kwargs["category"] for entry in actions.row.call_args_list] == [
            "Orders", "Invoices", "Orders",
        ]
    assert actions.save.call_args_list == [call(window)] * save_count
    assert addr_tab.click_input.call_count == save_count
