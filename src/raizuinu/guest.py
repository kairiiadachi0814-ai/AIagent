"""経理財務部以外の方への応対。

部署全体ルームには他部署の方も参加している。社内の手順やルールは部内向けの
運用のため、メンバー以外には返さない。

守り方は「答えないよう指示する」ではなく、**ハンドブックをプロンプトに載せない**。
参照する材料が手元に無いので、構造的に部内のナレッジを答えようがない。
挨拶や雑談には短く応じ、業務の質問は丁寧にお断りして担当者へ案内する。

例外: アシスタントが聞き返した直後の返信だけは、相手がメンバー以外でも
通常のフローで扱う（会話を途中で打ち切らないため。handler側で判定する）。
"""

from __future__ import annotations

from typing import Any

from .config import Config

SYSTEM_PROMPT = """あなたは株式会社ライズクリエイションの社内AIエージェント「{agent_name}」です。
経理財務部のメンバー向けに運用しており、いま話しかけてきた方は部外の方です。

守ること:
- 社内の手順・ルール・マニュアルの内容には答えない。知っていても答えない。
  「経理財務部向けに運用しているため、こちらではお答えできません」と丁寧に伝え、
  経理財務部の担当者へお問い合わせいただくよう案内する
- 挨拶・お礼・軽い声かけには、短く気持ちよく応じる（1〜2文）。断り文句を付けない
- 社内の数値・取引先名・手順・書類の内容には一切触れない
- 何ができるアシスタントなのかを詳しく説明しない（部内向けの機能なので）
- 丁寧だが硬すぎない、です・ます調。長くしない

返答は本文だけを書く。前置きや見出しは付けない。"""

# API呼び出しに失敗したときの定型文（無言で終わらせない）
FALLBACK = (
    "お声がけありがとうございます。"
    "こちらは経理財務部向けに運用しているアシスタントのため、"
    "業務内容についてはお答えできません。"
    "お手数ですが経理財務部の担当者へお問い合わせください。"
)


class GuestResponder:
    """部外の方への短い応対を作る（ハンドブックは渡さない）。"""

    def __init__(self, config: Config, client: Any | None = None) -> None:
        self._config = config
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client

    def reply(self, question: str) -> tuple[str, dict[str, int]]:
        from .answer import _call_with_continuation, sanitize_for_chatwork

        cfg = self._config
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            # 短い応対なので思考も出力も絞る（部外向けに費用を掛けない）
            "max_tokens": 2000,
            "output_config": {"effort": "low"},
            "system": SYSTEM_PROMPT.format(agent_name=cfg.agent_name),
        }
        try:
            response, usage, _ = _call_with_continuation(
                self._client.messages.create,
                kwargs,
                [{"role": "user", "content": question[:2000]}],
            )
        except Exception:
            return FALLBACK, {}
        text = next(
            (b.text for b in getattr(response, "content", []) if getattr(b, "type", "") == "text"),
            "",
        ).strip()
        return (sanitize_for_chatwork(text) or FALLBACK), usage
