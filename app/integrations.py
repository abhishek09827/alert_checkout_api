"""HTTP evidence services consumed by OpsPilot.

Two small ASGI apps live here so they can be bound to the ports OpsPilot expects:

    infra_app  -> POST /logs/search, POST /deployments/search, POST /deployments/{id}   (:8100)
    rag_app    -> POST /search, POST /documents/{id}                                     (:8200)

They read the service's own artefacts - var/log/checkout-api.log,
ops/deployments.json and the markdown docs - so the evidence OpsPilot
retrieves is real, not fabricated.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException

from app.core.config import get_settings

settings = get_settings()
REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = REPO_ROOT / settings.log_file
DEPLOYMENTS_FILE = REPO_ROOT / "ops" / "deployments.json"
DOCS_DIR = REPO_ROOT / "docs"

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is",
    "it", "of", "on", "or", "the", "to", "with", "service", "checkout", "api",
}


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        moment = datetime.fromisoformat(text)
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _tokens(text: str) -> list[str]:
    # Split CamelCase so "PostgresConnectionPoolExhaustion" indexes as its words.
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return [t for t in re.split(r"[^a-zA-Z0-9_]+", text.lower()) if t and t not in STOPWORDS]


# --------------------------------------------------------------------------- logs
def _render_log(record: dict[str, Any], fallback: str) -> str:
    parts: list[str] = []
    if record.get("service_name"):
        parts.append(f"service={record['service_name']}")
    if record.get("environment"):
        parts.append(f"environment={record['environment']}")
    parts.append(f"event={record.get('event', 'log')}")
    for key in ("error_code", "error_type", "error", "error_message", "status_code",
                "duration_ms", "request_id", "route", "dependency", "order_number"):
        value = record.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={value}")
    return " ".join(parts) or fallback


def search_logs(payload: dict[str, Any]) -> dict[str, Any]:
    if not LOG_FILE.exists():
        raise HTTPException(status_code=503, detail=f"log file unavailable: {LOG_FILE}")

    service = (payload.get("service") or settings.service_name).strip()
    start = _parse_ts(payload.get("start_time"))
    end = _parse_ts(payload.get("end_time"))
    query = (payload.get("query") or "").strip().lower()
    try:
        limit = max(1, min(int(payload.get("limit") or 100), 1000))
    except (TypeError, ValueError):
        limit = 100

    matched: list[dict[str, Any]] = []
    with LOG_FILE.open(encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if service and record.get("service_name") not in (None, service):
                continue
            moment = _parse_ts(record.get("timestamp"))
            if start and moment and moment < start:
                continue
            if end and moment and moment > end:
                continue
            message = _render_log(record, line[:400])
            if query and query not in message.lower():
                continue
            matched.append(
                {
                    "id": f"log-{index}",
                    "timestamp": moment.isoformat() if moment else record.get("timestamp", ""),
                    "message": message,
                    "level": record.get("level", ""),
                    "event": record.get("event", ""),
                    "request_id": record.get("request_id", ""),
                    "route": record.get("route", ""),
                }
            )

    matched.sort(key=lambda item: item["timestamp"] or "")
    return {"items": matched[-limit:], "total": len(matched), "source": str(LOG_FILE)}


# ------------------------------------------------------------------- deployments
def _load_deployments() -> list[dict[str, Any]]:
    if not DEPLOYMENTS_FILE.exists():
        raise HTTPException(status_code=503, detail=f"deployment records unavailable: {DEPLOYMENTS_FILE}")
    try:
        records = json.loads(DEPLOYMENTS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=503, detail=f"deployment records unreadable: {exc}") from exc
    return records if isinstance(records, list) else []


def search_deployments(payload: dict[str, Any]) -> dict[str, Any]:
    service = (payload.get("service") or settings.service_name).strip()
    environment = (payload.get("environment") or "").strip()
    start = _parse_ts(payload.get("start_time"))
    end = _parse_ts(payload.get("end_time"))

    items: list[dict[str, Any]] = []
    for record in _load_deployments():
        if service and record.get("service") not in (None, service):
            continue
        if environment and record.get("environment") != environment:
            continue
        moment = _parse_ts(record.get("timestamp"))
        if start and moment and moment < start:
            continue
        if end and moment and moment > end:
            continue
        items.append(
            {
                "id": record.get("id"),
                "timestamp": record.get("timestamp"),
                "commit_sha": record.get("commit_sha", ""),
                "status": record.get("status", ""),
                "environment": record.get("environment", ""),
                "version": record.get("version", ""),
                "deployed_by": record.get("deployed_by", ""),
            }
        )

    items.sort(key=lambda item: item["timestamp"] or "")
    return {"items": items, "total": len(items), "source": str(DEPLOYMENTS_FILE)}


def get_deployment(deployment_id: str) -> dict[str, Any]:
    record = next((r for r in _load_deployments() if r.get("id") == deployment_id), None)
    if record is None:
        raise HTTPException(status_code=404, detail=f"deployment {deployment_id} not found")
    return {
        "content": "\n".join(f"{key}: {value}" for key, value in record.items()),
        "deployment": record,
    }


# ------------------------------------------------------------------------- RAG
def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "section"


def _doc_sources() -> list[tuple[str, Path]]:
    sources: list[tuple[str, Path]] = []
    runbooks_dir = DOCS_DIR / "runbooks"
    runbooks = sorted(runbooks_dir.glob("*.md")) if runbooks_dir.exists() else []
    sources += [(f"runbook:{path.stem}", path) for path in runbooks]
    for name in ("operations.md", "architecture.md"):
        path = DOCS_DIR / name
        if path.exists():
            sources.append((f"doc:{path.stem}", path))
    if (REPO_ROOT / "README.md").exists():
        sources.append(("readme", REPO_ROOT / "README.md"))
    return sources


def _chunks() -> list[dict[str, str]]:
    chunks: list[dict[str, str]] = []
    for doc_id, path in _doc_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for section in re.split(r"\n(?=##+ )", text):
            section = section.strip()
            if len(section) < 40:
                continue
            heading = section.splitlines()[0].lstrip("# ").strip()
            chunks.append(
                {
                    "id": f"{doc_id}#{_slug(heading)[:80]}",
                    "content": section[:4000],
                    "title": heading,
                    "source": str(path.relative_to(REPO_ROOT)),
                }
            )
    return chunks


def search_documents(payload: dict[str, Any]) -> dict[str, Any]:
    query = (payload.get("query") or "").strip()
    try:
        top_k = max(1, min(int(payload.get("top_k") or 3), 10))
    except (TypeError, ValueError):
        top_k = 3

    query_tokens = _tokens(query)
    scored: list[tuple[int, dict[str, str]]] = []
    for chunk in _chunks():
        heading_tokens = set(_tokens(chunk["title"]))
        body_tokens = set(_tokens(chunk["content"]))
        score = sum(
            2 if token in heading_tokens else 1
            for token in query_tokens
            if token in body_tokens or token in heading_tokens
        )
        if score:
            scored.append((score, chunk))

    scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    items = [
        {"id": c["id"], "content": c["content"], "title": c["title"], "source": c["source"], "score": score}
        for score, c in scored[:top_k]
    ]
    if not items and query_tokens:
        fallback = _chunks()[:1]
        items = [
            {"id": c["id"], "content": c["content"], "title": c["title"], "source": c["source"], "score": 0}
            for c in fallback
        ]
    # OpsPilot's provider reads "items"; docs/HTTP_INTEGRATIONS.md documents "chunks".
    return {"items": items, "chunks": items, "total": len(items)}


def get_document(doc_id: str) -> dict[str, Any]:
    match = next((c for c in _chunks() if c["id"] == doc_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    return {"content": match["content"], "id": match["id"], "source": match["source"]}


# ------------------------------------------------------------------------- apps
infra_app = FastAPI(title="checkout-api-integrations", version="1.0")
rag_app = FastAPI(title="checkout-api-rag", version="1.0")


@infra_app.get("/health")
@rag_app.get("/health")
async def _health() -> dict[str, str]:
    return {"status": "healthy"}


@infra_app.post("/logs/search")
async def _logs_search(payload: dict = Body(default={})) -> dict[str, Any]:  # noqa: B008
    return search_logs(payload)


@infra_app.post("/deployments/search")
async def _deployments_search(payload: dict = Body(default={})) -> dict[str, Any]:  # noqa: B008
    return search_deployments(payload)


@infra_app.post("/deployments/{deployment_id}")
async def _deployment_get(deployment_id: str, payload: dict = Body(default={})) -> dict[str, Any]:  # noqa: B008
    return get_deployment(deployment_id)


@rag_app.post("/search")
async def _search(payload: dict = Body(default={})) -> dict[str, Any]:  # noqa: B008
    return search_documents(payload)


@rag_app.post("/documents/{doc_id}")
async def _document_get(doc_id: str, payload: dict = Body(default={})) -> dict[str, Any]:  # noqa: B008
    return get_document(doc_id)

