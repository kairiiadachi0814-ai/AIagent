"""監査ログ（NFR-03）。

いつ・誰の依頼で・何を参照し・何を返したかをJSONLで記録する。
月別ファイル（audit-YYYYMM.jsonl）へ追記し、保持期間（設定値）を過ぎた
ファイルは書き込み時に削除する。標準出力にも1行出す（Cloud Run等の
プラットフォームログにも同内容が残る）。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

JST = timezone(timedelta(hours=9))


class AuditLogger:
    def __init__(self, log_dir: Path, retention_days: int = 90) -> None:
        self._dir = log_dir
        self._retention_days = retention_days
        self._lock = threading.Lock()

    def log(self, record: dict[str, Any]) -> None:
        entry = {"ts": datetime.now(JST).isoformat(), **record}
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / f"audit-{datetime.now(JST).strftime('%Y%m')}.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._cleanup()
        try:
            print(f"[audit] {line}", file=sys.stdout, flush=True)
        except Exception:
            # cp932コンソール等でのUnicodeEncodeErrorはファイル記録があるため無視
            pass

    def _cleanup(self) -> None:
        cutoff = time.time() - self._retention_days * 86400
        for path in self._dir.glob("audit-*.jsonl"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass
