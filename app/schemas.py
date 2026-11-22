"""Pydantic request/response contracts (OpenAPI / Swagger source)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Import payload
# ---------------------------------------------------------------------------


class OptionIn(_Base):
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=32)
    text: str = Field(min_length=1, max_length=1000)


class QuestionIn(_Base):
    id: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=2000)
    options: list[OptionIn] = Field(min_length=2)
    display_order: int | None = Field(default=None, ge=0)


class FormOptionIn(_Base):
    option_id: str = Field(min_length=1, max_length=100)
    printed_label: str = Field(min_length=1, max_length=32)


class FormQuestionIn(_Base):
    paper_no: int = Field(ge=1)
    question_id: str = Field(min_length=1, max_length=100)
    options: list[FormOptionIn] = Field(min_length=2)


class FormIn(_Base):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    questions: list[FormQuestionIn] = Field(min_length=1)
    display_order: int | None = Field(default=None, ge=0)


class SheetAnswerIn(_Base):
    paper_no: int = Field(ge=1)
    printed_label: str = Field(min_length=1, max_length=64)


class SheetReviewIn(_Base):
    question_id: str = Field(min_length=1, max_length=100)
    option_id: str = Field(min_length=1, max_length=100)
    reviewer: str | None = Field(default=None, max_length=200)


class SheetIn(_Base):
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    known_form_id: str | None = Field(default=None, max_length=100)
    answers: list[SheetAnswerIn] = Field(default_factory=list)
    reviews: list[SheetReviewIn] = Field(default_factory=list)


class DatasetIn(_Base):
    questions: list[QuestionIn] = Field(min_length=1)
    forms: list[FormIn] = Field(min_length=1)
    sheets: list[SheetIn] = Field(default_factory=list)


class ReviewRegisterIn(_Base):
    option_id: str = Field(min_length=1, max_length=100)
    reviewer: str | None = Field(default=None, max_length=200)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class OptionOut(BaseModel):
    id: str
    label: str
    text: str


class QuestionOut(BaseModel):
    id: str
    prompt: str
    display_order: int
    options: list[OptionOut]


class FormQuestionOut(BaseModel):
    paper_no: int
    question_id: str
    options: list[FormOptionIn]


class FormOut(BaseModel):
    id: str
    name: str
    display_order: int
    questions: list[FormQuestionOut]


# ---------------------------------------------------------------------------
# Sheet detail
# ---------------------------------------------------------------------------


class SheetInfoOut(BaseModel):
    id: str
    label: str
    known_form_id: str | None
    created_at: str


class SheetAnswerOut(BaseModel):
    paper_no: int
    printed_label: str


class ReviewOut(BaseModel):
    question_id: str
    option_id: str
    option_label: str
    reviewer: str | None
    updated_at: str


class DerivedByFormOut(BaseModel):
    form_id: str
    paper_no: int
    printed_label: str
    option_id: str
    option_label: str


class CandidateExclusionOut(BaseModel):
    question_id: str | None = None
    reason: str
    detail: str


class CandidateEvaluationOut(BaseModel):
    form_id: str
    review_consistent: bool
    candidate: bool
    exclusions: list[CandidateExclusionOut]


class PendingReasonOut(BaseModel):
    scope: Literal["sheet", "question"]
    question_id: str | None = None
    reason: str
    detail: str


QuestionResultStatus = Literal[
    "counted", "no_candidate_form", "candidate_incomplete", "ambiguous_candidates"
]


class QuestionResultOut(BaseModel):
    question_id: str
    prompt: str
    paper_no_by_form: dict[str, int]
    status: QuestionResultStatus
    counted: bool
    derived_option_id: str | None
    derived_option_label: str | None
    reason: str | None
    detail: str | None
    derived_by_form: list[DerivedByFormOut]
    review: ReviewOut | None


class SheetResultOut(BaseModel):
    status: Literal["complete", "partial", "pending"]
    candidate_form_ids: list[str]
    candidate_evaluation: list[CandidateEvaluationOut]
    pending_reasons: list[PendingReasonOut]
    computed_at: str
    question_count: int
    counted_question_count: int


class SheetDetailOut(BaseModel):
    sheet: SheetInfoOut
    answers: list[SheetAnswerOut]
    result: SheetResultOut
    questions: list[QuestionResultOut]
    reviews: list[ReviewOut]


class SheetSummaryOut(BaseModel):
    id: str
    label: str
    known_form_id: str | None
    status: Literal["complete", "partial", "pending", "not_computed"]
    candidate_form_count: int
    question_count: int
    counted_question_count: int
    computed_at: str | None


class SheetListOut(BaseModel):
    sheets: list[SheetSummaryOut]
    total: int


class ReviewMutationResultOut(BaseModel):
    status: Literal["complete", "partial", "pending"]
    candidate_form_ids: list[str]
    pending_reasons: list[PendingReasonOut]
    question_count: int
    counted_question_count: int


class ReviewMutationOut(BaseModel):
    sheet_id: str
    question_id: str
    action: Literal["registered", "withdrawn"]
    result: ReviewMutationResultOut


class SheetPendingOut(BaseModel):
    sheet_id: str
    status: Literal["complete", "partial", "pending"]
    pending_reasons: list[PendingReasonOut]


class QuestionStatOptionOut(BaseModel):
    id: str
    label: str
    text: str
    count: int


class QuestionStatOut(BaseModel):
    question_id: str
    prompt: str
    total_sheets: int
    counted_sheets: int
    uncounted_sheets: int
    options: list[QuestionStatOptionOut]


class StatsOut(BaseModel):
    questions: list[QuestionStatOut]


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


class ImportReceiptOut(BaseModel):
    questions: int
    forms: int
    sheets: int
    reviews: int


class HealthOut(BaseModel):
    status: str
    database: str
