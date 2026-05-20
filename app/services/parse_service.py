"""
Parse service: converts documents to markdown for LLM extraction.

Routing logic:
  ACORD forms  → LlamaParse agentic (premium_mode=True, 10 credits/page)
  Other PDFs   → LlamaParse fast (1 credit/page)
  Excel files  → openpyxl → markdown (no API call)

ACORD detection: filename keywords first, then PyMuPDF first-page text peek.
"""

import asyncio
import io
import tempfile
import structlog
from pathlib import Path
from app.core.config import get_settings

log = structlog.get_logger()

_ACORD_FILENAME_KEYWORDS = ("acord", "accord", "125", "126", "130", "140")
_EXCEL_EXTENSIONS = (".xlsx", ".xls", ".xlsm")


class ParseService:
    def __init__(self):
        self.settings = get_settings()
        self._parser_fast = None
        self._parser_agentic = None
        self._cache = {}  # cache_key → markdown

    # ── Parser instances ──────────────────────────────────────────────────────

    @property
    def parser_fast(self):
        if self._parser_fast is None:
            from llama_parse import LlamaParse
            self._parser_fast = LlamaParse(
                api_key=self.settings.llama_cloud_api_key,
                result_type="markdown",
                verbose=False,
                num_workers=self.settings.llama_parse_workers,
            )
        return self._parser_fast

    @property
    def parser_agentic(self):
        if self._parser_agentic is None:
            from llama_parse import LlamaParse
            self._parser_agentic = LlamaParse(
                api_key=self.settings.llama_cloud_api_key,
                result_type="markdown",
                verbose=False,
                premium_mode=True,
                num_workers=self.settings.llama_parse_workers,
            )
        return self._parser_agentic

    # ── ACORD detection ───────────────────────────────────────────────────────

    def _is_acord(self, pdf_bytes: bytes, filename: str) -> bool:
        name_lower = filename.lower()
        if any(kw in name_lower for kw in _ACORD_FILENAME_KEYWORDS):
            return True
        try:
            import fitz
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            first_page_text = doc[0].get_text("text") if len(doc) > 0 else ""
            doc.close()
            return "ACORD" in first_page_text[:2000]
        except Exception:
            return False

    # ── Excel → markdown ──────────────────────────────────────────────────────

    def _excel_to_markdown(self, excel_bytes: bytes, filename: str) -> str:
        try:
            import openpyxl
        except ImportError:
            log.error("openpyxl_not_installed", filename=filename)
            return f"# Excel: {filename}\n\n[Error: openpyxl not installed — run: pip install openpyxl]"

        wb = openpyxl.load_workbook(io.BytesIO(excel_bytes), data_only=True)
        sections = []

        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows = [
                row for row in ws.iter_rows(values_only=True)
                if any(cell is not None for cell in row)
            ]
            if not rows:
                continue

            header = [str(c) if c is not None else "" for c in rows[0]]
            lines = [
                f"## Sheet: {sheet_name}",
                "",
                "| " + " | ".join(header) + " |",
                "| " + " | ".join(["---"] * len(header)) + " |",
            ]
            for row in rows[1:]:
                cells = [str(c) if c is not None else "" for c in row]
                # Pad row to header length in case of ragged sheets
                while len(cells) < len(header):
                    cells.append("")
                lines.append("| " + " | ".join(cells[:len(header)]) + " |")

            sections.append("\n".join(lines))

        return f"# Excel: {filename}\n\n" + "\n\n".join(sections)

    # ── PDF → markdown ────────────────────────────────────────────────────────

    async def pdf_to_markdown(self, pdf_bytes: bytes, filename: str = "document.pdf") -> str:
        cache_key = f"{filename}_{len(pdf_bytes)}"
        if cache_key in self._cache:
            log.info("parse_cache_hit", filename=filename)
            return self._cache[cache_key]

        is_acord = self._is_acord(pdf_bytes, filename)
        parser = self.parser_agentic if is_acord else self.parser_fast
        mode = "agentic" if is_acord else "fast"

        log.info("pdf_parse_starting", filename=filename, mode=mode)

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name

        try:
            docs = await parser.aload_data(tmp_path)
            markdown = "\n\n".join(getattr(d, "text", str(d)) for d in docs)
            self._cache[cache_key] = markdown
            log.info("pdf_parsed", filename=filename, chars=len(markdown), mode=mode)
            return markdown
        except Exception as e:
            log.error("pdf_parse_failed", filename=filename, error=str(e))
            return self._fallback_pymupdf(pdf_bytes, filename)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    # ── Batch entry point ─────────────────────────────────────────────────────

    async def parse_batch(self, files: dict) -> dict:
        """
        Parse all supported files in parallel. Returns {filename: markdown}.
        Supports: .pdf (ACORD → agentic, others → fast), .xlsx/.xls/.xlsm (openpyxl).
        """
        supported = {
            k: v for k, v in files.items()
            if Path(k).suffix.lower() in (".pdf", *_EXCEL_EXTENSIONS)
        }

        if not supported:
            return {}

        log.info("batch_parse_starting", file_count=len(supported))

        parsed = {}
        pdf_tasks = []
        pdf_names = []

        for filename, content in supported.items():
            ext = Path(filename).suffix.lower()
            if ext in _EXCEL_EXTENSIONS:
                parsed[filename] = self._excel_to_markdown(content, filename)
                log.info("excel_parsed", filename=filename, chars=len(parsed[filename]))
            else:
                pdf_tasks.append(self.pdf_to_markdown(content, filename))
                pdf_names.append(filename)

        if pdf_tasks:
            results = await asyncio.gather(*pdf_tasks, return_exceptions=True)
            for filename, result in zip(pdf_names, results):
                if isinstance(result, Exception):
                    log.error("batch_parse_failed", filename=filename, error=str(result))
                    parsed[filename] = self._fallback_pymupdf(supported[filename], filename)
                else:
                    parsed[filename] = result

        log.info("batch_parse_done", total=len(supported), success=len(parsed))
        return parsed

    # ── PyMuPDF fallback ──────────────────────────────────────────────────────

    def _fallback_pymupdf(self, pdf_bytes: bytes, filename: str) -> str:
        import fitz
        log.warning("using_pymupdf_fallback", filename=filename)
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text("text") + "\n\n"
        doc.close()
        return text.strip()