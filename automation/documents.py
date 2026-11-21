"""Read and verify persisted Fakturama document records."""

from datetime import date, datetime
from decimal import InvalidOperation
import re

from .fakturama_client import snapshot, wait_until
from .table import amount, controls, identity, norm, one, search_grid
from .vat import open_data_view


def display_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = " ".join(str(value or "").split())
    for pattern in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y",
                    "%d.%m.%Y", "%m %d, %Y"):
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"Could not read document date {value!r}")


def document_editor(window, title):
    if not wait_until(lambda: len(controls(window, "Pane", title)) == 1, timeout=7):
        raise RuntimeError(f"Fakturama did not expose the {title!r} editor")
    return one(controls(window, "Pane", title), f"{title} editor")


def header_edit(editor, label):
    named = controls(editor, "Edit", label)
    if named:
        return one(named, label)
    anchor = one(controls(editor, "Text", label), f"{label} label").rectangle()
    hits = []
    for edit in controls(editor, "Edit"):
        rect = edit.rectangle()
        if anchor.right - 5 <= rect.left <= anchor.right + 110 and rect.top < anchor.bottom and rect.bottom > anchor.top:
            hits.append((rect.left, edit))
    if not hits:
        raise RuntimeError(f"Could not locate the document {label!r} field")
    return min(hits, key=lambda hit: hit[0])[1]


def header_value(editor, label):
    return str(header_edit(editor, label).get_value() or "").strip()


def editor_for_number(window, number):
    active = controls(window, "Pane", number)
    if not active:
        matches = [tab for tab in controls(window, "TabItem")
                   if norm(tab.window_text().lstrip("*")) == norm(number)]
        one(matches, f"saved document tab {number}").click_input()
    pane = document_editor(window, number)
    if header_value(pane, "No.") != number:
        raise RuntimeError(f"Active document is not {number!r}")
    return pane


def _document_number_matches(value, number):
    seen = identity(value).replace(" ", "")
    expected = identity(number)
    if seen == expected:
        return True
    suffix = re.fullmatch(r"(.+?)([0-9]+)", expected)
    if not suffix or len(seen) != len(expected):
        return False
    prefix, digits = suffix.groups()
    # OCR may read the O in the PO prefix as zero. The editor's No. field
    # below provides the exact identity after the candidate row is opened.
    return (all(a == b or (a == "0" and b == "o") for a, b in zip(seen, prefix))
            and seen[len(prefix):].translate(str.maketrans("oil", "011")) == digits)


def _wide_documents(window):
    view = open_data_view(window, "Documents")
    outer = window.rectangle()
    # The minimized fast view truncates every column. Restore the docked view
    # first, then maximize its tab if the dock does not give the table room.
    if view.rectangle().width() < outer.width() * 0.55:
        restore = controls(view.parent(), "Button", "Restore")
        if restore:
            one(restore, "Documents Restore button").click_input()
        if not wait_until(lambda: any(p.rectangle().width() >= outer.width() * 0.55
                                      for p in controls(window, "Pane", "Documents")), timeout=4):
            raise RuntimeError("Expand the Documents view to verify the record")
        view = one(controls(window, "Pane", "Documents"), "Documents view")
    maximized_here = view.rectangle().height() < outer.height() * 0.55
    if maximized_here:
        one(controls(window, "TabItem", "Documents"), "Documents tab").double_click_input()
        if not wait_until(lambda: any(p.rectangle().height() >= outer.height() * 0.55
                                      for p in controls(window, "Pane", "Documents")), timeout=4):
            raise RuntimeError("Could not maximize Documents for verification")
        view = one(controls(window, "Pane", "Documents"), "Documents view")
    return view


def verify_saved_row(window, number, expected_date, reference, total, states, *, category):
    """Confirm a unique Documents row and cross-check the saved editor."""
    try:
        view = _wide_documents(window)
        # The first two entries are contextual transaction/contact filters.
        # Fakturama exposes either of them as "---" when it has no context.
        one(controls(view, "TreeItem", category), f"Documents category {category!r}").click_input()
        grid = search_grid(view, number, order_grid=True)
        # Observed Documents columns: icon, Document, Date, Name, Cust.Ref.,
        # State, Total, Printed. The expanded view exposes all required cells.
        if len(grid.cols) < 8:
            raise RuntimeError("Documents did not expose the Date, Cust.Ref., State, and Total columns")
        numbers = [grid.read(r, 1) for r in range(len(grid.rows) - 1)]
        rows = [r for r, value in enumerate(numbers) if _document_number_matches(value, number)]
        if not rows:
            raise RuntimeError(f"Documents > {category} did not show {number!r}. "
                               f"Read document numbers: {[value for value in numbers if value]}.")
        row = one(rows, f"Documents row for {number}")
        recorded_date = display_date(grid.read(row, 2))
        visible_ref = grid.read(row, 4).strip()
        ref_matches = identity(visible_ref) == identity(reference)
        if not ref_matches and visible_ref.endswith(("...", "…", "..")):
            prefix = identity(visible_ref.rstrip(".…").strip())
            ref_matches = len(prefix) >= 5 and identity(reference).startswith(prefix)
        state_text = norm(grid.read(row, 5))
        if not state_text:
            state_text = norm(grid.read_cell(row, 5))
        state = next((s for s in states if state_text == s), None)
        try:
            recorded_total = amount(grid.read(row, 6))
        except InvalidOperation:
            recorded_total = None
        if recorded_date != expected_date or not ref_matches or state is None or recorded_total != total:
            raise RuntimeError(
                f"Documents row {number!r} differs from the expected date, reference, state, or total. "
                f"Read {recorded_date}, {visible_ref!r}, {state_text!r}, {recorded_total!r}."
            )
        grid.click(row, 1, double=True)
        editor = document_editor(window, number)
        if header_value(editor, "No.") != number:
            raise RuntimeError(f"The opened Documents row does not have the exact number {number!r}")
        if header_value(editor, "Cust.Ref.") != reference:
            raise RuntimeError(f"Saved editor {number!r} does not show the full expected Cust.Ref.")
        if display_date(header_value(editor, "Date")) != expected_date:
            raise RuntimeError(f"Saved editor {number!r} does not show the expected Date.")
        if amount(header_value(editor, "Total")) != total:
            raise RuntimeError(f"Saved editor {number!r} does not show the expected Total.")
    except (RuntimeError, ValueError):
        snapshot(window, "document_verification_review")
        raise
    print(f"[UIA] Verified {number} in Documents: {state}, total {total:.2f}")
    return editor
