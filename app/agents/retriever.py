"""
Agent: Retriever — search Pinecone for relevant policy/guideline evidence.

Builds targeted queries from extraction results.
No metadata filter on doc_type since ingested docs have various types.
"""

import structlog
from app.models.schemas import PipelineState, RetrievedChunk
from app.services.vector_service import VectorService

log = structlog.get_logger()


class RetrieverAgent:
    def __init__(self):
        self.vector = VectorService()

    def _build_queries(self, state: PipelineState) -> list:
        queries = []
        ext = state.extraction
        lob_str = str(state.lob) if state.lob else ""

        # Query 1: Industry/class
        if ext.company.naics_code or ext.company.sic_code or ext.company.name:
            industry_q = " ".join(filter(None, [
                ext.company.naics_code,
                ext.company.sic_code,
                ext.company.name,
                ext.company.industry,
                lob_str,
            ]))
            queries.append({
                "query": f"coverage eligibility {industry_q}",
                "alpha": 0.7,
            })

        # Query 2: Coverage limits
        if ext.coverages:
            cov = ext.coverages[0]
            queries.append({
                "query": f"limits deductible {cov.coverage_type} {cov.limit} {cov.deductible}",
                "alpha": 0.6,
            })

        # Query 3: Loss history
        if ext.loss_history:
            queries.append({
                "query": f"loss history claims frequency threshold {lob_str}",
                "alpha": 0.5,
            })

        # Query 4: Property / construction
        if ext.locations:
            loc = ext.locations[0]
            queries.append({
                "query": f"property {loc.construction_type} {loc.occupancy} coverage exclusion",
                "alpha": 0.5,
            })

        # Query 5: State/territory
        if ext.company.state:
            queries.append({
                "query": f"state {ext.company.state} coverage requirements {lob_str}",
                "alpha": 0.4,
            })

        # Fallback
        if not queries:
            queries.append({
                "query": f"commercial insurance underwriting guidelines {lob_str or 'general'}",
                "alpha": 0.7,
            })

        return queries

    async def run(self, state: PipelineState) -> PipelineState:
        state.status = "retrieving"
        state.current_step = "retrieval"

        queries = self._build_queries(state)
        all_chunks = []
        seen_ids = set()

        for q in queries:
            try:
                log.info("rag_query", query=q["query"], alpha=q.get("alpha", 0.7))
                results = await self.vector.hybrid_search(
                    query=q["query"],
                    top_k=5,
                    alpha=q.get("alpha", 0.7),
                )
                log.info("rag_results", query=q["query"], chunks_found=len(results),
                         top_score=results[0]["score"] if results else 0)

                for r in results:
                    cid = r.get("id", r.get("chunk_id", ""))
                    if cid not in seen_ids:
                        seen_ids.add(cid)
                        all_chunks.append(RetrievedChunk(
                            chunk_id=cid,
                            text=r.get("text", ""),
                            source_doc=r.get("metadata", {}).get("source_doc", ""),
                            section=r.get("metadata", {}).get("section", ""),
                            score=r.get("score", 0.0),
                            match_type="hybrid",
                        ))
            except Exception as e:
                log.error("rag_query_failed", query=q["query"], error=str(e))

        state.retrieved_chunks = sorted(all_chunks, key=lambda c: c.score, reverse=True)[:20]
        log.info("retrieval_done", chunks_found=len(state.retrieved_chunks))
        return state