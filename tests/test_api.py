from __future__ import annotations

import importlib
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """Fresh database per test, seeded with the demo dataset on startup."""

    db_file = tmp_path / "answers.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_file))
    monkeypatch.setenv("SQLITE_BUSY_TIMEOUT_MS", "15000")
    import app.db as db_module
    import app.main as main_module

    importlib.reload(db_module)
    importlib.reload(main_module)

    with TestClient(main_module.app) as client:
        yield client, db_file


def stats(client: TestClient) -> dict[str, dict[str, int]]:
    body = client.get("/stats").json()
    return {
        q["question_id"]: {o["id"]: o["count"] for o in q["options"]}
        for q in body["questions"]
    }


def counted(client: TestClient) -> dict[str, int]:
    return {q["question_id"]: q["counted_sheets"] for q in client.get("/stats").json()["questions"]}


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_seed_states(app_env):
    client, _ = app_env
    expected = {
        "S001": ("complete", ["F2"]),
        "S002": ("complete", ["F3"]),
        "S003": ("partial", ["F1", "F2", "F3", "F4"]),
        "S004": ("pending", []),
        "S005": ("complete", ["F1"]),
    }
    for sheet_id, (state, candidates) in expected.items():
        body = client.get(f"/sheets/{sheet_id}").json()
        assert body["result"]["status"] == state, sheet_id
        assert body["result"]["candidate_form_ids"] == candidates, sheet_id


def test_only_unanimous_questions_are_counted(app_env):
    client, _ = app_env
    body = client.get("/sheets/S003").json()
    assert body["result"]["candidate_form_ids"] == ["F1", "F2", "F3", "F4"]
    assert body["result"]["counted_question_count"] == 0
    for question in body["questions"]:
        assert question["status"] == "ambiguous_candidates"
        assert question["counted"] is False
        assert question["derived_option_id"] is None
        # Every candidate form contributes its own reading.
        assert {d["form_id"] for d in question["derived_by_form"]} == {"F1", "F2", "F3", "F4"}


def test_no_candidate_form_makes_whole_sheet_pending(app_env):
    client, _ = app_env
    body = client.get("/sheets/S004").json()
    assert body["result"]["status"] == "pending"
    assert body["result"]["candidate_form_ids"] == []
    assert all(not q["counted"] for q in body["questions"])
    reasons = body["result"]["pending_reasons"]
    assert reasons[0]["scope"] == "sheet"
    assert reasons[0]["reason"] == "no_candidate_form"
    assert {r["question_id"] for r in reasons if r["scope"] == "question"} == {
        "Q1",
        "Q2",
        "Q3",
        "Q4",
        "Q5",
    }
    # Whole sheet contributes nothing to the distribution.
    assert counted(client) == {"Q1": 3, "Q2": 3, "Q3": 3, "Q4": 3, "Q5": 3}
    assert all(q["counted_sheets"] == 3 for q in client.get("/stats").json()["questions"])


def test_known_form_restricts_candidate_pool(app_env):
    client, _ = app_env
    body = client.get("/sheets/S002").json()
    assert body["result"]["candidate_form_ids"] == ["F3"]
    assert body["result"]["candidate_evaluation"][0]["form_id"] == "F3"
    assert body["result"]["status"] == "complete"


def test_question_missing_from_candidate_form_is_not_counted(app_env):
    """A candidate form that does not print a question blocks only that question."""

    client, _ = app_env
    original = client.post("/admin/recompute")
    assert original.status_code == 200

    # Build a dataset where form F2 omits Q5 entirely.
    from app import seed

    dataset = {
        "questions": seed.QUESTION_BANK,
        "forms": [
            {**form, "questions": [q for q in form["questions"] if q["question_id"] != "Q5"]}
            if form["id"] == "F2"
            else form
            for form in seed.FORMS
        ],
        "sheets": [
            {
                "id": "S900",
                "label": "缺题卷",
                "known_form_id": None,
                "answers": seed._scan(
                    "F2", {"Q1": "Q1-A", "Q2": "Q2-C", "Q3": "Q3-B", "Q4": "Q4-B"}
                ),
                "reviews": [
                    {"question_id": "Q1", "option_id": "Q1-A", "reviewer": "r"},
                    {"question_id": "Q2", "option_id": "Q2-C", "reviewer": "r"},
                ],
            }
        ],
    }
    response = client.post("/admin/import", json=dataset)
    assert response.status_code == 200, response.text
    body = client.get("/sheets/S900").json()
    assert body["result"]["candidate_form_ids"] == ["F2"]
    by_question = {q["question_id"]: q for q in body["questions"]}
    assert by_question["Q5"]["status"] == "candidate_incomplete"
    assert by_question["Q5"]["counted"] is False
    assert "F2" in by_question["Q5"]["detail"]
    # Other questions still count.
    assert by_question["Q1"]["counted"] is True
    assert by_question["Q1"]["derived_option_id"] == "Q1-A"


# ---------------------------------------------------------------------------
# Review register / withdraw
# ---------------------------------------------------------------------------


def test_register_narrows_candidates_and_updates_counts(app_env):
    client, _ = app_env
    before = stats(client)
    assert before["Q1"]["Q1-A"] == 2
    untouched_sheet = client.get("/sheets/S001").json()
    untouched_counts = {q["question_id"]: q for q in client.get("/stats").json()["questions"]}

    response = client.put(
        "/sheets/S003/reviews/Q1", json={"option_id": "Q1-A", "reviewer": "reviewer-9"}
    )
    assert response.status_code == 200
    assert response.json()["result"]["candidate_form_ids"] == ["F2", "F3", "F4"]
    assert response.json()["result"]["counted_question_count"] == 1

    after = stats(client)
    assert after["Q1"]["Q1-A"] == 3
    # Other questions untouched.
    assert after["Q2"] == before["Q2"]

    # Only the reviewed sheet was recomputed; other sheets keep their result rows.
    assert client.get("/sheets/S001").json() == untouched_sheet
    for q in client.get("/stats").json()["questions"]:
        if q["question_id"] != "Q1":
            assert q["options"] == untouched_counts[q["question_id"]]["options"]

    detail = client.get("/sheets/S003").json()
    assert detail["reviews"] == [
        {
            "question_id": "Q1",
            "option_id": "Q1-A",
            "option_label": "A",
            "reviewer": "reviewer-9",
            "updated_at": detail["reviews"][0]["updated_at"],
        }
    ]


def test_withdraw_restores_previous_counts(app_env):
    client, _ = app_env
    before = stats(client)
    assert client.put("/sheets/S003/reviews/Q1", json={"option_id": "Q1-A"}).status_code == 200
    assert stats(client)["Q1"]["Q1-A"] == before["Q1"]["Q1-A"] + 1

    response = client.delete("/sheets/S003/reviews/Q1")
    assert response.status_code == 200
    assert response.json()["result"]["candidate_form_ids"] == ["F1", "F2", "F3", "F4"]
    assert stats(client) == before
    assert client.get("/sheets/S003").json()["reviews"] == []

    # Withdrawing again is a 404 and changes nothing.
    assert client.delete("/sheets/S003/reviews/Q1").status_code == 404
    assert stats(client) == before


def test_register_replaces_existing_review(app_env):
    client, _ = app_env
    # S003 has no reviews; register Q1-A then overwrite with Q1-C.
    before = stats(client)
    assert client.put("/sheets/S003/reviews/Q1", json={"option_id": "Q1-A"}).status_code == 200
    assert stats(client)["Q1"]["Q1-A"] == before["Q1"]["Q1-A"] + 1

    response = client.put("/sheets/S003/reviews/Q1", json={"option_id": "Q1-C"})
    assert response.status_code == 200
    body = client.get("/sheets/S003").json()
    assert body["result"]["candidate_form_ids"] == ["F1"]
    by_question = {q["question_id"]: q for q in body["questions"]}
    assert by_question["Q1"]["derived_option_id"] == "Q1-C"

    after = stats(client)
    assert after["Q1"]["Q1-A"] == before["Q1"]["Q1-A"]
    assert after["Q1"]["Q1-C"] == before["Q1"]["Q1-C"] + 1
    assert len(client.get("/sheets/S003").json()["reviews"]) == 1


def test_register_validation(app_env):
    client, _ = app_env
    assert (
        client.put("/sheets/S003/reviews/Q1", json={"option_id": "Q2-A"}).status_code == 422
    )
    assert (
        client.put("/sheets/S003/reviews/NOPE", json={"option_id": "Q1-A"}).status_code == 404
    )
    assert (
        client.put("/sheets/NOPE/reviews/Q1", json={"option_id": "Q1-A"}).status_code == 404
    )
    assert client.put("/sheets/S003/reviews/Q1", json={"option_id": ""}).status_code == 422


def test_registering_contradictory_review_makes_sheet_pending(app_env):
    client, _ = app_env
    # S003 scan is F4; review Q1 as Q1-B, which no form supports at the scanned label.
    response = client.put("/sheets/S003/reviews/Q1", json={"option_id": "Q1-B"})
    assert response.status_code == 200
    body = client.get("/sheets/S003").json()
    assert body["result"]["status"] == "pending"
    assert body["result"]["candidate_form_ids"] == []
    # Previously counted questions are removed from the distribution.
    expected = counted(client)
    assert expected["Q1"] == 3  # S001, S002, S005 (S003 back out)


# ---------------------------------------------------------------------------
# Transactional guarantees
# ---------------------------------------------------------------------------


def test_failed_write_keeps_review_result_and_counts(app_env, monkeypatch):
    client, db_file = app_env
    before_sheet = client.get("/sheets/S003").json()
    before_stats = stats(client)

    from app import engine

    def explode(conn, old, new):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(engine, "apply_count_delta", explode)
    response = client.put("/sheets/S003/reviews/Q1", json={"option_id": "Q1-A"})
    assert response.status_code == 503

    assert client.get("/sheets/S003").json() == before_sheet
    assert stats(client) == before_stats
    # No half-written review.
    conn = sqlite3.connect(db_file)
    try:
        assert conn.execute("SELECT COUNT(*) FROM reviews WHERE sheet_id='S003'").fetchone()[0] == 0
    finally:
        conn.close()


def test_concurrent_reviews_use_all_saved_evidence(app_env):
    client, db_file = app_env
    import app.db as db_module
    from app import engine

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def review(question_id: str, option_id: str) -> None:
        try:
            barrier.wait(timeout=5)

            def work(conn):
                return engine.apply_review_change(
                    conn,
                    sheet_id="S003",
                    question_id=question_id,
                    option_id=option_id,
                    reviewer=f"t-{question_id}",
                    computed_at=engine.now_iso(),
                )

            db_module.run_write(db_file, work)
        except BaseException as exc:  # noqa: BLE001 - surface in the main thread
            errors.append(exc)

    threads = [
        threading.Thread(target=review, args=("Q1", "Q1-A")),
        threading.Thread(target=review, args=("Q2", "Q2-C")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not errors, errors

    conn = sqlite3.connect(db_file)
    try:
        reviews = dict(
            conn.execute("SELECT question_id, option_id FROM reviews WHERE sheet_id='S003'")
        )
    finally:
        conn.close()
    assert reviews == {"Q1": "Q1-A", "Q2": "Q2-C"}

    body = client.get("/sheets/S003").json()
    # Both reviews were applied: only F3 satisfies Q1->A and Q2->C together.
    assert body["result"]["candidate_form_ids"] == ["F3"]
    by_question = {q["question_id"]: q for q in body["questions"]}
    assert by_question["Q1"]["derived_option_id"] == "Q1-A"
    assert by_question["Q2"]["derived_option_id"] == "Q2-C"

    # Aggregate counts match the persisted per-sheet rows (no lost update).
    conn = sqlite3.connect(db_file)
    try:
        persisted = dict(
            conn.execute(
                "SELECT question_id, derived_option_id FROM sheet_question_results "
                "WHERE sheet_id='S003' AND counted=1"
            )
        )
        agg = {
            (q, o): n
            for q, o, n in conn.execute(
                "SELECT question_id, option_id, count FROM question_option_counts"
            )
        }
        expected = {}
        for q, o in conn.execute(
            "SELECT question_id, derived_option_id FROM sheet_question_results WHERE counted=1"
        ):
            expected[(q, o)] = expected.get((q, o), 0) + 1
    finally:
        conn.close()
    expected_persisted = {
        "Q1": "Q1-A",
        "Q2": "Q2-C",
        # The remaining questions are unanimous across the only candidate, F3.
        "Q3": "Q3-D",
        "Q4": "Q4-D",
        "Q5": "Q5-B",
    }
    assert persisted == expected_persisted
    assert agg == expected


def test_import_validation_rejects_inconsistent_mappings(app_env):
    client, _ = app_env
    from app import seed

    broken = {
        "questions": seed.QUESTION_BANK,
        "forms": [
            {
                "id": "FX",
                "name": "坏卷型",
                "questions": [
                    {
                        "paper_no": 1,
                        "question_id": "Q1",
                        # Missing standard options -> must be rejected.
                        "options": [{"option_id": "Q1-A", "printed_label": "A"}],
                    }
                ],
            }
        ],
        "sheets": [],
    }
    response = client.post("/admin/import", json=broken)
    assert response.status_code == 422

    # Original data still intact.
    assert client.get("/sheets/S001").status_code == 200


def test_import_is_fully_replaced(app_env):
    client, _ = app_env
    from app import seed

    dataset = {
        "questions": seed.QUESTION_BANK,
        "forms": seed.FORMS,
        "sheets": [sheet for sheet in seed.SHEETS if sheet["id"] != "S004"],
    }
    response = client.post("/admin/import", json=dataset)
    assert response.status_code == 200, response.text
    assert response.json()["sheets"] == 4
    assert client.get("/sheets/S004").status_code == 404
    assert counted(client)["Q1"] == 3
    assert client.get("/stats").json()["questions"][0]["total_sheets"] == 4
