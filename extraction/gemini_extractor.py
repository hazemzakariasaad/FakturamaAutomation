"""Vision-language extraction via Google Gemini.

Replaces the Ollama/Qwen3-VL path. One Gemini call per order image returns a
JSON object that maps onto `extraction.schemas.OrderData`.

Design choices:
- Gemini is asked to return JSON directly via `response_mime_type=application/json`.
- Strict system prompt: never invent missing fields, leave nulls.
- Pydantic validates the result; invalid results surface as errors, not silent
  "cleaned up" data — important because this feeds Fakturama writes later.
- API key is read from the GEMINI_API_KEY env var (optionally from a .env file).
"""

from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .schemas import OrderData


DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")


# Single source of truth for the shape: the Pydantic model itself. Serialize it
# to JSON Schema so the prompt and the validator cannot drift apart.
SCHEMA_DICT = OrderData.model_json_schema()
SCHEMA_HINT = json.dumps(SCHEMA_DICT, indent=2, ensure_ascii=False)

SYSTEM_PROMPT = (
    "You are an invoice/order data extractor. Read the image carefully, "
    "including small grayscale labels and table cells. "
    "Return ONLY a JSON object matching the schema the user provides.\n"
    "Rules:\n"
    "- Never invent values. If a field is unreadable or absent, use null.\n"
    "- Preserve leading zeros in zip codes and IDs (use strings).\n"
    "- Numbers must be plain JSON numbers without currency symbols or "
    "thousands separators. Use '.' as the decimal separator.\n"
    "- Round every money, percent, and quantity number to EXACTLY 2 decimal "
    "places (e.g. 250.00, 108.30, 2.00).\n"
    "- Dates must be ISO 8601 (YYYY-MM-DD).\n"
    "- Payment status must be exactly 'PAID' or 'UNPAID'.\n"
    "- VAT percent and discount percent are separate columns; do NOT mix them up.\n"
    "- Extract order_discount_percent, shipping_method and shipping_value only when "
    "explicitly shown for the whole order; never copy a line discount into the order discount.\n"
    "- For each address, fill additional_name, address_specification, and "
    "district ONLY when the source image clearly supplies those values; "
    "otherwise return null. Do not invent or copy them from other blocks.\n"
    "- street = street + house number only. Put warehouse/c/o lines in "
    "additional_name when present (e.g. 'Northstar Office Warehouse').\n"
    "- Delivery address must come from the DELIVERY ADDRESS block only; do NOT "
    "copy email/phone from the billing/contact block unless the delivery block "
    "explicitly lists them.\n"
    "- Do NOT include any prose, markdown, or code fences. JSON only."
)


def _load_dotenv_if_present() -> None:
    """Minimal .env loader (no external dependency). Only sets missing keys."""
    env_path = Path(".env")
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _read_image_bytes(image_path: str | Path) -> tuple[bytes, str]:
    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"Order image not found: {path}")
    mime, _ = mimetypes.guess_type(str(path))
    if mime is None or not mime.startswith("image/"):
        mime = "image/png"
    return path.read_bytes(), mime


def _parse_json_payload(raw: str) -> dict[str, Any]:
    """Gemini may wrap JSON in ``` fences even when asked not to; be tolerant."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip("`").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"Model response is not JSON:\n{raw[:500]}")
    return json.loads(text[start : end + 1])


def extract_order(
    image_path: str | Path,
    output_dir: str | Path = "output/gemini",
    *,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    temperature: float = 0.0,
) -> dict[str, Any]:
    """Call Gemini, validate against OrderData, and persist artifacts."""
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError as exc:  # pragma: no cover - import-time check
        raise RuntimeError(
            "The 'google-genai' package is required. Install with: "
            "pip install google-genai"
        ) from exc

    _load_dotenv_if_present()
    key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Put it in .env or export it before running."
        )

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)

    image_bytes, mime_type = _read_image_bytes(image_path)
    client = genai.Client(api_key=key)

    print(f"[gemini] model={model}")
    print(f"[gemini] sending {Path(image_path).name} ({mime_type}, {len(image_bytes)} bytes) ...")

    response = client.models.generate_content(
        model=model,
        contents=[
            genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            (
                "Extract the order data from the image. "
                "Conform strictly to the JSON Schema (OrderData) below. "
                "Return ONLY the JSON object, no prose or markdown.\n\n"
                + SCHEMA_HINT
            ),
        ],
        config=genai_types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=temperature,
            response_mime_type="application/json",
            response_schema=OrderData,
        ),
    )

    raw_content = (response.text or "").strip()
    (target / "gemini_raw.txt").write_text(raw_content, encoding="utf-8")
    print(f"[gemini] saved gemini_raw.txt ({len(raw_content)} chars)")
    if not raw_content:
        raise RuntimeError("Empty response from Gemini. Check model name and API key.")

    payload = _parse_json_payload(raw_content)
    (target / "gemini_payload.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("[gemini] saved gemini_payload.json")

    validation_errors: list[dict[str, Any]] = []
    order: OrderData | None = None
    try:
        order = OrderData.model_validate(payload)
    except ValidationError as exc:
        validation_errors = exc.errors()
        print(f"[gemini] pydantic validation failed: {len(validation_errors)} errors")

    if order is not None:
        order_dump = order.model_dump(mode="json")
        (target / "order.json").write_text(
            json.dumps(order_dump, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print("[gemini] saved order.json")
    else:
        order_dump = None

    (target / "validation.json").write_text(
        json.dumps(validation_errors, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return {
        "source_image": str(Path(image_path)),
        "model": model,
        "raw_response_chars": len(raw_content),
        "payload": payload,
        "order": order_dump,
        "validation_errors": validation_errors,
        "valid": order is not None,
    }
