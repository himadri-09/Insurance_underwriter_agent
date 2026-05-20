import os
import sys
import base64
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def ensure_parent(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────
# PyMuPDF
# ─────────────────────────────────────────────────────────────

def pymupdf_extract(pdf_path: Path) -> str:
    import fitz
    doc = fitz.open(str(pdf_path))
    parts = [f"# PyMuPDF extraction\n\nSource: {pdf_path.name}\n"]
    for i, page in enumerate(doc, start=1):
        text = page.get_text("text")
        parts.append(f"\n## Page {i}\n\n{text.strip() if text else '[No text extracted]'}\n")
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────
# Unstructured
# ─────────────────────────────────────────────────────────────

def unstructured_extract(pdf_path: Path) -> str:
    strategy = os.getenv("UNSTRUCTURED_PDF_STRATEGY", "hi_res")
    try:
        from unstructured.partition.pdf import partition_pdf
        elements = partition_pdf(
            filename=str(pdf_path),
            strategy=strategy,
            infer_table_structure=True,
        )
    except (ImportError, ModuleNotFoundError) as e:
        raise RuntimeError(f"Unstructured not available: {e}.")
    except TypeError:
        from unstructured.partition.pdf import partition_pdf
        elements = partition_pdf(filename=str(pdf_path), strategy=strategy)

    lines = [f"# Unstructured extraction\n\nSource: {pdf_path.name}\n"]
    for idx, el in enumerate(elements, start=1):
        category = getattr(el, "category", el.__class__.__name__)
        text = str(el).strip()
        metadata = getattr(el, "metadata", None)
        page_number = getattr(metadata, "page_number", None) if metadata else None
        header = f"## Element {idx}"
        if page_number is not None:
            header += f" (page {page_number})"
        header += f" - {category}"
        lines.extend([header, "", text if text else "[Empty element]", ""])
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# Excel — openpyxl
# For .xlsx files: property schedules, SOV, loss summaries.
# Output: markdown tables, one per sheet.
# pip install openpyxl
# ─────────────────────────────────────────────────────────────

def excel_extract(file_path: Path) -> str:
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("openpyxl not installed.\nRun: pip install openpyxl")

    wb = openpyxl.load_workbook(file_path, data_only=True)
    parts = [
        f"# Excel extraction (openpyxl)\n\n"
        f"Source: {file_path.name}\n"
        f"Sheets: {', '.join(wb.sheetnames)}\n\n"
        f"---\n"
    ]

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]

        if ws.max_row == 0 or ws.max_column == 0:
            parts.append(f"\n## Sheet: {sheet_name}\n\n[Empty sheet]\n")
            continue

        parts.append(f"\n## Sheet: {sheet_name}\n")
        parts.append(f"Dimensions: {ws.max_row} rows × {ws.max_column} columns\n")

        rows = []
        for row in ws.iter_rows(values_only=True):
            str_row = []
            for cell in row:
                if cell is None:
                    str_row.append("")
                elif isinstance(cell, float):
                    str_row.append(f"{cell:,.2f}" if cell != int(cell) else str(int(cell)))
                else:
                    str_row.append(str(cell).strip())
            if any(c for c in str_row):
                rows.append(str_row)

        if not rows:
            parts.append("\n[No data rows found]\n")
            continue

        num_cols = max(len(r) for r in rows)
        col_widths = [0] * num_cols
        for row in rows:
            for i, cell in enumerate(row):
                if i < num_cols:
                    col_widths[i] = max(col_widths[i], len(cell))

        header_row = rows[0]
        while len(header_row) < num_cols:
            header_row.append("")

        header_line = "| " + " | ".join(
            cell.ljust(col_widths[i]) for i, cell in enumerate(header_row)
        ) + " |"
        separator = "| " + " | ".join("-" * w for w in col_widths) + " |"

        parts.append("\n" + header_line)
        parts.append(separator)

        for row in rows[1:]:
            while len(row) < num_cols:
                row.append("")
            parts.append("| " + " | ".join(
                cell.ljust(col_widths[i]) for i, cell in enumerate(row)
            ) + " |")

        parts.append("")

    wb.close()
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────
# LlamaParse — Fast tier (1 credit/page = $0.00125)
# Good for: text-based PDFs (broker letters, loss runs, dec pages)
# Output: markdown
# ─────────────────────────────────────────────────────────────

def llamaparse_fast(pdf_path: Path) -> str:
    api_key = os.getenv("LLAMA_CLOUD_API_KEY") or os.getenv("LLAMAPARSE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing LLAMA_CLOUD_API_KEY or LLAMAPARSE_API_KEY")

    from llama_parse import LlamaParse

    parser = LlamaParse(
        api_key=api_key,
        result_type="markdown",
        verbose=True,
    )
    docs = parser.load_data(str(pdf_path))
    result = "\n\n".join(getattr(d, "text", str(d)) for d in docs)

    header = (
        f"# LlamaParse Fast (1 credit/page = $0.00125)\n\n"
        f"Source: {pdf_path.name}\n"
        f"Best for: text-based PDFs — broker letters, loss runs, dec pages\n\n"
        f"---\n\n"
    )
    return header + result


# ─────────────────────────────────────────────────────────────
# LlamaParse — Agentic tier (10 credits/page = $0.0125)
# Good for: scanned/image PDFs, ACORD forms, complex layouts
# Output: markdown — NO custom instruction, pure model output
# ─────────────────────────────────────────────────────────────

def llamaparse_agentic(pdf_path: Path) -> str:
    api_key = os.getenv("LLAMA_CLOUD_API_KEY") or os.getenv("LLAMAPARSE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing LLAMA_CLOUD_API_KEY or LLAMAPARSE_API_KEY")

    from llama_parse import LlamaParse

    parser = LlamaParse(
        api_key=api_key,
        result_type="markdown",
        verbose=True,
        premium_mode=True,  # enables Agentic tier
    )
    docs = parser.load_data(str(pdf_path))
    result = "\n\n".join(getattr(d, "text", str(d)) for d in docs)

    header = (
        f"# LlamaParse Agentic (10 credits/page = $0.0125)\n\n"
        f"Source: {pdf_path.name}\n"
        f"Best for: scanned PDFs, ACORD forms, mixed layouts\n\n"
        f"---\n\n"
    )
    return header + result


# ─────────────────────────────────────────────────────────────
# Vision — GPT-4o via Azure
# PDF → PNG pages → GPT-4o (detail: high)
# Mirrors production llm_service.extract_from_pdf()
# Output: markdown
# ─────────────────────────────────────────────────────────────

def vision_gpt4o(pdf_path: Path) -> str:
    import fitz
    from openai import AzureOpenAI

    endpoint   = os.getenv("AZURE_OPENAI_ENDPOINT")
    api_key    = os.getenv("AZURE_OPENAI_API_KEY")
    api_ver    = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

    if not endpoint or not api_key:
        raise RuntimeError("Missing AZURE_OPENAI_ENDPOINT or AZURE_OPENAI_API_KEY in .env")

    client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_ver)

    DPI = 200
    doc = fitz.open(str(pdf_path))
    pages_b64 = []
    for page in doc:
        mat = fitz.Matrix(DPI / 72, DPI / 72)
        pix = page.get_pixmap(matrix=mat)
        pages_b64.append(base64.b64encode(pix.tobytes("png")).decode())
    doc.close()

    print(f"Rendered {len(pages_b64)} page(s) → GPT-4o...", flush=True)

    content_parts = [
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
        }
        for b64 in pages_b64
    ] + [{"type": "text", "text": "Extract all text and data from this insurance document. Output as clean markdown. Preserve all field values, table rows, checkboxes, and numbers exactly as shown."}]

    response = client.chat.completions.create(
        model=deployment,
        messages=[{"role": "user", "content": content_parts}],
        temperature=0,
        max_tokens=4096,
    )

    extracted = response.choices[0].message.content
    header = (
        f"# Vision GPT-4o Azure\n\n"
        f"Source: {pdf_path.name}\n"
        f"Pages: {len(pages_b64)} @ {DPI} DPI\n"
        f"Model: {deployment}\n\n"
        f"---\n\n"
    )
    return header + extracted


# ─────────────────────────────────────────────────────────────
# Vision — Qwen2.5-VL-72B via OpenRouter
# Same model your production pipeline uses for incident photos.
# PDF → PNG pages → Qwen vision
# Output: markdown — NO custom instruction, pure model output
# ─────────────────────────────────────────────────────────────

def vision_qwen(pdf_path: Path) -> str:
    import fitz
    import httpx

    api_key = os.getenv("OPENROUTER_API_KEY")
    model   = os.getenv("QWEN_MODEL", "qwen/qwen2.5-vl-72b-instruct")

    if not api_key:
        raise RuntimeError("Missing OPENROUTER_API_KEY in .env")

    DPI = 200
    doc = fitz.open(str(pdf_path))
    pages_b64 = []
    for page in doc:
        mat = fitz.Matrix(DPI / 72, DPI / 72)
        pix = page.get_pixmap(matrix=mat)
        pages_b64.append(base64.b64encode(pix.tobytes("png")).decode())
    doc.close()

    print(f"Rendered {len(pages_b64)} page(s) → Qwen via OpenRouter...", flush=True)

    content_parts = [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
        for b64 in pages_b64
    ] + [{"type": "text", "text": "Extract all text and data from this insurance document. Output as clean markdown. Preserve all field values, table rows, checkboxes, and numbers exactly as shown."}]

    with httpx.Client(timeout=120) as client:
        resp = client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://triagepilot.app",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": content_parts}],
                "temperature": 0,
                "max_tokens": 4096,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    if "choices" not in data:
        raise RuntimeError(f"OpenRouter error: {data}")

    extracted = data["choices"][0]["message"]["content"]

    usage = data.get("usage", {})
    usage_str = ""
    if usage:
        usage_str = (
            f"Tokens: prompt={usage.get('prompt_tokens','?')} "
            f"completion={usage.get('completion_tokens','?')} "
            f"total={usage.get('total_tokens','?')}\n"
        )

    header = (
        f"# Vision Qwen2.5-VL-72B (OpenRouter)\n\n"
        f"Source: {pdf_path.name}\n"
        f"Pages: {len(pages_b64)} @ {DPI} DPI\n"
        f"Model: {model}\n"
        f"{usage_str}\n"
        f"---\n\n"
    )
    return header + extracted


# ─────────────────────────────────────────────────────────────
# Mixed (PyMuPDF + Unstructured)
# ─────────────────────────────────────────────────────────────

def mixed_extract(pdf_path: Path) -> str:
    sections = [f"# Mixed extraction\n\nSource: {pdf_path.name}\n"]
    sections.append("\n---\n\n## Plain text layer (PyMuPDF)\n")
    sections.append(pymupdf_extract(pdf_path))
    try:
        sections.append("\n---\n\n## Structured layer (Unstructured)\n")
        sections.append(unstructured_extract(pdf_path))
    except Exception as e:
        sections.append("\n---\n\n## Structured layer (Unstructured)\n")
        sections.append(f"Unstructured extraction failed: {e}\n")
    return "\n".join(sections)


# ─────────────────────────────────────────────────────────────
# Marker (local model, needs GPU)
# pip install marker-pdf
# ─────────────────────────────────────────────────────────────

def marker_extract(pdf_path: Path) -> str:
    try:
        from marker.converters.pdf import PdfConverter
        from marker.models import create_model_dict
        from marker.output import text_from_rendered
        from marker.config.parser import ConfigParser
    except ImportError as e:
        raise RuntimeError(f"Marker not installed: {e}\nRun: pip install marker-pdf")

    config_dict: dict = {"output_format": "markdown", "disable_image_extraction": True}
    if os.getenv("MARKER_MAX_PAGES"):
        config_dict["max_pages"] = int(os.getenv("MARKER_MAX_PAGES"))
    if os.getenv("TORCH_DEVICE"):
        config_dict["device"] = os.getenv("TORCH_DEVICE")

    config = ConfigParser(config_dict)
    print("Loading Marker models (slow on first run)…", flush=True)
    model_dict = create_model_dict()
    converter = PdfConverter(
        config=config.generate_config_dict(),
        artifact_dict=model_dict,
        processor_list=None,
        renderer=None,
    )
    print(f"Converting {pdf_path.name}…", flush=True)
    rendered = converter(str(pdf_path))
    full_text, _images, metadata = text_from_rendered(rendered)

    header = (
        f"# Marker extraction\n\n"
        f"Source: {pdf_path.name}\n"
        f"Pages: {metadata.get('page_count', '?')}\n"
        f"Languages: {', '.join(metadata.get('languages', [])) or 'n/a'}\n\n"
        f"---\n\n"
    )
    return header + full_text


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

MODES = [
    "pymupdf",
    "unstructured",
    "mixed",
    "excel",
    "llamaparse_fast",
    "llamaparse_agentic",
    "vision_gpt4o",
    "vision_qwen",
    "marker",
]


def main():
    if len(sys.argv) < 3:
        print("Usage: python parse_test.py <mode> <input_file> [output.md]")
        print()
        print("All modes output markdown (.md).")
        print()
        print("PDF modes:")
        print("  pymupdf            — text layer only, free, instant")
        print("  unstructured       — structure-aware text extraction")
        print("  mixed              — pymupdf + unstructured combined")
        print("  llamaparse_fast    — 1 cr/page ($0.00125)  text PDFs")
        print("  llamaparse_agentic — 10 cr/page ($0.0125)  scanned/ACORD forms")
        print("  vision_gpt4o       — PDF→PNG→GPT-4o Azure  ~$0.010/page")
        print("  vision_qwen        — PDF→PNG→Qwen OpenRouter ~$0.002/page")
        print("  marker             — local model, needs GPU")
        print()
        print("Spreadsheet modes:")
        print("  excel              — openpyxl for .xlsx (property schedules, SOV)")
        print()
        print("Test commands:")
        print("  python parse_test.py llamaparse_agentic Acord-oakridge.pdf output/acord_agentic.md")
        print("  python parse_test.py vision_qwen        Acord-oakridge.pdf output/acord_qwen.md")
        print("  python parse_test.py llamaparse_fast    loss_runs.pdf      output/loss_fast.md")
        print("  python parse_test.py excel              schedule.xlsx      output/schedule.md")
        sys.exit(1)

    mode       = sys.argv[1].strip().lower()
    input_path = Path(sys.argv[2]).expanduser().resolve()

    # Default output extension is .md
    if len(sys.argv) > 3:
        out_file = Path(sys.argv[3]).expanduser().resolve()
    else:
        out_file = Path("output") / f"{input_path.stem}_{mode}.md"

    if not input_path.exists():
        print(f"Input file not found: {input_path}")
        sys.exit(2)

    ensure_parent(out_file)

    dispatch = {
        "pymupdf":            pymupdf_extract,
        "unstructured":       unstructured_extract,
        "mixed":              mixed_extract,
        "excel":              excel_extract,
        "llamaparse_fast":    llamaparse_fast,
        "llamaparse_agentic": llamaparse_agentic,
        "vision_gpt4o":       vision_gpt4o,
        "vision_qwen":        vision_qwen,
        "marker":             marker_extract,
    }

    if mode not in dispatch:
        print(f"Unknown mode: {mode}")
        print(f"Valid modes: {' | '.join(MODES)}")
        sys.exit(3)

    result = dispatch[mode](input_path)
    out_file.write_text(result, encoding="utf-8")
    print(f"Written: {out_file}")


if __name__ == "__main__":
    main()