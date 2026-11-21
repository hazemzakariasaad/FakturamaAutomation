"""Connect to the running Fakturama window and snapshot its UIA tree."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from pywinauto import Application, Desktop

OUT = Path("output/uia")


def connect_fakturama():
    """Attach to the single visible Fakturama top-level window."""
    desktop = Desktop(backend="uia")
    windows = [
        w
        for w in desktop.windows()
        if re.match(r"Fakturama\b", w.window_text() or "", re.I) and w.is_visible()
    ]
    if len(windows) != 1:
        titles = [w.window_text() for w in windows]
        raise RuntimeError( 
            f"Expected one visible Fakturama window; found {len(windows)}: {titles}. "
            "Close duplicate windows before proceeding."
        )
    app = Application(backend="uia").connect(handle=windows[0].handle)
    window = app.window(handle=windows[0].handle)
    window.wait("visible", timeout=12)
    try:
        window.set_focus()
    except Exception:
        pass
    return window


def ctrl_info(control) -> dict | None:
    """Return control metadata, or None if the UIA element is stale/unreachable."""
    try:
        info = control.element_info
        rectangle = control.rectangle()
        try:
            name = control.window_text() or ""
        except Exception:
            name = ""
        try:
            visible = control.is_visible()
        except Exception:
            visible = False
        try:
            enabled = control.is_enabled()
        except Exception:
            enabled = False
        return {
            "name": name,
            "control_type": info.control_type,
            "automation_id": info.automation_id,
            "class_name": info.class_name,
            "rect": [rectangle.left, rectangle.top, rectangle.right, rectangle.bottom],
            "visible": visible,
            "enabled": enabled,
        }
    except Exception:
        return None


def snapshot(window, label: str) -> list:
    """Dump UIA tree for debugging. Never raises — live SWT trees can throw COMError mid-walk."""
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        controls = window.descendants()
    except Exception as exc:
        print(f"[UIA] snapshot {label!r}: descendants() failed ({exc!r})")
        return []

    infos = []
    for control in controls:
        info = ctrl_info(control)
        if info is not None:
            infos.append(info)

    output = OUT / f"{label}.json"
    output.write_text(json.dumps(infos, indent=2, ensure_ascii=False), encoding="utf-8")

    tree_path = OUT / f"{label}.txt"
    wrote_tree = False
    if hasattr(window, "print_control_identifiers"):
        try:
            window.print_control_identifiers(filename=str(tree_path))
            wrote_tree = True
        except Exception as exc:  # pywinauto UIA tree walk can throw COMError on live UI
            print(f"[UIA] print_control_identifiers failed ({exc!r}); falling back to flat list")
    if not wrote_tree:
        lines = ["Control list (fallback; print_control_identifiers unavailable):\n"]
        for info in infos:
            lines.append(
                f"  {info['control_type']:<14} name={info['name']!r} "
                f"auto_id={info['automation_id']!r} rect={info['rect']}"
            )
        tree_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[UIA] {len(infos)} controls; saved {output} and {tree_path}")
    interesting = (
        "order",
        "date",
        "cust.ref",
        "vat",
        "net",
        "address",
        "contact",
        "debtor",
        "company",
        "invoice",
        "delivery",
        "miscellaneous",
        "alias",
        "payment",
        "terms of payment",
    )
    for info in infos:
        if any(token in info["name"].lower() for token in interesting):
            print(
                f"  {info['control_type']:<14} name={info['name']!r} "
                f"auto_id={info['automation_id']!r} rect={info['rect']}"
            )
    return controls


def wait_until(predicate, timeout: float = 12.0, interval: float = 0.3) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def names_of(window) -> list[str]:
    found = []
    for control in window.descendants():
        try:
            found.append((control.window_text() or "").strip())
        except Exception:
            continue
    return found
