"""Find Fakturama controls by visible label. SWT automation ids change every launch."""

from __future__ import annotations

import time


def _text(control) -> str:
    try:
        return (control.window_text() or "").strip()
    except Exception:
        return ""


def visible_descendants(root):
    found = []
    for control in root.descendants():
        try:
            if control.is_visible():
                found.append(control)
        except Exception:
            continue
    return found


def find_exact(controls, name: str, control_type: str | None = None) -> list:
    wanted = name.casefold()
    matches = []
    for control in controls:
        if _text(control).casefold() != wanted:
            continue
        if control_type and control.element_info.control_type != control_type:
            continue
        matches.append(control)
    return matches


def find_button(controls, name: str) -> list:
    wanted = name.casefold()
    return [
        control
        for control in controls
        if control.element_info.control_type in {"Button", "SplitButton", "MenuItem"}
        and _text(control).casefold() == wanted
        and control.is_enabled()
    ]


def label_texts(controls, names: list[str], *, contains: bool = False) -> list:
    if contains:
        matches = []
        for control in controls:
            if control.element_info.control_type not in {"Text", "Static"}:
                continue
            text = _text(control).casefold()
            if not text:
                continue
            for name in names:
                wanted = name.casefold()
                if wanted == "city" and text == "district":
                    continue
                if wanted == "phone" and text not in {"telephone", "phone", "2nd phone"}:
                    if "phone" in text and text != wanted:
                        continue
                if wanted in text or text == wanted:
                    matches.append(control)
                    break
        return matches
    wanted = {name.casefold() for name in names}
    return [
        control
        for control in controls
        if control.element_info.control_type in {"Text", "Static"} and _text(control).casefold() in wanted
    ]


def _vertically_near(label_rect, other_rect, slack: int = 12) -> bool:
    return not (other_rect.top > label_rect.bottom + slack or other_rect.bottom < label_rect.top - slack)


def control_right_of(label, controls, control_types: set[str], max_dx: int = 420):
    """Nearest Edit/ComboBox to the right of a label, on the same row."""
    label_rect = label.rectangle()
    ranked = []
    for control in controls:
        if control.element_info.control_type not in control_types:
            continue
        rect = control.rectangle()
        if not _vertically_near(label_rect, rect):
            continue
        dx = rect.left - label_rect.right
        if dx < -8 or dx > max_dx:
            continue
        ranked.append((dx, rect.top, control))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return ranked[0][2] if ranked else None


def edit_for_label(root, label_names: list[str]):
    controls = visible_descendants(root)
    labels = label_texts(controls, label_names)
    if len(labels) != 1:
        labels = label_texts(controls, label_names, contains=True)
    if len(labels) > 1:
        # Prefer the shortest label (e.g. "Name" vs "Company name").
        labels.sort(key=lambda control: len(_text(control)))
        labels = [labels[0]]
    if len(labels) != 1:
        for name in label_names:
            named = [
                control
                for control in controls
                if control.element_info.control_type == "Edit" and _text(control).casefold() == name.casefold()
            ]
            if len(named) == 1:
                return named[0]
            named = [
                control
                for control in controls
                if control.element_info.control_type == "Edit" and name.casefold() in _text(control).casefold()
            ]
            if len(named) == 1:
                return named[0]
        raise RuntimeError(
            f"Expected one label {label_names}, found {len(labels)}: "
            f"{[_text(label) for label in labels]}"
        )
    edit = control_right_of(labels[0], controls, {"Edit"})
    if edit is None:
        named = find_exact(controls, label_names[0], "Edit")
        if len(named) == 1:
            return named[0]
        raise RuntimeError(f"No Edit to the right of {label_names[0]!r}")
    return edit


def combo_for_label(root, label_names: list[str], *, anchor: str = "top"):
    """Prefer a uniquely named combo, otherwise locate it beside its label."""
    controls = visible_descendants(root)
    wanted = {name.casefold() for name in label_names}
    named = [
        control
        for control in controls
        if control.element_info.control_type == "ComboBox"
        and _text(control).casefold() in wanted
    ]
    if len(named) == 1:
        # Adjacent SWT rows may both fall within the geometry tolerance.
        return named[0]
    labels = [
        control
        for control in controls
        if control.element_info.control_type in {"Text", "Static"}
        and _text(control).casefold() in wanted
    ]
    if not labels:
        raise RuntimeError(f"Label not found: {label_names}")
    labels = sorted(labels, key=lambda c: c.rectangle().top, reverse=(anchor == "bottom"))
    label = labels[0]
    combo = control_right_of(label, controls, {"ComboBox"})
    if combo is None:
        raise RuntimeError(f"No ComboBox to the right of {_text(label)!r}")
    return combo


def write_edit(edit, value: str) -> None:
    """Write text without interpreting '@' or '+' as keyboard shortcuts."""
    text = "" if value is None else str(value)
    try:
        edit.set_focus()
    except Exception:
        pass
    try:
        edit.iface_value.SetValue(text)
        return
    except Exception:
        pass
    edit.set_edit_text(text)

def write_date(edit, value: str) -> None:
    """Type a date and trigger the control's commit event."""
    text = str(value)

    edit.set_focus()

    # Select current date
    edit.type_keys("^a")

    # Type new date
    edit.type_keys(
        text,
        with_spaces=True,
        pause=0.05
    )

    print("[UIA] Typed date:", text)

    # Trigger focus loss / validation
    edit.type_keys("{TAB}")


def _combo_value(combo) -> str:
    """Read the selection without expanding the list or using its label as a value."""
    try:
        value = combo.selected_text()
        if value is not None:
            return str(value).strip()
    except Exception:
        pass
    try:
        return str(combo.iface_value.CurrentValue or "").strip()
    except Exception:
        return ""


class ComboOptionMissing(RuntimeError):
    pass


def _matching_combo_index(options: list[str], wanted: str, *, exact=False) -> int:
    names = [option.strip().casefold() for option in options]
    wanted_cf = wanted.strip().casefold()
    matches = [i for i, name in enumerate(names) if name == wanted_cf]
    if not matches and not exact:
        matches = [i for i, name in enumerate(names) if wanted_cf in name]
    if not matches:
        raise ComboOptionMissing(f"Combo value {wanted!r} not found among {len(options)} options")
    if len(matches) != 1:
        raise RuntimeError(f"Combo value {wanted!r} not unique: {[options[i] for i in matches]}")
    return matches[0]


def _select_native_combo(combo, wanted: str, *, exact=False) -> bool:
    """Select from a native ComboBox's full list, including offscreen options."""
    try:
        if not combo.handle or combo.element_info.class_name != "ComboBox":
            return False
        from pywinauto import win32defines
        from pywinauto.controls.win32_controls import ComboBoxWrapper

        native = ComboBoxWrapper(combo.handle)
        options = native.item_texts()
    except Exception:
        # Custom or inaccessible controls can still be handled through UIA.
        return False
    if not any(options):
        return False

    index = _matching_combo_index(options, wanted, exact=exact)
    if native.selected_index() != index:
        if native.send_message(win32defines.CB_GETDROPPEDSTATE):
            native.send_message(win32defines.CB_SHOWDROPDOWN, 0)
        native.select(index)  # Also sends the selection-change notification to SWT.
        if native.selected_index() != index:
            raise RuntimeError(f"Combo selection did not stick: expected {options[index]!r}")
    return True


def _visible_combo_items(combo) -> list:
    try:
        return [item for item in combo.descendants(control_type="ListItem") if item.is_visible()]
    except Exception:
        return []


def select_combo(combo, wanted: str, *, exact=False) -> None:
    """Select without opening a native combo, or open a UIA combo at most once."""
    wanted_cf = wanted.strip().casefold()
    if _combo_value(combo).casefold() == wanted_cf:
        print(f"[UIA] combo already := {wanted!r}")
        return
    if _select_native_combo(combo, wanted, exact=exact):
        print(f"[UIA] combo := {wanted!r}")
        return

    # Probe support before acting. ComboBoxWrapper.select() expands internally
    # and can fail after doing so; retrying it with expand/click opens twice.
    try:
        pattern = combo.iface_expand_collapse
        expanded = pattern.CurrentExpandCollapseState == 1
    except Exception:
        pattern = None
        expanded = bool(_visible_combo_items(combo))

    try:
        if not expanded:
            if pattern is not None:
                pattern.Expand()
            else:
                combo.click_input()

        deadline = time.monotonic() + 3
        items = _visible_combo_items(combo)
        while not items and time.monotonic() < deadline:
            time.sleep(0.1)
            items = _visible_combo_items(combo)
        options = [_text(item) for item in items]
        if not options:
            raise RuntimeError(f"Could not read the dropdown options for {wanted!r}")
        index = _matching_combo_index(options, wanted, exact=exact)
        selected = options[index]
        items[index].click_input()
    finally:
        # Selection may close the list itself. Never toggle it open again.
        try:
            if pattern is not None:
                if pattern.CurrentExpandCollapseState != 0:
                    pattern.Collapse()
            elif _visible_combo_items(combo):
                combo.type_keys("{ESC}")
        except Exception:
            pass

    current = _combo_value(combo)
    if current and current.casefold() != selected.casefold():
        raise RuntimeError(
            f"Combo selection did not stick: expected {selected!r}, got {current!r}"
        )
    print(f"[UIA] combo item {wanted!r}")
