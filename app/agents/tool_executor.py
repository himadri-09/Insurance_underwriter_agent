"""
Tool executor: processes tool calls from Claude's reasoning loop.
Routes each tool call to the appropriate retrieval function.
"""

import json
import structlog
from app.services.vector_service import VectorService
from app.models.schemas import RetrievedChunk

log = structlog.get_logger()


class ToolExecutor:
    def __init__(self):
        self.vector = VectorService()

    async def execute(self, tool_name: str, tool_input: dict) -> tuple[str, list[RetrievedChunk]]:
        """
        Execute a tool call and return (result_text, new_chunks).
        result_text goes back to Claude, new_chunks get added to state.
        """
        log.info("tool_executing", tool=tool_name, input=tool_input)
        handler = {
            "search_appetite_guide": self._search_appetite,
            "search_by_code": self._search_code,
            "search_loss_guidelines": self._search_loss,
            "search_referral_rules": self._search_referral,
        }.get(tool_name)

        if not handler:
            return f"Unknown tool: {tool_name}", []

        try:
            return await handler(tool_input)
        except Exception as e:
            log.error("tool_execution_failed", tool=tool_name, error=str(e))
            return f"Tool execution failed: {str(e)}", []

    async def _search_appetite(self, inp: dict) -> tuple[str, list[RetrievedChunk]]:
        query = inp["query"]
        search_type = inp.get("search_type", "hybrid")

        alpha = {"semantic": 0.9, "keyword": 0.1, "hybrid": 0.6}.get(search_type, 0.6)

        results = await self.vector.hybrid_search(
            query=query, top_k=6, alpha=alpha,
            filters={"doc_type": "reference"},
        )
        return self._format_results(results, query)

    async def _search_code(self, inp: dict) -> tuple[str, list[RetrievedChunk]]:
        code = inp["code"]
        results = await self.vector.search_by_code(code=code, top_k=5)
        return self._format_results(results, code)

    async def _search_loss(self, inp: dict) -> tuple[str, list[RetrievedChunk]]:
        lob = inp["lob"]
        query = inp["query"]
        results = await self.vector.hybrid_search(
            query=f"{lob} {query} loss ratio claims frequency",
            top_k=5, alpha=0.5,
            filters={"doc_type": "reference"},  # all appetite guide chunks tagged "reference"
        )
        return self._format_results(results, query)

    async def _search_referral(self, inp: dict) -> tuple[str, list[RetrievedChunk]]:
        query = inp["query"]
        lob = inp.get("lob", "")
        results = await self.vector.hybrid_search(
            query=f"referral authority escalation {query} {lob}",
            top_k=5, alpha=0.5,
        )
        return self._format_results(results, query)

    def _format_results(
        self, results: list[dict], query: str
    ) -> tuple[str, list[RetrievedChunk]]:
        """Format search results for Claude and return typed chunks."""
        if not results:
            return f"No results found for: {query}", []

        chunks = []
        lines = []

        for i, r in enumerate(results, 1):
            source = f"[{r['source_doc']}"
            if r.get("page"):
                source += f", p.{r['page']}"
            if r.get("section"):
                source += f", §{r['section']}"
            source += "]"

            lines.append(f"Result {i} {source} (score: {r['score']:.3f}):\n{r['text']}\n")

            chunks.append(RetrievedChunk(
                chunk_id=r["chunk_id"],
                text=r["text"],
                source_doc=r["source_doc"],
                section=r.get("section", ""),
                page=r.get("page"),
                score=r["score"],
                match_type="tool_retrieval",
            ))

        log.info(
            "tool_result",
            query=query,
            chunks_returned=len(chunks),
            top_score=chunks[0].score if chunks else 0,
        )
        return "\n".join(lines), chunks
