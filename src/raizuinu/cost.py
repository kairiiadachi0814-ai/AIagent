"""月次コスト集計・上限停止・アラート（NFR-04）。

- 月次累計を state/cost-YYYYMM.json に永続化する。
- 上限の70%（設定値）到達で管理者へ通知、100%到達で応答を停止し停止通知を出す。
- 「トークン上限到達で無言停止」を避けるため、停止の検知と通知を最初から組み込む。

注意（重要）: ファイルベースの永続化のため、
- 複数インスタンス並行時は集計が分かれる（単一インスタンス前提）。
- Cloud Runのローカルファイルシステムは揮発性（tmpfs）。GCSボリューム
  マウント等で state_dir を永続領域に向けないと、コールドスタートのたびに
  累計・通知フラグが消えて上限停止が機能しない（READMEのデプロイ手順参照）。
スケール時はFirestore等の外部ストアへ差し替える（Phase 2課題）。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))


@dataclass
class CostStatus:
    month: str
    total_jpy: float
    limit_jpy: float
    alert_sent: bool
    stopped_notified: bool

    @property
    def over_limit(self) -> bool:
        return self.total_jpy >= self.limit_jpy

    def ratio(self) -> float:
        return self.total_jpy / self.limit_jpy if self.limit_jpy > 0 else 0.0


class CostTracker:
    def __init__(
        self,
        state_dir: Path,
        limit_jpy: float,
        alert_threshold: float,
        usd_jpy_rate: float,
        pricing_usd_per_mtok: dict[str, float],
        prompt_cache_ttl: str = "1h",
    ) -> None:
        self._state_dir = state_dir
        self._limit = limit_jpy
        self._threshold = alert_threshold
        self._rate = usd_jpy_rate
        self._pricing = pricing_usd_per_mtok
        self._cache_write_key = (
            "cache_write_1h" if prompt_cache_ttl == "1h" else "cache_write_5m"
        )
        self._lock = threading.Lock()

    # --- 公開API ---

    def estimate_cost_jpy(self, usage: dict[str, int]) -> float:
        """APIレスポンスのusageから円建てコストを概算する。"""
        p = self._pricing
        usd = (
            usage.get("input_tokens", 0) * p["input"]
            + usage.get("output_tokens", 0) * p["output"]
            + usage.get("cache_read_input_tokens", 0) * p["cache_read"]
            + usage.get("cache_creation_input_tokens", 0) * p[self._cache_write_key]
        ) / 1_000_000
        return usd * self._rate

    def add_usage(self, usage: dict[str, int]) -> CostStatus:
        """使用量を月次累計へ加算し、加算後の状態を返す。"""
        cost = self.estimate_cost_jpy(usage)
        with self._lock:
            state = self._load()
            state["total_jpy"] = float(state.get("total_jpy", 0.0)) + cost
            self._save(state)
            return self._status(state)

    def status(self) -> CostStatus:
        with self._lock:
            return self._status(self._load())

    def mark_alert_sent(self) -> None:
        with self._lock:
            state = self._load()
            state["alert_sent"] = True
            self._save(state)

    def mark_stopped_notified(self) -> None:
        with self._lock:
            state = self._load()
            state["stopped_notified"] = True
            self._save(state)

    # --- 内部 ---

    @staticmethod
    def _current_month() -> str:
        return datetime.now(JST).strftime("%Y%m")

    def _state_path(self) -> Path:
        return self._state_dir / f"cost-{self._current_month()}.json"

    def _load(self) -> dict:
        path = self._state_path()
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {"total_jpy": 0.0, "alert_sent": False, "stopped_notified": False}

    def _save(self, state: dict) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._state_path().write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )

    def _status(self, state: dict) -> CostStatus:
        return CostStatus(
            month=self._current_month(),
            total_jpy=float(state.get("total_jpy", 0.0)),
            limit_jpy=self._limit,
            alert_sent=bool(state.get("alert_sent", False)),
            stopped_notified=bool(state.get("stopped_notified", False)),
        )

    def needs_alert(self, status: CostStatus) -> bool:
        return (
            not status.alert_sent
            and not status.over_limit
            and status.ratio() >= self._threshold
        )
