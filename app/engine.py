"""Deterministic per-sheet resolution of shuffled quiz papers.

The scanned sheet only records *printed* labels by *paper* number.  A form is a
hypothesis that maps paper numbers to standard questions and printed labels to
standard options.  A form survives as a candidate only if, for every active
review on the sheet, translating the scan through that form yields exactly the
reviewed option.  A question enters the distribution only when **all**
candidate forms translate the scan to the same standard option.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .validation import Catalog


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass
class QuestionDef:
    id: str
    prompt: str
    display_order: int
    options: list[dict[str, str]] = field(default_factory=list)


@dataclass
class FormDef:
    id: str
    name: str
    display_order: int
    # question_id -> (paper_no, {option_id: printed_label})
    questions: dict[str, tuple[int, dict[str, str]]] = field(default_factory=dict)


@dataclass
class DbCatalog:
    questions: list[QuestionDef]
    forms: list[FormDef]
    form_by_id: dict[str, FormDef]
    option_labels: dict[tuple[str, str], str]  # (question_id, option_id) -> standard label

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> DbCatalog:
        question_rows = conn.execute(
            "SELECT id, prompt, display_order FROM standard_questions "
            "ORDER BY display_order, id"
        ).fetchall()
        questions: list[QuestionDef] = []
        option_labels: dict[tuple[str, str], str] = {}
        for row in question_rows:
            options = [
                {
                    "id": o["id"],
                    "label": o["label"],
                    "text": o["text"],
                    "display_order": o["display_order"],
                }
                for o in conn.execute(
                    "SELECT id, label, text, display_order FROM standard_options "
                    "WHERE question_id = ? ORDER BY display_order, id",
                    (row["id"],),
                ).fetchall()
            ]
            for opt in options:
                option_labels[(row["id"], opt["id"])] = opt["label"]
            questions.append(
                QuestionDef(
                    id=row["id"],
                    prompt=row["prompt"],
                    display_order=row["display_order"],
                    options=options,
                )
            )

        form_rows = conn.execute(
            "SELECT id, name, display_order FROM forms ORDER BY display_order, id"
        ).fetchall()
        forms: list[FormDef] = []
        for row in form_rows:
            form = FormDef(id=row["id"], name=row["name"], display_order=row["display_order"])
            for fq in conn.execute(
                "SELECT paper_no, question_id FROM form_questions "
                "WHERE form_id = ? ORDER BY paper_no",
                (row["id"],),
            ).fetchall():
                printed = {
                    o["option_id"]: o["printed_label"]
                    for o in conn.execute(
                        "SELECT option_id, printed_label FROM form_options "
                        "WHERE form_id = ? AND question_id = ?",
                        (row["id"], fq["question_id"]),
                    ).fetchall()
                }
                form.questions[fq["question_id"]] = (fq["paper_no"], printed)
            forms.append(form)

        return cls(
            questions=questions,
            forms=forms,
            form_by_id={f.id: f for f in forms},
            option_labels=option_labels,
        )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _sort_text(value: str | None) -> tuple[int, str | None]:
    """Stable helper for ordering nullable strings in Python-side checks."""
    return (0, value) if value is not None else (1, None)


def resolve_sheet(
    catalog: DbCatalog,
    *,
    sheet_id: str,
    known_form_id: str | None,
    answers: dict[int, str],
    reviews: dict[str, str],
) -> dict[str, Any]:
    """Pure resolution of one sheet.  No database writes happen here."""

    pool = [catalog.form_by_id[known_form_id]] if known_form_id else list(catalog.forms)

    evaluations: list[dict[str, Any]] = []
    candidates: list[FormDef] = []
    for form in pool:
        exclusions: list[dict[str, Any]] = []
        for question_id in sorted(reviews, key=_sort_text):
            option_id = reviews[question_id]
            mapping = form.questions.get(question_id)
            if mapping is None:
                exclusions.append(
                    {
                        "question_id": question_id,
                        "reason": "question_not_on_form",
                        "detail": f"卷型 {form.id} 不含标准题 {question_id}",
                    }
                )
                continue
            paper_no, printed = mapping
            if paper_no not in answers:
                exclusions.append(
                    {
                        "question_id": question_id,
                        "reason": "missing_answer",
                        "detail": (
                            f"卷型 {form.id} 将标准题 {question_id} 排在题号 {paper_no}，"
                            "但答卷无该题号作答"
                        ),
                    }
                )
                continue
            label = answers[paper_no]
            derived = _reverse_lookup(printed, label)
            if derived is None:
                exclusions.append(
                    {
                        "question_id": question_id,
                        "reason": "unknown_label",
                        "detail": (
                            f"卷型 {form.id} 题号 {paper_no} 的选项上没有印刷标记 {label!r}"
                        ),
                    }
                )
                continue
            if derived != option_id:
                exclusions.append(
                    {
                        "question_id": question_id,
                        "reason": "review_mismatch",
                        "detail": (
                            f"卷型 {form.id} 将题号 {paper_no} 的标记 {label!r} 还原为标准选项 "
                            f"{derived}，与复核选项 {option_id} 不一致"
                        ),
                    }
                )
        is_candidate = not exclusions
        if is_candidate:
            candidates.append(form)
        evaluations.append(
            {
                "form_id": form.id,
                "review_consistent": is_candidate,
                "candidate": is_candidate,
                "exclusions": exclusions,
            }
        )

    question_results: list[dict[str, Any]] = []
    counted_options: dict[str, str] = {}
    pending_reasons: list[dict[str, Any]] = []

    if not candidates:
        reason = {
            "scope": "sheet",
            "question_id": None,
            "reason": "no_candidate_form",
            "detail": (
                "没有任何卷型符合该卷全部复核记录，整卷待核"
                if reviews
                else "没有可用的候选卷型，整卷待核"
            ),
        }
        pending_reasons.append(reason)

    for question in catalog.questions:
        paper_no_by_form = {
            form.id: form.questions[question.id][0]
            for form in catalog.forms
            if question.id in form.questions
        }
        derived_by_form: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        derived_options: set[str] = set()

        for form in candidates:
            mapping = form.questions.get(question.id)
            if mapping is None:
                failures.append(
                    {
                        "form_id": form.id,
                        "reason": "question_not_on_form",
                        "detail": f"候选卷型 {form.id} 不含该题",
                    }
                )
                continue
            paper_no, printed = mapping
            if paper_no not in answers:
                failures.append(
                    {
                        "form_id": form.id,
                        "reason": "missing_answer",
                        "detail": f"候选卷型 {form.id} 的题号 {paper_no} 缺少作答",
                    }
                )
                continue
            label = answers[paper_no]
            option_id = _reverse_lookup(printed, label)
            if option_id is None:
                failures.append(
                    {
                        "form_id": form.id,
                        "reason": "unknown_label",
                        "detail": (
                            f"候选卷型 {form.id} 的题号 {paper_no} 上无法识别印刷标记 {label!r}"
                        ),
                    }
                )
                continue
            derived_options.add(option_id)
            derived_by_form.append(
                {
                    "form_id": form.id,
                    "paper_no": paper_no,
                    "printed_label": label,
                    "option_id": option_id,
                    "option_label": catalog.option_labels.get((question.id, option_id), option_id),
                }
            )

        if not candidates:
            status = "no_candidate_form"
            counted = False
            reason = "no_candidate_form"
            detail = "整卷无候选卷型，该题待核"
        elif failures:
            status = "candidate_incomplete"
            counted = False
            reason = failures[0]["reason"]
            detail = "；".join(f["detail"] for f in failures)
        elif len(derived_options) > 1:
            status = "ambiguous_candidates"
            counted = False
            reason = "ambiguous_candidates"
            detail = "候选卷型还原出的标准选项不一致：" + "，".join(
                f"{f['form_id']}→{f['option_id']}" for f in derived_by_form
            )
        else:
            assert len(derived_options) == 1
            status = "counted"
            counted = True
            reason = None
            detail = "全部候选卷型还原为同一标准选项"
            counted_options[question.id] = next(iter(derived_options))

        if not counted:
            pending_reasons.append(
                {
                    "scope": "question",
                    "question_id": question.id,
                    "reason": reason,
                    "detail": detail,
                }
            )

        question_results.append(
            {
                "question_id": question.id,
                "prompt": question.prompt,
                "paper_no_by_form": paper_no_by_form,
                "status": status,
                "counted": counted,
                "derived_option_id": counted_options.get(question.id),
                "reason": reason,
                "detail": detail,
                "derived_by_form": derived_by_form,
            }
        )

    total = len(catalog.questions)
    counted_count = len(counted_options)
    if not candidates:
        sheet_status = "pending"
    elif counted_count == total:
        sheet_status = "complete"
    else:
        sheet_status = "partial"

    return {
        "sheet_id": sheet_id,
        "status": sheet_status,
        "candidate_form_ids": [f.id for f in candidates],
        "candidate_evaluation": evaluations,
        "pending_reasons": pending_reasons,
        "question_count": total,
        "counted_question_count": counted_count,
        "questions": question_results,
        "counted_options": counted_options,
    }


def _reverse_lookup(printed_by_option: dict[str, str], label: str) -> str | None:
    for option_id, printed_label in printed_by_option.items():
        if printed_label == label:
            return option_id
    return None


# ---------------------------------------------------------------------------
# Wrong-answer pair tracking (same-sheet co-occurrence of two target options)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PairTracking:
    """The tracked pair: two standard questions and their target options."""

    first_question_id: str
    first_option_id: str
    second_question_id: str
    second_option_id: str


def load_pair_tracking(conn: sqlite3.Connection) -> PairTracking | None:
    row = conn.execute(
        "SELECT first_question_id, first_option_id, second_question_id, second_option_id "
        "FROM pair_tracking WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    return PairTracking(
        first_question_id=row["first_question_id"],
        first_option_id=row["first_option_id"],
        second_question_id=row["second_question_id"],
        second_option_id=row["second_option_id"],
    )


def _restore_option(form: FormDef, question_id: str, answers: dict[int, str]) -> str | None:
    """Translate the scan to a standard option under one form, or ``None``."""

    mapping = form.questions.get(question_id)
    if mapping is None:
        return None
    paper_no, printed = mapping
    label = answers.get(paper_no)
    if label is None:
        return None
    return _reverse_lookup(printed, label)


def compute_pair_rows(
    catalog: DbCatalog,
    tracking: PairTracking,
    candidate_form_ids: list[str],
    answers: dict[int, str],
) -> list[tuple[str, int, int]]:
    """Feasible (sheet, form) rows for one sheet.

    The same candidate form restores both questions; only combinations where
    both restore are kept.  Each row is ``(form_id, first_hit, second_hit)``.
    """

    rows: list[tuple[str, int, int]] = []
    for form_id in candidate_form_ids:
        form = catalog.form_by_id.get(form_id)
        if form is None:
            continue
        first = _restore_option(form, tracking.first_question_id, answers)
        if first is None:
            continue
        second = _restore_option(form, tracking.second_question_id, answers)
        if second is None:
            continue
        rows.append(
            (
                form_id,
                1 if first == tracking.first_option_id else 0,
                1 if second == tracking.second_option_id else 0,
            )
        )
    return rows


def persist_pair_rows(
    conn: sqlite3.Connection, sheet_id: str, rows: list[tuple[str, int, int]]
) -> None:
    conn.execute("DELETE FROM pair_sheet_forms WHERE sheet_id = ?", (sheet_id,))
    conn.executemany(
        "INSERT INTO pair_sheet_forms (sheet_id, form_id, first_hit, second_hit) "
        "VALUES (?, ?, ?, ?)",
        [(sheet_id, form_id, first_hit, second_hit) for form_id, first_hit, second_hit in rows],
    )


def load_pair_rows(conn: sqlite3.Connection, sheet_id: str) -> list[tuple[str, int, int]]:
    return [
        (row["form_id"], row["first_hit"], row["second_hit"])
        for row in conn.execute(
            "SELECT form_id, first_hit, second_hit FROM pair_sheet_forms WHERE sheet_id = ?",
            (sheet_id,),
        ).fetchall()
    ]


def _aggregate_pair_rows(rows: list[tuple[str, int, int]]) -> dict[str, tuple[int, int, int]]:
    """(form_id -> (feasible, denominator, numerator)) for a sheet's rows."""

    aggregated: dict[str, tuple[int, int, int]] = {}
    for form_id, first_hit, second_hit in rows:
        feasible, denominator, numerator = aggregated.get(form_id, (0, 0, 0))
        aggregated[form_id] = (
            feasible + 1,
            denominator + first_hit,
            numerator + (1 if first_hit and second_hit else 0),
        )
    return aggregated


def apply_pair_delta(
    conn: sqlite3.Connection,
    old_rows: list[tuple[str, int, int]],
    new_rows: list[tuple[str, int, int]],
) -> None:
    """Adjust the pair summary by one sheet's difference (old -> new rows)."""

    old = _aggregate_pair_rows(old_rows)
    new = _aggregate_pair_rows(new_rows)
    for form_id in sorted(set(old) | set(new)):
        before = old.get(form_id, (0, 0, 0))
        after = new.get(form_id, (0, 0, 0))
        if before == after:
            continue
        if before != (0, 0, 0):
            updated = conn.execute(
                "UPDATE pair_form_summary "
                "SET feasible = feasible - ?, denominator = denominator - ?, "
                "    numerator = numerator - ? "
                "WHERE form_id = ? AND feasible >= ? AND denominator >= ? AND numerator >= ?",
                (*before, form_id, *before),
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    f"pair summary underflow for form {form_id}; refusing inconsistent data"
                )
            conn.execute(
                "DELETE FROM pair_form_summary "
                "WHERE form_id = ? AND feasible = 0 AND denominator = 0 AND numerator = 0",
                (form_id,),
            )
        if after != (0, 0, 0):
            conn.execute(
                "INSERT INTO pair_form_summary (form_id, feasible, denominator, numerator) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(form_id) DO UPDATE SET "
                "feasible = feasible + excluded.feasible, "
                "denominator = denominator + excluded.denominator, "
                "numerator = numerator + excluded.numerator",
                (form_id, *after),
            )


def rebuild_pair_rows(conn: sqlite3.Connection, tracking: PairTracking) -> None:
    """Recompute every sheet's feasible combinations from persisted results."""

    catalog = DbCatalog.load(conn)
    conn.execute("DELETE FROM pair_sheet_forms")
    for row in conn.execute(
        "SELECT sheet_id, candidate_form_ids FROM sheet_results"
    ).fetchall():
        candidates = json.loads(row["candidate_form_ids"])
        answers = {
            answer["paper_no"]: answer["printed_label"]
            for answer in conn.execute(
                "SELECT paper_no, printed_label FROM sheet_answers WHERE sheet_id = ?",
                (row["sheet_id"],),
            ).fetchall()
        }
        persist_pair_rows(
            conn, row["sheet_id"], compute_pair_rows(catalog, tracking, candidates, answers)
        )


def rebuild_pair_summary(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM pair_form_summary")
    conn.execute(
        "INSERT INTO pair_form_summary (form_id, feasible, denominator, numerator) "
        "SELECT form_id, COUNT(*), SUM(first_hit), "
        "       SUM(CASE WHEN first_hit = 1 AND second_hit = 1 THEN 1 ELSE 0 END) "
        "FROM pair_sheet_forms GROUP BY form_id"
    )


def set_pair_tracking(conn: sqlite3.Connection, tracking: PairTracking, updated_at: str) -> None:
    """Replace the tracked pair and rebuild the derived rows and summary."""

    conn.execute(
        "INSERT INTO pair_tracking "
        "(id, first_question_id, first_option_id, second_question_id, second_option_id, updated_at) "
        "VALUES (1, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "first_question_id = excluded.first_question_id, "
        "first_option_id = excluded.first_option_id, "
        "second_question_id = excluded.second_question_id, "
        "second_option_id = excluded.second_option_id, "
        "updated_at = excluded.updated_at",
        (
            tracking.first_question_id,
            tracking.first_option_id,
            tracking.second_question_id,
            tracking.second_option_id,
            updated_at,
        ),
    )
    rebuild_pair_rows(conn, tracking)
    rebuild_pair_summary(conn)


def clear_pair_tracking(conn: sqlite3.Connection) -> bool:
    """Drop the tracked pair together with its derived rows and summary."""

    deleted = conn.execute("DELETE FROM pair_tracking WHERE id = 1")
    conn.execute("DELETE FROM pair_sheet_forms")
    conn.execute("DELETE FROM pair_form_summary")
    return deleted.rowcount == 1


# ---------------------------------------------------------------------------
# Persistence (must be called inside a write transaction)
# ---------------------------------------------------------------------------


def load_sheet_evidence(
    conn: sqlite3.Connection, sheet_id: str
) -> tuple[str | None, dict[int, str], dict[str, str]]:
    sheet = conn.execute(
        "SELECT id, known_form_id FROM sheets WHERE id = ?", (sheet_id,)
    ).fetchone()
    if sheet is None:
        raise KeyError(sheet_id)
    answers = {
        row["paper_no"]: row["printed_label"]
        for row in conn.execute(
            "SELECT paper_no, printed_label FROM sheet_answers WHERE sheet_id = ?",
            (sheet_id,),
        ).fetchall()
    }
    reviews = {
        row["question_id"]: row["option_id"]
        for row in conn.execute(
            "SELECT question_id, option_id FROM reviews WHERE sheet_id = ?", (sheet_id,)
        ).fetchall()
    }
    return sheet["known_form_id"], answers, reviews


def load_counted_map(conn: sqlite3.Connection, sheet_id: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT question_id, derived_option_id FROM sheet_question_results "
        "WHERE sheet_id = ? AND counted = 1",
        (sheet_id,),
    ).fetchall()
    return {row["question_id"]: row["derived_option_id"] for row in rows}


def persist_sheet_result(
    conn: sqlite3.Connection, resolved: dict[str, Any], computed_at: str
) -> None:
    sheet_id = resolved["sheet_id"]
    conn.execute("DELETE FROM sheet_results WHERE sheet_id = ?", (sheet_id,))
    conn.execute(
        "INSERT INTO sheet_results ("
        "sheet_id, status, candidate_form_ids, candidate_evaluation, pending_reasons, "
        "question_count, counted_question_count, computed_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sheet_id,
            resolved["status"],
            json.dumps(resolved["candidate_form_ids"], ensure_ascii=False),
            json.dumps(resolved["candidate_evaluation"], ensure_ascii=False),
            json.dumps(resolved["pending_reasons"], ensure_ascii=False),
            resolved["question_count"],
            resolved["counted_question_count"],
            computed_at,
        ),
    )
    for item in resolved["questions"]:
        conn.execute(
            "INSERT INTO sheet_question_results ("
            "sheet_id, question_id, status, derived_option_id, counted, reason, detail, "
            "derived_by_form"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sheet_id,
                item["question_id"],
                item["status"],
                item["derived_option_id"],
                1 if item["counted"] else 0,
                item["reason"],
                item["detail"],
                json.dumps(item["derived_by_form"], ensure_ascii=False),
            ),
        )


def recompute_sheet(conn: sqlite3.Connection, sheet_id: str, computed_at: str) -> dict[str, Any]:
    catalog = DbCatalog.load(conn)
    known_form_id, answers, reviews = load_sheet_evidence(conn, sheet_id)
    if known_form_id is not None and known_form_id not in catalog.form_by_id:
        raise ValueError(f"sheet {sheet_id}: unknown known_form_id {known_form_id}")
    resolved = resolve_sheet(
        catalog,
        sheet_id=sheet_id,
        known_form_id=known_form_id,
        answers=answers,
        reviews=reviews,
    )
    persist_sheet_result(conn, resolved, computed_at)
    tracking = load_pair_tracking(conn)
    pair_rows = (
        compute_pair_rows(catalog, tracking, resolved["candidate_form_ids"], answers)
        if tracking is not None
        else []
    )
    persist_pair_rows(conn, sheet_id, pair_rows)
    return resolved


def apply_count_delta(
    conn: sqlite3.Connection, old: dict[str, str], new: dict[str, str]
) -> None:
    """Adjust the aggregate counts by the difference for one sheet."""

    for question_id in sorted(set(old) | set(new)):
        before = old.get(question_id)
        after = new.get(question_id)
        if before == after:
            continue
        if before is not None:
            updated = conn.execute(
                "UPDATE question_option_counts SET count = count - 1 "
                "WHERE question_id = ? AND option_id = ? AND count > 0",
                (question_id, before),
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    f"count underflow for {question_id}/{before}; refusing inconsistent data"
                )
        if after is not None:
            conn.execute(
                "INSERT INTO question_option_counts (question_id, option_id, count) "
                "VALUES (?, ?, 1) "
                "ON CONFLICT(question_id, option_id) DO UPDATE SET count = count + 1",
                (question_id, after),
            )


def rebuild_all_counts(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM question_option_counts")
    conn.execute(
        "INSERT INTO question_option_counts (question_id, option_id, count) "
        "SELECT question_id, derived_option_id, COUNT(*) "
        "FROM sheet_question_results WHERE counted = 1 "
        "GROUP BY question_id, derived_option_id"
    )


def apply_review_change(
    conn: sqlite3.Connection,
    *,
    sheet_id: str,
    question_id: str,
    option_id: str | None,
    reviewer: str | None,
    computed_at: str,
) -> dict[str, Any]:
    """Register (option_id set) or withdraw (option_id None) one review.

    The caller has already validated references.  All old evidence is read
    before the change and all results/counts are written in the same
    transaction, so a failure leaves the previous review, sheet result and
    counts intact.
    """

    sheet = conn.execute("SELECT id FROM sheets WHERE id = ?", (sheet_id,)).fetchone()
    if sheet is None:
        raise KeyError(sheet_id)

    old_counted = load_counted_map(conn, sheet_id)
    old_pair_rows = load_pair_rows(conn, sheet_id)

    if option_id is None:
        deleted = conn.execute(
            "DELETE FROM reviews WHERE sheet_id = ? AND question_id = ?",
            (sheet_id, question_id),
        )
        if deleted.rowcount != 1:
            raise LookupError((sheet_id, question_id))
    else:
        conn.execute(
            "INSERT INTO reviews (sheet_id, question_id, option_id, reviewer, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(sheet_id, question_id) DO UPDATE SET "
            "option_id = excluded.option_id, reviewer = excluded.reviewer, "
            "updated_at = excluded.updated_at",
            (sheet_id, question_id, option_id, reviewer, computed_at),
        )

    resolved = recompute_sheet(conn, sheet_id, computed_at)
    apply_count_delta(conn, old_counted, resolved["counted_options"])
    apply_pair_delta(conn, old_pair_rows, load_pair_rows(conn, sheet_id))
    return resolved


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def replace_dataset(
    conn: sqlite3.Connection, catalog: Catalog, dataset: Any, computed_at: str
) -> dict[str, int]:
    """Replace the whole working dataset and rebuild every derived row."""

    for table in (
        "pair_form_summary",
        "pair_sheet_forms",
        "pair_tracking",
        "question_option_counts",
        "sheet_question_results",
        "sheet_results",
        "reviews",
        "sheet_answers",
        "sheets",
        "form_options",
        "form_questions",
        "forms",
        "standard_options",
        "standard_questions",
    ):
        conn.execute(f"DELETE FROM {table}")

    for index, question in enumerate(dataset.questions):
        conn.execute(
            "INSERT INTO standard_questions (id, prompt, display_order) VALUES (?, ?, ?)",
            (
                question.id,
                question.prompt,
                question.display_order if question.display_order is not None else index,
            ),
        )
        for opt_index, option in enumerate(question.options):
            conn.execute(
                "INSERT INTO standard_options "
                "(id, question_id, label, text, display_order) VALUES (?, ?, ?, ?, ?)",
                (option.id, question.id, option.label, option.text, opt_index),
            )

    for index, form in enumerate(dataset.forms):
        conn.execute(
            "INSERT INTO forms (id, name, display_order) VALUES (?, ?, ?)",
            (
                form.id,
                form.name,
                form.display_order if form.display_order is not None else index,
            ),
        )
        for fq in form.questions:
            conn.execute(
                "INSERT INTO form_questions (form_id, paper_no, question_id) VALUES (?, ?, ?)",
                (form.id, fq.paper_no, fq.question_id),
            )
            for option in fq.options:
                conn.execute(
                    "INSERT INTO form_options "
                    "(form_id, question_id, option_id, printed_label) VALUES (?, ?, ?, ?)",
                    (form.id, fq.question_id, option.option_id, option.printed_label),
                )

    review_count = 0
    for sheet in dataset.sheets:
        conn.execute(
            "INSERT INTO sheets (id, label, known_form_id, created_at) VALUES (?, ?, ?, ?)",
            (sheet.id, sheet.label, sheet.known_form_id, computed_at),
        )
        for answer in sheet.answers:
            conn.execute(
                "INSERT INTO sheet_answers (sheet_id, paper_no, printed_label) VALUES (?, ?, ?)",
                (sheet.id, answer.paper_no, answer.printed_label),
            )
        for review in sheet.reviews:
            conn.execute(
                "INSERT INTO reviews (sheet_id, question_id, option_id, reviewer, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (sheet.id, review.question_id, review.option_id, review.reviewer, computed_at),
            )
            review_count += 1

    for sheet in dataset.sheets:
        recompute_sheet(conn, sheet.id, computed_at)

    rebuild_all_counts(conn)
    return {
        "questions": len(dataset.questions),
        "forms": len(dataset.forms),
        "sheets": len(dataset.sheets),
        "reviews": review_count,
    }
