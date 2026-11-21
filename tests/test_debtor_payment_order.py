from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from automation import debtor, order as order_ui, payment


@pytest.mark.parametrize("dialog_visible", [False, True])
def test_payment_conflict_checks_only_visible_fakturama_dialogs(monkeypatch, dialog_visible):
    workbench = Mock(handle=1, element_info=SimpleNamespace(process_id=42))
    workbench.window_text.return_value = "Fakturama duplicate tab text"
    dialog = Mock(handle=2)
    dialog.rectangle.return_value = SimpleNamespace(width=lambda: 350, height=lambda: 180)
    dialog.is_visible.return_value = dialog_visible
    dialog.window_text.return_value = "Bank Transfer already exists"
    desktop = Mock()
    desktop.windows.return_value = [workbench, dialog]
    monkeypatch.setattr(payment, "Desktop", lambda **_: desktop)

    assert payment._conflict_dialog_open(workbench) is dialog_visible
    desktop.windows.assert_called_once_with(process=42)


@pytest.fixture
def flow(monkeypatch):
    order = {"debtor": {"company": "Northstar"}, "payment": {"method": "Bank Transfer"}}
    window, order_key = object(), (42, 100)
    actions = Mock()
    actions.lookup.side_effect = ["not_found", "selected"]
    actions.create.return_value = "CUST000007"
    for name, action in {
        "select_existing_debtor": actions.lookup,
        "ensure_payment_method": actions.check,
        "return_to_original_order": actions.back,
        "create_debtor": actions.create,
        "_verify_order_addresses": actions.verify,
        "active_order_key": lambda _: order_key,
    }.items():
        monkeypatch.setattr(debtor, name, action)
    return window, order_key, order, actions


def test_check_payment_before_opening_new_debtor(flow):
    window, order_key, order, actions = flow
    assert debtor.resolve_debtor(window, order) == "created"
    assert actions.mock_calls == [
        call.lookup(window, order["debtor"]),
        call.check(window, "Bank Transfer", return_to_order=False),
        call.back(window, order_key),
        call.create(window, order["debtor"], order["payment"]),
        call.back(window, order_key),
        call.lookup(window, order["debtor"], customer_id="CUST000007"),
        call.verify(window, order["debtor"]),
    ]


def test_failed_payment_check_does_not_open_new_debtor(flow):
    window, _, order, actions = flow
    actions.check.side_effect = payment.PaymentError("Payment match is ambiguous")
    with pytest.raises(payment.PaymentError, match="ambiguous"):
        debtor.resolve_debtor(window, order)
    actions.create.assert_not_called()


def test_existing_debtor_skips_payment_setup(flow):
    window, _, order, actions = flow
    actions.lookup.side_effect = ["selected"]
    assert debtor.resolve_debtor(window, order) == "selected"
    actions.check.assert_not_called()
    actions.create.assert_not_called()


@pytest.mark.parametrize("original_available", [True, False])
def test_return_checks_draft_identity_after_tabs_move(monkeypatch, original_available):
    window = object()
    panes = []
    target = SimpleNamespace(element_info=SimpleNamespace(runtime_id=(42, 100)))
    other = SimpleNamespace(element_info=SimpleNamespace(runtime_id=(42, 101)))
    original_tab = Mock(window_text=Mock(return_value="*New Order"))
    other_tab = Mock(window_text=Mock(return_value="*New Order"))
    payment_tab = Mock(window_text=Mock(return_value="Bank Transfer"))
    def activate(pane):
        panes[:] = [pane]

    original_tab.click_input.side_effect = lambda: activate(target)
    other_tab.click_input.side_effect = lambda: activate(other)
    fresh_tabs = [payment_tab, original_tab, other_tab] if original_available else [payment_tab, other_tab]
    monkeypatch.setattr(order_ui, "table_controls", lambda _, kind, *args: panes if kind == "Pane" else fresh_tabs)
    monkeypatch.setattr(order_ui, "wait_until", lambda predicate, **_: predicate())
    snapshot = Mock()
    monkeypatch.setattr(order_ui, "snapshot", snapshot)

    if original_available:
        order_ui.return_to_original_order(window, (42, 100))
        original_tab.click_input.assert_called_once_with()
        snapshot.assert_not_called()
    else:
        with pytest.raises(RuntimeError, match="original Order"):
            order_ui.return_to_original_order(window, (42, 100))
        snapshot.assert_called_once_with(window, "order_return_review")
    other_tab.click_input.assert_called_once_with()
    payment_tab.click_input.assert_not_called()
