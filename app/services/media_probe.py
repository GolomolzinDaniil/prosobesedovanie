"""Изолированная проверка аудио (§3.5): OGG-контейнер, Opus, фактическая
длительность. Запускается отдельным процессом без сети и shell-интерполяции,
с ограничением памяти 256 MiB, процессорного времени и wall timeout 10 с.

Чистый парсер OGG-страниц: capture pattern 'OggS', идентификационный
заголовок 'OpusHead', длительность по granule position последней страницы
(48000 Гц для Opus). Полный декодер (ffmpeg) может заменить этот модуль
в production, сохранив контракт вывода JSON.

Вывод: {"ok": bool, "codec": "opus"|null, "duration_seconds": float|null,
"size_bytes": int, "error": str|null}
"""

from __future__ import annotations

import json
import struct
import sys

PAGE_HEADER_SIZE = 27
OPUS_SAMPLE_RATE = 48000
MAX_FILE_BYTES = 64 * 1024 * 1024  # защита парсера (вход уже ограничен 10 MiB)


def parse_ogg_opus(path: str) -> dict:
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        return {"ok": False, "codec": None, "duration_seconds": None,
                "size_bytes": 0, "error": f"read:{type(exc).__name__}"}
    size = len(data)
    if size > MAX_FILE_BYTES:
        return {"ok": False, "codec": None, "duration_seconds": None,
                "size_bytes": size, "error": "file_too_large_for_parser"}
    if size < PAGE_HEADER_SIZE or data[:4] != b"OggS":
        return {"ok": False, "codec": None, "duration_seconds": None,
                "size_bytes": size, "error": "not_ogg_container"}
    if data[4] != 0:  # версия структуры страницы
        return {"ok": False, "codec": None, "duration_seconds": None,
                "size_bytes": size, "error": "unsupported_ogg_version"}

    offset = 0
    page_index = 0
    codec = None
    max_granule = -1
    while offset + PAGE_HEADER_SIZE <= size:
        if data[offset:offset + 4] != b"OggS":
            return {"ok": False, "codec": codec, "duration_seconds": None,
                    "size_bytes": size, "error": "broken_page_sync"}
        header = data[offset:offset + PAGE_HEADER_SIZE]
        granule = struct.unpack("<q", header[6:14])[0]
        segments_count = header[26]
        if offset + PAGE_HEADER_SIZE + segments_count > size:
            break  # неполная последняя страница (обрезанный поток)
        segment_table = data[offset + PAGE_HEADER_SIZE:offset + PAGE_HEADER_SIZE + segments_count]
        payload_size = sum(segment_table)
        payload_start = offset + PAGE_HEADER_SIZE + segments_count
        payload_end = payload_start + payload_size
        if payload_end > size:
            break
        if page_index == 0:
            if data[payload_start:payload_start + 8] != b"OpusHead":
                return {"ok": False, "codec": None, "duration_seconds": None,
                        "size_bytes": size, "error": "not_opus_codec"}
            codec = "opus"
        if granule > max_granule:
            max_granule = granule
        offset = payload_end
        page_index += 1
        if page_index > 100000:  # защита от патологических файлов
            return {"ok": False, "codec": codec, "duration_seconds": None,
                    "size_bytes": size, "error": "too_many_pages"}

    if codec != "opus":
        return {"ok": False, "codec": codec, "duration_seconds": None,
                "size_bytes": size, "error": "not_opus_codec"}
    if max_granule <= 0:
        return {"ok": False, "codec": codec, "duration_seconds": None,
                "size_bytes": size, "error": "no_granule_position"}
    duration = max_granule / OPUS_SAMPLE_RATE
    return {"ok": True, "codec": codec, "duration_seconds": duration,
            "size_bytes": size, "error": None}


def main() -> None:
    if len(sys.argv) != 2:
        print(json.dumps({"ok": False, "error": "usage"}))
        sys.exit(2)
    result = parse_ogg_opus(sys.argv[1])
    print(json.dumps(result))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
