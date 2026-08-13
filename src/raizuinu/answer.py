"""Claude APIによる回答生成と出典検証。

ガードレール（要件定義書7章）の担保方法:
1. 出典をごまかさない       → 構造化出力で出典を必須化し、実在ファイルと突合。
                              実在しない出典は破棄し、有効な出典が残らない回答は
                              「記載なし」扱いに落とす（FR-03）。
2. 書かれていないことは不明と答える → has_answer=false の構造化フィールドで明示（FR-04）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .handbook import Handbook

# 出典付き回答を強制するJSONスキーマ（structured outputs）
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["question", "manual_update_report", "general_knowledge"],
            "description": "メッセージ種別。社内手順・ルールの質問=question／マニュアル更新の報告=manual_update_report／社内ルールではない一般的な会計・簿記・税務知識の質問=general_knowledge",
        },
        "reference_url": {
            "type": "string",
            "description": "general_knowledgeの場合のみ: 会計基礎知識リンク集に記載の該当記事URLを一字一句正確に転記。該当がなければ空文字。他のintentでは空文字",
        },
        "reported_manuals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "更新報告の場合、報告対象のマニュアル名（分かる範囲で）。質問の場合は空配列",
        },
        "has_answer": {
            "type": "boolean",
            "description": "ハンドブックに根拠がある回答ができたか",
        },
        "answer": {"type": "string", "description": "回答本文（Chatworkにそのまま掲載）"},
        "sources": {
            "type": "array",
            "description": "参照したハンドブックのファイル名と見出し。has_answer=trueなら必須",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "ファイル名（例: 銀行明細取得.md）"},
                    "heading": {"type": "string", "description": "参照した見出し。無ければ空文字"},
                    "url": {
                        "type": "string",
                        "description": "そのマニュアルの原本URL。マニュアルリンク集に記載がある場合のみ一字一句正確に転記。無ければ空文字",
                    },
                },
                "required": ["file", "heading", "url"],
                "additionalProperties": False,
            },
        },
        "suggested_file": {
            "type": "string",
            "description": "記載が無い場合に追記先として提案するファイル名。無ければ空文字",
        },
    },
    "required": ["intent", "reference_url", "reported_manuals", "has_answer", "answer", "sources", "suggested_file"],
    "additionalProperties": False,
}

SYSTEM_INSTRUCTIONS = """あなたは株式会社ライズクリエイションの社内AIエージェント「{agent_name}」です。
Chatworkで社員からの質問に、社内ハンドブック（経理・財務の業務手順書群）だけを根拠に日本語で回答します。

守るべき原則:
1. 回答は必ずハンドブックの記載に基づくこと。参照したファイル名と見出しをsourcesに正確に挙げること。実在しないファイル名を出典にしてはならない。
2. ハンドブックに書かれていないことは推測で埋めず、has_answer=falseとし、answerには「ハンドブックに記載がありません」という趣旨を書くこと。あわせて、どのファイルに追記すべきかをsuggested_fileで提案してよい。
3. 回答本文は簡潔に。手順を問われたら該当手順の要点を回答本文に直接書くこと。
4. 回答本文に「〜.md」等の内部ファイル名を書いてはならない。利用者はmdファイルを閲覧できない（管理者専用）。詳細への誘導は、マニュアルリンク集に記載の原本URL（GoogleドキュメントやシートのURL）で行い、URLが無い場合は本文の要約で完結させること。
5. パスワード等の認証情報がハンドブックに残っている場合でも、回答本文に転記しないこと。
6. 会話履歴は文脈理解の参考とし、回答の根拠にはしないこと。
7. 関連する原本文書・スプレッドシート・フォルダのURLがハンドブック（特に「マニュアルリンク集」）に記載されている場合は、回答本文またはsourcesのurlに含めて案内すること。URLは一字一句正確に転記し、加工・短縮・推測してはならない。ハンドブックに記載のないURLを作らないこと。
8. メッセージ種別の判定: 利用者が質問ではなく「マニュアル（原本）を更新した・変更した」と明確に報告している場合のみ、intentをmanual_update_reportとし、reported_manualsに対象マニュアル名を入れること。その場合answerには報告内容の短い受領確認文（1〜2文。対象マニュアル名を復唱）を書き、has_answerはfalse、sourcesは空とする。更新の仕方に関する質問や迷うケースはquestionとして扱うこと。
9. 一般的な会計・簿記・税務知識の質問（社内の手順・ルールではないもの）への対応: intentをgeneral_knowledgeとし、「会計基礎知識リンク集」に該当する解説記事があればreference_urlにそのURLを一字一句正確に転記すること。answerには「社内ハンドブックには記載がありません。参考記事: <URL>」の趣旨の案内文（税理士確認の注意書き付き）を書く。詳細な要約はシステム側が記事を取得して別途行うため、自身の知識だけで数値や限度額を断定して書いてはならない。該当記事がリンク集にない場合はreference_urlを空にし「記載なし」の回答とする。"""


@dataclass
class Answer:
    has_answer: bool
    text: str
    sources: list[dict[str, str]] = field(default_factory=list)
    suggested_file: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    refused: bool = False
    intent: str = "question"
    reported_manuals: list[str] = field(default_factory=list)
    reference_url: str = ""


class AnswerGenerator:
    """Claude API呼び出しと出典検証。"""

    def __init__(self, config: Config, client: Any | None = None) -> None:
        self._config = config
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client

    def generate(self, question: str, handbook: Handbook, context: str = "") -> Answer:
        response, usage = self._create_message(question, handbook, context)

        if getattr(response, "stop_reason", None) == "refusal":
            return Answer(
                has_answer=False,
                text="この質問には回答できませんでした（安全上の理由）。表現を変えてもう一度お試しください。",
                usage=usage,
                refused=True,
            )

        if getattr(response, "stop_reason", None) == "max_tokens":
            # 思考+本文が上限に達し出力が不完全（JSONパース失敗と切り分けるため先に検出）
            return Answer(
                has_answer=False,
                text="回答の生成が長さ上限に達しました。質問を分けてもう一度お試しください。",
                usage=usage,
            )

        # ツール使用時は途中に説明テキストが挟まるため、最後のtextブロックを回答とする
        text = next(
            (b.text for b in reversed(response.content) if getattr(b, "type", "") == "text"),
            "",
        )
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return Answer(
                has_answer=False,
                text="回答の生成に失敗しました。もう一度お試しください。",
                usage=usage,
            )

        intent = str(data.get("intent", "question"))
        if intent not in ("question", "manual_update_report", "general_knowledge"):
            intent = "question"
        answer = Answer(
            has_answer=bool(data.get("has_answer")),
            text=str(data.get("answer", "")).strip(),
            sources=list(data.get("sources") or []),
            suggested_file=str(data.get("suggested_file", "")),
            usage=usage,
            intent=intent,
            reported_manuals=[str(m) for m in (data.get("reported_manuals") or []) if str(m).strip()],
            reference_url=str(data.get("reference_url", "")).strip(),
        )
        answer = self._validate_citations(answer, handbook)
        return self._enrich_general_knowledge(answer, question, handbook)

    # --- 内部 ---

    def _create_message(self, question: str, handbook: Handbook, context: str) -> "tuple[Any, dict[str, int]]":
        """API呼び出し。サーバーツール使用時のpause_turn継続まで面倒を見る。

        戻り値は (最終レスポンス, 全リクエスト累積のusage)。
        """
        cfg = self._config
        system = [
            {
                "type": "text",
                "text": SYSTEM_INSTRUCTIONS.format(agent_name=cfg.agent_name),
            },
            {
                "type": "text",
                "text": "# ハンドブック\n\n" + handbook.render(),
                # ハンドブックは安定プレフィックスとしてキャッシュする（コスト最適化）
                "cache_control": {"type": "ephemeral", "ttl": cfg.prompt_cache_ttl},
            },
        ]
        user_content = question
        if context:
            user_content = (
                "## 直近の会話（文脈参考用）\n" + context + "\n\n## 質問\n" + question
            )

        messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "system": system,
            "output_config": {
                "effort": cfg.effort,
                "format": {"type": "json_schema", "schema": ANSWER_SCHEMA},
            },
        }
        if cfg.fallback_enabled:
            # 安全クラシファイアによる拒否時に別モデルで再実行される（Opus 5/Fable 5系）
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
            create = self._client.beta.messages.create
        else:
            create = self._client.messages.create
        return _call_with_continuation(create, kwargs, messages)

    def _enrich_general_knowledge(
        self, answer: Answer, question: str, handbook: Handbook
    ) -> Answer:
        """一般会計知識の質問: 検証済み参考記事URLをweb_fetchで取得し要点回答に差し替える。

        取得に失敗した場合は1段目のURL案内文のまま返す（安全側）。
        """
        cfg = self._config
        url = answer.reference_url
        if (
            answer.intent != "general_knowledge"
            or not cfg.web_fetch_enabled
            or not cfg.web_fetch_allowed_domains
            or not url
            or not _url_in_handbook(url, handbook)
        ):
            return answer
        try:
            text, usage2 = self._summarize_article(question, url)
        except Exception:
            return answer  # 要約失敗時は案内文のまま（呼び出し元で失敗扱いにしない）
        _accumulate_usage(answer.usage, usage2)
        if not text:
            return answer
        source_file = _find_url_file(url, handbook)
        answer.text = _scrub_body_urls(text, handbook)
        answer.has_answer = True
        answer.sources = [
            {"file": source_file or "", "heading": "", "url": url}
        ] if source_file else answer.sources
        return answer

    def _summarize_article(self, question: str, url: str) -> "tuple[str, dict[str, int]]":
        """記事をweb_fetchで取得し、質問への要点回答を生成する（2段目・軽量呼び出し）。"""
        cfg = self._config
        system = (
            "あなたは社内アシスタント。ユーザーの質問と参考記事URLが与えられる。"
            "web_fetchツールで記事を取得し、記事の内容に基づいて質問への答え"
            "（該当する数値・条件・区分など）を日本語で2〜6行に簡潔にまとめること。"
            "記事の長い引用・転載はしない。記事に書かれていないことは書かない。"
            "回答の最後に必ず「※社外の一般解説（freee会計）に基づく情報です。"
            "当社での具体的な取り扱い・税務判断は税理士にご確認ください。」を付けること。"
            "記事を取得できなかった場合は FETCH_FAILED とだけ出力すること。"
        )
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": 2048,
            "system": system,
            "output_config": {"effort": "low"},
            "tools": [
                {
                    "type": "web_fetch_20260209",
                    "name": "web_fetch",
                    "max_uses": cfg.web_fetch_max_uses,
                    "allowed_domains": cfg.web_fetch_allowed_domains,
                    # 記事ページのナビ等による入力トークン肥大を抑える
                    "max_content_tokens": cfg.web_fetch_max_content_tokens,
                }
            ],
        }
        messages = [{"role": "user", "content": f"質問: {question}\n参考記事URL: {url}"}]
        response, usage = _call_with_continuation(
            self._client.messages.create, kwargs, messages
        )
        text = next(
            (b.text for b in reversed(response.content) if getattr(b, "type", "") == "text"),
            "",
        ).strip()
        if not text or "FETCH_FAILED" in text:
            return "", usage
        return text, usage

    @staticmethod
    def _validate_citations(answer: Answer, handbook: Handbook) -> Answer:
        """出典を実在ファイル・実在見出し・実在URLと突合する（出典の捏造防止・FR-03）。"""
        headings_by_file = {f.name: set(f.headings) for f in handbook.files}
        valid: list[dict[str, str]] = []
        for src in answer.sources:
            normalized = handbook.normalize_citation(str(src.get("file", "")))
            if not normalized:
                continue
            heading = str(src.get("heading", "")).strip()
            if heading and heading not in headings_by_file.get(normalized, set()):
                heading = ""  # 実在しない見出しは出典に載せない
            url = str(src.get("url", "")).strip()
            if url and not _url_in_handbook(url, handbook):
                url = ""  # ハンドブックに記載のないURLは出典に載せない（捏造防止）
            valid.append({"file": normalized, "heading": heading, "url": url})
        answer.sources = valid
        answer.text = _scrub_body_urls(answer.text, handbook)
        if answer.has_answer and not valid:
            # 出典なしの回答は返さない（ガードレール1）
            answer.has_answer = False
            answer.text = (
                "ハンドブック内に確かな根拠を特定できませんでした。"
                "担当者に直接ご確認いただくか、質問を具体的にしてもう一度お試しください。"
            )
        return answer


def _accumulate_usage(total: dict[str, int], usage: dict[str, int]) -> None:
    for key, value in usage.items():
        total[key] = total.get(key, 0) + value


def _call_with_continuation(
    create: Any, kwargs: dict[str, Any], messages: list[dict[str, Any]]
) -> "tuple[Any, dict[str, int]]":
    """API呼び出し。サーバーツールの反復上限（pause_turn）を最大3回まで自動継続する。"""
    usage_total: dict[str, int] = {}
    response = None
    for _ in range(4):
        response = create(messages=messages, **kwargs)
        _accumulate_usage(usage_total, _usage_dict(response))
        if getattr(response, "stop_reason", None) != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": response.content}]
    return response, usage_total


def _find_url_file(url: str, handbook: Handbook) -> str | None:
    """URLが記載されているハンドブックファイル名を返す（出典表示用）。"""
    candidates = {url, url.rstrip("/")}
    if not url.endswith("/"):
        candidates.add(url + "/")
    for f in handbook.files:
        if any(c in f.content for c in candidates):
            return f.name
    return None


def _usage_dict(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    result = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        value = getattr(usage, key, None)
        if value is not None:
            result[key] = int(value)
    return result


_URL_RE = re.compile(r"https?://[^\s<>\"」』】）\)。、！？]+")


def _url_in_handbook(url: str, handbook: Handbook) -> bool:
    candidates = {url, url.rstrip("/")}
    if not url.endswith("/"):
        candidates.add(url + "/")
    return any(any(c in f.content for c in candidates) for f in handbook.files)


def _scrub_body_urls(text: str, handbook: Handbook) -> str:
    """本文中のURLのうち、ハンドブックに記載のないものを除去する（捏造リンク対策）。"""

    def repl(match: re.Match) -> str:
        url = match.group(0)
        return url if _url_in_handbook(url, handbook) else "（URL省略）"

    return _URL_RE.sub(repl, text)


def display_name(file_name: str) -> str:
    """出典表示用のマニュアル名（内部ファイルの拡張子は見せない）。"""
    return file_name[:-3] if file_name.endswith(".md") else file_name


def sanitize_for_chatwork(text: str) -> str:
    """モデル由来文字列のChatworkタグを無害化する（[toall]等の注入防止）。

    角括弧を全角に置換してタグとして解釈されないようにする。
    """
    return text.replace("[", "［").replace("]", "］")


def format_reply(answer: Answer, event_account_id: int, room_id: int, message_id: str) -> str:
    """Chatworkへの返信本文を組み立てる（返信タグ＋本文＋出典）。

    返信タグは自前生成の値のみで構成し、モデル由来の文字列はすべて
    sanitize_for_chatwork を通す。
    """
    lines = [
        f"[rp aid={int(event_account_id)} to={int(room_id)}-{message_id}]",
        sanitize_for_chatwork(answer.text),
    ]
    if answer.has_answer and answer.sources:
        lines.append("")
        lines.append("【出典】")
        seen = set()
        for src in answer.sources:
            label = display_name(src["file"])
            if src.get("heading"):
                label += f"（{src['heading']}）"
            label = sanitize_for_chatwork(label)
            if src.get("url"):
                label += f" {src['url']}"  # URLは検証済みのため加工しない
            if label not in seen:
                seen.add(label)
                lines.append(f"・{label}")
    elif not answer.has_answer and answer.suggested_file:
        lines.append("")
        lines.append(
            "※追記候補マニュアル: "
            + sanitize_for_chatwork(display_name(answer.suggested_file))
        )
    return "\n".join(lines)
