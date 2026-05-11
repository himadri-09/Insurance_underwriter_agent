"""
Agent: Document Classifier — Commercial Insurance

PDFs  → LlamaParse → markdown → GPT/Claude for classification
Images → Qwen via OpenRouter for classification
"""

import structlog
from app.models.schemas import UploadedDocument, DocType, PipelineState
from app.services.llm_service import LLMService
from app.services.parse_service import ParseService
from app.utils.document_processor import DocumentProcessor
from app.agents.prompts import CLASSIFY_DOCUMENT

log = structlog.get_logger()


class ClassifierAgent:
    def __init__(self):
        self.llm = LLMService()
        self.parser = ParseService()
        self.processor = DocumentProcessor()

    async def classify_document(
        self, doc: UploadedDocument, file_bytes: bytes
    ) -> UploadedDocument:

        if doc.file_type == "pdf":
            markdown = await self.parser.pdf_to_markdown(file_bytes, doc.filename)
            preview = markdown[:2000]

            result = await self.llm.reason(
                system_prompt="You are a document classifier for commercial insurance. Respond with JSON only.",
                user_prompt=f"{CLASSIFY_DOCUMENT}\n\n---\n\nDOCUMENT PREVIEW:\n\n{preview}",
                response_format="json",
            )

        elif doc.file_type in ("png", "jpg", "jpeg", "tiff"):
            img_b64 = self.processor.image_to_base64(file_bytes)
            doc.page_count = 1
            result = await self.llm.extract_from_image(
                image_base64=img_b64,
                prompt=CLASSIFY_DOCUMENT,
                media_type=f"image/{doc.file_type}",
            )
        else:
            doc.doc_class = DocType.UNKNOWN
            return doc

        if isinstance(result, dict) and "parse_error" not in result:
            doc_type_str = result.get("doc_type", "unknown").lower()
            try:
                doc.doc_class = DocType(doc_type_str)
            except ValueError:
                doc.doc_class = DocType.UNKNOWN
            doc.classification_confidence = result.get("confidence", 0.0)
        else:
            doc.doc_class = DocType.UNKNOWN

        log.info("doc_classified", filename=doc.filename, doc_class=doc.doc_class, confidence=doc.classification_confidence)
        return doc

    async def run(self, state: PipelineState, files: dict) -> PipelineState:
        state.status = "classifying"
        state.current_step = "classification"

        for doc in state.documents:
            if doc.filename in files:
                try:
                    await self.classify_document(doc, files[doc.filename])
                except Exception as e:
                    log.error("classification_failed", filename=doc.filename, error=str(e))
                    doc.doc_class = DocType.UNKNOWN
                    state.errors.append(f"Classification failed for {doc.filename}: {str(e)}")

        return state