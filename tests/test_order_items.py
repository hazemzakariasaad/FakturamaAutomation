from _ctypes import COMError
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from automation import product, table, vat


def item(sku="CHR-ERG-01", **overrides):
    return dict(
        dict(sku=sku, description="Ergonomic Desk Chair", quantity="2.00",
             unit_net_price="250.00", vat_percent="19.00", discount_percent="10.00",
             source_line_net_total="450.00"),
        **overrides,
    )


def test_product_master_price_excludes_line_discount():
    prepared = product.prepare_items([item()])
    _, _, _, _, _, gross, net = prepared[0]
    assert gross == Decimal("297.50")
    assert net == Decimal("450.00")


def test_currency_edit_is_on_its_label_row(monkeypatch):
    def field(left, top, right, bottom):
        return Mock(rectangle=Mock(return_value=SimpleNamespace(
            left=left, top=top, right=right, bottom=bottom,
        )))

    label = field(300, 362, 374, 375)
    description = field(370, 285, 800, 353)
    gross = field(385, 357, 491, 378)
    cost = field(370, 382, 476, 402)
    monkeypatch.setattr(product, "controls", lambda root, kind, name=None: (
        [label] if kind == "Text" else [] if name else [description, gross, cost]
    ))
    assert product._product_edit(object(), "Price (gross)") is gross


@pytest.mark.parametrize("gross, readback, attempts, saves", [
    ("297.50", "$297.50", 1, True),
    ("47.60", "$47.60", 2, True),
    ("47.60", "$0.00", 2, False),
])
def test_product_save_requires_price_readback(monkeypatch, gross, readback, attempts, saves):
    from unittest.mock import call

    window, draft = object(), object()
    source = item()
    text = {key: Mock(get_value=Mock(return_value=value)) for key, value in {
        "Item Number": source["sku"], "Name": source["description"], "Description": source["description"],
    }.items()}
    price = Mock()
    price.get_value.side_effect = lambda: readback if price.type_keys.call_count >= attempts else "$0.00"
    text.update({"Price (gross)": price, "cost price (net)": Mock(get_value=Mock(return_value="$0.00")),
                 "Stock": Mock(get_value=Mock(return_value="0.00"))})
    saved = Mock()
    monkeypatch.setattr(product, "controls", lambda root, kind, label: [draft] if kind == "Pane" else [Mock()])
    monkeypatch.setattr(product, "_product_edit", lambda editor, label: text[label])
    monkeypatch.setattr(product, "write_edit", Mock())
    monkeypatch.setattr(product, "combo_for_label", Mock())
    monkeypatch.setattr(product, "select_combo", Mock())
    monkeypatch.setattr(product, "_combo_value", lambda _: "VAT 19%")
    monkeypatch.setattr(product, "wait_until", lambda predicate, **_: bool(predicate()))
    monkeypatch.setattr(product, "click_toolbar_save", saved)
    if saves:
        product._create_product(window, source, "VAT 19%", Decimal(gross))
        saved.assert_called_once_with(window)
    else:
        with pytest.raises(RuntimeError, match="read back as '\\$0.00'"):
            product._create_product(window, source, "VAT 19%", Decimal(gross))
        saved.assert_not_called()
    assert price.type_keys.call_args_list == [call(f"^a{gross}{{TAB}}", set_foreground=False)] * attempts
    assert price.click_input.call_count == attempts
    text["cost price (net)"].type_keys.assert_not_called()
    text["Stock"].type_keys.assert_not_called()


@pytest.mark.parametrize("results_has_scrollbar", [False, True])
def test_documents_search_uses_results_beside_scrollable_category_tree(monkeypatch, results_has_scrollbar):
    from pywinauto.win32structures import RECT

    def control(kind, bounds):
        return Mock(element_info=SimpleNamespace(control_type=kind), rectangle=Mock(return_value=RECT(*bounds)))

    search = control("Edit", (1471, 272, 1621, 298))
    tree = control("Tree", (476, 267, 651, 980))
    results = control("Pane", (659, 308, 1634, 980))
    bars = [SimpleNamespace(parent=lambda: tree)]
    if results_has_scrollbar:
        bars.append(SimpleNamespace(parent=lambda: results))
    monkeypatch.setattr(table, "controls", lambda _, kind, *args: bars if kind == "ScrollBar" else [results])
    assert table.grid_below(object(), search) is results


def test_items_canvas_beside_label_without_a_horizontal_scrollbar(monkeypatch):
    from pywinauto.win32structures import RECT

    def control(kind, bounds):
        return SimpleNamespace(element_info=SimpleNamespace(control_type=kind),
                               rectangle=lambda: RECT(*bounds))

    label = control("Text", (429, 440, 465, 460))
    gutter = control("Pane", (429, 440, 465, 568))
    border = control("Pane", (473, 440, 1684, 702))
    canvas = control("Pane", (475, 442, 1674, 700))
    monkeypatch.setattr(table, "controls", lambda _, kind, *args: [gutter, border, canvas] if kind == "Pane" else [])
    assert table.grid_below(object(), label) is canvas


def test_search_reacquires_dialog_after_swt_replaces_search_edit(monkeypatch):
    from PIL import Image

    first, refreshed = object(), object()
    edits = Mock(side_effect=[object(), object()])
    pane = object()
    stale = COMError(-2147220991, "An event was unable to invoke any of the subscribers", None)
    layouts = Mock(side_effect=[stale, pane])
    image = SimpleNamespace(image=Image.new("RGB", (50, 50), "white"))
    monkeypatch.setattr(table, "search", edits)
    monkeypatch.setattr(table, "grid_below", layouts)
    monkeypatch.setattr(table, "Grid", Mock(return_value=image))
    monkeypatch.setattr(table, "controls", lambda *_: [])
    monkeypatch.setattr(table, "wait_until", lambda predicate, **_: any(predicate() for _ in range(3)))
    assert table.search_grid(first, "CHR-ERG-01", refresh_root=Mock(side_effect=[first, refreshed])) is image
    assert [entry.args for entry in edits.call_args_list] == [(first, "CHR-ERG-01"), (refreshed, "CHR-ERG-01")]


@pytest.mark.parametrize("values, uses_keyboard, accepted", [
    (["", "", "VAT 19%"], False, True),
    (["", "", "", "VAT 19%"], True, True),
    (["VAT 9%"] * 6, True, False),
])
def test_search_waits_for_vat_readback_then_types_literal_percent(monkeypatch, values, uses_keyboard, accepted):
    from unittest.mock import call

    edit = Mock()
    edit.get_value.side_effect = values
    monkeypatch.setattr(table, "controls", lambda *_: [object()])
    monkeypatch.setattr(table, "visible_descendants", lambda _: [])
    monkeypatch.setattr(table, "control_right_of", lambda *_args, **_kwargs: edit)
    monkeypatch.setattr(table, "write_edit", Mock())
    monkeypatch.setattr(table, "wait_until", lambda predicate, **_: any(predicate() for _ in range(2)))

    if accepted:
        assert table.search(object(), "VAT 19%") is edit
    else:
        with pytest.raises(RuntimeError, match="Read 'VAT 9%'"):
            table.search(object(), "VAT 19%")
    if uses_keyboard:
        assert edit.type_keys.call_args_list == [call("^a"), call("VAT 19{%}", with_spaces=True)]
    else:
        edit.type_keys.assert_not_called()


@pytest.mark.parametrize("net_after, editor_before, editor_after, inserted", [
    ("250", None, None, True),
    ("0", None, 661904, True),
    ("0", 661904, 661904, False),
    ("0", None, None, False),
])
def test_exact_sku_can_close_picker_and_insert_without_ok(monkeypatch, net_after, editor_before, editor_after, inserted):
    order, dialog = object(), object()
    label = Mock(rectangle=Mock(return_value=SimpleNamespace(left=0, bottom=15)))
    icon = Mock(rectangle=Mock(return_value=SimpleNamespace(left=5, top=20, width=lambda: 16)))

    def controls(root, kind, name=None):
        return {"Text": [label], "Image": [icon]}[kind]

    monkeypatch.setattr(product, "_active_order", lambda _: order)
    monkeypatch.setattr(product, "_total_net", Mock(side_effect=[Decimal("0"), Decimal(net_after)]))
    monkeypatch.setattr(product, "_inline_item_editor", Mock(side_effect=[
        None if handle is None else SimpleNamespace(handle=handle)
        for handle in (editor_before, editor_after)
    ]))
    monkeypatch.setattr(product, "controls", controls)
    monkeypatch.setattr(product, "_select_dialog", Mock(side_effect=[dialog, dialog, None]))
    monkeypatch.setattr(product, "wait_until", lambda predicate, **_: bool(predicate()))
    monkeypatch.setattr(product, "filtered_grid", Mock(side_effect=RuntimeError("Search view closed")))
    if inserted:
        assert product._pick_product(object(), "CHR-ERG-01")
    else:
        with pytest.raises(RuntimeError, match="Search view closed"):
            product._pick_product(object(), "CHR-ERG-01")
    icon.click_input.assert_called_once()


def test_commit_goes_to_the_order_item_editor(monkeypatch):
    edit = Mock()
    order = object()
    monkeypatch.setattr(product, "_inline_item_editor", lambda active: edit if active is order else None)
    product._commit_item_editor(order)
    edit.type_keys.assert_called_once_with("{ENTER}")


@pytest.mark.parametrize("obscured_desktop", [False, True])
def test_order_grid_recovers_rows_when_selection_hides_header_rules(monkeypatch, obscured_desktop):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (550, 250), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 1, 549, 20), fill="#a2a2a2")
    draw.rectangle((0, 21, 549, 41), fill="#0079d6")
    draw.text((175, 26), "CHR-ERG-01", fill="white")
    for y in [62, 82, 103, 123, 144, 164, 185, 205, 226]:
        draw.line((0, y, 549, y), fill="#bfbfbf")
    for x in [40, 150, 250, 350, 450]:
        draw.line((x, 62, x, 226), fill="#bfbfbf")
    # Scrollbar and selected row would throw off the old first-rule anchor.
    draw.rectangle((0, 231, 549, 249), fill="#a2a2a2")
    pane = Mock(handle=123, rectangle=Mock(return_value=SimpleNamespace(left=2499, top=50, right=3049, bottom=300)))
    bar = Mock(rectangle=Mock(return_value=SimpleNamespace(top=281)))
    captures = [Image.new("RGB", image.size), image] if obscured_desktop else [image]
    grab = Mock(side_effect=captures)
    monkeypatch.setattr(table.ImageGrab, "grab", grab)
    monkeypatch.setattr(table, "controls", lambda pane, kind, name: [bar] if name == "Horizontal" else [])

    grid = table.Grid(pane, order_grid=True)

    assert all(abs(actual - expected) <= 1 for actual, expected in zip(grid.rows[:4], [21, 41, 62, 82]))
    assert grid.cols == [0, 40, 150, 250, 350, 450, 550]
    assert grid.ink(0, 2)
    assert not grid.ink(1, 2)
    assert grid.starts_at_top
    if obscured_desktop:
        assert grab.call_args.kwargs == {"window": 123}


def test_scaled_native_grid_maps_sku_and_click_to_client_area(monkeypatch):
    from PIL import Image, ImageDraw
    from pywinauto.win32structures import RECT

    pane = Mock(handle=123, rectangle=Mock(return_value=RECT(2446, 378, 3415, 588)))
    native = Image.new("RGB", (1207, 258), "white")
    draw = ImageDraw.Draw(native)
    for y in range(24, 258, 25):
        draw.line((0, y, 1206, y), fill="#bfbfbf")
    for x in range(49, 1207, 125):
        draw.line((x, 0, x, 257), fill="#bfbfbf")
    draw.text((185, 30), "MAT-DESK-02", fill="black")
    monkeypatch.setattr(table, "controls", lambda *_: [])
    monkeypatch.setattr(table.ImageGrab, "grab", Mock(side_effect=[Image.new("RGB", (969, 210)), native]))
    monkeypatch.setattr(table.win32gui, "GetClientRect", lambda _: (0, 0, 965, 206))
    monkeypatch.setattr(table.win32gui, "ClientToScreen", lambda _, pt: (2447 + pt[0], 379 + pt[1]))
    grid = table.Grid(pane, order_grid=True)
    assert grid.bounds(0, 2) == (140, 20, 240, 40)
    assert grid.ink(0, 2)
    assert not grid.ink(1, 2)
    grid.click(0, 2)
    pane.click_input.assert_called_once_with(coords=(190, 30))


@pytest.mark.parametrize("occupied", [True, False])
def test_empty_order_waits_for_readable_grid_and_checks_sku(monkeypatch, occupied):
    grid = Mock(rows=[20, 40, 60])
    grid.ink.side_effect = lambda row, col: occupied and row == 0 and col == 2
    capture = Mock(side_effect=[table.GridLayoutError("not painted"), grid])
    monkeypatch.setattr(product, "_active_order", Mock())
    monkeypatch.setattr(product, "_items_grid", capture)
    if occupied:
        with pytest.raises(RuntimeError, match="already has items"):
            product.require_empty_items(object())
    else:
        product.require_empty_items(object())
    assert capture.call_count == 2


def test_order_grid_rejects_unverified_row_layout():
    from PIL import Image

    with pytest.raises(table.GridLayoutError):
        table._order_row_layout(Image.new("L", (550, 250), 255))


@pytest.mark.parametrize("readback, saves", [("19%", True), ("0%", False)])
def test_new_vat_types_percent_and_saves_only_if_verified(monkeypatch, readback, saves):
    from unittest.mock import call

    window, view, editor = object(), object(), object()
    rate = Decimal("19.00")
    create_button, name, value, code = Mock(), Mock(), Mock(), Mock()
    name.get_value.return_value = "VAT 19%"
    value.get_value.side_effect = ["0%", readback]
    saved = Mock()
    monkeypatch.setattr(vat, "open_data_view", lambda *_: view)
    monkeypatch.setattr(vat, "filtered_grid", lambda *_: (None, None))
    monkeypatch.setattr(vat, "controls", lambda root, kind, label: [{
        "Create a new tax rate": create_button, "New TAX Rate": editor, "Name": name, "Value": value,
    }[label]])
    monkeypatch.setattr(vat, "_fill", Mock())
    monkeypatch.setattr(vat, "combo_for_label", lambda *_: code)
    monkeypatch.setattr(vat, "select_combo", Mock())
    monkeypatch.setattr(vat, "_combo_value", lambda _: vat.STANDARD)
    monkeypatch.setattr(vat, "click_toolbar_save", saved)

    if saves:
        assert vat.ensure_vat(window, rate) == "VAT 19%"
        saved.assert_called_once_with(window)
    else:
        with pytest.raises(RuntimeError, match="Read name 'VAT 19%', value '0%'"):
            vat.ensure_vat(window, rate)
        saved.assert_not_called()
    assert value.type_keys.call_args_list == [call("^a"), call("19{%}"), call("{TAB}")]


def test_source_total_mismatch_stops_before_ui():
    with pytest.raises(ValueError, match="calculated price is 450.00"):
        product.prepare_items([item(source_line_net_total="500.00")])


@pytest.mark.parametrize("values, expected", [
    ([], None),
    (["CHR-ERG-01"], 0),
    (["CHR - ERG - 01"], 0),
    (["CHR-ERG-01", "CHR-ERG-01"], "Multiple exact matches"),
    (["CHR-ERG-OI"], 0),
    ([""], "Unreadable result"),
])
def test_filtered_grid_distinguishes_match_empty_and_conflict(monkeypatch, values, expected):
    from PIL import Image

    grid = Mock()
    grid.rows = [20 * i + 20 for i in range(len(values) + 1)]
    grid.cols = [0, 100]
    grid.image = Image.new("RGB", (100, 100), "white")
    grid.ink.return_value = True
    grid.read.side_effect = values
    grid.copy_cell.return_value = "CHR-ERG-01" if values == ["CHR-ERG-OI"] else None
    monkeypatch.setattr(table, "search_grid", lambda *args: grid)
    monkeypatch.setattr(table, "_words", lambda img: [{"text": "Item No."}])
    if isinstance(expected, str):
        with pytest.raises(RuntimeError, match=expected):
            table.filtered_grid(object(), "CHR-ERG-01", "Item No.", 0)
    else:
        assert table.filtered_grid(object(), "CHR-ERG-01", "Item No.", 0) == (grid, expected)


def test_line_vat_uses_visible_rate_when_inline_editor_is_empty(monkeypatch):
    edit = Mock(get_value=Mock(return_value=""))
    grid = Mock(read_cell=Mock(return_value="VAT (19.0%)"))
    monkeypatch.setattr(product, "_cell_editor", Mock(return_value=edit))
    lookup = Mock()
    monkeypatch.setattr(product, "controls", lookup)
    product._set_line_vat(object(), object(), grid, 0, Decimal("19"), "VAT 19%")
    grid.read_cell.assert_called_once_with(0, 6)
    edit.type_keys.assert_called_once_with("{ESC}")
    lookup.assert_not_called()


def test_row_verification_rejects_wrong_sku_even_when_row_number_missing():
    grid = Mock(starts_at_top=True, rows=[25, 50, 75], cols=[0, 50, 175, 300])
    grid.read.return_value = "MAT-DESK-02"
    grid.matches.return_value = False
    with pytest.raises(RuntimeError, match="expected 'CHR-ERG-01'"):
        product._line_row(grid, 1, "CHR-ERG-01")


def test_scrolled_order_requires_both_position_and_sku():
    grid = Mock(starts_at_top=False, rows=[25, 50, 75], cols=[0, 50, 175, 300])
    cells = {(0, 0): "4", (1, 0): "5", (1, 2): "MAT-DESK-02"}
    grid.read.side_effect = lambda row, col: cells[row, col]
    assert product._line_row(grid, 5, "MAT-DESK-02") == 1


@pytest.mark.parametrize("obscured_second_line", [False, True])
def test_two_lines_reuse_first_create_and_reselect_second(monkeypatch, obscured_second_line):
    actions = Mock()
    actions.pick.side_effect = [True, False, True]
    actions.vat.return_value = "VAT 19%"
    actions.total.side_effect = map(Decimal, ["0", "450", "450", "570"])
    grid = Mock(starts_at_top=True, rows=[25, 50, 75], cols=[0, 50, 175, 300])
    # The selected row number isn't readable. The SKU still identifies the row.
    grid.read.side_effect = lambda row, col: ["CHR-ERG-OI", "MAT-DESK-02"][row] if col == 2 else ""
    obscured = Mock(starts_at_top=True, rows=grid.rows, cols=grid.cols)
    obscured.read.return_value = "all products 102"
    captures = [grid, obscured, grid] if obscured_second_line else [grid, grid]
    capture = Mock(side_effect=captures)
    move = Mock()
    values = ["2", "250", "-10.00%", "3", "40", "0"]
    actions.editor.side_effect = [Mock(get_value=Mock(return_value=value)) for value in values]
    window, order = Mock(), Mock(rectangle=Mock(return_value=SimpleNamespace(left=500, top=250)))
    order_key = (42, 100)
    monkeypatch.setattr(product, "active_order_key", lambda w: order_key)
    monkeypatch.setattr(product, "_active_order", lambda w: order)
    monkeypatch.setattr(product, "_pick_product", actions.pick)
    monkeypatch.setattr(product, "ensure_vat", actions.vat)
    monkeypatch.setattr(product, "_create_product", actions.create)
    monkeypatch.setattr(product, "return_to_original_order", actions.back)
    monkeypatch.setattr(product, "_total_net", actions.total)
    monkeypatch.setattr(product, "_commit_item_editor", Mock())
    monkeypatch.setattr(product.mouse, "move", move)
    monkeypatch.setattr(product, "_items_grid", capture)
    monkeypatch.setattr(product, "_set_line_vat", actions.line_vat)
    monkeypatch.setattr(product, "_set_cell", actions.write)
    monkeypatch.setattr(product, "_cell_editor", actions.editor)
    source = [item(), item("MAT-DESK-02", description="Anti-Fatigue Desk Mat", quantity="3",
                           unit_net_price="40", discount_percent="0", source_line_net_total="120")]

    product.fill_order_items(window, product.prepare_items(source))

    assert [call.args[-1] for call in actions.pick.call_args_list] == [
        "CHR-ERG-01", "MAT-DESK-02", "MAT-DESK-02",
    ]
    actions.create.assert_called_once_with(window, source[1], "VAT 19%", Decimal("47.60"))
    actions.back.assert_called_once_with(window, order_key)
    assert [call.args[2] for call in actions.write.call_args_list] == [0, 0, 0, 1, 1, 1]
    assert all(call.args[1] is grid for call in actions.write.call_args_list)
    assert move.call_count == 2
    assert capture.call_count == len(captures)
    obscured.matches.assert_not_called()
