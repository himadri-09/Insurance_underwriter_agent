"""
Supabase service: file storage + submission persistence.
All DB queries are scoped by user_id.
"""

import structlog
from supabase import create_client, Client
from typing import Union, Optional
from app.core.config import get_settings

log = structlog.get_logger()


def get_supabase() -> Client:
    s = get_settings()
    return create_client(s.supabase_url, s.supabase_service_key)


class StorageService:
    """Upload/download files to Supabase Storage."""

    BUCKET = "submissions"

    def __init__(self):
        self.client = get_supabase()

    async def upload_file(
        self, submission_id: str, filename: str, file_bytes: bytes, content_type: str
    ) -> str:
        path = f"{submission_id}/{filename}"
        self.client.storage.from_(self.BUCKET).upload(
            path, file_bytes, {"content-type": content_type}
        )
        log.info("file_uploaded", path=path, size=len(file_bytes))
        return path

    async def download_file(self, path: str) -> bytes:
        return self.client.storage.from_(self.BUCKET).download(path)

    async def get_public_url(self, path: str) -> str:
        return self.client.storage.from_(self.BUCKET).get_public_url(path)


class SubmissionDB:
    """CRUD for submissions — all queries scoped by user_id."""

    TABLE = "submissions"

    def __init__(self):
        self.client = get_supabase()

    async def create_submission(self, submission_id: str, data: dict) -> dict:
        result = (
            self.client.table(self.TABLE)
            .insert({"id": submission_id, **data})
            .execute()
        )
        return result.data[0] if result.data else {}

    async def update_submission(
        self, submission_id: str, user_id: str, data: dict
    ) -> dict:
        result = (
            self.client.table(self.TABLE)
            .update(data)
            .eq("id", submission_id)
            .eq("user_id", user_id)
            .execute()
        )
        return result.data[0] if result.data else {}

    async def get_submission(
        self, submission_id: str, user_id: str
    ) -> Union[dict, None]:
        result = (
            self.client.table(self.TABLE)
            .select("*")
            .eq("id", submission_id)
            .eq("user_id", user_id)
            .single()
            .execute()
        )
        return result.data

    async def list_submissions(
        self, user_id: str, limit: int = 20, offset: int = 0
    ) -> list[dict]:
        result = (
            self.client.table(self.TABLE)
            .select("id,created_at,status,lob,insured_name,appetite_score,appetite_status,winnability,priority,queue,referral_required,processing_time")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        return result.data or []

    async def delete_submission(
        self, submission_id: str, user_id: str
    ) -> bool:
        result = (
            self.client.table(self.TABLE)
            .delete()
            .eq("id", submission_id)
            .eq("user_id", user_id)
            .execute()
        )
        return bool(result.data)
