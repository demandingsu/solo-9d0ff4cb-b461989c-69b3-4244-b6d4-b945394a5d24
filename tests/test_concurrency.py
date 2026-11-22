"""HTTP-level concurrency: parallel reviewer requests must not lose evidence."""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def live_server(tmp_path, monkeypatch):
    db_file = tmp_path / "answers.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_file))
    monkeypatch.setenv("SQLITE_BUSY_TIMEOUT_MS", "15000")
    import app.db as db_module
    import app.main as main_module

    db_module.DEFAULT_DB_PATH = str(db_file)
    port = _free_port()
    config = uvicorn.Config(
        main_module.app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        if server.started:
            break
        time.sleep(0.05)
    else:  # pragma: no cover
        raise RuntimeError("server did not start")
    try:
        yield base, db_file
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_parallel_reviews_see_all_saved_evidence(live_server):
    base, _ = live_server
    reviews = {
        "Q1": "Q1-A",
        "Q2": "Q2-C",
    }
    barrier = threading.Barrier(len(reviews))
    results: dict[str, httpx.Response] = {}

    def submit(question_id: str, option_id: str) -> None:
        barrier.wait(timeout=10)
        with httpx.Client(base_url=base, timeout=30) as client:
            results[question_id] = client.put(
                f"/sheets/S003/reviews/{question_id}",
                json={"option_id": option_id, "reviewer": f"http-{question_id}"},
            )

    threads = [threading.Thread(target=submit, args=item) for item in reviews.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=40)

    assert {q: r.status_code for q, r in results.items()} == {"Q1": 200, "Q2": 200}

    with httpx.Client(base_url=base, timeout=30) as client:
        detail = client.get("/sheets/S003").json()
        stats = {
            q["question_id"]: {o["id"]: o["count"] for o in q["options"]}
            for q in client.get("/stats").json()["questions"]
        }
        listing = {s["id"]: s["status"] for s in client.get("/sheets").json()["sheets"]}

    # Both reviews are present and both narrowed the candidate pool together.
    assert {r["question_id"]: r["option_id"] for r in detail["reviews"]} == reviews
    assert detail["result"]["candidate_form_ids"] == ["F3"]
    by_question = {q["question_id"]: q for q in detail["questions"]}
    assert by_question["Q1"]["derived_option_id"] == "Q1-A"
    assert by_question["Q2"]["derived_option_id"] == "Q2-C"

    # Counts equal the sum over per-sheet results (nothing lost or double counted).
    assert stats["Q1"]["Q1-A"] == 3
    assert stats["Q2"]["Q2-C"] == 2
    assert listing["S003"] == "complete"
