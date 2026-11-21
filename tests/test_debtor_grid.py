from types import SimpleNamespace
from threading import get_ident
from unittest.mock import Mock, call

import pytest
from PIL import Image, ImageDraw

from automation import debtor, table


def test_address_picker_only_reads_exact_nearby_roles_in_fakturama(monkeypatch):
    def checkbox(name, left, top, key):
        return Mock(window_text=Mock(return_value=name), is_visible=Mock(return_value=True),
                    rectangle=Mock(return_value=SimpleNamespace(left=left, top=top)),
                    element_info=SimpleNamespace(runtime_id=(key,)))

    invoice = checkbox("Invoice addr.", 420, 322, 1)
    delivery = checkbox("Delivery addr.", 523, 322, 2)
    unrelated = checkbox("Delivery addr.", 800, 420, 3)
    similar = checkbox("Delivery note", 500, 322, 4)
    root = Mock()
    root.descendants.return_value = [invoice, delivery, unrelated, similar]
    desktop = Mock()
    desktop.windows.return_value = [root]
    monkeypatch.setattr(debtor, "Desktop", lambda **_: desktop)
    main = SimpleNamespace(element_info=SimpleNamespace(process_id=123))
    opener = Mock(rectangle=Mock(return_value=SimpleNamespace(left=403, top=300)))

    assert debtor._address_role_boxes(main, opener) == {"invoice addr.": invoice, "delivery addr.": delivery}
    desktop.windows.assert_called_once_with(process=123)


@pytest.mark.parametrize("opens_address", [True, False])
def test_add_delivery_waits_for_a_separate_address_tab(monkeypatch, opens_address):
    form, main = object(), object()
    folder = Mock()
    folder.window_text.return_value = "Main address"
    button = SimpleNamespace(parent=lambda: folder, click_input=Mock())
    tabs = [object()]

    def click_add():
        if opens_address:
            tabs.append(object())
            folder.window_text.return_value = "Additional address"

    button.click_input.side_effect = click_add
    monkeypatch.setattr(debtor, "_add_address_button", lambda _: button)
    monkeypatch.setattr(debtor, "table_controls", lambda *_: tabs)
    monkeypatch.setattr(debtor, "wait_until", lambda predicate, **_: predicate())
    fill, roles = Mock(), Mock()
    monkeypatch.setattr(debtor, "_fill_main_address", fill)
    monkeypatch.setattr(debtor, "_assign_address_roles", roles)
    monkeypatch.setattr(debtor, "snapshot", Mock())
    delivery = {"street": "Beusselstrasse 44", "additional_name": "Northstar Office Warehouse"}

    if opens_address:
        debtor._add_delivery_address(form, delivery, main=main)
        fill.assert_called_once_with(form, delivery)
        roles.assert_called_once_with(form, invoice=False, delivery=True, main=main)
    else:
        with pytest.raises(debtor.UIAMappingError, match="additional address tab did not open"):
            debtor._add_delivery_address(form, delivery, main=main)
        fill.assert_not_called()
        roles.assert_not_called()


def address_grid(*, row=False, unruled_text=False):
    image = Image.new("L", (618, 300), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 617, 18), fill=242)
    for x in [39, 139, 239, 339, 439, 539]:
        draw.line((x, 0, x, 18), fill=95)
    draw.line((0, 19, 617, 19), fill=80)
    if row:
        for x in [39, 139, 239, 339, 439, 539]:
            draw.line((x, 20, x, 38), fill=192)
        draw.text((42, 21), "CUST000001", fill=30)
        draw.line((0, 39, 617, 39), fill=192)
    if unruled_text:
        draw.text((145, 23), "Marta", fill=30)
    return image


@pytest.mark.parametrize("row", [False, True])
def test_sparse_address_grid_accepts_header_only_or_one_result(row):
    rows, cols = table._sparse_grid_layout(address_grid(row=row))
    assert rows == ([19, 39] if row else [19])
    assert cols == [0, 39, 139, 239, 339, 439, 539, 618]


def test_unruled_text_is_not_treated_as_no_results():
    with pytest.raises(table.GridLayoutError):
        table._sparse_grid_layout(address_grid(unruled_text=True))


def test_address_search_waits_through_incomplete_paint(monkeypatch):
    pane = Mock()
    rendered = SimpleNamespace(image=address_grid())
    read_grid = Mock(side_effect=[table.GridLayoutError("Repainting"), rendered, rendered])
    monkeypatch.setattr(table, "Grid", read_grid)
    monkeypatch.setattr(table, "search", lambda *args: object())
    monkeypatch.setattr(table, "grid_below", lambda *args: pane)
    monkeypatch.setattr(table, "controls", lambda *args: [])
    monkeypatch.setattr(
        table, "wait_until", lambda predicate, **kwargs: any(predicate() for _ in range(3)),
    )
    assert table.search_grid(object(), "Marta", sparse=True) is rendered


def test_street_write_prefers_exact_named_edit(monkeypatch):
    def field(kind, name):
        c = Mock()
        c.element_info.control_type = kind
        c.window_text.return_value = name
        return c

    additional = field("Edit", "additional name")
    street = field("Edit", "Street")
    street.get_value.return_value = "Friedrichstrasse 88"
    form = Mock(descendants=Mock(return_value=[additional, field("Text", "Street"), street]))
    write = Mock()
    monkeypatch.setattr(debtor, "write_edit", write)
    monkeypatch.setattr(debtor, "_fill_pair", Mock())
    debtor._fill_main_address(form, {"street": "Friedrichstrasse 88"})
    write.assert_called_once_with(street, "Friedrichstrasse 88")


def test_copied_customer_number_restores_clipboard_on_ole_thread(monkeypatch):
    import pythoncom
    import win32clipboard

    caller = get_ident()
    events = []
    original = object()
    monkeypatch.setattr(pythoncom, "OleInitialize", lambda: events.append(("init", get_ident())))
    monkeypatch.setattr(pythoncom, "OleGetClipboard", lambda: original)
    monkeypatch.setattr(pythoncom, "OleSetClipboard", lambda data: events.append((data, get_ident())))
    monkeypatch.setattr(pythoncom, "OleFlushClipboard", lambda: events.append(("flush", get_ident())))
    monkeypatch.setattr(win32clipboard, "GetClipboardSequenceNumber", lambda: 27)
    monkeypatch.setattr(table, "windll", SimpleNamespace(
        ole32=SimpleNamespace(OleUninitialize=lambda: events.append(("done", get_ident())))
    ))
    grid = object.__new__(table.Grid)
    grid._copy_selected_text = Mock(return_value=("CUST000007", 27))

    assert grid.copy_cell(0, 1) == "CUST000007"
    worker = events[0][1]
    assert worker != caller
    assert events == [("init", worker), (original, worker), ("flush", worker), ("done", worker)]


@pytest.mark.parametrize("rows, customer_number, padded_read, accepted", [
    ([19, 39], "CUSTOOOOO9", None, True),
    ([19, 39], "USTOOOOO9", "CUSTOOOOO 9", True),
    ([19, 39], "CUST000007", "CUST000007", False),
    ([19, 39, 59], "CUST000009", None, False),
    ([19], "", None, False),
])
def test_saved_customer_search_confirms_only_one_matching_id(monkeypatch, rows, customer_number, padded_read, accepted):
    window, dialog = object(), object()
    actions = Mock()
    grid = SimpleNamespace(
        rows=rows,
        cols=list(range(8)),
        ink=lambda row, col: col == 1,
        read=lambda row, col: customer_number,
        read_cell=Mock(return_value=padded_read),
        click=actions.click,
    )
    monkeypatch.setattr(debtor, "_select_address_dialog", Mock(side_effect=[dialog, None]))
    monkeypatch.setattr(debtor, "visible_descendants", lambda _: [SimpleNamespace(
        element_info=SimpleNamespace(control_type="Edit"),
    )])
    monkeypatch.setattr(debtor, "write_edit", Mock())
    search = Mock(return_value=grid)
    monkeypatch.setattr(debtor, "search_grid", search)
    monkeypatch.setattr(debtor, "_dialog_button", actions.button)
    monkeypatch.setattr(debtor, "snapshot", Mock())
    if accepted:
        assert debtor.select_existing_debtor(window, {"first_name": "Marta"},
                                             customer_id="CUST000009") == "selected"
        assert actions.mock_calls == [call.click(0, 1), call.button(dialog, "OK")]
    else:
        with pytest.raises(debtor.UIAMappingError, match="Could not verify one address row"):
            debtor.select_existing_debtor(window, {"first_name": "Marta"}, customer_id="CUST000009")
        assert actions.mock_calls == []
    search.assert_called_once_with(dialog, "CUST000009", sparse=True)
    assert grid.read_cell.call_count == int(padded_read is not None)


@pytest.mark.parametrize("rows, expected_calls, outcome", [
    ([19], [call.button("selector", "Cancel")], "not_found"),
    ([19, 39], [call.click(0, 1), call.button("selector", "OK")], "selected"),
    ([19, 39, 59], [], None),
])
def test_initial_lookup_uses_first_name_result_count(monkeypatch, rows, expected_calls, outcome):
    actions = Mock()
    grid = SimpleNamespace(rows=rows, click=actions.click)  # No OCR or clipboard methods.
    search = Mock(return_value=grid)
    monkeypatch.setattr(debtor, "_select_address_dialog", Mock(side_effect=["selector", None]))
    monkeypatch.setattr(debtor, "search_grid", search)
    monkeypatch.setattr(debtor, "_dialog_button", actions.button)
    monkeypatch.setattr(debtor, "snapshot", Mock())
    source = {"first_name": "Marta", "company": "Northstar Office GmbH"}
    if outcome is None:
        with pytest.raises(debtor.UIAMappingError, match="2 rows returned"):
            debtor.select_existing_debtor(object(), source)
    else:
        assert debtor.select_existing_debtor(object(), source) == outcome
    search.assert_called_once_with("selector", "Marta", sparse=True)
    assert actions.mock_calls == expected_calls
