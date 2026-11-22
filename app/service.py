"""Read models built from the persisted sheet results and counts."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .validation import not_found


def _json(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def list_questions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, prompt, display_order FROM standard_questions ORDER BY display_order, id"
    ).fetchall()
    result = []
    for row in rows:
        options = conn.execute(
            "SELECT id, label, text FROM standard_options WHERE question_id = ? "
            "ORDER BY display_order, id",
            (row["id"],),
        ).fetchall()
        result.append(
            {
                "id": row["id"],
                "prompt": row["prompt"],
                "display_order": row["display_order"],
                "options": [dict(o) for o in options],
            }
        )
    return result


def list_forms(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, name, display_order FROM forms ORDER BY display_order, id"
    ).fetchall()
    result = []
    for row in rows:
        fq_rows = conn.execute(
            "SELECT paper_no, question_id FROM form_questions WHERE form_id = ? "
            "ORDER BY paper_no",
            (row["id"],),
        ).fetchall()
        questions = []
        for fq in fq_rows:
            options = conn.execute(
                "SELECT option_id, printed_label FROM form_options "
                "WHERE form_id = ? AND question_id = ? ORDER BY printed_label, option_id",
                (row["id"], fq["question_id"]),
            ).fetchall()
            questions.append(
                {
                    "paper_no": fq["paper_no"],
                    "question_id": fq["question_id"],
                    "options": [dict(o) for o in options],
                }
            )
        result.append(
            {
                "id": row["id"],
                "name": row["name"],
                "display_order": row["display_order"],
                "questions": questions,
            }
        )
    return result


def _reviews_for_sheet(conn: sqlite3.Connection, sheet_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT r.question_id, r.option_id, r.reviewer, r.updated_at, o.label AS option_label "
        "FROM reviews r JOIN standard_options o "
        "  ON o.id = r.option_id AND o.question_id = r.question_id "
        "WHERE r.sheet_id = ? ORDER BY r.question_id",
        (sheet_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_sheet_detail(conn: sqlite3.Connection, sheet_id: str) -> dict[str, Any]:
    sheet = conn.execute(
        "SELECT id, label, known_form_id, created_at FROM sheets WHERE id = ?", (sheet_id,)
    ).fetchone()
    if sheet is None:
        raise not_found(f"sheet {sheet_id!r} not found")

    result = conn.execute(
        "SELECT * FROM sheet_results WHERE sheet_id = ?", (sheet_id,)
    ).fetchone()
    if result is None:
        raise not_found(
            f"sheet {sheet_id!r} has no computed result; run POST /admin/recompute"
        )

    answers = conn.execute(
        "SELECT paper_no, printed_label FROM sheet_answers WHERE sheet_id = ? ORDER BY paper_no",
        (sheet_id,),
    ).fetchall()

    reviews = _reviews_for_sheet(conn, sheet_id)
    review_by_question = {r["question_id"]: r for r in reviews}

    paper_no_by_form: dict[str, dict[str, int]] = {}
    for fq in conn.execute(
        "SELECT fq.form_id, fq.question_id, fq.paper_no FROM form_questions fq"
    ).fetchall():
        paper_no_by_form.setdefault(fq["question_id"], {})[fq["form_id"]] = fq["paper_no"]

    question_rows = conn.execute(
        "SELECT r.question_id, r.status, r.derived_option_id, r.counted, r.reason, r.detail, "
        "       r.derived_by_form, q.prompt, q.display_order, o.label AS derived_option_label "
        "FROM sheet_question_results r "
        "JOIN standard_questions q ON q.id = r.question_id "
        "LEFT JOIN standard_options o "
        "  ON o.id = r.derived_option_id AND o.question_id = r.question_id "
        "WHERE r.sheet_id = ? ORDER BY q.display_order, q.id",
        (sheet_id,),
    ).fetchall()

    questions = []
    for row in question_rows:
        questions.append(
            {
                "question_id": row["question_id"],
                "prompt": row["prompt"],
                "paper_no_by_form": paper_no_by_form.get(row["question_id"], {}),
                "status": row["status"],
                "counted": bool(row["counted"]),
                "derived_option_id": row["derived_option_id"],
                "derived_option_label": row["derived_option_label"],
                "reason": row["reason"],
                "detail": row["detail"],
                "derived_by_form": _json(row["derived_by_form"]) or [],
                "review": review_by_question.get(row["question_id"]),
            }
        )

    return {
        "sheet": dict(sheet),
        "answers": [dict(a) for a in answers],
        "result": {
            "status": result["status"],
            "candidate_form_ids": _json(result["candidate_form_ids"]) or [],
            "candidate_evaluation": _json(result["candidate_evaluation"]) or [],
            "pending_reasons": _json(result["pending_reasons"]) or [],
            "computed_at": result["computed_at"],
            "question_count": result["question_count"],
            "counted_question_count": result["counted_question_count"],
        },
        "questions": questions,
        "reviews": reviews,
    }


def list_sheets(
    conn: sqlite3.Connection, *, status: str | None, limit: int, offset: int
) -> dict[str, Any]:
    where = ""
    params: list[Any] = []
    if status == "not_computed":
        where = "WHERE sr.sheet_id IS NULL"
    elif status is not None:
        where = "WHERE sr.status = ?"
        params.append(status)

    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM sheets s LEFT JOIN sheet_results sr "
        f"ON sr.sheet_id = s.id {where}",
        params,
    ).fetchone()["n"]

    rows = conn.execute(
        "SELECT s.id, s.label, s.known_form_id, s.created_at, sr.status, "
        "       sr.candidate_form_ids, sr.question_count, sr.counted_question_count, "
        "       sr.computed_at "
        "FROM sheets s LEFT JOIN sheet_results sr ON sr.sheet_id = s.id "
        f"{where} ORDER BY s.created_at, s.id LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()

    sheets = []
    for row in rows:
        candidates = _json(row["candidate_form_ids"]) if row["candidate_form_ids"] else []
        sheets.append(
            {
                "id": row["id"],
                "label": row["label"],
                "known_form_id": row["known_form_id"],
                "status": row["status"] or "not_computed",
                "candidate_form_count": len(candidates or []),
                "question_count": row["question_count"] or 0,
                "counted_question_count": row["counted_question_count"] or 0,
                "computed_at": row["computed_at"],
            }
        )
    return {"sheets": sheets, "total": total}


def get_stats(conn: sqlite3.Connection, question_id: str | None = None) -> list[dict[str, Any]]:
    questions = list_questions(conn)
    if question_id is not None:
        questions = [q for q in questions if q["id"] == question_id]
        if not questions:
            raise not_found(f"question {question_id!r} not found")

    total_sheets = conn.execute("SELECT COUNT(*) AS n FROM sheets").fetchone()["n"]

    stats = []
    for question in questions:
        counted_sheets = conn.execute(
            "SELECT COUNT(*) AS n FROM sheet_question_results "
            "WHERE question_id = ? AND counted = 1",
            (question["id"],),
        ).fetchone()["n"]
        counts = {
            row["option_id"]: row["count"]
            for row in conn.execute(
                "SELECT option_id, count FROM question_option_counts WHERE question_id = ?",
                (question["id"],),
            ).fetchall()
        }
        total_counted = sum(counts.values())
        if total_counted != counted_sheets:
            raise RuntimeError(
                f"count drift for question {question['id']}: "
                f"options total {total_counted} != counted sheets {counted_sheets}"
            )
        stats.append(
            {
                "question_id": question["id"],
                "prompt": question["prompt"],
                "total_sheets": total_sheets,
                "counted_sheets": counted_sheets,
                "uncounted_sheets": total_sheets - counted_sheets,
                "options": [
                    {**option, "count": counts.get(option["id"], 0)}
                    for option in question["options"]
                ],
            }
        )
    return stats


def get_pending(conn: sqlite3.Connection, sheet_id: str) -> dict[str, Any]:
    detail = get_sheet_detail(conn, sheet_id)
    return {
        "sheet_id": sheet_id,
        "status": detail["result"]["status"],
        "pending_reasons": detail["result"]["pending_reasons"],
    }
