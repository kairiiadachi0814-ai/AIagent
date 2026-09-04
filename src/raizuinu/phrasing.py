"""定型の受け答えに揺らぎを持たせる。

「承知しました。下書きを作成しました。」のように、コード側で書いている一言が
毎回まったく同じだと、機械が喋っている感じが強く出る。同じ意味の言い方を
いくつか持ち、前回と違うものを選ぶ。

揺らぎを持たせてよいのは**社交辞令の部分だけ**である。次のものは対象外:
- ハンドブックに基づく回答の本文・出典
- 税務会計の回答と、その免責文
- 金額・日付・件数・宛先などの値
- 契約書ひな形の案内、コスト上限の停止通知（決まった案内文）
- 総務など他部署へ送る依頼文（相手が読む文面は揃っていたほうがよい）

各言い回しは「同じことを言っている」必要がある。たとえば登録し終えた合図なら、
どれも完了として読めなければならない。呼び出し側が持っている判定
（未来形を弾く等）を、辞書のすべての候補が満たすことをテストで固定している。
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Callable

# --- 言い回しの辞書 ---
# 同じ意味の言い方を並べる。増やすときも「意味が変わらないこと」だけ守ればよい。

BANKS: dict[str, tuple[str, ...]] = {
    # 書類の下書きを作り終えた合図（依頼を受けた側の言い方。完了として読めること）
    "doc_done": (
        "承知しました。下書きを作成しました。",
        "はい、下書きができました。",
        "下書き、作成しました。",
        "こちら、下書きです。",
        "作成しましたので、ご確認ください。",
        "下書きが仕上がりました。",
    ),
    # 足りない項目を聞き返すときの入り（まだ何も作っていないと読めること）
    "doc_ask": (
        "書類の下書き、お作りしますね。",
        "下書き、お作りします。",
        "はい、お作りしますね。",
        "こちらで作りますね。",
        "承知しました、取りかかります。",
    ),
    # どの書類か決めきれないときの入り
    "doc_kind_ask": (
        "送付状ですね、お作りします。",
        "送付状ですね。すぐ取りかかります。",
        "はい、送付状ですね。",
        "承知しました、送付状ですね。",
    ),
    # 予定を登録し終えた合図（完了として読めること）
    "schedule_done": (
        "承知しました。次の予定で登録しました。",
        "はい、登録しました。",
        "こちらの内容で登録しました。",
        "登録しました。下記のとおりです。",
        "入れておきました。",
    ),
    # レターパックが不要と言われたとき
    "letterpack_declined": (
        "承知しました。レターパックの手配は行いません。",
        "了解です。レターパックの手配は見送りますね。",
        "はい、手配はなしにしておきます。",
    ),
    # 文面の確認で取りやめになったとき
    "letterpack_cancelled": (
        "承知しました。レターパックの依頼は送らずに取りやめます。",
        "了解です。依頼は送らずに止めておきます。",
        "はい、送らずに取りやめますね。",
    ),
    # 依頼者の答えを依頼先へ返したとき（送り先は会社によって変わる）
    "letterpack_forwarded": (
        "先方へお伝えしました。返信があればまたお知らせします。",
        "依頼先へ伝えておきました。返事が来たらお知らせしますね。",
        "お伝えしました。返信があり次第、こちらへ流します。",
    ),
    # FAXの発注書に「対応完了」と報告をもらったときの一言（お礼として読めること。
    # 「完了」「済」など、こちらの投稿が完了の報告と誤読される言葉は入れない）
    "fax_done_thanks": (
        "対応ありがとうございます。",
        "ご対応ありがとうございます。助かります。",
        "ありがとうございます。お疲れさまでした。",
        "承知しました。ありがとうございます。",
    ),
}


class Phrasebook:
    """言い回しを1つ選ぶ。前回と同じものは避ける。

    連続で同じ言い方になると、揺らぎを入れた意味がなくなるため、
    直前に使ったものを場面（ルーム等）ごとに覚えておく。
    """

    def __init__(
        self,
        path: Path | None = None,
        rng: Callable[[int], int] | None = None,
        enabled: bool = True,
    ) -> None:
        self._path = path
        self._rng = rng or (lambda n: random.randrange(n))
        self._enabled = enabled

    def pick(self, key: str, scope: str = "") -> str:
        """辞書 key から1つ選ぶ。未登録のキーは空文字を返さず例外にする。"""
        options = BANKS.get(key)
        if not options:
            raise KeyError(f"言い回しの辞書に {key} がありません")
        if not self._enabled or len(options) == 1:
            return options[0]
        recent = self._load()
        slot = f"{key}:{scope}"
        last = recent.get(slot)
        choices = [o for o in options if o != last] or list(options)
        chosen = choices[self._rng(len(choices))]
        recent[slot] = chosen
        self._save(recent)
        return chosen

    # --- 内部 ---

    def _load(self) -> dict[str, str]:
        if self._path is None:
            return dict(getattr(self, "_memory", {}))
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, recent: dict[str, str]) -> None:
        if self._path is None:
            self._memory = recent
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # 覚えるのは直前の1つだけ。際限なく育たないよう場面ごとに上書きする
            self._path.write_text(json.dumps(recent, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass  # 覚えられなくても会話は続けられる（同じ言い方が続くだけ）


def build(config: Any) -> Phrasebook:
    """設定からPhrasebookを作る。vary_openings を false にすると固定に戻る。"""
    settings = getattr(config, "phrasing", {}) or {}
    path = None
    try:
        path = config.resolve_path(config.state_dir) / "phrasing.json"
    except Exception:
        path = None
    return Phrasebook(path=path, enabled=bool(settings.get("vary_openings", True)))
