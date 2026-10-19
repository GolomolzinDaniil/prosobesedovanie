"""Манифест версий развёртывания (§3.10: фиксировать версии зависимостей,
контейнера, миграции, профиля и промптов; задача T23).

Пишет planning/version-manifest.json: pinned-зависимости, базовый образ,
список миграций, версии конфигураций/контрактов/каталога, отпечатки
(hash sha256) промптов и заполненного профиля провайдера (без значений
секретов). Запускать перед каждым развёртыванием staging/prod.

Использование:
    python scripts/make_version_manifest.py [--profile config/provider-profile.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_rev() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _profile_fingerprint(path: Path) -> dict:
    """Отпечаток профиля без секретов: только структура, версии и настройки,
    значения *_env-имён и ключей заменяются отметкой (секреты не покидают
    окружение, §3.9)."""
    if not path.is_file():
        return {"present": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"present": True, "parse_error": True}

    def redact(node):
        if isinstance(node, dict):
            return {
                k: ("<redacted>" if any(
                    marker in k.lower() for marker in ("key", "token", "secret")
                ) else redact(v))
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [redact(v) for v in node]
        return node

    redacted = redact(data)
    return {
        "present": True,
        "sha256": _sha256(path),
        "content_redacted": redacted,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        default=str(ROOT / "config" / "provider-profile.json"),
        help="путь к заполненному профилю провайдера (рабочая копия)",
    )
    args = parser.parse_args()

    requirements = {}
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "==" in line:
            name, _, version = line.partition("==")
            requirements[name.strip()] = version.strip()

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    base_image = None
    match = re.search(r"^FROM\s+(\S+)", dockerfile, re.MULTILINE)
    if match:
        base_image = match.group(1)

    migrations = sorted(
        p.name for p in (ROOT / "migrations").glob("*.sql")
    )

    prompts = {
        p.name: {"sha256": _sha256(p), "bytes": p.stat().st_size}
        for p in sorted((ROOT / "prompts").glob("*.txt"))
    }

    configs = {}
    for name in ("settings.json", "questions.json", "products.json"):
        path = ROOT / "config" / name
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            configs[name] = {
                "version": data.get("version"),
                "sha256": _sha256(path),
            }

    manifest = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_rev": _git_rev(),
        "python": sys.version.split()[0],
        "base_image": base_image,
        "requirements": requirements,
        "migrations": migrations,
        "prompts": prompts,
        "config": configs,
        "contracts_version": json.loads(
            (ROOT / "contracts" / "methods.json").read_text(encoding="utf-8")
        ).get("version"),
        "provider_profile": _profile_fingerprint(Path(args.profile)),
    }

    out = ROOT / "planning" / "version-manifest.json"
    out.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"OK: манифест версий -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
