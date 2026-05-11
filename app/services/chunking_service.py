"""
Chunking service: structure-aware splitting of markdown documents.

Takes LlamaParse markdown output and splits by heading boundaries.
Each chunk gets rich metadata for filtered Pinecone retrieval.

Strategy:
  1. Split at heading boundaries (# ## ###)
  2. Each chunk = heading + its content until next same-or-higher heading
  3. If chunk > MAX_TOKENS, sub-split at paragraph boundaries with overlap
  4. Attach metadata: doc_type, section, sub_section, content_type, keywords
"""

import re
import hashlib
import structlog
from dataclasses import dataclass, field

log = structlog.get_logger()


@dataclass
class Chunk:
    chunk_id: str
    text: str
    metadata: dict = field(default_factory=dict)


class ChunkingService:
    MAX_CHUNK_TOKENS: int = 500
    MIN_CHUNK_TOKENS: int = 30
    OVERLAP_CHARS: int = 200

    CONTENT_TYPE_PATTERNS = {
        "exclusion": r"(?i)(exclusion|we (do not|will not) (pay|provide|cover)|not covered|exceptions?)",
        "condition": r"(?i)(condition|provided that|subject to|requirement)",
        "definition": r"(?i)(definition|means|shall mean|as used in)",
        "coverage": r"(?i)(coverage|insuring agreement|we will pay|covered|protection)",
        "procedure": r"(?i)(procedure|process|how to|steps|filing|claim)",
        "endorsement": r"(?i)(endorsement|amendment|rider|addendum|IMT|PP\s*\d)",
        "deductible": r"(?i)(deductible|excess|retention|self.insured)",
        "limit": r"(?i)(limit|maximum|cap|aggregate|per.?occurrence)",
        "premium": r"(?i)(premium|rate|pricing|discount|surcharge)",
    }

    def chunk_markdown(
        self,
        markdown: str,
        filename: str,
        doc_type: str = "reference",
        insurer: str = "",
        lob: str = "personal_auto",
    ) -> list:
        """Main entry. Takes markdown, returns list of Chunks with metadata."""

        sections = self._split_by_headings(markdown)
        chunks = []

        for section in sections:
            heading = section["heading"]
            level = section["level"]
            body = section["body"].strip()

            if not body or self._estimate_tokens(body) < self.MIN_CHUNK_TOKENS:
                continue

            content_type = self._detect_content_type(heading, body)

            meta = {
                "source_doc": filename,
                "doc_type": doc_type,
                "lob": lob,
                "insurer": insurer,
                "section": heading,
                "heading_level": level,
                "content_type": content_type,
                "keywords": self._extract_keywords(heading, body),
            }

            if self._estimate_tokens(body) <= self.MAX_CHUNK_TOKENS:
                chunk_text = f"## {heading}\n\n{body}" if heading else body
                chunk_id = self._make_chunk_id(filename, heading, 0)
                chunks.append(Chunk(chunk_id=chunk_id, text=chunk_text, metadata=meta))
            else:
                sub_chunks = self._sub_split(body, heading)
                for i, sub_text in enumerate(sub_chunks):
                    chunk_text = f"## {heading}\n\n{sub_text}" if heading else sub_text
                    chunk_id = self._make_chunk_id(filename, heading, i)
                    sub_meta = {**meta, "sub_section": f"part_{i+1}_of_{len(sub_chunks)}"}
                    chunks.append(Chunk(chunk_id=chunk_id, text=chunk_text, metadata=sub_meta))

        log.info("chunking_done", filename=filename, total_chunks=len(chunks))
        return chunks

    def _split_by_headings(self, markdown: str) -> list:
        """Split markdown into sections at heading boundaries."""
        heading_pattern = re.compile(r"^(#{1,4})\s+(.+)$", re.MULTILINE)

        sections = []
        matches = list(heading_pattern.finditer(markdown))

        if not matches:
            return [{"heading": "", "level": 0, "body": markdown}]

        # Text before first heading
        if matches[0].start() > 0:
            pre_text = markdown[:matches[0].start()].strip()
            if pre_text:
                sections.append({"heading": "Introduction", "level": 0, "body": pre_text})

        # Each heading + content until next heading
        for i, match in enumerate(matches):
            level = len(match.group(1))
            heading = match.group(2).strip()
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
            body = markdown[start:end].strip()
            sections.append({"heading": heading, "level": level, "body": body})

        return sections

    def _sub_split(self, text: str, heading: str) -> list:
        """Split large section at paragraph boundaries with overlap."""
        paragraphs = re.split(r"\n\n+", text)
        paragraphs = [p.strip() for p in paragraphs if p.strip()]

        chunks = []
        current = []
        current_tokens = 0

        for para in paragraphs:
            para_tokens = self._estimate_tokens(para)

            if current_tokens + para_tokens > self.MAX_CHUNK_TOKENS and current:
                chunks.append("\n\n".join(current))
                overlap = current[-1] if current else ""
                current = [overlap] if overlap else []
                current_tokens = self._estimate_tokens(overlap)

            current.append(para)
            current_tokens += para_tokens

        if current:
            chunks.append("\n\n".join(current))

        return chunks if chunks else [text]

    def _detect_content_type(self, heading: str, body: str) -> str:
        """Detect what kind of insurance content this chunk contains."""
        combined = f"{heading} {body[:500]}"
        for content_type, pattern in self.CONTENT_TYPE_PATTERNS.items():
            if re.search(pattern, combined):
                return content_type
        return "general"

    def _extract_keywords(self, heading: str, body: str) -> str:
        """Extract key insurance terms for BM25 sparse search."""
        combined = f"{heading} {body}"

        term_patterns = [
            r"(?i)(Part [A-D])",
            r"(?i)(Section [IVX]+|Section \d+)",
            r"(?i)(PP\s*\d{2}\s*\d{2})",
            r"(?i)(IMT[\.\s]*\d+[A-Z]?)",
            r"(?i)(collision|comprehensive|liability|um|uim|pip|med.?pay)",
            r"(?i)(DUI|DWI|SR.?22|total.?loss)",
            r"(?i)(deductible|premium|NCB|no.?claim)",
            r"(?i)(exclusion|condition|definition|endorsement)",
            r"(?i)(bodily.?injury|property.?damage|uninsured.?motorist)",
            r"(?i)(subrogation|salvage|actual.?cash.?value|ACV)",
        ]

        found = set()
        for pattern in term_patterns:
            matches = re.findall(pattern, combined)
            for m in matches:
                found.add(m.strip().lower())

        heading_words = [w.lower() for w in heading.split() if len(w) > 3]
        found.update(heading_words)

        return ",".join(sorted(found)[:20])

    def _estimate_tokens(self, text: str) -> int:
        return len(text) // 4

    def _make_chunk_id(self, filename: str, heading: str, index: int) -> str:
        raw = f"{filename}:{heading}:{index}"
        return hashlib.md5(raw.encode()).hexdigest()[:16]