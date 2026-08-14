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
            "enum": [
                "question",
                "manual_update_report",
                "general_knowledge",
                "legal_knowledge",
                "answer_feedback",
            ],
            "description": "メッセージ種別。社内手順・ルールの質問=question／マニュアル更新の報告=manual_update_report／一般的な会計・簿記・税務知識の質問=general_knowledge／法令・法律の知識に関する質問=legal_knowledge／過去の回答への誤り指摘・改善要望=answer_feedback",
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

# e-Gov法令検索を叩くカスタムツール（実行はサーバー側 _execute_tool）
LEGAL_TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_law",
        "description": (
            "日本の法令（法律・政令・省令）をe-Gov法令検索で条文キーワード検索する。"
            "法令・条文の特定に必ず使うこと。結果にはlaw_id・法令名・該当箇所の抜粋・"
            "e-GovのURLが含まれる。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "検索キーワード（例: 下請代金 支払期日）",
                }
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "get_law_article",
        "description": (
            "law_idと条番号を指定して条文の原文を取得する（e-Gov法令検索）。"
            "回答は必ずこの原文に基づくこと。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "law_id": {"type": "string", "description": "search_lawで得たlaw_id"},
                "article": {
                    "type": "string",
                    "description": "条番号（例: '522'、'24条の5'。省略時は法令冒頭）",
                },
            },
            "required": ["law_id"],
        },
    },
]

SYSTEM_INSTRUCTIONS = """あなたは株式会社ライズクリエイションの社内AIエージェント「{agent_name}」です。
Chatworkで社員からの質問に、社内ハンドブック（経理・財務の業務手順書群）だけを根拠に日本語で回答します。

話し方（すべての回答に適用）:
- 社内の気さくで頼れる経理の先輩が、隣の席からさらっと教えてくれるような話し言葉にすること。です・ます調の丁寧さは保ちつつ、かしこまりすぎない。「〜ですよ」「〜なんです」「ちなみに」「ざっくり言うと」のような柔らかい言い回しは自然に使ってよい（敬語が崩れるくだけ方や馴れ馴れしい呼びかけはしない）。
- 利用者の質問に答えるときは、質問を受け止める短い一言から入ってよい（例:「〜の件ですね。」「はい、ありますよ。」「それ、ちょうど手順がありますよ。」）。例文は雰囲気の見本であり、そのまま使い回さず質問の内容に合わせて毎回言い方を変えること。「あ、」のような感動詞で始める書き出しを多用しない。短い質問には前置きなしで結論から入ってよい。いずれの場合も結論→必要な補足の順で話す。
- 質問への回答以外の依頼（依頼文の側で書き出しや文体が指定されている場合。例: メンバー同士の議論への横からの助言）では、その指定を優先し、受け止めの一言やくだけた言い回しは使わず、落ち着いた控えめな丁寧さで書くこと。
- 「以下のとおりです」「上記をご確認ください」のような機械的な定型表現の連発や、過剰な箇条書きを避ける。短い説明は文章の流れで伝え、手順が3ステップ以上あるときや選択肢の列挙が必要なときだけ箇条書きを使う。
- 相手を急かさない・突き放さない。分からないことは正直に、次にどうすればよいかを添えて案内する。
- 長さは質問に見合う分だけ。聞かれていないことまで説明しない。
- 絵文字・顔文字は使わない。
- ※で始まる末尾の免責文は、原則9・10（一般会計知識・法令知識）で指定された場合のみ付けること。社内手順の回答（intent=question）には免責文を付けず、自然な会話として完結させる。

分かりやすさ（すべての回答に適用）:
- 経理財務の経験が浅い人が読んでも分かるように説明すること。専門用語（勘定科目・税務用語・帳票名・社内システム名など）は、初出時に括弧や続く一言で意味を添える（例:「買掛金（仕入れ代金の未払い分）」）。ただし質問者や議論中のメンバーが自分の発言ですでに使っている用語には説明を付けない。
- 勘定科目名・法令名・条番号・帳票名・社内システム名などの正式名称は、日常の言葉に置き換えず必ずそのまま書くこと。言い換えや例えは、正式名称に添える説明としてのみ使う。
- 手順だけを並べず、根拠（ハンドブック・条文・記事）に理由や目的が書かれている場合は「なぜそうするのか」を一言添える。根拠に書かれていない理由を自分の知識で補って書いてはならない。
- 具体的な場面や数字の例が助けになるときは短く添える。ただし数字（期限・金額・税率・条番号など）は根拠に記載のある値に限り、丸めたり概数にしたりせず正確に書くこと。自分の知識で金額・税率・限度額の例を作らない（「ざっくり」してよいのは説明の順序や全体像であり、数値そのものではない）。
- 分かりやすさのために内容を変えてはならない。根拠にある事実の範囲で、言い方だけを易しくすること。

守るべき原則:
1. 回答は必ずハンドブックの記載に基づくこと。参照したファイル名と見出しをsourcesに正確に挙げること。実在しないファイル名を出典にしてはならない。
2. ハンドブックに書かれていないことは推測で埋めず、has_answer=falseとし、answerには「ハンドブックに記載がありません」という趣旨を書くこと。あわせて、どのファイルに追記すべきかをsuggested_fileで提案してよい。
3. 回答本文は簡潔に。手順を問われたら該当手順の要点を回答本文に直接書くこと。
4. 回答本文に「〜.md」等の内部ファイル名を書いてはならない。利用者はmdファイルを閲覧できない（管理者専用）。詳細への誘導は、マニュアルリンク集に記載の原本URL（GoogleドキュメントやシートのURL）で行い、URLが無い場合は本文の要約で完結させること。
5. パスワード等の認証情報がハンドブックに残っている場合でも、回答本文に転記しないこと。
6. 会話履歴は文脈理解の参考とし、回答の根拠にはしないこと。
7. 関連する原本文書・スプレッドシート・フォルダのURLがハンドブック（特に「マニュアルリンク集」）に記載されている場合は、回答本文またはsourcesのurlに含めて案内すること。URLは一字一句正確に転記し、加工・短縮・推測してはならない。ハンドブックに記載のないURLを作らないこと。
8. メッセージ種別の判定: 利用者が質問ではなく「マニュアル（原本）を更新した・変更した」と明確に報告している場合のみ、intentをmanual_update_reportとし、reported_manualsに対象マニュアル名を入れること。その場合answerには報告内容の短い受領確認文（1〜2文。対象マニュアル名を復唱）を書き、has_answerはfalse、sourcesは空とする。更新の仕方に関する質問や迷うケースはquestionとして扱うこと。
9. 一般的な会計・簿記・税務知識の質問（社内の手順・ルールではないもの）への対応: intentをgeneral_knowledgeとし、「会計基礎知識リンク集」に該当する解説記事があればreference_urlにそのURLを一字一句正確に転記すること。answerには「社内ハンドブックには記載がありません。参考記事: <URL>」の趣旨の案内文（税理士確認の注意書き付き）を書く。詳細な要約はシステム側が記事を取得して別途行うため、自身の知識だけで数値や限度額を断定して書いてはならない。該当記事がリンク集にない場合はreference_urlを空にし「記載なし」の回答とする。
10. 法令・法律の知識に関する質問（社内手順ではないもの）: intentをlegal_knowledgeとし、必ずsearch_lawツールで法令を特定し、get_law_articleツールで条文の原文を取得したうえで、その原文に基づいて回答すること。ツールを使わず記憶だけで条文の内容や条番号を断定してはならない。回答には法令名・条番号を明記し、ツール結果に含まれるURL（laws.e-gov.go.jp）をそのまま含める。回答の最後に必ず「※e-Gov法令検索の条文に基づく一般的な情報です。法的判断や契約書の内容確認は、顧問弁護士への相談またはLegalForceでのリーガルチェックを経てください。」を付ける。この場合sourcesは空でよい（出典は本文中の法令名・条番号・URLで示す）。has_answerは条文を取得できた場合にtrue。**ツール呼び出しは効率よく行うこと**: 独立した検索・条文取得は1ターンに複数まとめて並列に呼んでよい。網羅を目指さず、質問に最も関係する法令1〜2件・条文2〜4件に絞り、取得できた範囲で回答をまとめること（ツール往復は合計5回以内を目安）。
11. 契約書の作成・ひな形に関する相談: 「ひな形リンク集」から該当するひな形のURLを案内し、あわせて「作成した契約書は締結前に必ずLegalForceでリーガルチェックを行う」こと（手順はハンドブック「LegalForce_リーガルチェック」参照）を必ず案内する。契約条項の妥当性を自ら断定してはならない。
12. 過去の回答への誤り指摘・不満・改善要望（フィードバック）: intentをanswer_feedbackとし、answerには丁寧なお詫びと受領の一言を書くこと（指摘内容を短く復唱し、改善リストに追加して管理者が確認する旨を伝える）。反論や弁明はせず、has_answerはfalse、sourcesは空とする。"""


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

    def __init__(
        self, config: Config, client: Any | None = None, egov: Any | None = None
    ) -> None:
        self._config = config
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client
        if egov is None and config.legal_enabled:
            from .egov import EgovClient

            egov = EgovClient()
        self._egov = egov

    def generate(self, question: str, handbook: Handbook, context: str = "") -> Answer:
        response, usage, tool_used = self._create_message(question, handbook, context)

        if getattr(response, "stop_reason", None) == "refusal":
            return Answer(
                has_answer=False,
                text=(
                    "申し訳ありません、このご質問には安全上の理由でお答えできませんでした。"
                    "表現を変えて、もう一度お試しいただけますか。"
                ),
                usage=usage,
                refused=True,
            )

        if getattr(response, "stop_reason", None) == "max_tokens":
            # 思考+本文が上限に達し出力が不完全（JSONパース失敗と切り分けるため先に検出）
            return Answer(
                has_answer=False,
                text=(
                    "すみません、回答が長さの上限に達して途中で切れてしまいました。"
                    "質問をいくつかに分けて、もう一度お試しいただけますか。"
                ),
                usage=usage,
            )

        if getattr(response, "stop_reason", None) in ("tool_use", "pause_turn"):
            # ツール反復の上限に到達。中間出力を回答として扱わない
            return Answer(
                has_answer=False,
                text=(
                    "すみません、確認作業の往復が上限に達してしまい、回答をまとめきれませんでした。"
                    "質問をもう少し絞って、もう一度お試しいただけますか。"
                ),
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
                text=(
                    "申し訳ありません、回答をうまくまとめられませんでした。"
                    "もう一度お試しいただけますか。"
                ),
                usage=usage,
            )

        intent = str(data.get("intent", "question"))
        if intent not in (
            "question",
            "manual_update_report",
            "general_knowledge",
            "legal_knowledge",
            "answer_feedback",
        ):
            intent = "question"
        if intent == "legal_knowledge" and not (self._config.legal_enabled and tool_used):
            # 実際にe-Govツールを使っていない「法令回答」は根拠がないため
            # 通常質問扱いに降格し、出典検証（FR-03）を必ず通す
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

    def _create_message(
        self, question: str, handbook: Handbook, context: str
    ) -> "tuple[Any, dict[str, int], bool]":
        """API呼び出し。ツール継続まで面倒を見る。

        戻り値は (最終レスポンス, 全リクエスト累積のusage, カスタムツール実行有無)。
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
        if cfg.legal_enabled and self._egov is not None:
            kwargs["tools"] = LEGAL_TOOLS
        if cfg.fallback_enabled:
            # 安全クラシファイアによる拒否時に別モデルで再実行される（Opus 5/Fable 5系）
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
            create = self._client.beta.messages.create
        else:
            create = self._client.messages.create
        return _call_with_continuation(
            create, kwargs, messages, tool_executor=self._execute_tool
        )

    def _execute_tool(self, name: str, tool_input: dict) -> str:
        """カスタムツール（e-Gov法令検索）を実行し、結果をJSON文字列で返す。"""
        from .egov import EgovError

        print(f"[tool] {name} {json.dumps(tool_input, ensure_ascii=False)[:200]}", flush=True)
        try:
            if name == "search_law" and self._egov is not None:
                keyword = str(tool_input.get("keyword", "")).strip()
                results = self._egov.search(keyword)
                if not results:
                    results = self._egov.search_by_title(keyword)
                if not results:
                    return "該当する法令が見つかりませんでした。キーワードを変えて再検索してください。"
                return json.dumps(results, ensure_ascii=False)
            if name == "get_law_article" and self._egov is not None:
                result = self._egov.get_article(
                    str(tool_input.get("law_id", "")),
                    str(tool_input.get("article") or "") or None,
                )
                return json.dumps(result, ensure_ascii=False)
            return f"エラー: 未知のツール {name}"
        except EgovError as exc:
            return f"エラー: {exc}"
        except Exception as exc:  # ツール失敗で回答全体を落とさない
            return f"エラー: 条文の取得に失敗しました（{exc}）"

    def _enrich_general_knowledge(
        self, answer: Answer, question: str, handbook: Handbook
    ) -> Answer:
        """一般会計知識の質問: 検証済み参考記事URLをweb_fetchで取得し要点回答に差し替える。

        取得に失敗した場合は1段目のURL案内文のまま返す（安全側）。
        """
        from urllib.parse import urlparse

        cfg = self._config
        url = answer.reference_url
        allowed_hosts = {d.lower() for d in cfg.web_fetch_allowed_domains}
        host = (urlparse(url).hostname or "").lower() if url else ""
        if (
            answer.intent != "general_knowledge"
            or not cfg.web_fetch_enabled
            or not allowed_hosts
            or not url
            or host not in allowed_hosts  # 取得できないドメインへの無駄な2段目を防ぐ
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
            "文体は、気さくで頼れる経理の先輩が教えてくれるようなです・ます調の"
            "話し言葉にする（かしこまりすぎない）。経理財務の経験が浅い人にも"
            "分かるように、専門用語には初出時にひと言の意味を添えてよい。"
            "ただし記事に書かれた事実の範囲で言い方だけを易しくし、"
            "数値や正式な用語は記事のとおり正確に書くこと。"
            "記事の長い引用・転載はしない。記事に書かれていないことは書かない。"
            "回答の最後に必ず「※社外の一般解説（freee会計）に基づく情報です。"
            "当社での具体的な取り扱い・税務判断は税理士にご確認ください。」を付けること。"
            "記事を取得できなかった場合は FETCH_FAILED とだけ出力すること。"
        )
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": 8192,  # 思考+本文の合計上限。小さいと途中切れする
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
        response, usage, _ = _call_with_continuation(
            self._client.messages.create, kwargs, messages
        )
        if getattr(response, "stop_reason", None) != "end_turn":
            # max_tokens（途中切れ）・refusal等の要約は採用しない（免責文欠落の防止）
            return "", usage
        text = next(
            (b.text for b in reversed(response.content) if getattr(b, "type", "") == "text"),
            "",
        ).strip()
        if not text or "FETCH_FAILED" in text:
            return "", usage
        return text, usage

    def _validate_citations(self, answer: Answer, handbook: Handbook) -> Answer:
        """出典を実在ファイル・実在見出し・実在URLと突合する（出典の捏造防止・FR-03）。"""
        allowlist = list(self._config.body_url_allowlist or [])
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
        answer.text = _scrub_body_urls(answer.text, handbook, allowlist)
        if answer.has_answer and not valid and answer.intent != "legal_knowledge":
            # 出典なしの回答は返さない（ガードレール1）。
            # legal_knowledgeはe-Govツール実行済みの場合のみ（generateで担保）例外とし、
            # 出典は本文中の法令名・条番号・URLで示す
            answer.has_answer = False
            keep_guidance = (
                answer.intent == "general_knowledge"
                and answer.reference_url
                and _url_in_handbook(answer.reference_url, handbook)
            )
            if not keep_guidance:  # 参考URL案内文は破壊しない（2段目失敗時の受け皿）
                answer.text = (
                    "すみません、ハンドブックの中に確かな根拠を見つけられませんでした。"
                    "お手数ですが担当の方にご確認いただくか、質問をもう少し具体的にして"
                    "もう一度聞いていただけますか。"
                )
        return answer


def _accumulate_usage(total: dict[str, int], usage: dict[str, int]) -> None:
    for key, value in usage.items():
        total[key] = total.get(key, 0) + value


_MAX_LOOP_STEPS = 10


def _call_with_continuation(
    create: Any,
    kwargs: dict[str, Any],
    messages: list[dict[str, Any]],
    tool_executor: Any | None = None,
) -> "tuple[Any, dict[str, int], bool]":
    """API呼び出しループ。戻り値は (最終レスポンス, 累積usage, ツール実行有無)。

    - pause_turn（サーバーツールの反復上限）: そのまま再送して継続
    - tool_use（カスタムツール）: tool_executorで実行し結果を返して継続
    - 最終回では実行・継続せずに返す（結果を送れないツール実行をしない）
    """
    usage_total: dict[str, int] = {}
    response = None
    tool_used = False
    for attempt in range(_MAX_LOOP_STEPS):
        response = create(messages=messages, **kwargs)
        _accumulate_usage(usage_total, _usage_dict(response))
        stop_reason = getattr(response, "stop_reason", None)
        is_last = attempt == _MAX_LOOP_STEPS - 1
        if stop_reason == "pause_turn" and not is_last:
            messages = messages + [{"role": "assistant", "content": response.content}]
            continue
        if stop_reason == "tool_use" and tool_executor is not None and not is_last:
            tool_results = []
            for block in response.content:
                if getattr(block, "type", "") == "tool_use":
                    result = tool_executor(block.name, dict(block.input or {}))
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )
            if not tool_results:
                break
            tool_used = True
            messages = messages + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_results},
            ]
            continue
        break
    return response, usage_total, tool_used


def _find_url_file(url: str, handbook: Handbook) -> str | None:
    """URLが記載されているハンドブックファイル名を返す（出典表示用）。"""
    return _handbook_url_map(handbook).get(url.rstrip("/"))


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


def _handbook_url_map(handbook: Handbook) -> dict[str, str]:
    """ハンドブック本文から抽出したURL（末尾スラッシュ正規化）→ファイル名の対応表。

    完全一致で照合するための集合（部分文字列一致だと切り詰めURLが通ってしまう）。
    Handbookインスタンスにキャッシュする。
    """
    cached = getattr(handbook, "_url_map", None)
    if cached is not None:
        return cached
    mapping: dict[str, str] = {}
    for f in handbook.files:
        for match in _URL_RE.finditer(f.content):
            mapping.setdefault(match.group(0).rstrip("/"), f.name)
    handbook._url_map = mapping  # type: ignore[attr-defined]
    return mapping


def _url_in_handbook(url: str, handbook: Handbook) -> bool:
    return url.rstrip("/") in _handbook_url_map(handbook)


def _scrub_body_urls(
    text: str, handbook: Handbook, allowlist: list[str] | None = None
) -> str:
    """本文中のURLのうち、ハンドブック・許可リストのいずれにも無いものを除去する。

    許可リスト（例: laws.e-gov.go.jp）はツール実行結果由来のURL向け。
    """
    allowlist = allowlist or []

    def repl(match: re.Match) -> str:
        url = match.group(0)
        if any(url.startswith(prefix) for prefix in allowlist):
            return url
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
