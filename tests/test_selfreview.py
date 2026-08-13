import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from raizuinu.config import Config
from raizuinu.cost import CostTracker
from raizuinu.selfreview import (
    JST,
    build_proposals,
    recent_audit_records,
    render_report,
    summarize,
)


def write_audit(log_dir, records):
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"audit-{datetime.now(JST).strftime('%Y%m')}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def make_records():
    now = datetime.now(JST)
    old = now - timedelta(days=30)
    return [
        {"ts": now.isoformat(), "type": "answer", "has_answer": True,
         "question": "銀行明細の取得手順は？", "sources": ["銀行明細取得.md"], "cost_jpy": 10.0},
        {"ts": now.isoformat(), "type": "answer", "has_answer": False,
         "question": "社宅の家賃補助はいくら？", "sources": [], "cost_jpy": 8.0},
        {"ts": now.isoformat(), "type": "error", "question": "エラーになった質問"},
        {"ts": now.isoformat(), "type": "update_report", "question": "更新しました"},
        # 期間外レコードは集計に含めない
        {"ts": old.isoformat(), "type": "answer", "has_answer": False,
         "question": "古い質問", "sources": [], "cost_jpy": 5.0},
    ]


class TestSummarize:
    def test_counts_and_unanswered(self, tmp_path):
        write_audit(tmp_path / "logs", make_records())
        records = recent_audit_records(tmp_path / "logs")
        stats = summarize(records)
        assert stats["total"] == 4  # 期間外1件は除外
        assert stats["answered"] == 1
        assert stats["unanswered"] == 1
        assert stats["unanswered_questions"] == ["社宅の家賃補助はいくら？"]
        assert stats["errors"] == 1
        assert stats["cost_jpy"] == 18.0


class TestBuildProposals:
    def _cost(self, tmp_path, limit=10000.0):
        return CostTracker(
            state_dir=tmp_path / "state", limit_jpy=limit, alert_threshold=0.7,
            usd_jpy_rate=155.0,
            pricing_usd_per_mtok={"input": 3.0, "output": 15.0, "cache_read": 0.3,
                                  "cache_write_5m": 3.75, "cache_write_1h": 6.0},
        )

    def test_calls_claude_and_returns_text(self, tmp_path):
        config = Config.load(tmp_path / "no-config.json")
        response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="1. 社宅制度をハンドブックに追記する【要承認】")],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=100,
                                  cache_creation_input_tokens=0, cache_read_input_tokens=0),
        )
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: response))
        proposals = build_proposals(
            {"unanswered_questions": ["社宅の家賃補助は？"]}, [], [],
            ["銀行明細取得.md"], config, self._cost(tmp_path), client=client,
        )
        assert "社宅制度" in proposals

    def test_over_limit_skips_api_call(self, tmp_path):
        config = Config.load(tmp_path / "no-config.json")
        cost = self._cost(tmp_path, limit=1.0)
        cost.add_usage({"output_tokens": 10_000_000})  # 上限超過状態

        def boom(**kwargs):
            raise AssertionError("上限超過時はAPIを呼ばない")

        client = SimpleNamespace(messages=SimpleNamespace(create=boom))
        proposals = build_proposals({}, [], [], [], config, cost, client=client)
        assert proposals == ""


class TestRenderReport:
    def test_report_contains_stats_proposals_and_approval_gate(self):
        stats = {
            "total": 10, "answered": 8, "unanswered": 2, "errors": 0,
            "by_type": {"answer": 10}, "cost_jpy": 120.5,
            "unanswered_questions": ["社宅の家賃補助は？"],
        }
        report = render_report(stats, "1. 社宅制度の追記【要承認】")
        assert "週次自己分析レポート" in report
        assert "社宅の家賃補助は？" in report
        assert "改善提案（要承認）" in report
        # 承認ゲートの明示（勝手に変更しないことの宣言）
        assert "まだ何も変更していません" in report
        assert "反映して" in report
