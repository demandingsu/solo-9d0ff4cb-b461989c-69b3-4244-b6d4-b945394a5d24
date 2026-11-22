"""Demo dataset: one quiz in four shuffled forms plus scanned sheets."""

from __future__ import annotations

# Standard questions and their canonical option order.
QUESTION_BANK = [
    {
        "id": "Q1",
        "prompt": "HTTP 中哪一个是幂等方法？",
        "options": [
            {"id": "Q1-A", "label": "A", "text": "GET"},
            {"id": "Q1-B", "label": "B", "text": "POST"},
            {"id": "Q1-C", "label": "C", "text": "PATCH"},
            {"id": "Q1-D", "label": "D", "text": "CONNECT"},
        ],
    },
    {
        "id": "Q2",
        "prompt": "关系数据库中消除部分依赖通常要达到第几范式？",
        "options": [
            {"id": "Q2-A", "label": "A", "text": "第一范式"},
            {"id": "Q2-B", "label": "B", "text": "第二范式"},
            {"id": "Q2-C", "label": "C", "text": "第三范式"},
            {"id": "Q2-D", "label": "D", "text": "BCNF"},
        ],
    },
    {
        "id": "Q3",
        "prompt": "TCP 三次握手的第二步发送什么标志？",
        "options": [
            {"id": "Q3-A", "label": "A", "text": "SYN"},
            {"id": "Q3-B", "label": "B", "text": "SYN + ACK"},
            {"id": "Q3-C", "label": "C", "text": "ACK"},
            {"id": "Q3-D", "label": "D", "text": "FIN"},
        ],
    },
    {
        "id": "Q4",
        "prompt": "以下哪种排序算法平均时间复杂度为 O(n log n) 且稳定？",
        "options": [
            {"id": "Q4-A", "label": "A", "text": "快速排序"},
            {"id": "Q4-B", "label": "B", "text": "归并排序"},
            {"id": "Q4-C", "label": "C", "text": "堆排序"},
            {"id": "Q4-D", "label": "D", "text": "选择排序"},
        ],
    },
    {
        "id": "Q5",
        "prompt": "SQLite 中保证读一致性的事务隔离级别通常依赖什么？",
        "options": [
            {"id": "Q5-A", "label": "A", "text": "WAL 快照"},
            {"id": "Q5-B", "label": "B", "text": "表级排他锁"},
            {"id": "Q5-C", "label": "C", "text": "行级锁"},
            {"id": "Q5-D", "label": "D", "text": "两阶段提交"},
        ],
    },
]


def _form(form_id: str, name: str, paper_order: list[str], labels: dict[str, list[str]]):
    """Build a form: paper_order lists question ids by paper number and
    ``labels`` maps question id -> printed labels in standard option order."""

    questions = []
    for paper_no, question_id in enumerate(paper_order, start=1):
        question = next(q for q in QUESTION_BANK if q["id"] == question_id)
        printed = labels[question_id]
        assert len(printed) == len(question["options"])
        questions.append(
            {
                "paper_no": paper_no,
                "question_id": question_id,
                "options": [
                    {"option_id": option["id"], "printed_label": label}
                    for option, label in zip(question["options"], printed)
                ],
            }
        )
    return {"id": form_id, "name": name, "questions": questions}


FORMS = [
    _form(
        "F1",
        "卷型 1（标准顺序）",
        ["Q1", "Q2", "Q3", "Q4", "Q5"],
        {
            "Q1": ["A", "B", "C", "D"],
            "Q2": ["A", "B", "C", "D"],
            "Q3": ["A", "B", "C", "D"],
            "Q4": ["A", "B", "C", "D"],
            "Q5": ["A", "B", "C", "D"],
        },
    ),
    _form(
        "F2",
        "卷型 2（题序与选项均打乱）",
        ["Q3", "Q1", "Q5", "Q2", "Q4"],
        {
            "Q1": ["C", "A", "D", "B"],
            "Q2": ["B", "D", "A", "C"],
            "Q3": ["D", "C", "B", "A"],
            "Q4": ["A", "C", "B", "D"],
            "Q5": ["B", "A", "C", "D"],
        },
    ),
    _form(
        "F3",
        "卷型 3（题序打乱，选项部分打乱）",
        ["Q5", "Q4", "Q2", "Q1", "Q3"],
        {
            "Q1": ["B", "C", "A", "D"],
            "Q2": ["A", "C", "D", "B"],
            "Q3": ["C", "A", "B", "D"],
            "Q4": ["D", "B", "A", "C"],
            "Q5": ["A", "C", "B", "D"],
        },
    ),
    _form(
        "F4",
        "卷型 4（题序与选项均打乱）",
        ["Q2", "Q3", "Q4", "Q5", "Q1"],
        {
            "Q1": ["D", "A", "B", "C"],
            "Q2": ["C", "A", "B", "D"],
            "Q3": ["B", "D", "A", "C"],
            "Q4": ["C", "B", "D", "A"],
            "Q5": ["D", "C", "A", "B"],
        },
    ),
]


def _scan(form_id: str, answers: dict[str, str]) -> list[dict[str, object]]:
    """Turn standard answers into printed labels for one form."""

    form = next(f for f in FORMS if f["id"] == form_id)
    by_question = {q["question_id"]: q for q in form["questions"]}
    result = []
    for question_id, option_id in answers.items():
        question = by_question[question_id]
        printed = next(
            o["printed_label"] for o in question["options"] if o["option_id"] == option_id
        )
        result.append({"paper_no": question["paper_no"], "printed_label": printed})
    result.sort(key=lambda item: item["paper_no"])
    return result


SHEETS = [
    {
        "id": "S001",
        "label": "答卷 001（卷型未知，已复核两题）",
        "known_form_id": None,
        "answers": _scan(
            "F2", {"Q1": "Q1-A", "Q2": "Q2-C", "Q3": "Q3-B", "Q4": "Q4-B", "Q5": "Q5-A"}
        ),
        "reviews": [
            {"question_id": "Q1", "option_id": "Q1-A", "reviewer": "reviewer-1"},
            {"question_id": "Q4", "option_id": "Q4-B", "reviewer": "reviewer-1"},
        ],
    },
    {
        "id": "S002",
        "label": "答卷 002（卷型 F3，未复核）",
        "known_form_id": "F3",
        "answers": _scan(
            "F3", {"Q1": "Q1-C", "Q2": "Q2-B", "Q3": "Q3-A", "Q4": "Q4-D", "Q5": "Q5-C"}
        ),
        "reviews": [],
    },
    {
        "id": "S003",
        "label": "答卷 003（无复核，卷型未定）",
        "known_form_id": None,
        "answers": _scan(
            "F4", {"Q1": "Q1-A", "Q2": "Q2-A", "Q3": "Q3-D", "Q4": "Q4-C", "Q5": "Q5-D"}
        ),
        "reviews": [],
    },
    {
        "id": "S004",
        "label": "答卷 004（复核记录互相矛盾）",
        "known_form_id": None,
        "answers": _scan(
            "F1", {"Q1": "Q1-B", "Q2": "Q2-D", "Q3": "Q3-C", "Q4": "Q4-A", "Q5": "Q5-B"}
        ),
        "reviews": [
            {"question_id": "Q1", "option_id": "Q1-B", "reviewer": "reviewer-2"},
            {"question_id": "Q2", "option_id": "Q2-A", "reviewer": "reviewer-2"},
        ],
    },
    {
        "id": "S005",
        "label": "答卷 005（卷型 F1，复核两题）",
        "known_form_id": None,
        "answers": _scan(
            "F1", {"Q1": "Q1-A", "Q2": "Q2-B", "Q3": "Q3-B", "Q4": "Q4-B", "Q5": "Q5-A"}
        ),
        "reviews": [
            {"question_id": "Q2", "option_id": "Q2-B", "reviewer": "reviewer-3"},
            {"question_id": "Q5", "option_id": "Q5-A", "reviewer": "reviewer-3"},
        ],
    },
]


DEMO_DATASET = {
    "questions": QUESTION_BANK,
    "forms": FORMS,
    "sheets": SHEETS,
}
