"""Pure resolution tests (no HTTP, no database)."""

from __future__ import annotations

import copy

from app import seed
from app.engine import DbCatalog, FormDef, QuestionDef, resolve_sheet


def _catalog(forms: list[dict]) -> DbCatalog:
    questions = [
        QuestionDef(
            id=q["id"],
            prompt=q["prompt"],
            display_order=index,
            options=list(q["options"]),
        )
        for index, q in enumerate(seed.QUESTION_BANK)
    ]
    defs = []
    for form in forms:
        mapping = {
            fq["question_id"]: (
                fq["paper_no"],
                {o["option_id"]: o["printed_label"] for o in fq["options"]},
            )
            for fq in form["questions"]
        }
        defs.append(FormDef(id=form["id"], name=form["name"], display_order=0, questions=mapping))
    return DbCatalog(
        questions=questions,
        forms=defs,
        form_by_id={f.id: f for f in defs},
        option_labels={
            (q.id, o["id"]): o["label"] for q in questions for o in q.options
        },
    )


def test_unanimous_across_all_candidate_forms_counts_without_reviews():
    """Two forms that print the same label for Q1 let an unreviewed sheet count Q1."""

    forms = [copy.deepcopy(seed.FORMS[0]), copy.deepcopy(seed.FORMS[0])]
    forms[0]["id"] = "A"
    forms[1]["id"] = "B"
    # Form B is shuffled but Q1 keeps the standard printed labels.
    for fq in forms[1]["questions"]:
        if fq["question_id"] == "Q1":
            fq["options"] = [
                {"option_id": f"Q1-{label}", "printed_label": label} for label in "ABCD"
            ]
        else:
            # Make every other question disagree between the two forms.
            fq["options"] = [
                {"option_id": o["option_id"], "printed_label": chr(ord(o["printed_label"]) + 1)}
                if o["printed_label"] != "D"
                else {"option_id": o["option_id"], "printed_label": "A"}
                for o in fq["options"]
            ]
    catalog = _catalog(forms)
    # Same scan for every paper number; only Q1 agrees between the two forms.
    answers = {paper_no: "B" for paper_no in range(1, 6)}
    result = resolve_sheet(
        catalog, sheet_id="T", known_form_id=None, answers=answers, reviews={}
    )
    by_question = {q["question_id"]: q for q in result["questions"]}
    assert by_question["Q1"]["status"] == "counted"
    assert by_question["Q1"]["derived_option_id"] == "Q1-B"
    assert by_question["Q2"]["status"] == "ambiguous_candidates"
    assert result["status"] == "partial"


def test_review_mismatch_removes_form_from_candidates():
    catalog = _catalog(seed.FORMS)
    # S001 scan was produced from F2; reviewing it as F3's answer must exclude F3.
    answers = {a["paper_no"]: a["printed_label"] for a in seed.SHEETS[0]["answers"]}
    result = resolve_sheet(
        catalog,
        sheet_id="S001",
        known_form_id=None,
        answers=answers,
        reviews={"Q1": "Q1-A", "Q4": "Q4-B", "Q5": "Q5-A"},
    )
    assert result["candidate_form_ids"] == ["F2"]
    evaluation = {e["form_id"]: e for e in result["candidate_evaluation"]}
    assert evaluation["F3"]["candidate"] is False
    assert any(e["reason"] == "review_mismatch" for e in evaluation["F3"]["exclusions"])


def test_missing_answer_blocks_only_that_question():
    catalog = _catalog(seed.FORMS)
    answers = {a["paper_no"]: a["printed_label"] for a in seed.SHEETS[1]["answers"]}
    # Drop the paper number that F3 uses for Q2.
    q2_paper = next(
        fq["paper_no"] for fq in seed.FORMS[2]["questions"] if fq["question_id"] == "Q2"
    )
    del answers[q2_paper]
    result = resolve_sheet(
        catalog, sheet_id="S002", known_form_id="F3", answers=answers, reviews={}
    )
    by_question = {q["question_id"]: q for q in result["questions"]}
    assert by_question["Q2"]["status"] == "candidate_incomplete"
    assert by_question["Q2"]["reason"] == "missing_answer"
    assert by_question["Q1"]["counted"] is True
    assert result["status"] == "partial"


def test_unknown_printed_label_blocks_question():
    catalog = _catalog(seed.FORMS)
    answers = {1: "Z"}
    result = resolve_sheet(
        catalog, sheet_id="T", known_form_id="F1", answers=answers, reviews={}
    )
    by_question = {q["question_id"]: q for q in result["questions"]}
    assert by_question["Q1"]["status"] == "candidate_incomplete"
    assert by_question["Q1"]["reason"] == "unknown_label"
    # Other questions have no answer at all.
    assert by_question["Q2"]["reason"] == "missing_answer"


def test_review_of_question_absent_from_all_forms_yields_no_candidates():
    catalog = _catalog(seed.FORMS)
    answers = {a["paper_no"]: a["printed_label"] for a in seed.SHEETS[0]["answers"]}
    result = resolve_sheet(
        catalog,
        sheet_id="T",
        known_form_id=None,
        answers=answers,
        reviews={"Q404": "Q1-A"},
    )
    assert result["candidate_form_ids"] == []
    assert result["status"] == "pending"
    assert any(r["reason"] == "question_not_on_form" for e in result["candidate_evaluation"] for r in e["exclusions"])
    assert result["pending_reasons"][0]["scope"] == "sheet"
