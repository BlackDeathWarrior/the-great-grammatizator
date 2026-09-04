"""Phase 0: the scaffold holds together and the compose invariants are honoured."""

import pathlib

import pytest
from fastapi.testclient import TestClient

from app.main import app

ROOT = pathlib.Path(__file__).resolve().parents[2]

# These assert facts about the REPO (compose file, docs), which .dockerignore
# keeps out of the runtime image. Skip rather than fail when running in-container.
repo_only = pytest.mark.skipif(
    not (ROOT / "docker-compose.yml").exists(),
    reason="repo-root files not present (running inside the image)",
)


def test_health_returns_ok():
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.p0
@repo_only
def test_service_names_not_localhost():
    """TC-1107: inside the container, services are addressed by name."""
    compose = (ROOT / "docker-compose.yml").read_text()
    for line in compose.splitlines():
        if "QDRANT_URL:" in line or "DATABASE_URL:" in line or "REDIS_URL:" in line:
            assert "localhost" not in line, f"localhost in compose env: {line.strip()}"
            assert "127.0.0.1" not in line, f"loopback in compose env: {line.strip()}"


@pytest.mark.p0
@repo_only
def test_persistent_volumes_are_mounted():
    """TC-1106: without these, embeddings vanish on `docker compose down`."""
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "./qdrant_storage:/qdrant/storage" in compose
    assert "./pg_data:/var/lib/postgresql/data" in compose


@repo_only
def test_doc_image_links_resolve():
    """Every relative image link in the docs must point at a file that exists.

    Two ways this has broken: the diagrams/*.svg links while the files sat flat
    beside them, and the v1 -> docs/v1/ move that left every path one level off.
    """
    for doc in sorted((ROOT / "docs").rglob("*.md")):
        for line in doc.read_text(encoding="utf-8").splitlines():
            if "![" not in line or "](" not in line:
                continue
            rel = line.split("](", 1)[1].split(")", 1)[0]
            if rel.startswith(("http://", "https://", "#")):
                continue
            assert (doc.parent / rel).exists(), f"{doc.name} links missing {rel}"
