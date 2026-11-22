"""FastAPI application: review-driven answer statistics for shuffled quizzes."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import Body, Depends, FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse

from . import db, engine, seed, service
from .schemas import (
    DatasetIn,
    FormOut,
    HealthOut,
    ImportReceiptOut,
    QuestionOut,
    ReviewMutationOut,
    ReviewRegisterIn,
    SheetDetailOut,
    SheetListOut,
    SheetPendingOut,
    StatsOut,
)
from .validation import bad_request, not_found, validate_catalog, validate_sheets

logger = logging.getLogger("quiz_stats")

DbPath = Annotated[str, Depends(db.database_path)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    path = db.init_db()
    # Only seed an empty database so restarts keep reviewer changes.
    def maybe_seed(conn):
        count = conn.execute("SELECT COUNT(*) AS n FROM standard_questions").fetchone()["n"]
        if count == 0:
            dataset = DatasetIn.model_validate(seed.DEMO_DATASET)
            catalog = validate_catalog(dataset)
            validate_sheets(dataset, catalog)
            return engine.replace_dataset(conn, catalog, dataset, engine.now_iso())
        return None

    db.run_write(path, maybe_seed)
    yield


app = FastAPI(
    title="打乱卷型单选测验 · 答项统计 API",
    version="1.0.0",
    description=(
        "扫描答卷只记录卷面题号与印刷选项标记，卷型丢失时用复核记录反推。\n\n"
        "- 候选卷型：符合该卷 **全部** 复核记录的卷型。\n"
        "- 只统计全部候选卷型都还原为同一标准选项的题目。\n"
        "- 无候选卷型时整卷待核，不参与任何题目的分布。\n"
        "- 登记/撤回复核只重算该卷，并与逐卷结果、题目计数在同一事务内生效。"
    ),
    lifespan=lifespan,
)


def _api_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, KeyError):
        return not_found(str(exc))
    if isinstance(exc, LookupError):
        return not_found(f"review not found: {exc}")
    if isinstance(exc, ValueError):
        return bad_request(str(exc))
    logger.exception("unhandled error", exc_info=exc)
    return HTTPException(status_code=500, detail="internal error")


def _persistence_error(exc: sqlite3.Error) -> HTTPException:
    logger.exception("database error", exc_info=exc)
    return HTTPException(
        status_code=503,
        detail="数据库访问失败，原有的复核记录、逐卷结果与计数均保持不变",
    )


def _read(db_path: str, fn) -> Any:
    try:
        return db.run_read(db_path, fn)
    except sqlite3.Error as exc:
        raise _persistence_error(exc) from exc
    except Exception as exc:
        raise _api_error(exc) from exc


def _write(db_path: str, fn) -> Any:
    try:
        return db.run_write(db_path, fn)
    except sqlite3.Error as exc:
        raise _persistence_error(exc) from exc
    except Exception as exc:
        raise _api_error(exc) from exc


# ---------------------------------------------------------------------------
# Admin / bootstrap
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthOut, tags=["admin"])
def health(db_path: DbPath) -> dict[str, str]:
    try:
        db.run_read(db_path, lambda conn: conn.execute("SELECT 1").fetchone())
    except Exception as exc:  # noqa: BLE001 - health endpoint reports, never raises
        logger.warning("health check failed: %s", exc)
        return JSONResponse(status_code=503, content={"status": "error", "database": "unavailable"})
    return {"status": "ok", "database": db_path}


@app.post(
    "/admin/import",
    response_model=ImportReceiptOut,
    tags=["admin"],
    summary="替换全部数据（标准题、卷型对照、答卷与复核），并全量重算",
)
def import_dataset(payload: Annotated[DatasetIn, Body(...)], db_path: DbPath) -> dict[str, int]:
    try:
        catalog = validate_catalog(payload)
        validate_sheets(payload, catalog)
    except HTTPException:
        raise
    except ValueError as exc:
        raise bad_request(str(exc))
    return _write(
        db_path,
        lambda conn: engine.replace_dataset(conn, catalog, payload, engine.now_iso()),
    )


@app.post(
    "/admin/recompute",
    response_model=ImportReceiptOut,
    tags=["admin"],
    summary="按现有数据重算全部答卷与计数（用于修复/演示）",
)
def recompute_all(db_path: DbPath) -> dict[str, int]:
    def work(conn):
        rows = conn.execute("SELECT id FROM sheets ORDER BY id").fetchall()
        at = engine.now_iso()
        for row in rows:
            engine.recompute_sheet(conn, row["id"], at)
        engine.rebuild_all_counts(conn)
        return {
            "questions": conn.execute(
                "SELECT COUNT(*) AS n FROM standard_questions"
            ).fetchone()["n"],
            "forms": conn.execute("SELECT COUNT(*) AS n FROM forms").fetchone()["n"],
            "sheets": len(rows),
            "reviews": conn.execute("SELECT COUNT(*) AS n FROM reviews").fetchone()["n"],
        }

    return _write(db_path, work)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


@app.get("/questions", response_model=list[QuestionOut], tags=["catalog"])
def get_questions(db_path: DbPath) -> list[dict]:
    return _read(db_path, service.list_questions)


@app.get("/forms", response_model=list[FormOut], tags=["catalog"])
def get_forms(db_path: DbPath) -> list[dict]:
    return _read(db_path, service.list_forms)


# ---------------------------------------------------------------------------
# Sheets and reviews
# ---------------------------------------------------------------------------


@app.get("/sheets", response_model=SheetListOut, tags=["sheets"])
def get_sheets(
    db_path: DbPath,
    status_filter: Annotated[
        Literal["complete", "partial", "pending", "not_computed"] | None,
        Query(alias="status", description="按卷状态过滤"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    return _read(
        db_path,
        lambda conn: service.list_sheets(
            conn, status=status_filter, limit=limit, offset=offset
        ),
    )


@app.get("/sheets/{sheet_id}", response_model=SheetDetailOut, tags=["sheets"])
def get_sheet(sheet_id: Annotated[str, Path(min_length=1, max_length=100)], db_path: DbPath) -> dict:
    return _read(db_path, lambda conn: service.get_sheet_detail(conn, sheet_id))


@app.get("/sheets/{sheet_id}/pending", response_model=SheetPendingOut, tags=["sheets"], summary="待核原因")
def get_sheet_pending(
    sheet_id: Annotated[str, Path(min_length=1, max_length=100)], db_path: DbPath
) -> dict:
    return _read(db_path, lambda conn: service.get_pending(conn, sheet_id))


def _review_write(
    sheet_id: str, question_id: str, payload: ReviewRegisterIn | None, db_path: str
) -> dict:
    def work(conn):
        sheet = conn.execute("SELECT id FROM sheets WHERE id = ?", (sheet_id,)).fetchone()
        if sheet is None:
            raise not_found(f"sheet {sheet_id!r} not found")
        question = conn.execute(
            "SELECT id FROM standard_questions WHERE id = ?", (question_id,)
        ).fetchone()
        if question is None:
            raise not_found(f"question {question_id!r} not found")
        if payload is not None:
            option = conn.execute(
                "SELECT id FROM standard_options WHERE id = ? AND question_id = ?",
                (payload.option_id, question_id),
            ).fetchone()
            if option is None:
                raise bad_request(
                    f"option {payload.option_id!r} does not belong to question {question_id!r}"
                )
        resolved = engine.apply_review_change(
            conn,
            sheet_id=sheet_id,
            question_id=question_id,
            option_id=payload.option_id if payload is not None else None,
            reviewer=payload.reviewer if payload is not None else None,
            computed_at=engine.now_iso(),
        )
        return {
            "sheet_id": sheet_id,
            "question_id": question_id,
            "action": "registered" if payload is not None else "withdrawn",
            "result": {
                "status": resolved["status"],
                "candidate_form_ids": resolved["candidate_form_ids"],
                "pending_reasons": resolved["pending_reasons"],
                "question_count": resolved["question_count"],
                "counted_question_count": resolved["counted_question_count"],
            },
        }

    return _write(db_path, work)


@app.put(
    "/sheets/{sheet_id}/reviews/{question_id}",
    response_model=ReviewMutationOut,
    tags=["reviews"],
    summary="登记（或覆盖）一道标准题的实际选项",
)
def register_review(
    sheet_id: Annotated[str, Path(min_length=1, max_length=100)],
    question_id: Annotated[str, Path(min_length=1, max_length=100)],
    payload: Annotated[ReviewRegisterIn, Body(...)],
    db_path: DbPath,
) -> dict:
    return _review_write(sheet_id, question_id, payload, db_path)


@app.delete(
    "/sheets/{sheet_id}/reviews/{question_id}",
    response_model=ReviewMutationOut,
    tags=["reviews"],
    summary="撤回一道标准题的复核",
)
def withdraw_review(
    sheet_id: Annotated[str, Path(min_length=1, max_length=100)],
    question_id: Annotated[str, Path(min_length=1, max_length=100)],
    db_path: DbPath,
) -> dict:
    return _review_write(sheet_id, question_id, None, db_path)


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


@app.get("/stats", response_model=StatsOut, tags=["stats"])
def get_stats(
    db_path: DbPath,
    question_id: Annotated[str | None, Query(description="只看某道标准题")] = None,
) -> dict:
    return {"questions": _read(db_path, lambda conn: service.get_stats(conn, question_id))}


@app.get("/stats/{question_id}", response_model=StatsOut, tags=["stats"])
def get_question_stats(
    question_id: Annotated[str, Path(min_length=1, max_length=100)], db_path: DbPath
) -> dict:
    return {"questions": _read(db_path, lambda conn: service.get_stats(conn, question_id))}
