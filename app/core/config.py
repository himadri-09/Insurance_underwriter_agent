from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Supabase
    supabase_url: str
    supabase_key: str
    supabase_service_key: str

    # Pinecone
    pinecone_api_key: str
    pinecone_index: str = "triagepilot-appetite"

    # Provider keys
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_api_version: str = "2024-12-01-preview"

    # LlamaParse
    llama_cloud_api_key: str = ""
    llama_parse_workers: int = 8

    # App
    app_env: str = "development"
    log_level: str = "DEBUG"
    max_upload_size_mb: int = 50
    allowed_extensions: str = "pdf,png,jpg,jpeg,tiff"
    skip_classification: bool = True  # skip separate classification stage

    # Models
    extraction_model: str = "qwen/qwen2.5-vl-72b-instruct"
    reasoning_model: str = "claude-sonnet-4-20250514"
    embedding_deployment: str = "text-embedding-3-small"
    embedding_dimension: int = 1536

    @property
    def allowed_ext_list(self) -> list:
        return [e.strip() for e in self.allowed_extensions.split(",")]

    @property
    def reasoning_provider(self) -> str:
        return "anthropic" if self.reasoning_model.startswith("claude") else "azure"

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()