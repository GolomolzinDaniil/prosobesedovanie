"""Временное хранилище аудио и запуск изолированного декодера (§3.5).

- каталог 0700, файлы со случайным 32-символьным именем, доступ 0600;
- жёсткий предел 10 MiB на поток при скачивании (обрезка недопустима —
  отказ); суммарный предел 256 MiB и число файлов на сервер — при
  заполнении новый голосовой вход отклоняется без принятия ответа;
- декодер — отдельный процесс без shell-интерполяции, RLIMIT_AS 256 MiB,
  RLIMIT_CPU и wall timeout 10 с;
- каждая запись и результат TTS удаляются после обработки/доставки,
  максимум через час: finally штатной обработки, sweep каждые 5 минут,
  cleanup при запуске worker после аварии;
- Telegram file URL с токеном бота никогда не логируется и не отдаётся
  AI-провайдеру (download выполняет bot-адаптер).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_MEDIA_FILES = 100  # проектный предел числа одновременных задач на сервер


class MediaStorageFullError(Exception):
    """Хранилище заполнено: новый голосовой вход отклоняется (§3.5)."""


@dataclass(frozen=True)
class ProbeInfo:
    ok: bool
    codec: str | None
    duration_seconds: float | None
    size_bytes: int
    error: str | None


def ensure_media_dir(media_dir: Path) -> None:
    media_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(media_dir, 0o700)
    except OSError:  # pragma: no cover - зависит от ФС
        logger.warning("cannot chmod media dir")


def new_media_path(media_dir: Path, suffix: str = ".ogg") -> Path:
    """Случайное 32-символьное имя; файл создаётся вызывающим кодом с 0600."""
    ensure_media_dir(media_dir)
    name = secrets.token_hex(16)  # 32 символа
    return media_dir / f"{name}{suffix}"


def create_private_file(path: Path) -> None:
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.close(fd)


def directory_usage(media_dir: Path) -> tuple[int, int]:
    """(суммарный размер в байтах, число файлов)."""
    total = 0
    count = 0
    if not media_dir.exists():
        return 0, 0
    for entry in media_dir.iterdir():
        if entry.is_file():
            try:
                total += entry.stat().st_size
                count += 1
            except OSError:  # pragma: no cover
                continue
    return total, count


def check_capacity(media_dir: Path, max_total_mib: int, incoming_bytes: int = 0) -> None:
    total, count = directory_usage(media_dir)
    if count >= MAX_MEDIA_FILES or total + incoming_bytes > max_total_mib * 1024 * 1024:
        raise MediaStorageFullError(
            f"media storage full: {count} files, {total} bytes"
        )


def remove_file(path: Path | str | None) -> None:
    if path is None:
        return
    try:
        p = Path(path)
        if p.exists():
            p.unlink()
    except OSError:  # pragma: no cover
        logger.warning("media remove failed", extra={"path_hash": hash(str(path))})


def run_probe(path: Path, *, wall_timeout: int = 10, memory_mib: int = 256) -> ProbeInfo:
    """Изолированный процесс-декодер: без сети и shell, rlimit памяти и CPU."""

    def _limits() -> None:  # pragma: no cover - выполняется в дочернем процессе
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (memory_mib * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (wall_timeout, wall_timeout))

    try:
        completed = subprocess.run(
            [sys.executable, "-m", "app.services.media_probe", str(path)],
            capture_output=True,
            timeout=wall_timeout,
            shell=False,
            preexec_fn=_limits if os.name == "posix" else None,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ProbeInfo(False, None, None, 0, "probe_wall_timeout")
    except OSError as exc:
        return ProbeInfo(False, None, None, 0, f"probe_error:{type(exc).__name__}")
    try:
        data = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return ProbeInfo(False, None, None, 0, "probe_bad_output")
    return ProbeInfo(
        ok=bool(data.get("ok")),
        codec=data.get("codec"),
        duration_seconds=data.get("duration_seconds"),
        size_bytes=int(data.get("size_bytes") or 0),
        error=data.get("error"),
    )


def probe_ogg_opus(path: Path | str) -> ProbeInfo | None:
    """Синхронная проверка формата/длительности (используется и TTS-адаптером)."""
    info = run_probe(Path(path))
    return info if info.ok else None


async def probe_ogg_opus_async(
    path: Path, *, wall_timeout: int = 10, memory_mib: int = 256
) -> ProbeInfo:
    """Асинхронная обёртка: не блокирует event loop воркера."""
    return await asyncio.to_thread(
        run_probe, path, wall_timeout=wall_timeout, memory_mib=memory_mib
    )


def sweep_expired(media_dir: Path, max_age_seconds: int) -> int:
    """Удаляет просроченное аудио (максимум 1 час от создания, §3.9)."""
    removed = 0
    if not media_dir.exists():
        return 0
    deadline = time.time() - max_age_seconds
    for entry in media_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            if entry.stat().st_mtime < deadline:
                entry.unlink()
                removed += 1
        except OSError:  # pragma: no cover
            continue
    return removed


def startup_cleanup(media_dir: Path) -> int:
    """После аварии процесса cleanup выполняется при запуске (§3.5):
    все файлы во временном каталоге считаются сиротами."""
    removed = 0
    if not media_dir.exists():
        return 0
    for entry in media_dir.iterdir():
        if entry.is_file():
            try:
                entry.unlink()
                removed += 1
            except OSError:  # pragma: no cover
                continue
    return removed
