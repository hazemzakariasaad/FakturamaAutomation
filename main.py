"""Extract an order image, then fill the open Fakturama window.

Uses the existing Fakturama window and verifies the Order and its linked Invoice.
"""

import argparse
import json
from pathlib import Path

from automation.debtor import resolve_debtor
from automation.fakturama_client import connect_fakturama, snapshot
from automation.invoice import complete_order_and_invoice, validate_document_source
from automation.order import fill_order_header, open_new_order
from automation.product import fill_order_items, prepare_items, require_empty_items
from extraction.gemini_extractor import DEFAULT_MODEL, extract_order


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Extract structured order data from an image, then fill the "
            "already-open Fakturama window. Verifies and saves the Order and its linked Invoice."
        )
    )
    parser.add_argument("image", type=Path, help="Order image (.png/.jpg/.tif)")
    parser.add_argument("--output", default="output/gemini", help="Output directory")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Gemini model name")
    parser.add_argument("--from-order", metavar="NUMBER",
                        help="Continue from a saved Order whose linked Invoice has yet to be created")
    args = parser.parse_args()

    result = extract_order(args.image, args.output, model=args.model)

    print("\n========== RESULT ==========")
    print(f"Model: {result['model']}")
    print(f"Valid against OrderData schema: {result['valid']}")
    print(f"Raw response chars: {result['raw_response_chars']}")
    print(f"Files saved to: {Path(args.output).resolve()}")

    if result["order"]:
        order = result["order"]
        print("\n--- ORDER SUMMARY ---")
        print(f"  external_reference : {order.get('external_reference')}")
        print(f"  order_date         : {order.get('order_date')}")
        debtor = order.get("debtor") or {}
        print(f"  debtor.company     : {debtor.get('company')}")
        print(f"  debtor.alias       : {debtor.get('alias')}")
        payment = order.get("payment") or {}
        print(
            f"  payment            : {payment.get('method')} / "
            f"{payment.get('status')} on {payment.get('payment_date')}"
        )
        print(f"  items              : {len(order.get('items') or [])}")
        for i, item in enumerate(order.get("items") or [], 1):
            print(
                f"    [{i}] {item.get('sku')}  qty={item.get('quantity')}  "
                f"unit={item.get('unit_net_price')}  vat={item.get('vat_percent')}%  "
                f"disc={item.get('discount_percent')}%  line_net={item.get('source_line_net_total')}"
            )
        totals = order.get("totals") or {}
        print(
            f"  totals             : net={totals.get('net')} "
            f"vat={totals.get('vat')} gross={totals.get('gross')}"
        )
    else:
        print("\n--- VALIDATION ERRORS ---")
        print(json.dumps(result["validation_errors"], indent=2, ensure_ascii=False))
        return 1

    prepared = prepare_items(order.get("items") or [])
    validate_document_source(order)
    print("\n========== FAKTURAMA ==========")
    print("Attaching to the Fakturama window that is already open.")
    window = connect_fakturama()
    if args.from_order is None:
        snapshot(window, "before_order")
        open_new_order(window)
        snapshot(window, "after_order")
        require_empty_items(window)
        fill_order_header(window, order)
        outcome = resolve_debtor(window, order)
        snapshot(window, "after_debtor")
        print(f"[UIA] Debtor {outcome}.")
        fill_order_items(window, prepared)
        snapshot(window, "after_items")
    order_no, invoice_no = complete_order_and_invoice(window, order, prepared, saved_order_no=args.from_order)
    print(f"[UIA] Verified saved Order {order_no} and linked Invoice {invoice_no}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
