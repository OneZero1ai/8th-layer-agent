"""Embedding generation via AWS Bedrock Titan.

Single-call, synchronous embedder. Returns packed float32 little-endian
bytes for storage; numpy unpacking happens in store.semantic_query.

Configured by environment:
    CQ_EMBED_MODEL    — Bedrock model ID (default: amazon.titan-embed-text-v2:0)
    CQ_EMBED_REGION   — AWS region (default: us-east-1)
    CQ_EMBED_ENABLED  — "true"/"false"; when false, embed_text returns None
                        (lets the server boot without Bedrock perms for tests)
"""

import json
import logging
import os
import struct
from functools import lru_cache

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "amazon.titan-embed-text-v2:0"
DEFAULT_REGION = "us-east-1"
DEFAULT_DIM = 1024


def model_id() -> str:
    return os.environ.get("CQ_EMBED_MODEL", DEFAULT_MODEL)


def is_enabled() -> bool:
    return os.environ.get("CQ_EMBED_ENABLED", "true").lower() != "false"


@lru_cache(maxsize=1)
def _client():
    import boto3
    from botocore.config import Config

    region = os.environ.get("CQ_EMBED_REGION", DEFAULT_REGION)
    # Bounded timeouts + no retries: an unbounded invoke_model hang
    # outlasts CloudFront's 30s origin timeout and 504s the whole
    # /propose request. Worst case here is ~13s; embed_text swallows
    # the failure and the embedding is backfilled later.
    cfg = Config(
        connect_timeout=3,
        read_timeout=10,
        retries={"max_attempts": 1},
    )
    return boto3.client("bedrock-runtime", region_name=region, config=cfg)


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def embed_text(text: str) -> tuple[bytes, str] | None:
    """Embed text via Bedrock Titan. Returns (bytes, model_id) or None on failure.

    Failures are logged and swallowed so propose still succeeds without
    embedding (backfill catches it later).
    """
    if not is_enabled():
        return None
    if not text or not text.strip():
        return None
    try:
        body = json.dumps({"inputText": text[:50_000]})  # Titan caps at ~50k chars
        resp = _client().invoke_model(
            modelId=model_id(),
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        payload = json.loads(resp["body"].read())
        vec = payload.get("embedding")
        if not vec:
            logger.warning("titan returned no embedding field")
            return None
        return _pack(vec), model_id()
    except Exception:
        logger.exception("bedrock embed_text failed")
        return None


def compose_text(summary: str, detail: str = "", action: str = "") -> str:
    """Compose the text to embed from KU fields. Concat with newlines."""
    parts = [p for p in (summary, detail, action) if p]
    return "\n".join(parts)
