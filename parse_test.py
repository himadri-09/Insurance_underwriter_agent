import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def ensure_parent(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)


def pymupdf_extract(pdf_path: Path) -> str:
    import fitz
    doc = fitz.open(str(pdf_path))
    parts = [f"# PyMuPDF extraction\n\nSource: {pdf_path.name}\n"]
    for i, page in enumerate(doc, start=1):
        text = page.get_text("text")
        parts.append(f"\n## Page {i}\n\n{text.strip() if text else '[No text extracted]'}\n")
    return "\n".join(parts)


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
        raise RuntimeError(f"Unstructured library not available: {e}. Use PyMuPDF or LlamaParse instead.")
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
        lines.append(header)
        lines.append("")
        lines.append(text if text else "[Empty element]")
        lines.append("")
    return "\n".join(lines)


def llamaparse_extract(pdf_path: Path) -> str:
    api_key = os.getenv("LLAMA_CLOUD_API_KEY") or os.getenv("LLAMAPARSE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing LLAMA_CLOUD_API_KEY or LLAMAPARSE_API_KEY")
    from llama_parse import LlamaParse
    parser = LlamaParse(api_key=api_key, result_type="markdown", verbose=True)
    docs = parser.load_data(str(pdf_path))
    return "\n\n".join(getattr(d, "text", str(d)) for d in docs)


def mixed_extract(pdf_path: Path) -> str:
    sections = [f"# Mixed extraction\n\nSource: {pdf_path.name}\n"]

    pymupdf_text = pymupdf_extract(pdf_path)
    sections.append("\n---\n\n## Plain text layer (PyMuPDF)\n")
    sections.append(pymupdf_text)

    try:
        unstructured_text = unstructured_extract(pdf_path)
        sections.append("\n---\n\n## Structured layer (Unstructured)\n")
        sections.append(unstructured_text)
    except (ImportError, ModuleNotFoundError, RuntimeError) as e:
        sections.append("\n---\n\n## Structured layer (Unstructured)\n")
        sections.append(f"Unstructured extraction failed: {e}\n")
    except Exception as e:
        sections.append("\n---\n\n## Structured layer (Unstructured)\n")
        sections.append(f"Unstructured extraction failed: {e}\n")

    return "\n".join(sections)


def marker_extract(pdf_path: Path) -> str:
    """
    Extract text from a PDF using Marker.

    Install deps first:
        pip install marker-pdf

    GPU (CUDA) is strongly recommended for speed.
    CPU works but is ~10-20x slower on large docs.

    Marker respects these env vars (set in .env or shell):
        TORCH_DEVICE        — e.g. "cuda", "cpu", "mps"  (default: auto-detect)
        MARKER_MAX_PAGES    — limit pages processed (handy for testing)
    """
    try:
        from marker.converters.pdf import PdfConverter
        from marker.models import create_model_dict
        from marker.output import text_from_rendered
        from marker.config.parser import ConfigParser
    except ImportError as e:
        raise RuntimeError(
            f"Marker not installed: {e}\n"
            "Run:  pip install marker-pdf"
        )

    # --- build config ----------------------------------------------------
    # ConfigParser accepts the same kwargs as the CLI flags.
    # See: marker --help  or  marker.config.parser for all options.
    config_dict: dict = {
        "output_format": "markdown",   # "markdown" | "json" | "html"
        "disable_image_extraction": True,  # skip saving figure images to disk
    }

    # honour MARKER_MAX_PAGES for quick smoke-tests
    max_pages = os.getenv("MARKER_MAX_PAGES")
    if max_pages:
        config_dict["max_pages"] = int(max_pages)

    # honour TORCH_DEVICE override
    device = os.getenv("TORCH_DEVICE")
    if device:
        config_dict["device"] = device

    config = ConfigParser(config_dict)

    # --- load all Surya sub-models once ----------------------------------
    # create_model_dict() loads: layout, order, OCR, table, formula models.
    # This is the slow step (~30-60 s first run; cached afterwards).
    print("Loading Marker models (slow on first run)…", flush=True)
    model_dict = create_model_dict()

    # --- run conversion --------------------------------------------------
    converter = PdfConverter(
        config=config.generate_config_dict(),
        artifact_dict=model_dict,
        processor_list=None,   # None → use all default processors
        renderer=None,         # None → use default renderer for output_format
    )

    print(f"Converting {pdf_path.name}…", flush=True)
    rendered = converter(str(pdf_path))

    # text_from_rendered returns (full_text, images_dict, metadata_dict)
    full_text, _images, metadata = text_from_rendered(rendered)

    # --- attach a header with source info --------------------------------
    page_count = metadata.get("page_count", "?")
    languages  = metadata.get("languages", [])
    header = (
        f"# Marker extraction\n\n"
        f"Source: {pdf_path.name}\n"
        f"Pages: {page_count}\n"
        f"Detected languages: {', '.join(languages) if languages else 'n/a'}\n\n"
        f"---\n\n"
    )
    return header + full_text


def main():
    if len(sys.argv) < 3:
        print("Usage: python parse_test.py <mode> <input.pdf> [output.txt]")
        print("Modes: pymupdf | unstructured | mixed | llamaparse | marker")
        sys.exit(1)

    mode = sys.argv[1].strip().lower()
    pdf_path = Path(sys.argv[2]).expanduser().resolve()
    out_file = (
        Path(sys.argv[3]).expanduser().resolve()
        if len(sys.argv) > 3
        else Path("output") / f"{pdf_path.stem}_{mode}.txt"
    )

    if not pdf_path.exists():
        print(f"Input file not found: {pdf_path}")
        sys.exit(2)

    ensure_parent(out_file)

    if mode == "pymupdf":
        result = pymupdf_extract(pdf_path)
    elif mode == "unstructured":
        result = unstructured_extract(pdf_path)
    elif mode == "mixed":
        result = mixed_extract(pdf_path)
    elif mode == "llamaparse":
        result = llamaparse_extract(pdf_path)
    elif mode == "marker":
        result = marker_extract(pdf_path)
    else:
        print(f"Unknown mode: {mode}")
        sys.exit(3)

    out_file.write_text(result, encoding="utf-8")
    print(f"Written: {out_file}")


if __name__ == "__main__":
    main()