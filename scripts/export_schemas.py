"""Экспорт JSON Schema из нормативных Pydantic-моделей в contracts/schemas/.

Единый источник истины — contracts/models.py; схемы являются производным
артефактом версии 2.0. Запуск: python scripts/export_schemas.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from contracts import models  # noqa: E402

SCHEMAS = ROOT / "contracts" / "schemas"

EXPORTED = [
    "Context",
    "StartSession",
    "SubmitAnswer",
    "VoiceMessage",
    "ConfirmTranscript",
    "TurnCommand",
    "SessionCommand",
    "ConfirmAction",
    "CreateInvoice",
    "ErrorResponse",
    "ErrorBody",
    "SessionView",
    "TurnView",
    "StartResponse",
    "AnswerResponse",
    "ReportResponse",
    "BalanceResponse",
    "DeleteResponse",
    "InvoiceResponse",
    "TurnScores",
    "TurnAssessment",
    "PreviousAssessmentBrief",
    "GenerateTurnInput",
    "SnapshotTurn",
    "GenerateReportInput",
    "Coverage",
    "ReportFinding",
    "ReportAction",
    "ImprovedExample",
    "FeedbackReport",
    "GenerateSTTInput",
    "Transcription",
    "GenerateTTSInput",
    "SynthesizedAudio",
    "SuccessfulPaymentPayload",
]


def main() -> None:
    SCHEMAS.mkdir(parents=True, exist_ok=True)
    for name in EXPORTED:
        model = getattr(models, name)
        schema = model.model_json_schema()
        schema["$comment"] = (
            f"Производная схема из contracts/models.py, версия {models.CONTRACTS_VERSION}"
        )
        out = SCHEMAS / f"{name.replace('.', '_')}.schema.json"
        out.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"OK: {len(EXPORTED)} схем -> {SCHEMAS}")


if __name__ == "__main__":
    main()
