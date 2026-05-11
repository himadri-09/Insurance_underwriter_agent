"""
LLM service:
  PDFs    → Azure GPT / Claude (full document, one call)
  Images  → Qwen via OpenRouter
  Reasoning → Azure GPT or Claude (based on config)
  Embeddings → Azure OpenAI
"""

import json
import base64
import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential
from app.core.config import get_settings

log = structlog.get_logger()


class LLMService:
    def __init__(self):
        self.settings = get_settings()
        self._anthropic_client = None
        self._azure_client = None

    @property
    def anthropic(self):
        if self._anthropic_client is None:
            import anthropic
            self._anthropic_client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        return self._anthropic_client

    @property
    def azure(self):
        if self._azure_client is None:
            from openai import AzureOpenAI
            self._azure_client = AzureOpenAI(
                azure_endpoint=self.settings.azure_openai_endpoint,
                api_key=self.settings.azure_openai_api_key,
                api_version=self.settings.azure_openai_api_version,
            )
        return self._azure_client

    # ── PDF extraction (Azure GPT or Claude — one call per PDF) ──

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30))
    async def extract_from_pdf(self, pdf_base64: str, prompt: str) -> dict:
        """Send full PDF to reasoning model in one call."""

        if self.settings.reasoning_provider == "anthropic":
            response = self.anthropic.messages.create(
                model=self.settings.reasoning_model,
                max_tokens=4096,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": pdf_base64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
                temperature=0.1,
            )
            content = response.content[0].text
        else:
            # Azure GPT-4o — send pages as images in one message
            from app.utils.document_processor import DocumentProcessor
            processor = DocumentProcessor()
            pdf_bytes = base64.b64decode(pdf_base64)
            pages = processor.split_pdf_to_pages(pdf_bytes)

            content_parts = []
            for page in pages:
                content_parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{page['image_base64']}",
                        "detail": "high",
                    },
                })
            content_parts.append({"type": "text", "text": prompt})

            response = self.azure.chat.completions.create(
                model=self.settings.reasoning_model,
                messages=[{"role": "user", "content": content_parts}],
                temperature=0.1,
                max_tokens=4096,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content

        log.info("pdf_extraction_done", provider=self.settings.reasoning_provider)
        return self._parse_json_safe(content)

    # ── Image extraction (always Qwen via OpenRouter) ─────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30))
    async def extract_from_image(
        self, image_base64: str, prompt: str, media_type: str = "image/png"
    ) -> dict:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.openrouter_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.settings.extraction_model,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_base64}"}},
                            {"type": "text", "text": prompt},
                        ],
                    }],
                    "temperature": 0.1,
                    "max_tokens": 4096,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            log.info("image_extraction_done", model=self.settings.extraction_model)
            return self._parse_json_safe(content)

    # ── Reasoning (Anthropic or Azure) ────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30))
    async def reason(
        self, system_prompt: str, user_prompt: str, response_format: str = "json"
    ) -> dict:
        log.info(
            "llm_call_starting",
            provider=self.settings.reasoning_provider,
            model=self.settings.reasoning_model,
            prompt_chars=len(system_prompt) + len(user_prompt),
            format=response_format,
        )
        if self.settings.reasoning_provider == "anthropic":
            kwargs = {
                "model": self.settings.reasoning_model,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": user_prompt}],
                "temperature": 0.2,
            }
            if system_prompt:
                kwargs["system"] = system_prompt
            message = self.anthropic.messages.create(**kwargs)
            content = message.content[0].text
        else:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": user_prompt})

            kwargs = {
                "model": self.settings.reasoning_model,
                "messages": messages,
                "temperature": 0.2,
                "max_tokens": 4096,
            }
            if response_format == "json":
                kwargs["response_format"] = {"type": "json_object"}

            response = self.azure.chat.completions.create(**kwargs)
            content = response.choices[0].message.content

        log.info("reasoning_done", provider=self.settings.reasoning_provider)

        if response_format == "json":
            return self._parse_json_safe(content)
        return content

    # ── Brief generation (returns text, not JSON) ─────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30))
    async def generate_brief(self, system_prompt: str, user_prompt: str) -> str:
        if self.settings.reasoning_provider == "anthropic":
            response = self.anthropic.messages.create(
                model=self.settings.reasoning_model,
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
                temperature=0.3,
            )
            return response.content[0].text
        else:
            response = self.azure.chat.completions.create(
                model=self.settings.reasoning_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=4096,
            )
            return response.choices[0].message.content or ""

    # ── Embeddings (always Azure OpenAI) ──────────────────

    def embed(self, text: str) -> list:
        resp = self.azure.embeddings.create(
            input=text,
            model=self.settings.embedding_deployment,
        )
        return resp.data[0].embedding

    # ── JSON parser ───────────────────────────────────────

    def _parse_json_safe(self, text: str) -> dict:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            log.error("json_parse_failed", raw=text[:200])
            return {"raw_text": text, "parse_error": True}