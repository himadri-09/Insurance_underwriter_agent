"""
One-time setup: create Pinecone index for appetite guide retrieval.
Run: python -m scripts.setup_pinecone
"""

from pinecone import Pinecone, ServerlessSpec
from app.core.config import get_settings


def create_index():
    s = get_settings()
    pc = Pinecone(api_key=s.pinecone_api_key)

    index_name = s.pinecone_index

    # Check if exists
    existing = [idx.name for idx in pc.list_indexes()]
    if index_name in existing:
        print(f"Index '{index_name}' already exists.")
        return

    # Create with hybrid search support (dense + sparse)
    pc.create_index(
        name=index_name,
        dimension=1536,  # text-embedding-3-small dimension
        metric="dotproduct",  # required for hybrid/sparse-dense
        spec=ServerlessSpec(
            cloud="aws",
            region="us-east-1",
        ),
    )
    print(f"Index '{index_name}' created successfully.")


if __name__ == "__main__":
    create_index()
