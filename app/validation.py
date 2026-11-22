"""Validation helpers shared by the import and review endpoints."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from fastapi import HTTPException, status

from .schemas import DatasetIn, FormIn, QuestionIn


@dataclass(frozen=True)
class Catalog:
    """In-memory view of the standard questions and the form mappings."""

    questions: dict[str, QuestionIn]
    forms: dict[str, FormIn]


def bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _duplicates(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    dupes: list[str] = []
    for value in values:
        if value in seen and value not in dupes:
            dupes.append(value)
        seen.add(value)
    return dupes


def validate_catalog(dataset: DatasetIn) -> Catalog:
    """Validate internal consistency of an import payload.

    Raises ``HTTPException(422)`` describing every problem found in the
    first failing structure.
    """

    dup_questions = _duplicates(q.id for q in dataset.questions)
    if dup_questions:
        raise bad_request(f"duplicate question ids: {', '.join(dup_questions)}")

    dup_option_ids = _duplicates(o.id for q in dataset.questions for o in q.options)
    if dup_option_ids:
        raise bad_request(
            f"option ids must be globally unique: {', '.join(dup_option_ids)}"
        )

    questions: dict[str, QuestionIn] = {}
    for q in dataset.questions:
        dup_opts = _duplicates(o.id for o in q.options)
        if dup_opts:
            raise bad_request(f"question {q.id}: duplicate option ids: {', '.join(dup_opts)}")
        dup_labels = _duplicates(o.label for o in q.options)
        if dup_labels:
            raise bad_request(f"question {q.id}: duplicate option labels: {', '.join(dup_labels)}")

    questions = {q.id: q for q in dataset.questions}

    dup_forms = _duplicates(f.id for f in dataset.forms)
    if dup_forms:
        raise bad_request(f"duplicate form ids: {', '.join(dup_forms)}")

    forms: dict[str, FormIn] = {}
    for form in dataset.forms:
        dup_paper = _duplicates(str(fq.paper_no) for fq in form.questions)
        if dup_paper:
            raise bad_request(f"form {form.id}: duplicate paper numbers: {', '.join(dup_paper)}")
        dup_q = _duplicates(fq.question_id for fq in form.questions)
        if dup_q:
            raise bad_request(
                f"form {form.id}: question appears twice on the paper: {', '.join(dup_q)}"
            )

        for fq in form.questions:
            if fq.question_id not in questions:
                raise bad_request(
                    f"form {form.id}: unknown standard question {fq.question_id!r}"
                )
            std = questions[fq.question_id]
            std_options = {o.id: o for o in std.options}
            unknown = [o.option_id for o in fq.options if o.option_id not in std_options]
            if unknown:
                raise bad_request(
                    f"form {form.id}, question {fq.question_id}: "
                    f"options not in standard question: {', '.join(unknown)}"
                )
            dup_opt = _duplicates(o.option_id for o in fq.options)
            if dup_opt:
                raise bad_request(
                    f"form {form.id}, question {fq.question_id}: "
                    f"duplicate option mappings: {', '.join(dup_opt)}"
                )
            dup_printed = _duplicates(o.printed_label for o in fq.options)
            if dup_printed:
                raise bad_request(
                    f"form {form.id}, question {fq.question_id}: "
                    f"duplicate printed labels: {', '.join(dup_printed)}"
                )
            mapped = {o.option_id for o in fq.options}
            missing = [o.id for o in std.options if o.id not in mapped]
            if missing:
                raise bad_request(
                    f"form {form.id}, question {fq.question_id}: "
                    f"standard options missing a printed label: {', '.join(missing)}"
                )
        forms[form.id] = form

    return Catalog(questions=questions, forms=forms)


def validate_sheets(dataset: DatasetIn, catalog: Catalog) -> None:
    """Validate the scanned sheets and their reviews against the catalog."""

    dup_sheets = _duplicates(s.id for s in dataset.sheets)
    if dup_sheets:
        raise bad_request(f"duplicate sheet ids: {', '.join(dup_sheets)}")

    for sheet in dataset.sheets:
        if sheet.known_form_id is not None and sheet.known_form_id not in catalog.forms:
            raise bad_request(
                f"sheet {sheet.id}: unknown known_form_id {sheet.known_form_id!r}"
            )
        dup_paper = _duplicates(str(a.paper_no) for a in sheet.answers)
        if dup_paper:
            raise bad_request(
                f"sheet {sheet.id}: duplicate answer paper numbers: {', '.join(dup_paper)}"
            )
        dup_reviews = _duplicates(r.question_id for r in sheet.reviews)
        if dup_reviews:
            raise bad_request(
                f"sheet {sheet.id}: duplicate reviews for questions: {', '.join(dup_reviews)}"
            )
        for review in sheet.reviews:
            question = catalog.questions.get(review.question_id)
            if question is None:
                raise bad_request(
                    f"sheet {sheet.id}: review references unknown question "
                    f"{review.question_id!r}"
                )
            if review.option_id not in {o.id for o in question.options}:
                raise bad_request(
                    f"sheet {sheet.id}: review option {review.option_id!r} "
                    f"does not belong to question {review.question_id!r}"
                )
