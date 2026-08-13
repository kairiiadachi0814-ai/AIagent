"""ローカル動作確認用CLI。Chatworkを介さずハンドブック回答を試す。

使い方（ANTHROPIC_API_KEYの設定が必要）:
    python run_local.py "銀行明細の取得手順は？"
    python run_local.py            # 対話モード
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from raizuinu.config import Config  # noqa: E402
from raizuinu.handler import RaizuinuHandler  # noqa: E402


class _NoopChatwork:
    def send_message(self, room_id, body):
        return ""

    def get_recent_messages(self, room_id, limit=20):
        return []


def main() -> None:
    config = Config.load()
    if not config.anthropic_api_key:
        print("ANTHROPIC_API_KEY を設定してください")
        sys.exit(1)
    handler = RaizuinuHandler(config, chatwork=_NoopChatwork())

    if len(sys.argv) > 1:
        questions = [" ".join(sys.argv[1:])]
    else:
        print("質問を入力してください（空行で終了）")
        questions = iter(lambda: input("> ").strip(), "")

    try:
        for question in questions:
            text, meta = handler.answer_question(question)
            print("\n--- 回答 ---")
            print(text)
            print(f"\n[usage] {meta['usage']}  [今月累計] {meta['monthly_total_jpy']}円\n")
    except (EOFError, KeyboardInterrupt):
        print("\n終了します")


if __name__ == "__main__":
    main()
