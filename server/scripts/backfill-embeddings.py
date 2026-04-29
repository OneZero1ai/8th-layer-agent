"""Embed every approved KU that doesn't yet have a vector.

Run inside the cq Remote container so it has IAM credentials and the
EFS-mounted /data/cq.db. Resumable: walks rows where embedding IS NULL
in batches, embeds each, UPDATEs the row.

Usage (inside container):
    /app/.venv/bin/python /app/scripts/backfill-embeddings.py

Env (mirrors the server):
    CQ_DB_PATH       /data/cq.db
    CQ_EMBED_MODEL   amazon.titan-embed-text-v2:0
    CQ_EMBED_REGION  us-east-1
    BACKFILL_BATCH   default 50
    BACKFILL_LIMIT   default 0 (no cap)
    BACKFILL_STATUS  default approved
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from cq_server.embed import compose_text, embed_text  # noqa: E402
from cq_server.store import RemoteStore  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill")


def _payload(data_str: str) -> tuple[str, str, str]:
    obj = json.loads(data_str)
    insight = obj.get("insight") or {}
    return (
        insight.get("summary", "") or "",
        insight.get("detail", "") or "",
        insight.get("action", "") or "",
    )


def main() -> int:
    db_path = Path(os.environ.get("CQ_DB_PATH", "/data/cq.db"))
    batch = int(os.environ.get("BACKFILL_BATCH", "50"))
    cap = int(os.environ.get("BACKFILL_LIMIT", "0"))
    status = os.environ.get("BACKFILL_STATUS", "approved")

    store = RemoteStore(db_path=db_path)
    log.info("backfill starting db=%s batch=%d cap=%d status=%s", db_path, batch, cap, status)

    embedded = 0
    skipped = 0
    failed = 0
    started = time.time()

    while True:
        rows = store.iter_unembedded(status=status, limit=batch)
        if not rows:
            log.info("no more unembedded rows")
            break
        for unit_id, data_str in rows:
            try:
                summary, detail, action = _payload(data_str)
                text = compose_text(summary, detail, action)
                if not text.strip():
                    skipped += 1
                    log.warning("ku %s has empty text, marking with sentinel embedding_model='skip:empty'", unit_id)
                    store.set_embedding(unit_id, b"", "skip:empty")
                    continue
                payload = embed_text(text)
                if payload is None:
                    failed += 1
                    log.warning("ku %s embed returned None", unit_id)
                    continue
                store.set_embedding(unit_id, payload[0], payload[1])
                embedded += 1
                if embedded % 25 == 0:
                    log.info("progress embedded=%d skipped=%d failed=%d", embedded, skipped, failed)
            except Exception:
                failed += 1
                log.exception("ku %s backfill failed", unit_id)
        if cap and embedded >= cap:
            log.info("hit cap=%d, stopping", cap)
            break

    elapsed = time.time() - started
    log.info("done embedded=%d skipped=%d failed=%d elapsed=%.1fs", embedded, skipped, failed, elapsed)
    store.close()
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
