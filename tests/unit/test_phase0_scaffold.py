"""Phase 0: the scaffold holds together and the compose invariants are honoured."""

import pathlib

import pytest
from fastapi.testclient import TestClient

from app.main import app

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_health_returns_ok():
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.p0
def test_service_names_not_localhost():
    """TC-1107: inside the container, services are addressed by name."""
    compose = (ROOT / "docker-compose.yml").read_text()
    for line in compose.splitlines():
        if "QDRANT_URL:" in line or "DATABASE_URL:" in line or "REDIS_URL:" in line:
            assert "localhost" not in line, f"localhost in compose env: {line.strip()}"
            assert "127.0.0.1" not in line, f"loopback in compose env: {line.strip()}"


@pytest.mark.p0
def test_persistent_volumes_are_mounted():
    """TC-1106: without these, embeddings vanish on `docker compose down`."""
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "./qdrant_storage:/qdrant/storage" in compose
    assert "./pg_data:/var/lib/postgresql/data" in compose


def test_doc_diagram_links_resolve():
    """Both docs linked diagrams/*.svg while the files sat in docs/ (fixed Phase 0)."""
    for doc in ("ARCHITECTURE.md", "USE-CASES.md"):
        text = (ROOT / "docs" / doc).read_text(encoding="utf-8")
        for line in text.splitlines():
            if "](diagrams/" in line:
                rel = line.split("](", 1)[1].split(")", 1)[0]
                assert (ROOT / "docs" / rel).exists(), f"{doc} links missing {rel}"
