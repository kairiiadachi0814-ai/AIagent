"""週次自己分析 — 承認制の自己改善サイクル（観察と提案のみ）。

監査ログ・フィードバック・更新受付リストを分析し、改善提案レポートを
管理者ルームへ投稿する。**本モジュールはハンドブック・コード・設定に一切の
変更を加えない**。提案の反映は、管理者の承認のもとClaude Code経由で行う
（要件定義書ガードレール4「権限の境界は人間が決める」に基づく設計）。

実行: python -m raizuinu.selfreview（VPSではsystemdタイマーで週次実行）
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .chatwork import ChatworkClient
from .config import Config
from .cost import CostTracker
from .handbook import HandbookLoader

JST = timezone(timedelta(hours=9))
ANALYSIS_DAYS = 7
MAX_LISTED_QUESTIONS = 15

APPROVAL_FOOTER = (
    "――――――――――\n"
    "本レポートは提案のみで、まだ何も変更していません。\n"
    "反映したい項目があれば、Claude Codeで「自己分析レポートの提案◯番を反映して」と"
    "ご指示ください（実装・テスト・レビューのうえ反映します）。"
)


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def recent_audit_records(log_dir: Path, days: int = ANALYSIS_DAYS) -> list[dict]:
    """直近days日分の監査レコードを返す（当月・前月のファイルを対象）。"""
    cutoff = datetime.now(JST) - timedelta(days=days)
    records: list[dict] = []
    for path in sorted(log_dir.glob("audit-*.jsonl")):
        for record in load_jsonl(path):
            try:
                ts = datetime.fromisoformat(str(record.get("ts", "")))
            except ValueError:
                continue
            if ts >= cutoff:
                records.append(record)
    return records


def summarize(records: list[dict]) -> dict[str, Any]:
    """監査レコードから決定的に集計する（ここにAIは使わない）。"""
    by_type = Counter(str(r.get("type", "answer")) for r in records)
    answers = [r for r in records if r.get("type", "answer") == "answer"]
    unanswered = [r for r in answers if not r.get("has_answer")]
    source_counter: Counter = Counter()
    for r in answers:
        for s in r.get("sources") or []:
            source_counter[str(s)] += 1
    cost = sum(float(r.get("cost_jpy") or 0.0) for r in records)
    return {
        "total": len(records),
        "by_type": dict(by_type),
        "answered": len(answers) - len(unanswered),
        "unanswered": len(unanswered),
        "unanswered_questions": [
            str(r.get("question", ""))[:120] for r in unanswered[:MAX_LISTED_QUESTIONS]
        ],
        "errors": by_type.get("error", 0),
        "top_sources": source_counter.most_common(5),
        "cost_jpy": round(cost, 1),
    }


def build_proposals(
    stats: dict,
    feedbacks: list[dict],
    pending_updates: list[dict],
    handbook_names: list[str],
    config: Config,
    cost: CostTracker,
    client: Any | None = None,
) -> str:
    """統計とフィードバックを材料に、改善提案文をClaudeに書かせる。

    コスト上限到達時はAPIを呼ばず空文字を返す（統計のみのレポートになる）。
    """
    if cost.status().over_limit:
        return ""
    if client is None:
        import anthropic

        client = anthropic.Anthropic()

    material = {
        "統計": stats,
        "答えられなかった質問": stats.get("unanswered_questions", []),
        "利用者フィードバック（未対応）": [
            str(f.get("body", ""))[:200] for f in feedbacks if f.get("status") == "pending"
        ][:10],
        "未反映のマニュアル更新報告": [
            {"対象": f.get("manuals"), "報告": str(f.get("body", ""))[:100]}
            for f in pending_updates
            if f.get("status") == "pending"
        ][:10],
        "実在するハンドブックファイル名": handbook_names,
    }
    system = (
        "あなたは社内AIエージェント「らいずいぬ」の自己改善分析の担当です。"
        "与えられた1週間の利用実績から、管理者向けの改善提案を日本語で書いてください。"
        "構成: (1)答えられなかった質問の傾向と、どのマニュアルに何を追記すべきかの提案"
        "（追記先は実在するハンドブックファイル名から選ぶ。適切なものが無ければ新規ファイル名を提案）"
        "(2)フィードバックへの対応案 (3)その他の改善案（あれば）。"
        "提案は通し番号を付けて3〜6件に絞り、各提案は2行以内で具体的に。"
        "実行や変更の宣言はしない（すべて管理者の承認待ちの提案として書く）。"
        "データが少ない場合は無理に提案をひねり出さず、その旨を正直に書くこと。"
    )
    response = client.messages.create(
        model=config.model,
        max_tokens=4096,
        system=system,
        output_config={"effort": "low"},
        messages=[
            {"role": "user", "content": json.dumps(material, ensure_ascii=False)}
        ],
    )
    usage = {}
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = getattr(getattr(response, "usage", None), key, None)
        if value:
            usage[key] = int(value)
    cost.add_usage(usage)
    if getattr(response, "stop_reason", None) != "end_turn":
        return ""
    return next(
        (b.text for b in reversed(response.content) if getattr(b, "type", "") == "text"),
        "",
    ).strip()


def render_report(stats: dict, proposals: str, limit_note: str = "") -> str:
    lines = [
        "[info][title]らいずいぬ 週次自己分析レポート[/title]",
        f"対象期間: 直近{ANALYSIS_DAYS}日間",
        "",
        "■利用状況",
        f"・応答件数: {stats['total']}件"
        f"（回答 {stats['answered']}／記載なし {stats['unanswered']}／エラー {stats['errors']}）",
        f"・内訳: {stats['by_type']}",
        f"・API費用: 約{stats['cost_jpy']}円",
    ]
    if stats.get("unanswered_questions"):
        lines.append("")
        lines.append("■答えられなかった質問")
        for q in stats["unanswered_questions"]:
            lines.append(f"・{q}")
    if proposals:
        lines.append("")
        lines.append("■改善提案（要承認）")
        lines.append(proposals)
    if limit_note:
        lines.append("")
        lines.append(limit_note)
    lines.append("")
    lines.append(APPROVAL_FOOTER)
    lines.append("[/info]")
    return "\n".join(lines)


def main() -> None:
    config = Config.load()
    admin_room = config.admin_room_id
    if not admin_room:
        print("admin_room_id が未設定のためレポートを送信できません")
        sys.exit(1)

    state_dir = config.resolve_path(config.state_dir)
    log_dir = config.resolve_path(config.audit_log_dir)
    records = recent_audit_records(log_dir)
    stats = summarize(records)
    feedbacks = load_jsonl(state_dir / "feedback.jsonl")
    pending_updates = load_jsonl(state_dir / "pending_updates.jsonl")

    handbook = HandbookLoader(
        roots=[config.resolve_path(r) for r in config.handbook["roots"]],
        include=config.handbook["include"],
        exclude=config.handbook["exclude"],
    ).load()

    cost = CostTracker(
        state_dir=state_dir,
        limit_jpy=config.monthly_cost_limit_jpy,
        alert_threshold=config.cost_alert_threshold,
        usd_jpy_rate=config.usd_jpy_rate,
        pricing_usd_per_mtok=config.pricing_usd_per_mtok,
        prompt_cache_ttl=config.prompt_cache_ttl,
    )

    limit_note = ""
    proposals = build_proposals(
        stats, feedbacks, pending_updates, sorted(handbook.file_names), config, cost
    )
    if not proposals and cost.status().over_limit:
        limit_note = "（今月のAPI利用上限に達しているため、提案生成は省略し統計のみ掲載しています）"

    report = render_report(stats, proposals, limit_note)

    # レポートを記録として保存（反映作業時にClaude Codeが参照する）
    reports_dir = state_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"self-review-{datetime.now(JST).strftime('%Y%m%d')}.md"
    report_path.write_text(report, encoding="utf-8")

    chatwork = ChatworkClient(config.chatwork_api_token or "")
    chatwork.send_message(int(admin_room), report)
    print(f"レポート送信完了: {report_path}")


if __name__ == "__main__":
    main()
