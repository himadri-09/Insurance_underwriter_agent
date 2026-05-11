"""
Pinecone service: hybrid search over appetite guides.
Sparse (keyword) + dense (semantic) in one index.
"""

import structlog
from pinecone import Pinecone
from typing import Union, Optional
from app.core.config import get_settings

log = structlog.get_logger()


class VectorService:
    def __init__(self):
        s = get_settings()
        self.pc = Pinecone(api_key=s.pinecone_api_key)
        self.index = self.pc.Index(s.pinecone_index)
        self._llm = None

    @property
    def llm(self):
        if self._llm is None:
            from app.services.llm_service import LLMService
            self._llm = LLMService()
        return self._llm

    def _embed(self, text: str) -> list[float]:
        return self.llm.embed(text)

    def _sparse_encode(self, text: str) -> dict:
        """
        Simple term-frequency sparse vector.
        Deduplicate indices by summing values for collisions.
        """
        from collections import Counter, defaultdict
        tokens = text.lower().split()
        counts = Counter(tokens)
        
        # Deduplicate: if two tokens hash to same index, sum their counts
        index_values = defaultdict(float)
        for token, count in counts.items():
            idx = hash(token) % 50000
            if idx < 0:
                idx += 50000
            index_values[idx] += float(count)
        
        indices = list(index_values.keys())
        values = list(index_values.values())
        return {"indices": indices, "values": values}

    async def upsert_chunk(
        self,
        chunk_id: str,
        text: str,
        metadata: dict,
    ):
        dense = self._embed(text)
        sparse = self._sparse_encode(text)
        self.index.upsert(
            vectors=[
                {
                    "id": chunk_id,
                    "values": dense,
                    "sparse_values": sparse,
                    "metadata": {**metadata, "text": text[:1000]},
                }
            ]
        )
        log.info("chunk_upserted", chunk_id=chunk_id)

    async def hybrid_search(
        self,
        query: str,
        top_k: int = 8,
        filters: Optional[dict] = None,
        alpha: float = 0.7,  # weight toward dense (semantic)
    ) -> list[dict]:
        """
        Hybrid search: blends dense (semantic) and sparse (keyword) results.
        alpha=1.0 → pure semantic, alpha=0.0 → pure keyword.
        """
        log.info("pinecone_search", query=query[:100], top_k=top_k, alpha=alpha, filters=filters)
        dense = self._embed(query)
        sparse = self._sparse_encode(query)

        # Scale vectors by alpha for blending
        scaled_dense = [v * alpha for v in dense]
        scaled_sparse = {
            "indices": sparse["indices"],
            "values": [v * (1 - alpha) for v in sparse["values"]],
        }

        results = self.index.query(
            vector=scaled_dense,
            sparse_vector=scaled_sparse,
            top_k=top_k,
            filter=filters or {},
            include_metadata=True,
        )

        log.info("pinecone_results", matches=len(results.matches), query=query[:50])
        return [
            {
                "chunk_id": m.id,
                "score": m.score,
                "text": m.metadata.get("text", ""),
                "source_doc": m.metadata.get("source_doc", ""),
                "section": m.metadata.get("section", ""),
                "page": m.metadata.get("page"),
                "doc_type": m.metadata.get("doc_type", ""),
            }
            for m in results.matches
        ]

    async def search_by_code(
        self, code: str, top_k: int = 5
    ) -> list[dict]:
        """Exact keyword search for endorsement codes, IMT numbers, etc."""
        return await self.hybrid_search(
            query=code, top_k=top_k, alpha=0.1  # heavily keyword-weighted
        )