"""
Parse service: converts PDFs to markdown via LlamaParse.
Supports parallel processing and caching.
"""

import asyncio
import tempfile
import structlog
from pathlib import Path
from app.core.config import get_settings

log = structlog.get_logger()


class ParseService:
    def __init__(self):
        self.settings = get_settings()
        self._parser = None
        self._cache = {}  # filename → markdown cache

    @property
    def parser(self):
        if self._parser is None:
            from llama_parse import LlamaParse
            self._parser = LlamaParse(
                api_key=self.settings.llama_cloud_api_key,
                result_type="markdown",
                verbose=False,
                num_workers=self.settings.llama_parse_workers,
            )
        return self._parser

    async def pdf_to_markdown(self, pdf_bytes: bytes, filename: str = "document.pdf") -> str:
        """Convert PDF to markdown. Returns cached result if available."""
        # Check cache
        cache_key = f"{filename}_{len(pdf_bytes)}"
        if cache_key in self._cache:
            log.info("parse_cache_hit", filename=filename)
            return self._cache[cache_key]

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name

        try:
            docs = await self.parser.aload_data(tmp_path)
            markdown = "\n\n".join(
                getattr(d, "text", str(d)) for d in docs
            )
            self._cache[cache_key] = markdown
            log.info("pdf_parsed", filename=filename, chars=len(markdown))
            return markdown
        except Exception as e:
            log.error("pdf_parse_failed", filename=filename, error=str(e))
            return self._fallback_pymupdf(pdf_bytes, filename)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    async def parse_batch(self, files: dict) -> dict:
        """Parse multiple PDFs in parallel. Returns {filename: markdown}."""
        pdf_files = {k: v for k, v in files.items() if k.lower().endswith(".pdf")}

        if not pdf_files:
            return {}

        log.info("batch_parse_starting", file_count=len(pdf_files))

        tasks = [
            self.pdf_to_markdown(content, filename)
            for filename, content in pdf_files.items()
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        parsed = {}
        for filename, result in zip(pdf_files.keys(), results):
            if isinstance(result, Exception):
                log.error("batch_parse_failed", filename=filename, error=str(result))
                parsed[filename] = self._fallback_pymupdf(pdf_files[filename], filename)
            else:
                parsed[filename] = result

        log.info("batch_parse_done", total=len(pdf_files), success=len(parsed))
        return parsed

    def _fallback_pymupdf(self, pdf_bytes: bytes, filename: str) -> str:
        import fitz
        log.warning("using_pymupdf_fallback", filename=filename)
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text("text") + "\n\n"
        doc.close()
        return text.strip()