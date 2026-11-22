"""Wrong-answer pair tracking: same-sheet co-occurrence of two target options."""

from __future__ import annotations

import importlib
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

PAIR_AB = {
    "first_question_id": "Q1",
    "first_option_id": "Q1-A",
    "second_question_id": "Q2",
    "second_option_id": "Q2-B",
}


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


def set_pair(client: TestClient, payload: dict | None = None) -> dict:
    response = client.put("/pair-tracking", json=payload or PAIR_AB)
    assert response.status_code == 200, response.text
    return response.json()


def forms_by_id(body: dict) -> dict[str, dict]:
    return {form["form_id"]: form for form in body["forms"]}


def pair_summary_rows(db_file) -> dict[str, tuple[int, int, int]]:
    conn = sqlite3.connect(db_file)
    try:
        return {
            form_id: (feasible, denominator, numerator)
            for form_id, feasible, denominator, numerator in conn.execute(
                "SELECT form_id, feasible, denominator, numerator FROM pair_form_summary"
            )
        }
    finally:
        conn.close()


def pair_rows_aggregated(db_file) -> dict[str, tuple[int, int, int]]:
    """Full aggregation over the per-sheet rows: the summary must equal this."""

    conn = sqlite3.connect(db_file)
    try:
        return {
            form_id: (feasible, denominator, numerator)
            for form_id, feasible, denominator, numerator in conn.execute(
                "SELECT form_id, COUNT(*), SUM(first_hit), "
                "       SUM(CASE WHEN first_hit = 1 AND second_hit = 1 THEN 1 ELSE 0 END) "
                "FROM pair_sheet_forms GROUP BY form_id"
            )
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Set / query
# ---------------------------------------------------------------------------


def test_get_before_set_is_404(app_env):
    client, _ = app_env
    response = client.get("/pair-tracking")
    assert response.status_code == 404
    assert client.delete("/pair-tracking").status_code == 404


def test_set_and_query_pair_summary(app_env):
    client, _ = app_env
    body = set_pair(client)

    tracking = body["tracking"]
    assert tracking["first"]["question_id"] == "Q1"
    assert tracking["first"]["option_id"] == "Q1-A"
    assert tracking["first"]["option_label"] == "A"
    assert tracking["second"]["question_id"] == "Q2"
    assert tracking["second"]["option_id"] == "Q2-B"

    # Hand-computed from the seed data (see README demo):
    # S001xF2: Q1-A hit / Q2-C; S002xF3: Q1-C; S003xF1: Q1-C; S003xF2: Q1-A / Q2-A;
    # S003xF3: Q1-A / Q2-C; S003xF4: Q1-A / Q2-A; S005xF1: Q1-A / Q2-B hit.
    # S004 is pending (no candidate form) and contributes nothing.
    forms = forms_by_id(body)
    assert forms["F1"] | {} == {
        "form_id": "F1",
        "form_name": forms["F1"]["form_name"],
        "feasible_sheets": 2,
        "denominator": 1,
        "numerator": 1,
        "proportion": 1.0,
    }
    assert (forms["F2"]["feasible_sheets"], forms["F2"]["denominator"], forms["F2"]["numerator"]) == (2, 2, 0)
    assert forms["F2"]["proportion"] == 0.0
    assert (forms["F3"]["feasible_sheets"], forms["F3"]["denominator"], forms["F3"]["numerator"]) == (2, 1, 0)
    assert forms["F3"]["proportion"] == 0.0
    assert (forms["F4"]["feasible_sheets"], forms["F4"]["denominator"], forms["F4"]["numerator"]) == (1, 1, 0)
    assert forms["F4"]["proportion"] == 0.0

    assert body["feasible_combinations"] == 7
    assert body["proportion_range"] == {
        "min": 0.0,
        "max": 1.0,
        "min_form_ids": ["F2", "F3", "F4"],
        "max_form_ids": ["F1"],
    }
    assert body["zero_denominator_reason"] is None

    # GET reads the same maintained summary.
    assert client.get("/pair-tracking").json() == body


def test_only_one_pair_tracked_at_a_time(app_env):
    client, _ = app_env
    set_pair(client)
    replacement = set_pair(
        client,
        {
            "first_question_id": "Q3",
            "first_option_id": "Q3-B",
            "second_question_id": "Q4",
            "second_option_id": "Q4-B",
        },
    )
    assert replacement["tracking"]["first"]["question_id"] == "Q3"
    assert replacement["tracking"]["second"]["question_id"] == "Q4"

    # Summary was rebuilt for the new pair (hand-computed from the seed data).
    forms = forms_by_id(replacement)
    assert (forms["F1"]["denominator"], forms["F1"]["numerator"]) == (1, 1)
    assert (forms["F2"]["denominator"], forms["F2"]["numerator"]) == (2, 1)
    assert forms["F2"]["proportion"] == 0.5
    assert (forms["F3"]["denominator"], forms["F3"]["numerator"]) == (0, 0)
    assert forms["F3"]["proportion"] is None
    assert (forms["F4"]["denominator"], forms["F4"]["numerator"]) == (0, 0)
    assert replacement["proportion_range"] == {
        "min": 0.5,
        "max": 1.0,
        "min_form_ids": ["F2"],
        "max_form_ids": ["F1"],
    }

    current = client.get("/pair-tracking").json()
    assert current["tracking"]["first"]["question_id"] == "Q3"
    assert current["tracking"]["second"]["question_id"] == "Q4"


def test_set_validation(app_env):
    client, _ = app_env
    # Same question twice.
    assert (
        client.put(
            "/pair-tracking",
            json={
                "first_question_id": "Q1",
                "first_option_id": "Q1-A",
                "second_question_id": "Q1",
                "second_option_id": "Q1-B",
            },
        ).status_code
        == 422
    )
    # Option belongs to another question.
    assert (
        client.put(
            "/pair-tracking",
            json={
                "first_question_id": "Q1",
                "first_option_id": "Q2-A",
                "second_question_id": "Q2",
                "second_option_id": "Q2-B",
            },
        ).status_code
        == 422
    )
    # Unknown question / empty ids.
    assert (
        client.put(
            "/pair-tracking",
            json={
                "first_question_id": "NOPE",
                "first_option_id": "Q1-A",
                "second_question_id": "Q2",
                "second_option_id": "Q2-B",
            },
        ).status_code
        == 404
    )
    assert (
        client.put(
            "/pair-tracking",
            json={
                "first_question_id": "Q1",
                "first_option_id": "",
                "second_question_id": "Q2",
                "second_option_id": "Q2-B",
            },
        ).status_code
        == 422
    )
    # Nothing was set by the failed attempts.
    assert client.get("/pair-tracking").status_code == 404


def test_delete_clears_tracking(app_env):
    client, db_file = app_env
    set_pair(client)
    assert client.delete("/pair-tracking").json() == {"cleared": True}
    assert client.get("/pair-tracking").status_code == 404
    assert client.delete("/pair-tracking").status_code == 404
    assert pair_summary_rows(db_file) == {}

    conn = sqlite3.connect(db_file)
    try:
        assert conn.execute("SELECT COUNT(*) FROM pair_sheet_forms").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM pair_tracking").fetchone()[0] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Review register / withdraw: only the touched sheet's contribution changes
# ---------------------------------------------------------------------------


def test_register_and_withdraw_update_summary_incrementally(app_env):
    client, db_file = app_env
    before = set_pair(client)

    def other_sheet_rows() -> list[tuple]:
        conn = sqlite3.connect(db_file)
        try:
            return conn.execute(
                "SELECT sheet_id, form_id, first_hit, second_hit FROM pair_sheet_forms "
                "WHERE sheet_id != 'S003' ORDER BY sheet_id, form_id"
            ).fetchall()
        finally:
            conn.close()

    untouched = other_sheet_rows()

    # S003 has candidates F1..F4; reviewing Q1 -> Q1-A drops F1 (restores Q1-C there).
    response = client.put("/sheets/S003/reviews/Q1", json={"option_id": "Q1-A", "reviewer": "t"})
    assert response.status_code == 200
    assert response.json()["result"]["candidate_form_ids"] == ["F2", "F3", "F4"]

    after = client.get("/pair-tracking").json()
    after_forms = forms_by_id(after)
    # Only S003's F1 row disappeared (feasible 2 -> 1); its other rows are unchanged.
    assert after_forms["F1"]["feasible_sheets"] == 1
    assert (after_forms["F1"]["denominator"], after_forms["F1"]["numerator"]) == (1, 1)
    for form_id in ("F2", "F3", "F4"):
        assert after_forms[form_id] == forms_by_id(before)[form_id]
    assert after["feasible_combinations"] == 6
    # Other sheets' per-form rows were not touched at all.
    assert other_sheet_rows() == untouched
    # The maintained summary equals a full aggregation over the per-sheet rows.
    assert pair_summary_rows(db_file) == pair_rows_aggregated(db_file)

    # Withdrawing restores the previous summary exactly.
    assert client.delete("/sheets/S003/reviews/Q1").status_code == 200
    restored = client.get("/pair-tracking").json()
    assert forms_by_id(restored) == forms_by_id(before)
    assert restored["feasible_combinations"] == before["feasible_combinations"]
    assert restored["proportion_range"] == before["proportion_range"]
    assert pair_summary_rows(db_file) == pair_rows_aggregated(db_file)


def test_failed_review_write_keeps_pair_summary(app_env, monkeypatch):
    client, db_file = app_env
    before = set_pair(client)

    from app import engine

    def explode(conn, old, new):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(engine, "apply_pair_delta", explode)
    response = client.put("/sheets/S003/reviews/Q1", json={"option_id": "Q1-A"})
    assert response.status_code == 503

    # Summary, per-sheet rows and the review itself are all unchanged.
    assert client.get("/pair-tracking").json() == before
    assert pair_summary_rows(db_file) == pair_rows_aggregated(db_file)
    conn = sqlite3.connect(db_file)
    try:
        assert conn.execute("SELECT COUNT(*) FROM reviews WHERE sheet_id='S003'").fetchone()[0] == 0
    finally:
        conn.close()


def test_failed_set_keeps_previous_tracking(app_env, monkeypatch):
    client, _ = app_env
    before = set_pair(client)

    from app import engine

    def explode(conn):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(engine, "rebuild_pair_summary", explode)
    response = client.put(
        "/pair-tracking",
        json={
            "first_question_id": "Q3",
            "first_option_id": "Q3-B",
            "second_question_id": "Q4",
            "second_option_id": "Q4-B",
        },
    )
    assert response.status_code == 503

    # The original pair and its summary are still there.
    assert client.get("/pair-tracking").json() == before


# ---------------------------------------------------------------------------
# Zero denominator
# ---------------------------------------------------------------------------


def test_zero_denominator_when_nobody_picked_first_target(app_env):
    client, _ = app_env
    body = set_pair(
        client,
        {
            "first_question_id": "Q1",
            "first_option_id": "Q1-D",
            "second_question_id": "Q2",
            "second_option_id": "Q2-A",
        },
    )
    assert body["feasible_combinations"] == 7
    assert body["proportion_range"] is None
    assert "Q1-D" in body["zero_denominator_reason"]
    assert "7" in body["zero_denominator_reason"]


def test_zero_denominator_when_no_feasible_combination(app_env):
    client, _ = app_env
    from app import seed

    dataset = {
        "questions": seed.QUESTION_BANK,
        "forms": seed.FORMS,
        "sheets": [
            {
                "id": "S900",
                "label": "复核互相矛盾，整卷待核",
                "known_form_id": None,
                "answers": seed._scan("F1", {"Q1": "Q1-B", "Q2": "Q2-D"}),
                "reviews": [
                    {"question_id": "Q1", "option_id": "Q1-B", "reviewer": "r"},
                    {"question_id": "Q2", "option_id": "Q2-A", "reviewer": "r"},
                ],
            }
        ],
    }
    assert client.post("/admin/import", json=dataset).status_code == 200
    body = set_pair(client)
    assert body["feasible_combinations"] == 0
    assert body["proportion_range"] is None
    assert "同时还原" in body["zero_denominator_reason"]


def test_zero_denominator_when_no_sheets(app_env):
    client, _ = app_env
    from app import seed

    dataset = {"questions": seed.QUESTION_BANK, "forms": seed.FORMS, "sheets": []}
    assert client.post("/admin/import", json=dataset).status_code == 200
    body = set_pair(client)
    assert body["feasible_combinations"] == 0
    assert body["proportion_range"] is None
    assert "没有任何答卷" in body["zero_denominator_reason"]


# ---------------------------------------------------------------------------
# Import / recompute
# ---------------------------------------------------------------------------


def test_import_clears_tracking_and_summary(app_env):
    client, db_file = app_env
    set_pair(client)

    from app import seed

    dataset = {
        "questions": seed.QUESTION_BANK,
        "forms": seed.FORMS,
        "sheets": [sheet for sheet in seed.SHEETS if sheet["id"] != "S004"],
    }
    assert client.post("/admin/import", json=dataset).status_code == 200

    assert client.get("/pair-tracking").status_code == 404
    assert pair_summary_rows(db_file) == {}
    conn = sqlite3.connect(db_file)
    try:
        assert conn.execute("SELECT COUNT(*) FROM pair_sheet_forms").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM pair_tracking").fetchone()[0] == 0
    finally:
        conn.close()


def test_recompute_keeps_summary_consistent(app_env):
    client, db_file = app_env
    set_pair(client)
    assert client.put("/sheets/S003/reviews/Q1", json={"option_id": "Q1-A"}).status_code == 200
    before = client.get("/pair-tracking").json()

    assert client.post("/admin/recompute").status_code == 200
    assert client.get("/pair-tracking").json() == before
    assert pair_summary_rows(db_file) == pair_rows_aggregated(db_file)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_reviews_keep_pair_summary_consistent(app_env):
    client, db_file = app_env
    set_pair(client)

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
    # Both reviews landed: S003's only candidate is F3, so only one feasible row.
    assert client.get("/sheets/S003").json()["result"]["candidate_form_ids"] == ["F3"]
    body = client.get("/pair-tracking").json()
    forms = forms_by_id(body)
    assert forms["F3"]["feasible_sheets"] == 2  # S002 + S003
    assert body["feasible_combinations"] == 4
    # The incrementally maintained summary matches a full aggregation.
    assert pair_summary_rows(db_file) == pair_rows_aggregated(db_file)
