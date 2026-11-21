# FakturamaAutomation

Turn a **scanned or photographed order** into a saved **Fakturama Order** and its linked **Invoice**. The pipeline has two complementary vision layers:

| Source | Technology | What it reads |
|--------|------------|---------------|
| **Order image** (your input file) | **Google Gemini** (vision + JSON) | Customer, addresses, payment, line items, totals — the full business payload |
| **Fakturama on-screen grids** | **Windows OCR** + UI Automation | Rows in NatTables where accessibility does not expose cell text (address picker, product search, Items grid, Documents list) |

**UI Automation (pywinauto)** drives buttons, tabs, fields, and combos. **Gemini** reads the order image; **pywinauto** writes into Fakturama; **Windows OCR** is only for on-screen grid cells.

---

## Why the order image is **not** read with Windows OCR

Fakturama’s own UI is not the order PDF or photo. You pass a separate **order image** (`image.png`, `.jpg`, etc.). That image typically contains:

- Multi-column tables (SKU, qty, unit price, VAT %, discount %, line totals)
- Long addresses, references, and payment wording
- Mixed fonts, noise, skew, and small print

**Windows OCR** (`automation/ocr_grid.ps1`) is tuned for **cropped single-column grid cells** inside Fakturama. It is **not** used to parse the full order image because:

1. **Coverage** — One OCR pass on the whole page often **misses or garbles** table cells, headers, and footers; it does not return a structured object with validated amounts.
2. **Structure** — You need a **schema** (debtor, items\[\], totals, payment status). OCR gives loose words and boxes, not reliable JSON.
3. **Business rules** — Quantities, net vs gross, line discounts vs order discount, and total cross-checks need **reasoning and strict validation** (Pydantic), which Gemini + `extraction/schemas.py` provide.

So: **extract order data with Gemini** → validate → **write into Fakturama** with pywinauto. Use **Windows OCR only where UIA cannot read NatTable cells** (`automation/table.py`).

### Example: `output/ocr/` (whole-page OCR is incomplete)

The repository includes **`output/ocr/`** — sample artifacts from running **Windows OCR on the full order image** (preprocessed variants, word boxes, `ocr_text.txt`, `ocr_result.json`). They are **not** used by `main.py`; they document **why** the pipeline does not rely on OCR for extraction.

Typical problems visible in those outputs:

| Issue | Example from OCR on the sample order |
|-------|--------------------------------------|
| Garbled labels | `USTOMER`, `DELIVERY ADDRISS`, `PAYMIUNT` |
| Wrong characters in IDs | `CHR-ERG-O1` vs `CHR-ERG-01`, `200` vs `250.00` |
| Broken email / contact | `marta klemn@example.test` |
| No structured JSON | Loose lines, not `items[]`, totals, or payment fields |
| Missing / merged columns | Line table values run together |

`ocr_boxes.png` and the preprocessed PNGs show what the engine actually “sees” — enough for a human skimming, **not** enough for validated automation. That gap is why **Gemini + `OrderData`** handles the **order image**, while **`output/ocr/`** is kept as a **before / comparison** reference only.

---

## Prerequisites

### Machine and Fakturama

- **Windows** with an unlocked desktop session (automation sends real mouse/keyboard input).
- **Fakturama** installed (development used **2.2.1**). Use a **test workspace**; the script **saves into the workspace currently loaded** in that window.
- **English Fakturama UI** — controls are matched by English labels (`Cust.Ref.`, `Items`, `Data`, etc.).
- **Exactly one** visible top-level window whose title starts with `Fakturama` (close File Explorer tabs or browser windows whose titles also start with `Fakturama`).
- Before a full run: open Fakturama, load the target workspace, and leave the main window visible.

### Python and packages

- **Python 3.13** (or compatible 3.11+); a venv is recommended.
- Install from `requirements.txt`:

  | Package | Role |
  |---------|------|
  | `google-genai` | Gemini API for order image extraction |
  | `pydantic` | `OrderData` schema and validation |
  | `pywinauto` | UI Automation (UIA backend) |
  | `Pillow` | Screenshots and grid image processing |
  | `pytest` | Unit tests (no live Fakturama required) |

- **PowerShell** (`powershell.exe`) and **Windows OCR** language pack matching your Windows display language (used by `ocr_grid.ps1` for grids only).
- **Network** access to the **Gemini API**.

`pywinauto` on Windows typically needs **pywin32** (installed as a dependency when you use UIA/clipboard features in `table.py`).

### Gemini API key

- Create a key in [Google AI Studio](https://aistudio.google.com/) (or your Google Cloud project).
- Set **`GEMINI_API_KEY`** before running (never commit the key).

Optional:

- **`GEMINI_MODEL`** — override default model (`gemini-3.1-flash-lite` if unset).
- **`.env`** in the repo root — `gemini_extractor` loads `GEMINI_API_KEY=...` if the variable is not already set (see `extraction/gemini_extractor.py`).

---

## Setup and run (quick start)

From the **repository root** in **PowerShell**: create a virtual environment in the **`.venv`** folder, install dependencies, set your API key, then run against your order image.

```powershell
# 1) Create .venv and install packages (once per machine / clone)
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2) API key (once per session, or put GEMINI_API_KEY=... in .env)
$env:GEMINI_API_KEY = '<your key>'

# 3) Fakturama must already be open (one window, test workspace)

# 4) Run the automation on your order image
.\.venv\Scripts\python.exe main.py image.png
```

Put your scan or screenshot in the project folder as `image.png`, or pass any path (e.g. `orders\scan-0714.png`).

Optional flags:

```powershell
.\.venv\Scripts\python.exe main.py image.png --output output\gemini --model gemini-3.1-flash-lite
.\.venv\Scripts\python.exe main.py --help
```

---

## How to run (details)

### Full flow (new Order from image)

1. Open **Fakturama** (one window, test workspace).
2. From the repo root, with **`.venv`** already created:

```powershell
.\.venv\Scripts\python.exe main.py image.png
```

**What `main.py` does:**

| Phase | Module | Action |
|-------|--------|--------|
| 1. Extract | `extraction/gemini_extractor.py` | Send image to Gemini → JSON → Pydantic `OrderData` |
| 2. Validate | `extraction/schemas.py`, `automation/invoice.py` | Reject bad totals / missing fields before touching Fakturama |
| 3. Attach | `automation/fakturama_client.py` | Connect to the running Fakturama window |
| 4. Order shell | `automation/order.py` | Open or reuse empty **New Order**; Date, Cust.Ref., Net, With VAT |
| 5. Debtor | `automation/debtor.py`, `automation/payment.py` | Address select or **New Debtor**; payment terms; save; re-select on Order |
| 6. Lines | `automation/product.py`, `automation/vat.py` | Products, VAT, qty, net price, discounts; OCR on Items grid |
| 7. Save & invoice | `automation/invoice.py`, `automation/documents.py` | Save Order, verify in Documents, **Invoice** follow-up, payment if PAID |

If validation fails after Gemini, the run **exits before Fakturama** and writes errors under `output/gemini/`.

### Resume when the Order is already saved

If the run stopped **after** the Order was saved but the **Invoice** step did not finish:

```powershell
.\.venv\Scripts\python.exe main.py image.png --from-order PO000003
```

This still **re-extracts** the image with Gemini for cross-checks, opens that Order from **Data → Documents**, and continues with the linked **Invoice** workflow. Do not use this if an Invoice was already created for that Order unless you have verified Documents first.

---

## End-to-end workflow (detail)

1. **Extract and validate (Gemini)**  
   Customer, billing/delivery addresses, payment method/status/date, each line (SKU, description, qty, **unit net price**, VAT %, discount %, line net total), and order totals. Totals and line maths are checked before UI writes.

2. **Order header**  
   Empty **New Order** required. Set order date and customer reference; **Net** pricing and **With VAT**; do not change the proposed document number.

3. **Debtor**  
   Search **Select the address**, using the first name when available. If a new debtor is needed, go to **Data → terms of payment** and reuse or create the exact payment term before opening **New Debtor**. The **Payment** dropdown in an already-open debtor editor does not refresh when a new term is saved. Preparing the term first makes it available when the editor opens. Then set the invoice and delivery address roles, add a separate delivery address when needed, select the payment term, and save. Back in the Order, search by the saved customer number and confirm both populated addresses.

4. **Items**  
   For each SKU: product picker (grid OCR + search). Create product with **gross** master price = net × (1 + VAT/100) if missing. Set line VAT, quantity, **net** unit price, discount; verify line net and document totals (Items grid OCR + inline editors).

5. **Save Order**  
   Save once; verify row in **Data → Documents → Orders** (number, date, reference, open, total). Open row and click **Invoice** under **Create a follow-up document**.

6. **Invoice**  
   Verify copied data; payment method; if **PAID**, set payment date and value. Save; verify in **Documents → Invoices** and payment fields; confirm source Order still matches.

The script **stops on verification failure** (ambiguous grid OCR, mismatched amounts, etc.). An **earlier save may already have succeeded** — check **Data → Documents** before re-running the same image.

## Assignment scope

- **Debtor matching.** The current implementation selects a single search result and then verifies the populated addresses. Multiple results stop for review. The assignment’s five-field comparison of Company, First Name, Last Name, ZIP, and City before selecting the row remains to be implemented.
- **Source image extraction.** The production flow sends the image directly to Gemini and validates the response. The design’s local source-image OCR, preprocessing variants, and per-field confidence tracking remain outside the production flow. Fakturama grid OCR runs locally.

### With three more hours

I would focus on improving reliability and testing edge cases rather than adding unnecessary features.

First, I would strengthen the existing debtor and product matching logic by testing ambiguous, duplicate, and partially unreadable records, ensuring the automation never selects an incorrect entry.

Second, I would improve recovery from Fakturama UI state issues, particularly when newly created payment methods are not immediately available in an already-open Debtor editor.

Finally, I would run additional end-to-end tests in both clean and pre-populated Fakturama workspaces to verify correct record creation, duplicate prevention, Order-to-Invoice linking, and payment persistence.

---

## Two “OCR” roles (do not confuse them)

### A. Order image → structured data (**Gemini**, not `ocr_grid.ps1`)

- Input: `main.py` image argument.  
- Output: `output/gemini/order.json`, `validation.json`, raw model text.  
- Used for: everything you need from the paper/PDF order.

### B. Fakturama NatTable cells (**Windows OCR** + UIA)

Fakturama draws many lists as **SWT NatTable** canvases. UIA sees the pane and scrollbars but often **not** each cell’s text.

For **Select the address**, **Select a product**, **VATs**, **Items**, and **Documents**:

1. Locate the grid (`automation/table.py`: `grid_below`, layout from screenshot ruling lines).  
2. Crop cells and run **`automation/ocr_grid.ps1`** (Windows.Media.Ocr).  
3. Map words back to rows/columns; use **search filters**, **editor readback**, and **Ctrl+C** where OCR confuses `O`/`0` or `PO`/`P0`.

OCR here is **necessary but imperfect** — that is why extraction of the **order image** does not rely on it.

---

## Project layout

```
main.py                 Entry: extract → Fakturama → save Order + Invoice
extraction/
  gemini_extractor.py   Gemini vision → JSON
  schemas.py            Pydantic OrderData
automation/
  fakturama_client.py   Attach to Fakturama, snapshots, waits
  order.py              New Order header
  debtor.py             Address select / New Debtor
  payment.py            Terms of payment
  product.py            Order lines, product create
  vat.py                VAT master data
  table.py              Grid geometry + OCR orchestration
  ocr_grid.ps1          Windows OCR for grid cell images
  invoice.py            Save Order, create Invoice
  documents.py          Documents list verification
  control_locator.py    Fields, combos, edits
tests/                  pytest (mocked UI + synthetic grid images)
output/gemini/          Extraction artifacts (production path)
output/ocr/             Sample whole-page OCR — incomplete; not used by main.py
output/uia/             UIA snapshots (*_review on failures)
```

---

## Tests and diagnostics

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests cover debtor/payment ordering, address grids, customer-number matching, VAT/products, grid geometry, and document verification using **simulated** controls — not a live Fakturama session.

| Output | Contents |
|--------|----------|
| `output/gemini/` | Raw model response, `order.json`, validation errors |
| `output/ocr/` | Demo OCR on the full order image (text/boxes/preprocessed PNGs); shows missing or wrong detail vs Gemini |
| `output/uia/` | UIA tree dumps; files ending in `_review` mark the failing step |

These folders may contain **customer or order data** — treat them as sensitive.

---

## Troubleshooting

| Problem | What to check |
|---------|----------------|
| `Expected one visible Fakturama window; found 2` | Close Explorer/browser tabs whose titles start with `Fakturama`. |
| Gemini / validation errors | `output/gemini/validation.json`; fix image or schema fields before re-running Fakturama. |
| Grid / OCR errors | `output/uia/*_review.*`; ensure Windows OCR language is installed; widen Documents view if verification fails. |
| Partial run created data | **Data → Documents** before running the same image again. |
| Wrong language UI | Switch Fakturama to **English** or extend label matching in code. |

---