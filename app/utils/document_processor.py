"""
PDF/image processing: split pages, render to images, extract text.
"""

import base64
import io
import fitz  # PyMuPDF
import structlog
from pathlib import Path

log = structlog.get_logger()


class DocumentProcessor:
    """Handles PDF splitting, page rendering, and text extraction."""

    DPI = 200  # good balance of quality vs size for VLM extraction

    def split_pdf_to_pages(self, pdf_bytes: bytes) -> list[dict]:
        """
        Split a PDF into individual page images + text.
        Returns list of {page_number, image_base64, text, width, height}.
        """
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages = []

        for i, page in enumerate(doc):
            # Render page to image
            mat = fitz.Matrix(self.DPI / 72, self.DPI / 72)
            pix = page.get_pixmap(matrix=mat)
            img_bytes = pix.tobytes("png")
            img_b64 = base64.b64encode(img_bytes).decode()

            # Extract raw text (useful for keyword search fallback)
            text = page.get_text("text")

            pages.append({
                "page_number": i + 1,
                "image_base64": img_b64,
                "text": text,
                "width": pix.width,
                "height": pix.height,
            })

        doc.close()
        log.info("pdf_split", page_count=len(pages))
        return pages

    def image_to_base64(self, image_bytes: bytes) -> str:
        """Convert raw image bytes to base64."""
        return base64.b64encode(image_bytes).decode()

    def get_pdf_page_count(self, pdf_bytes: bytes) -> int:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        count = doc.page_count
        doc.close()
        return count

    def extract_text_from_pdf(self, pdf_bytes: bytes) -> str:
        """Extract all text from PDF (for non-VLM paths)."""
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text("text") + "\n\n"
        doc.close()
        return text.strip()
