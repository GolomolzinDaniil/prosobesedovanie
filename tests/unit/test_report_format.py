"""Форматтер отчёта и детерминированный запасной отчёт (§3.4; T13).

Порядок разделов, метки partial/degraded, разбиение на 1–4 части по ≤3500
без разрыва цитат, сокращение до ≤12000, самопроверка запасного отчёта.
"""

from __future__ import annotations

import uuid

import pytest

from app.config import load_config
from app.services.report_service import (
    build_deterministic_report,
    compute_partial,
    format_report_text,
    shrink_to_limit,
    split_into_parts,
)
from contracts.models import (
    MESSAGE_PART_MAX_CHARS,
    REPORT_MAX_CHARS,
    Coverage,
    FeedbackReport,
    ImprovedExample,
    QuestionType,
    ReportAction,
    ReportFinding,
    SnapshotTurn,
    TurnAssessment,
    TurnKind,
    TurnScores,
)
from contracts.semantic_validation import validate_report
from tests.conftest import make_env


@pytest.fixture()
def config(tmp_path_factory):
    from tests.conftest import write_filled_provider_profile

    cfg_dir = tmp_path_factory.mktemp("report-fmt")
    return load_config(
        make_env(
            PROVIDER_PROFILE_PATH=write_filled_provider_profile(cfg_dir),
            LLM_API_KEY="k", STT_API_KEY="k", TTS_API_KEY="k",
        )
    )


def _session_row(**overrides) -> dict:
    row = {
        "id": uuid.uuid4(),
        "role_id": "python",
        "level": "junior",
        "format": "text",
    }
    row.update(overrides)
    return row


def _turn(index: int, *, topic: str, answer: str | None, skipped: bool = False,
          assessment: TurnAssessment | None = None,
          kind: TurnKind = TurnKind.main) -> SnapshotTurn:
    turn_id = uuid.uuid4()
    return SnapshotTurn(
        turn_id=turn_id,
        ordinal=index,
        kind=kind,
        question_type=QuestionType.hr if index <= 1 else QuestionType.technical,
        main_index=index if kind is TurnKind.main else None,
        topic=topic,
        question=f"Вопрос {index} по теме {topic}?",
        answer_text=answer,
        skipped=skipped,
        assessment=assessment,
    )


def _assessment(turn_id, *, quote: str, missing: list[str] | None = None,
                score: int = 3, technical: bool = True) -> TurnAssessment:
    return TurnAssessment(
        turn_id=turn_id,
        scores=TurnScores(
            relevance=score, structure=score,
            correctness=score if technical else None,
            evidence=None if technical else score,
        ),
        missing_points=missing or [],
        evidence_quotes=[quote],
        explanation="Ответ по существу.",
        suggested_action="next_topic",
        next_question=None,
    )


class TestSplitIntoParts:
    def test_short_text_single_part(self):
        parts = split_into_parts("Короткий отчёт.\n\nВторой блок.")
        assert parts == ["Короткий отчёт.\n\nВторой блок."]

    def test_sections_split_at_boundaries(self):
        sections = [f"Раздел {i}\n" + "текст " * 200 for i in range(8)]
        text = "\n\n".join(sections)
        parts = split_into_parts(text)
        assert 2 <= len(parts) <= 4 or len(parts) > 4  # допускаем >4 до shrink
        assert all(len(p) <= MESSAGE_PART_MAX_CHARS for p in parts)
        # ни одна секция не разорвана посередине
        joined = "\n\n".join(parts)
        assert joined == text

    def test_quote_not_torn_apart(self):
        # цитата помещается в часть (3000 < 3500) и переносится целиком
        quote = "квалификация. " * 210  # ~2940 символов с границами предложений
        block = ("Заголовок раздела.\n" + "пояснение. " * 300 + "\n"
                 + "  Цитата: «" + quote.strip() + "»\n  Конец.")
        parts = split_into_parts(block)
        assert len(parts) >= 2
        for part in parts:
            # баланс кавычек: часть не начинается и не кончается внутри цитаты
            assert part.count("«") == part.count("»"), part[:80]
        assert any("«" + quote.strip()[:100] in p for p in parts)

    def test_oversized_quote_split_is_safe(self):
        # цитата длиннее предела части: деление вынужденно, но без зависаний
        quote = "квалификация " * 300  # ~3900 символов
        block = "  Цитата: «" + quote.strip() + "»"
        parts = split_into_parts(block)
        assert all(len(p) <= MESSAGE_PART_MAX_CHARS for p in parts)
        assert "".join(p.strip() for p in parts).count("квалификация") == 300

    def test_limit_respected_for_huge_paragraph(self):
        para = "слово " * 3000  # 18000 символов одним абзацем
        parts = split_into_parts(para.strip())
        assert all(len(p) <= MESSAGE_PART_MAX_CHARS for p in parts)


class TestFormatReportText:
    def _report(self, session_row, snapshot, *, partial=False) -> FeedbackReport:
        return build_deterministic_report(
            load_config(make_env()),
            session=session_row,
            snapshot=snapshot,
            partial=partial,
        )

    def test_section_order_and_labels(self, config):
        session = _session_row()
        t1 = _turn(1, topic="self_introduction", answer="Учусь на третьем курсе.")
        t1 = SnapshotTurn(
            **{**t1.model_dump(),
               "assessment": _assessment(t1.turn_id, quote="Учусь на третьем курсе.",
                                         missing=["мотивация"], technical=False)}
        )
        t2 = _turn(2, topic="collections", answer=None, skipped=True)
        snapshot = [t1, t2]
        report = build_deterministic_report(
            config, session=session, snapshot=snapshot, partial=True
        )
        text = format_report_text(
            config, report, session=session, snapshot=snapshot, degraded=True
        )
        order = [
            "Персональный разбор",
            "Неполное интервью",
            "Разбор ограничен из-за технической ошибки",
            "Параметры:",
            "Разобранные темы:",
            "Сильные стороны:",
            "Ошибки и пробелы:",
            "Приоритетные действия:",
        ]
        positions = [text.index(part) for part in order]
        assert positions == sorted(positions)
        assert "«Учусь на третьем курсе.»" in text
        assert "пропущено" in text

    def test_examples_come_last(self, config):
        session = _session_row()
        t1 = _turn(1, topic="self_introduction", answer="Ответ про курс и проект.")
        snapshot = [t1]
        report = FeedbackReport(
            session_id=session["id"],
            partial=False,
            coverage=Coverage(main_total=1, main_answered=1, main_skipped=0,
                              follow_ups=0),
            strengths=[],
            gaps=[ReportFinding(turn_id=t1.turn_id, quote="Ответ про курс",
                                skipped=False, comment="Мало деталей проекта.")],
            actions=[ReportAction(priority=1, related_turn_id=t1.turn_id,
                                  text="Добавьте детали проекта.")],
            improved_examples=[ImprovedExample(
                turn_id=t1.turn_id, text="Улучшенный пример ответа.",
                uses_placeholders=False,
            )],
        )
        text = format_report_text(
            config, report, session=session, snapshot=snapshot, degraded=False
        )
        assert (
            text.index("Приоритетные действия:")
            < text.index("Улучшенные примеры ответов:")
        )
        assert text.index("Улучшенный пример ответа.") > text.index(
            "Добавьте детали проекта."
        )


class TestShrinkToLimit:
    def test_huge_report_shrinks_to_12000(self, config):
        session = _session_row()
        snapshot = []
        for i in range(1, 7):
            answer = f"Ответ {i}. " + "детали " * 200
            turn = _turn(i, topic=f"topic_{i}" if i > 2 else
                         ("self_introduction" if i == 1 else "project_story"),
                         answer=answer)
            assessment = _assessment(
                turn.turn_id, quote=f"Ответ {i}.", missing=[f"пункт {j}" for j in range(4)]
            )
            snapshot.append(
                SnapshotTurn(**{**turn.model_dump(), "assessment": assessment})
            )
        report = build_deterministic_report(
            config, session=session, snapshot=snapshot, partial=False
        )
        # искусственно раздуваем комментарии и добавляем примеры
        report = report.model_copy(update={
            "strengths": [
                f.model_copy(update={"comment": "пояснение " * 120})
                for f in report.strengths
            ],
            "improved_examples": [
                ImprovedExample(turn_id=t.turn_id, text=("пример " * 600).strip(),
                                uses_placeholders=False)
                for t in snapshot[:3]
            ],
        })
        text, parts = shrink_to_limit(
            config, report, session=session, snapshot=snapshot, degraded=True
        )
        assert len(text) <= REPORT_MAX_CHARS
        assert all(len(p) <= MESSAGE_PART_MAX_CHARS for p in parts)
        assert 1 <= len(parts) <= 4
        # минимум одно действие и связь ошибок с ответами сохранены
        assert "Приоритетные действия:" in text
        assert "Цитата: «" in text


class TestDeterministicReport:
    def test_passes_self_validation(self, config):
        session = _session_row()
        t1 = _turn(1, topic="self_introduction",
                   answer="Учусь на третьем курсе, пишу проект.")
        t1 = SnapshotTurn(**{**t1.model_dump(), "assessment": _assessment(
            t1.turn_id, quote="Учусь на третьем курсе", missing=["мотивация"],
            score=3, technical=False)})
        t2 = _turn(2, topic="project_story", answer=None, skipped=True)
        t3 = _turn(3, topic="collections", answer="list изменяемый, tuple нет.",
                   assessment=None)
        snapshot = [t1, t2, t3]
        report = build_deterministic_report(
            config, session=session, snapshot=snapshot, partial=True
        )
        issues = validate_report(
            report, snapshot=snapshot, expected_session_id=session["id"],
            expected_partial=True,
        )
        assert issues == []
        # пропущенные не попадают в сильные стороны
        assert all(not f.skipped for f in report.strengths)
        # пропуск представлен как пробел без цитаты
        skipped_gaps = [g for g in report.gaps if g.skipped]
        assert skipped_gaps and all(g.quote is None for g in skipped_gaps)
        # действие связано с пробелом
        gap_ids = {g.turn_id for g in report.gaps}
        assert any(a.related_turn_id in gap_ids for a in report.actions)
        # приоритеты 1..n без пропусков
        assert [a.priority for a in report.actions] == list(
            range(1, len(report.actions) + 1)
        )
        # примеров нет: детерминированный отчёт не выдумывает факты
        assert report.improved_examples == []

    def test_no_assessments_no_fabrication(self, config):
        session = _session_row()
        t1 = _turn(1, topic="self_introduction", answer="Просто ответ без оценки.")
        snapshot = [t1]
        report = build_deterministic_report(
            config, session=session, snapshot=snapshot, partial=True
        )
        assert report.strengths == []
        assert report.gaps == []
        assert len(report.actions) >= 1
        issues = validate_report(
            report, snapshot=snapshot, expected_session_id=session["id"],
            expected_partial=True,
        )
        assert issues == []

    def test_invalid_saved_quote_ignored(self, config):
        """Цитата, не найденная в ответе, не воспроизводится (§3.4)."""
        session = _session_row()
        t1 = _turn(1, topic="self_introduction", answer="Короткий ответ.")
        t1 = SnapshotTurn(**{**t1.model_dump(), "assessment": _assessment(
            t1.turn_id, quote="Этого текста нет в ответе", missing=["детали"],
            score=4, technical=False)})
        snapshot = [t1]
        report = build_deterministic_report(
            config, session=session, snapshot=snapshot, partial=False
        )
        for finding in [*report.strengths, *report.gaps]:
            assert finding.quote is None or finding.quote in (t1.answer_text or "")
        assert report.strengths == []


class TestPartialAndCoverage:
    def test_compute_partial(self):
        snapshot = [_turn(i, topic="t", answer="a") for i in range(1, 5)]
        assert compute_partial(snapshot, main_total=6) is True
        full = [_turn(i, topic="t", answer="a") for i in range(1, 7)]
        assert compute_partial(full, main_total=6) is False

    def test_skipped_main_is_not_partial(self):
        snapshot = [
            _turn(i, topic="t", answer=None, skipped=True) if i == 3
            else _turn(i, topic="t", answer="a")
            for i in range(1, 7)
        ]
        assert compute_partial(snapshot, main_total=6) is False
