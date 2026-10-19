"""Воспроизводимая проверка согласованности нормативного пакета (§5.1, §5.2).

Проверяет (без БД и сети):
  schemas   — contracts/schemas/*.json соответствуют моделям (регенерация);
  examples  — примеры contracts/examples проходят заявленную валидность;
  catalog   — 6 сценариев × 6 основных = 36 записей, рубрики, длины ≤700;
  settings  — значения лимитов/retention/SLO/бюджетов равны зафиксированным
              в ТЗ 2.0 (§1.3, §3.6, §3.9, §3.10);
  states    — state-machine.json согласован с перечислениями кода;
  wbs       — суммы часов 90/18/30/6, критический путь 58, задачи ≤6 ч,
              зависимости существуют и ацикличны;
  matrix    — чеклист-матрица ≥67 строк и покрывает пункты §5.2;
  prompts   — три файла версий 2.0 существуют, непустые, хеши совпадают
              с version-manifest (если он есть);
  secrets   — example-профиль и .env.example не содержат значений секретов;
  refs      — 18 эталонов AT04/AT10 (3 роли × 2 уровня × 3 качества),
              оценки в диапазоне 0..4, reviewer_status заполнен.

Проверка подтверждает согласованность спецификации, а НЕ предметную точность
вопросов, реальную задержку провайдеров или работающий бот (для этого
T21–T23 и AT01–AT17).

Запуск:
    python verification/verify_spec.py            # вывод + spec-check.json
    python verification/verify_spec.py --check    # не перезаписывать json
Код возврата: 0 — все проверки ok; 1 — есть провалы.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS: list[dict] = []


def _record(check_id: str, description: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append(
        {"id": check_id, "description": description,
         "status": "ok" if ok else "fail", "detail": detail[:300]}
    )
    return ok


# ---------------------------------------------------------------------------
# schemas: регенерация совпадает с сохранёнными
# ---------------------------------------------------------------------------


def check_schemas() -> None:
    from contracts import models
    from scripts.export_schemas import EXPORTED

    problems = []
    schema_dir = ROOT / "contracts" / "schemas"
    for name in EXPORTED:
        model = getattr(models, name, None)
        if model is None:
            problems.append(f"{name}: модель не найдена")
            continue
        schema = model.model_json_schema()
        schema["$comment"] = (
            f"Производная схема из contracts/models.py, версия "
            f"{models.CONTRACTS_VERSION}"
        )
        path = schema_dir / f"{name.replace('.', '_')}.schema.json"
        if not path.is_file():
            problems.append(f"нет файла {path.name}")
            continue
        current = json.loads(path.read_text(encoding="utf-8"))
        if json.dumps(current, sort_keys=True) != json.dumps(schema, sort_keys=True):
            problems.append(f"{path.name} расходится с моделью")
    on_disk = {p.name for p in schema_dir.glob("*.schema.json")}
    expected = {f"{n.replace('.', '_')}.schema.json" for n in EXPORTED}
    for extra in sorted(on_disk - expected):
        problems.append(f"лишняя схема {extra}")
    _record("schemas", "contracts/schemas соответствуют моделям (регенерация)",
            not problems, "; ".join(problems))


# ---------------------------------------------------------------------------
# examples: заявленная валидность подтверждается
# ---------------------------------------------------------------------------


def check_examples() -> None:
    import contracts.models as models
    from app.domain.answers import validate_answer_text
    from app.domain.errors import DomainError
    from contracts.models import SnapshotTurn
    from contracts.semantic_validation import validate_assessment, validate_report

    examples_dir = ROOT / "contracts" / "examples"
    manifest = json.loads((examples_dir / "manifest.json").read_text("utf-8"))
    problems = []
    for entry in manifest["examples"]:
        path = examples_dir / entry["file"]
        if not path.is_file():
            problems.append(f"{entry['file']}: нет файла")
            continue
        payload = json.loads(path.read_text("utf-8"))
        model = getattr(models, entry["model"], None)
        if model is None:
            problems.append(f"{entry['file']}: неизвестная модель {entry['model']}")
            continue
        stage = entry["stage"]
        expected_valid = entry["valid"]
        try:
            if stage == "structural":
                parsed = model.model_validate(payload)
                ok = True
                _ = parsed
            elif stage == "domain":
                parsed = model.model_validate(payload)
                try:
                    validate_answer_text(parsed.text)
                    ok = True
                except DomainError:
                    ok = False
            elif stage == "semantic":
                parsed = model.model_validate(payload)
                context = entry.get("semantic_context") or {}
                if model is models.TurnAssessment:
                    issues = validate_assessment(
                        parsed,
                        question_type=models.QuestionType(context["question_type"]),
                        answer_text=context["answer_text"],
                    )
                else:
                    snapshot = [
                        SnapshotTurn.model_validate(t)
                        for t in context.get("snapshot", [])
                    ]
                    issues = validate_report(
                        parsed,
                        snapshot=snapshot,
                        expected_session_id=parsed.session_id,
                        expected_partial=context["expected_partial"],
                    )
                ok = not issues
            else:
                problems.append(f"{entry['file']}: неизвестный stage {stage}")
                continue
        except Exception as exc:
            name = type(exc).__name__
            if stage == "structural" and name == "ValidationError":
                ok = False
            else:
                problems.append(f"{entry['file']}: неожиданная ошибка {name}")
                continue
        if ok != expected_valid:
            problems.append(
                f"{entry['file']}: ожидалась valid={expected_valid}, получено {ok}"
            )
    _record("examples", "примеры contracts/examples соответствуют manifest.json",
            not problems, "; ".join(problems))


# ---------------------------------------------------------------------------
# catalog: 36 основных записей, рубрики, длины
# ---------------------------------------------------------------------------


def check_catalog() -> None:
    data = json.loads((ROOT / "config" / "questions.json").read_text("utf-8"))
    problems = []
    scenarios = data["scenarios"]
    if len(scenarios) != 6:
        problems.append(f"сценариев {len(scenarios)}, ожидалось 6")
    combos = {(s["role_id"], s["level"]) for s in scenarios}
    expected = {
        (r, lv) for r in ("python", "analyst", "qa") for lv in ("intern", "junior")
    }
    if combos != expected:
        problems.append(f"комбинации ролей/уровней: {sorted(combos)}")
    total_main = 0
    for sc in scenarios:
        mains = [e for e in sc["entries"] if e.get("main_index")]
        total_main += len(mains)
        if len(mains) != 6:
            problems.append(f"{sc['role_id']}/{sc['level']}: {len(mains)} основных")
        types = [e["question_type"] for e in sorted(mains, key=lambda x: x["main_index"])]
        if types.count("hr") != 2 or types.count("technical") != 4:
            problems.append(f"{sc['role_id']}/{sc['level']}: не 2 HR + 4 технических")
        for e in sc["entries"]:
            rubric = e.get("rubric") or {}
            if e["question_type"] == "hr" and len(
                rubric.get("evidence_criteria") or []
            ) < 3:
                problems.append(f"{e['id']}: rubric.evidence_criteria < 3")
            if len(rubric.get("key_points") or []) < 3:
                problems.append(f"{e['id']}: rubric.key_points < 3")
            if len(rubric.get("score_guidance") or {}) < 5:
                problems.append(f"{e['id']}: rubric.score_guidance < 5 градаций")
            if not e.get("fallback_question"):
                problems.append(f"{e['id']}: нет fallback_question")
            if not e.get("advice"):
                problems.append(f"{e['id']}: нет advice")
            q = e.get("question") or ""
            if q and len(q) > 700:
                problems.append(f"{e['id']}: вопрос длиннее 700")
    if total_main != 36:
        problems.append(f"основных записей {total_main}, ожидалось 36")
    _record("catalog", "каталог: 6 сценариев, 36 основных, рубрики и длины",
            not problems, "; ".join(problems))


# ---------------------------------------------------------------------------
# settings: зафиксированные значения ТЗ 2.0
# ---------------------------------------------------------------------------


def check_settings() -> None:
    s = json.loads((ROOT / "config" / "settings.json").read_text("utf-8"))
    problems = []

    def eq(path: str, expected) -> None:
        node = s
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None
            if node is None:
                problems.append(f"{path}: отсутствует")
                return
        if node != expected:
            problems.append(f"{path} = {node}, ожидалось {expected}")

    # §1.3
    eq("limits.answer_min_chars", 1)
    eq("limits.answer_max_chars", 4000)
    eq("limits.question_max_chars", 700)
    eq("limits.voice_max_bytes", 10 * 1024 * 1024)
    eq("limits.voice_max_seconds", 120)
    eq("limits.report_max_chars", 12000)
    eq("limits.message_part_max_chars", 3500)
    eq("limits.report_parts_min", 1)
    eq("limits.report_parts_max", 4)
    eq("limits.active_sessions_per_user", 1)
    eq("limits.starts_per_24h", 3)
    eq("limits.commands_per_minute", 10)
    eq("limits.concurrent_active_sessions_total", 20)
    eq("limits.concurrent_llm", 5)
    eq("limits.concurrent_stt", 2)
    eq("limits.concurrent_tts", 2)
    eq("limits.ai_queue_max_seconds", 60)
    eq("limits.max_provider_calls_per_operation", 2)
    eq("limits.stt_attempts_per_turn", 3)
    eq("limits.media_total_max_mib", 256)
    eq("interview.main_questions", 6)
    eq("interview.hr_questions", 2)
    eq("interview.technical_questions", 4)
    eq("interview.max_follow_ups_per_main", 1)
    eq("interview.max_follow_ups_per_session", 2)
    eq("interview.max_turns", 8)
    eq("interview.initial_difficulty.intern", 1)
    eq("interview.initial_difficulty.junior", 2)
    eq("interview.difficulty_min.intern", 1)
    eq("interview.difficulty_max.intern", 2)
    eq("interview.difficulty_min.junior", 1)
    eq("interview.difficulty_max.junior", 3)
    # §1.3 токены
    eq("tokens_budget.assess_input_max", 12000)
    eq("tokens_budget.assess_output_max", 1500)
    eq("tokens_budget.report_input_max", 12000)
    eq("tokens_budget.report_output_max", 3500)
    eq("tokens_budget.session_input_budget", 100000)
    eq("tokens_budget.session_output_budget", 22000)
    eq("tokens_budget.report_reserve_input", 24000)
    eq("tokens_budget.report_reserve_output", 7000)
    # §1.3 SLO p95
    eq("slo_p95_seconds.action_ack", 2)
    eq("slo_p95_seconds.first_text_question", 5)
    eq("slo_p95_seconds.next_text_question", 15)
    eq("slo_p95_seconds.text_report", 45)
    eq("slo_p95_seconds.transcription", 45)
    eq("slo_p95_seconds.voiced_question", 60)
    # §3.9 retention
    eq("retention.session_inactive_expire_hours", 24)
    eq("retention.cancelled_content_removal_hours", 24)
    eq("retention.completed_content_days", 30)
    eq("retention.audio_max_hours", 1)
    eq("retention.stt_draft_max_hours", 1)
    eq("retention.queue_payload_nullify_hours", 24)
    eq("retention.queue_metadata_days", 7)
    eq("retention.technical_logs_days", 14)
    eq("retention.events_days", 90)
    eq("retention.ai_calls_days", 90)
    eq("retention.backup_rolling_days", 7)
    eq("retention.tombstone_days", 7)
    eq("retention.deletion_deadline_hours", 24)
    # §3.10 alerts
    eq("alerts.oldest_ai_job_seconds", 60)
    eq("alerts.degraded_report_ratio_max", 0.1)
    eq("alerts.degraded_report_window", 20)
    eq("alerts.worker_heartbeat_missing_seconds", 60)
    eq("costs.budget_low_remainder_alert_ratio", 0.2)
    # §3.7
    eq("ui.loading_update_after_seconds", 15)
    eq("ui.loading_update_max_times", 1)
    _record("settings", "значения settings.json равны зафиксированным в ТЗ 2.0",
            not problems, "; ".join(problems))


# ---------------------------------------------------------------------------
# states: state-machine ↔ перечисления кода
# ---------------------------------------------------------------------------


def check_states() -> None:
    from contracts.models import SessionStatus

    sm = json.loads((ROOT / "contracts" / "state-machine.json").read_text("utf-8"))
    problems = []
    states = set(sm["transitions"].keys())
    code_states = {s.value for s in SessionStatus}
    if states != code_states:
        problems.append(
            f"расхождение состояний: {sorted(states ^ code_states)}"
        )
    terminals = set(sm["terminal_states"])
    if terminals != {"completed", "cancelled", "expired", "failed"}:
        problems.append(f"терминальные: {sorted(terminals)}")
    for state, events in sm["transitions"].items():
        for ev in events:
            # "to" допускает дизъюнкцию исходов: delivering_question|reporting
            for target in str(ev.get("to") or "").split("|"):
                if target and target not in code_states:
                    problems.append(f"{state}: неизвестный переход {target}")
    _record("states", "state-machine.json согласован с SessionStatus",
            not problems, "; ".join(problems))


# ---------------------------------------------------------------------------
# wbs: суммы, критический путь, зависимости
# ---------------------------------------------------------------------------


def check_wbs() -> None:
    est = json.loads((ROOT / "planning" / "estimates.json").read_text("utf-8"))
    problems = []
    tasks = est["tasks"]
    ids = [t["id"] for t in tasks]
    if len(ids) != len(set(ids)):
        problems.append("дубли ID задач")
    totals = est["totals"]
    main_hours = sum(t["hours"] for t in tasks if t["type"] == "main")
    reserve = sum(t["hours"] for t in tasks if t["type"] == "reserve" and t["id"] != "R04")
    commercial = sum(t["hours"] for t in tasks if t["type"] == "commercial")
    r04 = sum(t["hours"] for t in tasks if t["id"] == "R04")
    if main_hours != 90:
        problems.append(f"основные часы {main_hours}, ожидалось 90")
    if reserve != 18:
        problems.append(f"резерв {reserve}, ожидалось 18")
    if commercial != 30:
        problems.append(f"коммерческие {commercial}, ожидалось 30")
    if r04 != 6:
        problems.append(f"R04 {r04}, ожидалось 6")
    if totals.get("main_hours") != main_hours:
        problems.append("totals.main_hours расходится с задачами")
    for t in tasks:
        if not 2 <= t["hours"] <= 6:
            problems.append(f"{t['id']}: {t['hours']} ч вне диапазона 2–6")
        for dep in t["dependencies"]:
            if dep not in ids:
                problems.append(f"{t['id']}: неизвестная зависимость {dep}")
    # ацикличность
    graph = {t["id"]: set(t["dependencies"]) for t in tasks}
    order: list[str] = []
    while graph:
        ready = sorted(k for k, v in graph.items() if not v - set(order))
        if not ready:
            problems.append("цикл в зависимостях")
            break
        for node in ready:
            order.append(node)
            del graph[node]
    cp = est["critical_path"]
    if cp["hours"] != 58:
        problems.append(f"критический путь {cp['hours']} ч, ожидалось 58 (§4.2)")
    by_id = {t["id"]: t["hours"] for t in tasks}
    chain_sum = sum(by_id.get(cid, 0) for cid in cp["chain"])
    if chain_sum != cp["hours"]:
        problems.append(f"сумма цепи {chain_sum} != {cp['hours']}")
    _record("wbs", "WBS: суммы 90/18/30/6, задачи 2–6 ч, зависимости ацикличны, "
                   "критический путь 58 ч", not problems, "; ".join(problems))


# ---------------------------------------------------------------------------
# matrix: структура и покрытие пунктов §5.2
# ---------------------------------------------------------------------------


def check_matrix() -> None:
    import csv

    path = ROOT / "planning" / "checklist-matrix.csv"
    rows = list(csv.DictReader(path.read_text("utf-8").splitlines()))
    problems = []
    if len(rows) < 67:
        problems.append(f"строк {len(rows)}, ожидалось не менее 67")
    required_cols = {"checkpoint", "requirement", "where_implemented",
                     "verified_by", "status"}
    if rows and not required_cols.issubset(rows[0].keys()):
        problems.append(f"колонки: {sorted(rows[0].keys())}")
    covered = {r["checkpoint"].split("/")[0] for r in rows}
    for point in ("1.1", "1.2", "1.3", "2.1", "2.2", "2.3", "2.4", "3.1",
                  "3.2", "3.3", "4.1", "5.1", "5.2", "5.3"):
        if not any(c.startswith(point) for c in covered):
            problems.append(f"не раскрыт пункт {point}")
    for r in rows:
        if not r["status"].strip():
            problems.append(f"пустой статус: {r['requirement'][:40]}")
    _record("matrix", "checklist-matrix.csv: ≥67 детализаций, покрытие §5.2",
            not problems, "; ".join(problems))


# ---------------------------------------------------------------------------
# prompts: три файла 2.0, непустые, хеши стабильны
# ---------------------------------------------------------------------------


def check_prompts() -> None:
    problems = []
    hashes = {}
    for name in ("turn-system.txt", "report-system.txt", "user-envelope.txt"):
        path = ROOT / "prompts" / name
        if not path.is_file():
            problems.append(f"{name}: отсутствует")
            continue
        text = path.read_text("utf-8")
        if not text.strip():
            problems.append(f"{name}: пуст")
        if "2.0" not in text:
            problems.append(f"{name}: нет отметки версии 2.0")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path = ROOT / "planning" / "version-manifest.json"
    if manifest_path.is_file() and not problems:
        manifest = json.loads(manifest_path.read_text("utf-8"))
        for name, digest in hashes.items():
            recorded = (manifest.get("prompts") or {}).get(name, {}).get("sha256")
            if recorded and recorded != digest:
                problems.append(
                    f"{name}: хеш расходится с version-manifest (обновите манифест)"
                )
    _record("prompts", "промпты 2.0 на месте, непустые, хеши совпадают с манифестом",
            not problems, "; ".join(problems))


# ---------------------------------------------------------------------------
# secrets: пакет не содержит значений секретов
# ---------------------------------------------------------------------------


def check_secrets() -> None:
    problems = []
    profile = json.loads(
        (ROOT / "config" / "provider-profile.example.json").read_text("utf-8")
    )
    for block in ("llm", "stt", "tts"):
        data = profile.get(block) or {}
        for key in ("api_key", "api_key_env"):
            value = data.get(key)
            if key == "api_key" and value not in (None, ""):
                problems.append(f"{block}.api_key заполнен в example-профиле")
    env_example = (ROOT / "config" / ".env.example").read_text("utf-8")
    for line in env_example.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if name in ("BOT_TOKEN", "WEBHOOK_SECRET", "WEBHOOK_ROUTE_TOKEN",
                    "LLM_API_KEY", "STT_API_KEY", "TTS_API_KEY",
                    "POSTGRES_PASSWORD", "MIGRATOR_PASSWORD",
                    "BACKUP_PASSPHRASE") and value.strip():
            problems.append(f".env.example: {name} содержит значение")
    if (ROOT / "config" / "provider-profile.json").exists():
        # рабочая копия не должна попадать в git
        ignore = (ROOT / ".gitignore").read_text("utf-8")
        if "provider-profile.json" not in ignore:
            problems.append("provider-profile.json не в .gitignore")
    _record("secrets", "example-профиль и .env.example без значений секретов",
            not problems, "; ".join(problems))


# ---------------------------------------------------------------------------
# refs: эталоны AT04/AT10
# ---------------------------------------------------------------------------


def check_references() -> None:
    data = json.loads(
        (ROOT / "tests" / "references" / "reference_sets.json").read_text("utf-8")
    )
    problems = []
    sets = data["sets"]
    if len(sets) != 18:
        problems.append(f"наборов {len(sets)}, ожидалось 18")
    combos = {(s["role_id"], s["level"], s["quality"]) for s in sets}
    expected = {
        (r, lv, q)
        for r in ("python", "analyst", "qa")
        for lv in ("intern", "junior")
        for q in ("weak", "medium", "strong")
    }
    if combos != expected:
        problems.append("комбинации роль×уровень×качество неполны")
    for s in sets:
        for key in ("answers", "expected_assessment", "expected_transitions",
                    "report_constraints", "reviewer_status", "technical_topic"):
            if key not in s:
                problems.append(f"{s['id']}: нет поля {key}")
        for section in ("hr_self_introduction", "technical_main3"):
            a = s["expected_assessment"][section]
            for field in ("relevance", "structure"):
                if not 0 <= a[field] <= 4:
                    problems.append(f"{s['id']}/{section}.{field} вне 0..4")
            topic = a.get("correctness", a.get("evidence"))
            if topic is None or not 0 <= topic <= 4:
                problems.append(f"{s['id']}/{section}: тематическая оценка вне 0..4")
        if not s["report_constraints"]["forbidden_claims"]:
            problems.append(f"{s['id']}: нет недопустимых утверждений")
    _record("references", "18 эталонов AT04/AT10: структура, диапазоны, "
                          "reviewer_status", not problems, "; ".join(problems))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="не перезаписывать verification/spec-check.json")
    args = parser.parse_args()

    check_schemas()
    check_examples()
    check_catalog()
    check_settings()
    check_states()
    check_wbs()
    check_matrix()
    check_prompts()
    check_secrets()
    check_references()

    failed = [c for c in CHECKS if c["status"] != "ok"]
    result = {
        "version": "2.0",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "note": (
            "Результат воспроизводимой проверки согласованности пакета (§5.1). "
            "Подтверждает согласованность спецификации, не предметную точность "
            "вопросов, не реальную задержку провайдеров и не работающий бот."
        ),
        "overall": "ok" if not failed else "fail",
        "checks": CHECKS,
    }
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if not args.check:
        (ROOT / "verification" / "spec-check.json").write_text(text, encoding="utf-8")
    for check in CHECKS:
        mark = "OK  " if check["status"] == "ok" else "FAIL"
        line = f"[{mark}] {check['id']}: {check['description']}"
        if check["status"] != "ok" and check["detail"]:
            line += f" — {check['detail']}"
        print(line)
    print(f"\noverall: {result['overall']} ({len(CHECKS) - len(failed)}/{len(CHECKS)})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
