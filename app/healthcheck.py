"""HEALTHCHECK контейнера: проверяет /health/live API-процесса."""

from __future__ import annotations

import os
import sys
import urllib.request


def main() -> int:
    port = os.environ.get("API_PORT", "8080")
    url = f"http://127.0.0.1:{port}/health/live"
    try:
        with urllib.request.urlopen(url, timeout=4) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
