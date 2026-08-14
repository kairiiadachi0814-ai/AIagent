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
                "task",
                "chat",
            ],
            "description": "メッセージ種別。社内手順・ルールの質問=question／マニュアル更新の報告=manual_update_report／一般的な会計・簿記・税務知識または補助金・助成金・支援制度の質問=general_knowledge／法令・法律の知識に関する質問=legal_knowledge／過去の回答への誤り指摘・改善要望=answer_feedback／文章作成・計算・整形・翻訳など調べものではない作業依頼=task（ただし社内手順・ルールの説明・要約・まとめ直し・チェックリスト化はquestion）／挨拶・お礼・雑談（情報を求めていないもの）=chat",
        },
        "reference_url": {
            "type": "string",
            "description": "general_knowledgeの場合のみ: 会計基礎知識リンク集（会計・税務）または補助金リンク集（補助金・助成金の制度内容）に記載の該当ページURLを一字一句正確に転記。どちらを使うかは質問の主眼（会計処理か制度内容か）で選ぶ。該当がなければ空文字。他のintentでは空文字",
        },
        "reported_manuals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "更新報告の場合、報告対象のマニュアル名（分かる範囲で）。質問の場合は空配列",
        },
        "has_answer": {
            "type": "boolean",
            "description": "回答・成果物を返せたか。question=ハンドブックに根拠がある場合のみtrue／general_knowledge=参考ページの案内または一般知識での回答ができた場合true／legal_knowledge=条文を取得できた場合true／task・chat=原則true／manual_update_report・answer_feedbackは常にfalse",
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
            "description": "記載が無い場合に追記先として提案するファイル名。無ければ空文字。task・chatでは常に空文字",
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
            "法令・法律の知識に関する質問（intent=legal_knowledge）の回答専用。"
            "法令の質問では法令・条文の特定に必ず使い、社内手順・一般会計知識・"
            "補助金/助成金の制度内容など法令以外の質問では決して呼び出さないこと。"
            "結果にはlaw_id・法令名・該当箇所の抜粋・e-GovのURLが含まれる。"
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
            "法令・法律の知識に関する質問（intent=legal_knowledge）の回答専用で、"
            "law_idにはsearch_lawの結果から得た実際の値のみを指定すること。"
            "法令の回答は必ずこの原文に基づき、法令以外の質問"
            "（社内手順・一般会計知識・補助金/助成金の制度内容など）では決して呼び出さないこと。"
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

# 一般知識の直接回答（原則9(d)）に必須の免責文。_validate_citations が
# この接頭辞（※一般的な◯◯知識）の存在をコード側でも検証する。
# 文言を変える場合は原則9(d)・_GENERAL_DISCLAIMER_MARKERS・テストをセットで更新すること
GENERAL_KNOWLEDGE_DISCLAIMER = (
    "※一般的な会計知識としての説明です。"
    "当社での具体的な取り扱い・税務判断は税理士にご確認ください。"
)
SUBSIDY_KNOWLEDGE_DISCLAIMER = (
    "※一般的な制度知識としての説明です。"
    "実際の要件・金額・期間は公式サイト・公募要領でご確認ください。"
)
_GENERAL_DISCLAIMER_MARKERS = ("※一般的な会計知識", "※一般的な制度知識")

# 出典なしのtask成果物に自動付加する注記（ハンドブック根拠の回答との混同防止）
TASK_NO_SOURCE_NOTE = (
    "※社内ハンドブックの記載を根拠にした回答ではありません。"
    "日付・金額・固有の情報が含まれる場合は、ご確認のうえお使いください。"
)

SYSTEM_INSTRUCTIONS = """あなたは株式会社ライズクリエイションの社内AIエージェント「{agent_name}」です。
Chatworkで社員からの質問・依頼に日本語で応対します。役割は主に4つ:
(1)社内手順・ルールの質問への回答（必ずハンドブック=経理・財務の業務手順書群を根拠にする）
(2)一般的な会計知識・法令・補助金の参照回答（原則9〜11）
(3)文章作成・計算などの雑務の手伝い（原則13）
(4)挨拶や雑談への気さくな応対（原則14）

あなたにできること（誤った自己申告をしないこと）:
- Wordファイル（.docx）・テキストファイル（.txt等）の添付や、Googleドキュメント／スプレッドシートのURLが依頼に添えられている場合、システム側がその中身を読み取って議事録作成・要約などを行う仕組みがある。したがってこれらについて「ファイルを読み取れません」「その機能はありません」と答えてはならない。
- あなたにファイルの内容が渡されていない場合は、機能が無いのではなく対象の文書を特定できていないだけである。その場合は「ファイルを添付したメッセージの中でメンションしてください」「GoogleドキュメントのURLを依頼文に貼ってください」と案内すること（本文の貼り付けを求めるのは最後の手段）。
- ただしPDF・旧形式の.doc・画像・音声には対応していない。これらを渡された場合は正直にその旨を伝え、Wordで.docxとして保存し直すよう案内すること。

話し方（すべての回答に適用）:
- 社内の気さくで頼れる経理の先輩が、隣の席からさらっと教えてくれるような話し言葉にすること。です・ます調の丁寧さは保ちつつ、かしこまりすぎない。「〜ですよ」「〜なんです」「ちなみに」「ざっくり言うと」のような柔らかい言い回しは自然に使ってよい（敬語が崩れるくだけ方や馴れ馴れしい呼びかけはしない）。
- 利用者の質問に答えるときは、質問を受け止める短い一言から入ってよい（例:「〜の件ですね。」「はい、ありますよ。」「それ、ちょうど手順がありますよ。」）。例文は雰囲気の見本であり、そのまま使い回さず質問の内容に合わせて毎回言い方を変えること。「あ、」のような感動詞で始める書き出しを多用しない。短い質問には前置きなしで結論から入ってよい。いずれの場合も結論→必要な補足の順で話す。
- 質問への回答以外の依頼（依頼文の側で書き出しや文体が指定されている場合。例: メンバー同士の議論への横からの助言）では、その指定を優先し、受け止めの一言やくだけた言い回しは使わず、落ち着いた控えめな丁寧さで書くこと。
- 「以下のとおりです」「上記をご確認ください」のような機械的な定型表現の連発や、過剰な箇条書きを避ける。短い説明は文章の流れで伝え、手順が3ステップ以上あるときや選択肢の列挙が必要なときだけ箇条書きを使う。
- ChatworkはMarkdownを表示できない。箇条書きは「・」で始め、「- 」「* 」による箇条書き・「**」による強調・「#」見出し・バッククォート等のMarkdown記法は使わないこと。強調したい語は「」で括る。
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
1. 回答は必ずハンドブックの記載に基づくこと。参照したファイル名と見出しをsourcesに正確に挙げること。実在しないファイル名を出典にしてはならない。**sourcesの各項目には、そのマニュアルの原本URLが「マニュアルリンク集」に載っている場合、必ずurlに一字一句正確に転記すること**（利用者は原本を開いて確認するため、出典名だけでは足りない）。リンク集に見当たらない場合のみ空文字にしてよい。
2. ハンドブックに書かれていないことは推測で埋めず、has_answer=falseとし、answerには「ハンドブックに記載がありません」という趣旨を書くこと。あわせて、どのファイルに追記すべきかをsuggested_fileで提案してよい。
3. 回答本文は簡潔に。手順を問われたら該当手順の要点を回答本文に直接書くこと。
4. 回答本文に「〜.md」等の内部ファイル名を書いてはならない。利用者はmdファイルを閲覧できない（管理者専用）。詳細への誘導は、マニュアルリンク集に記載の原本URL（GoogleドキュメントやシートのURL）で行い、URLが無い場合は本文の要約で完結させること。
5. パスワード等の認証情報がハンドブックに残っている場合でも、回答本文に転記しないこと。
6. 会話履歴は文脈理解の参考とし、回答の根拠にはしないこと。
7. 関連する原本文書・スプレッドシート・フォルダのURLがハンドブック（特に「マニュアルリンク集」）に記載されている場合は、回答本文またはsourcesのurlに含めて案内すること。URLは一字一句正確に転記し、加工・短縮・推測してはならない。ハンドブックに記載のないURLを作らないこと。本文中に一字一句正確に写す自信がないURLは本文に書かず、「末尾の【出典】のリンクからご覧ください」のように出典欄へ誘導すること（本文のURLはハンドブックとの完全一致チェックがあり、一致しないと「（URL省略）」に置換されて読みにくくなる）。
8. メッセージ種別の判定: 利用者が質問ではなく「マニュアル（原本）を更新した・変更した」と明確に報告している場合のみ、intentをmanual_update_reportとし、reported_manualsに対象マニュアル名を入れること。その場合answerには報告内容の短い受領確認文（1〜2文。対象マニュアル名を復唱）を書き、has_answerはfalse、sourcesは空とする。更新の仕方に関する質問や迷うケースはquestionとして扱うこと。
9. 一般的な会計・簿記・税務知識の質問、または補助金・助成金・支援制度に関する質問（いずれも社内の手順・ルールではないもの）への対応: intentをgeneral_knowledgeとし、会計・税務なら「会計基礎知識リンク集」、補助金・助成金・支援制度なら「補助金リンク集」に該当する解説ページがあればreference_urlにそのURLを一字一句正確に転記すること。リンク集の使い分けは質問の主眼で決める: (a)補助金に触れる質問でも、主眼が受給後の会計処理・仕訳・税務（圧縮記帳、収益計上時期、消費税・法人税の扱いなど）であれば「会計基礎知識リンク集」から選ぶ。(b)制度の内容・対象・金額・申請方法・探し方が主眼なら「補助金リンク集」から選び、会計基礎知識リンク集に類似記事があっても補助金リンク集を優先する。(c)個別の制度への質問はその制度の個別ページがある場合のみ使い、一覧・チラシ集などの総覧ページは「どんな補助金があるか」のような探し方の質問にのみ使う。(d)主眼側のリンク集に該当ページが無い場合（もう一方のリンク集で代用しない）: 用語の意味・仕組み・考え方を尋ねる概念的で簡単な質問であれば、reference_urlを空にしたまま自身の一般知識で簡潔に回答してよい（has_answer=true、sourcesは空）。その場合、回答の最後に必ず免責文を付けること（会計・税務の質問は「※一般的な会計知識としての説明です。当社での具体的な取り扱い・税務判断は税理士にご確認ください。」、補助金・助成金・支援制度の質問は「※一般的な制度知識としての説明です。実際の要件・金額・期間は公式サイト・公募要領でご確認ください。」）。ただし税率・金額・限度額・期限などの具体的な数値は記憶で断定して書かず、「正確な数値は税理士や公的情報でご確認ください」と案内する。数値が答えの核心になる質問や自信が持てない内容は、無理に答えず「記載なし」として正直に伝える。answerに参考URLの案内を書く場合は「社内ハンドブックには記載がありません。参考: <URL>」の趣旨の案内文（会計・税務は税理士確認、補助金は公式サイト・公募要領確認の注意書き付き）とする。詳細な要約はシステム側がページを取得して別途行うため、リンク集に該当がある場合に自身の知識だけで数値・限度額・公募期間・申請可否を断定して書いてはならない。
10. 法令・法律の知識に関する質問（社内手順ではないもの）: intentをlegal_knowledgeとし、必ずsearch_lawツールで法令を特定し、get_law_articleツールで条文の原文を取得したうえで、その原文に基づいて回答すること。ツールを使わず記憶だけで条文の内容や条番号を断定してはならない。回答には法令名・条番号を明記し、ツール結果に含まれるURL（laws.e-gov.go.jp）をそのまま含める。回答の最後に必ず「※e-Gov法令検索の条文に基づく一般的な情報です。法的判断や契約書の内容確認は、顧問弁護士への相談またはLegalForceでのリーガルチェックを経てください。」を付ける。この場合sourcesは空でよい（出典は本文中の法令名・条番号・URLで示す）。has_answerは条文を取得できた場合にtrue。**ツール呼び出しは効率よく行うこと**: 独立した検索・条文取得は1ターンに複数まとめて並列に呼んでよい。網羅を目指さず、質問に最も関係する法令1〜2件・条文2〜4件に絞り、取得できた範囲で回答をまとめること（ツール往復は合計5回以内を目安）。search_law／get_law_articleは法令・法律の質問の回答専用ツールであり、intentがlegal_knowledge以外になる質問（社内手順・一般会計知識・更新報告・フィードバック等）では一切呼び出さないこと。「placeholder」のような仮の引数での試し呼び出しもしてはならない。補助金・助成金・支援制度の内容・要件・金額・申請方法の質問は法令の質問ではない（原則9のgeneral_knowledgeとして扱い、これらのツールを使わない）。補助金の根拠となる法律の条文そのものを明確に問われた場合のみ法令の質問として扱う。
11. 契約書の作成・ひな形に関する相談: 「ひな形リンク集」から該当するひな形のURLを案内し、あわせて「作成した契約書は締結前に必ずLegalForceでリーガルチェックを行う」こと（手順はハンドブック「LegalForce_リーガルチェック」参照）を必ず案内する。契約条項の妥当性を自ら断定してはならない。
12. 過去の回答への誤り指摘・不満・改善要望（フィードバック）: intentをanswer_feedbackとし、answerには丁寧なお詫びと受領の一言を書くこと（指摘内容を短く復唱し、改善リストに追加して管理者が確認する旨を伝える）。反論や弁明はせず、has_answerはfalse、sourcesは空とする。
13. 雑務の依頼（文章作成・文面の下書き〔周知文・お礼・依頼文・お詫び文など〕・計算・表やリストの整形・翻訳・アイデア出しなど、調べものではない作業）: intentをtaskとし、answerに成果物を直接書くこと。has_answerはtrue、sourcesは空でよい。ただし社内ルールの内容を使う作業（例: 締め日変更の周知文）はハンドブックの記載に忠実に作り、参照したファイルをsourcesに挙げること。ハンドブックに無い金額・日付・名前など不確かな部分は勝手に決めず「◯◯」のようなプレースホルダにして、依頼者が埋められるようにする。計算は途中式も短く示し、計算に使う数値・料率は依頼文またはハンドブックに書かれたものに限る（税率・限度額など裏取りが必要な数値が要る場合は、その部分を「◯◯」にして計算式だけ示すか、原則9として扱う）。**ハンドブックの内容を調べて伝えることが主眼の依頼（社内手順の要約・抜粋・まとめ直し・チェックリスト化・表化など）は、言い方が作業依頼でもtaskにせずquestionとして扱い、出典を付けること。** taskは依頼文に与えられた材料だけで完結する作業に限る。なお、添付ファイルやGoogleドキュメントの処理はシステム側で別に行うため、このintentの対象はメッセージ本文だけで完結する作業に限る。
14. 挨拶・お礼・雑談・軽い声かけ（情報を求めていないもの）: intentをchatとし、answerに気さくで丁寧な短い返答を書くこと（1〜3文。相手の言葉に自然に応じ、必要なら「お手伝いできることがあればいつでもどうぞ」と添える程度）。has_answerはtrue、sourcesは空とする。業務の手順・数値など情報を尋ねる内容が少しでも含まれる場合は、砕けた口調でもchatにせず通常のintentで扱うこと。"""


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
    stage2: str = ""  # 2段目取得の結果: ""=対象外 / "ok" / "fetch_failed" / "error"


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
            "task",
            "chat",
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
        answer = self._validate_citations(answer, handbook, question)
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

    def _execute_tool(self, name: str, tool_input: dict) -> "tuple[str, bool]":
        """カスタムツール（e-Gov法令検索）を実行する。

        戻り値は (モデルへ返す結果文字列, 実行としてカウントするか)。
        placeholder的な無意味引数はe-Govへ送らずエラー文だけ返し、実行にカウント
        しない（法令と無関係な質問での退行的ツール呼び出しを根拠扱いしないため）。
        """
        from .egov import EgovError

        print(f"[tool] {name} {json.dumps(tool_input, ensure_ascii=False)[:200]}", flush=True)
        try:
            if name == "search_law" and self._egov is not None:
                keyword = str(tool_input.get("keyword", "")).strip()
                # 「a」等の1文字キーワードもplaceholder同様の退行呼び出し（実地で観測）
                if _is_placeholder_value(keyword) or len(keyword) < 2:
                    return _tool_misuse_error(
                        "検索キーワード（keyword）", "質問の内容に即した具体的なキーワード"
                    ), False
                results = self._egov.search(keyword)
                if not results:
                    results = self._egov.search_by_title(keyword)
                if not results:
                    return "該当する法令が見つかりませんでした。キーワードを変えて再検索してください。", True
                return json.dumps(results, ensure_ascii=False), True
            if name == "get_law_article" and self._egov is not None:
                law_id = str(tool_input.get("law_id", "")).strip()
                if _is_placeholder_value(law_id):
                    return _tool_misuse_error(
                        "law_id", "search_lawの結果に含まれる実際のlaw_id"
                    ), False
                result = self._egov.get_article(
                    law_id,
                    str(tool_input.get("article") or "") or None,
                )
                return json.dumps(result, ensure_ascii=False), True
            return f"エラー: 未知のツール {name}", False
        except EgovError as exc:
            return f"エラー: {exc}", True
        except Exception as exc:  # ツール失敗で回答全体を落とさない
            return f"エラー: 条文の取得に失敗しました（{exc}）", True

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
            answer.stage2 = "error"
            return answer  # 要約失敗時は案内文のまま（呼び出し元で失敗扱いにしない）
        _accumulate_usage(answer.usage, usage2)
        if not text:
            answer.stage2 = "fetch_failed"  # リンク切れ・ソフト404の検知用（監査ログへ）
            return answer
        answer.stage2 = "ok"
        source_file = _find_url_file(url, handbook)
        answer.text = _scrub_body_urls(text, handbook)
        answer.has_answer = True
        answer.sources = [
            {"file": source_file or "", "heading": "", "url": url}
        ] if source_file else answer.sources
        return answer

    def _summarize_article(self, question: str, url: str) -> "tuple[str, dict[str, int]]":
        """参考ページをweb_fetchで取得し、質問への要点回答を生成する（2段目・軽量呼び出し）。"""
        cfg = self._config
        is_subsidy = _is_mirasapo_url(url)
        line_range = "2〜8行" if is_subsidy else "2〜6行"
        subsidy_note = (
            "補助金・助成金のページでは、公募期間・受付状況（公募中／受付終了／次回公募の予定）が"
            "ページに記載されている場合、質問と直接関係なくても必ず1行含めること。"
            if is_subsidy
            else ""
        )
        system = (
            "あなたは社内アシスタント。ユーザーの質問と参考ページのURLが与えられる。"
            "web_fetchツールでページを取得し、ページの内容に基づいて質問への答え"
            f"（該当する数値・条件・区分・期限など）を日本語で{line_range}に簡潔にまとめること。"
            "文体は、気さくで頼れる経理の先輩が教えてくれるようなです・ます調の"
            "話し言葉にする（かしこまりすぎない）。経理財務の経験が浅い人にも"
            "分かるように、専門用語には初出時にひと言の意味を添えてよい。"
            "ただしページに書かれた事実の範囲で言い方だけを易しくし、"
            "数値や正式な用語はページのとおり正確に書くこと。"
            "ページの長い引用・転載はしない。ページに書かれていないことは書かない。"
            "回答はChatworkに掲載されMarkdownは表示されないため、箇条書きは「・」で始め、"
            "「- 」「**強調**」等のMarkdown記法は使わないこと。"
            + subsidy_note +
            "「内容を取得しました」「回答をまとめます」のような作業経過の前置きは"
            "一切書かず、回答本文から直接始めること。"
            f"回答の最後に必ず「{_disclaimer_for_url(url)}」を付けること。"
            "ページを取得できなかった場合、または取得したページが"
            "「ページが見つかりません」等のエラーページや、参考ページURLの主題と"
            "明らかに無関係な内容（トップページへの転送等）だった場合は、"
            "FETCH_FAILED とだけ出力すること。"
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
        messages = [{"role": "user", "content": f"質問: {question}\n参考ページURL: {url}"}]
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

    def _validate_citations(
        self, answer: Answer, handbook: Handbook, question: str = ""
    ) -> Answer:
        """出典を実在ファイル・実在見出し・実在URLと突合する（出典の捏造防止・FR-03）。"""
        allowlist = list(self._config.body_url_allowlist or [])
        if answer.intent == "task" and question:
            # 利用者が依頼文に自分で貼ったURLは成果物への復唱を許す
            # （モデルが新たに作ったURLは従来どおり除去される）
            allowlist += [m.group(0) for m in _URL_RE.finditer(question)]
        claimed_sources = bool(answer.sources)  # 検証前に「出典を主張したか」を記録
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
            if url and not (
                _url_in_handbook(url, handbook)
                and _url_belongs_to_file(url, normalized, handbook)
            ):
                # 捏造URL、および「そのマニュアルのものではないURL」（隣の行を
                # 写した等）は載せない。正しいURLは直後の補完で付け直す
                url = ""
            if not url:
                # モデルがurlを埋め忘れても、原本URLを機械的に補完する
                url = _lookup_manual_url(normalized, handbook)
            valid.append({"file": normalized, "heading": heading, "url": url})
        answer.sources = valid
        answer.text = _scrub_body_urls(answer.text, handbook, allowlist)
        # 出典なし回答の扱い（ガードレール1）。例外は次のintentのみ:
        # - legal_knowledge: e-Govツール実行済みの場合のみ（generateで担保）。出典は本文中の法令名・条番号・URL
        # - chat: 雑談への返答であり情報提供ではない
        # - task: モデルが出典を一切主張しなかった場合のみ（＝依頼文の材料だけで完結する
        #   純作業）。出典を主張したのに全て検証落ちした場合は、ハンドブック準拠を
        #   装った成果物とみなし通常どおり降格する
        # - general_knowledge: 概念的な一般知識の直接回答は、規定の免責文
        #   （※一般的な会計知識／※一般的な制度知識）が本文に含まれる場合のみ許可
        exempt = (
            answer.intent in ("legal_knowledge", "chat")
            or (answer.intent == "task" and not claimed_sources)
            or (
                answer.intent == "general_knowledge"
                and any(m in answer.text for m in _GENERAL_DISCLAIMER_MARKERS)
            )
        )
        if answer.intent == "task" and answer.has_answer and not valid and not claimed_sources:
            # 出典なしのtask成果物には、ハンドブック根拠の回答と区別できる注記を必ず付ける
            answer.text = answer.text.rstrip() + "\n\n" + TASK_NO_SOURCE_NOTE
        if answer.has_answer and not valid and not exempt:
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


# 構造化出力＋ツール併用時に、法令と無関係な質問でモデルがスキーマ例のような
# 無意味引数（例: "placeholder"）のツール呼び出しを行う退行が観測されている。
# 実行前に弾いてe-Govへの無駄なリクエストを防ぐ
_PLACEHOLDER_VALUES = frozenset(
    {
        "placeholder", "string", "example", "sample", "dummy", "test",
        "keyword", "law_id", "article", "todo", "tbd", "unknown",
        "none", "null", "n/a", "na", "foo", "bar", "xxx",
    }
)


def _is_placeholder_value(value: str) -> bool:
    """引数が未指定・placeholder的トークン・記号のみのいずれかならTrue。"""
    normalized = value.strip().strip("<>「」『』\"'").strip().lower()
    if not normalized or normalized in _PLACEHOLDER_VALUES:
        return True
    return not any(c.isalnum() for c in normalized)


def _tool_misuse_error(field: str, hint: str) -> str:
    return (
        f"エラー: {field}が指定されていないか、無意味な値です。"
        "このツールは法令・法律に関する質問の回答にのみ使用できます。"
        f"法令の質問の場合は{hint}を指定して再実行し、"
        "法令と無関係な質問の場合はツールを使わずそのまま回答してください。"
    )


_MAX_LOOP_STEPS = 10


def _call_with_continuation(
    create: Any,
    kwargs: dict[str, Any],
    messages: list[dict[str, Any]],
    tool_executor: Any | None = None,
) -> "tuple[Any, dict[str, int], bool]":
    """API呼び出しループ。戻り値は (最終レスポンス, 累積usage, ツール実行有無)。

    - pause_turn（サーバーツールの反復上限）: そのまま再送して継続
    - tool_use（カスタムツール）: tool_executorで実行し結果を返して継続。
      tool_executorは (結果文字列, 実行としてカウントするか) を返す。
      placeholder引数等で棄却された呼び出しはツール実行有無に数えない
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
                    result, executed = tool_executor(block.name, dict(block.input or {}))
                    tool_used = tool_used or executed
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )
            if not tool_results:
                break
            messages = messages + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_results},
            ]
            continue
        break
    return response, usage_total, tool_used


# 2段目要約の末尾に付ける免責文（参照元ドメイン別）
_FREEE_DISCLAIMER = (
    "※社外の一般解説（freee会計）に基づく情報です。"
    "当社での具体的な取り扱い・税務判断は税理士にご確認ください。"
)
_MIRASAPO_DISCLAIMER = (
    "※ミラサポplus（経済産業省 中小企業庁）の掲載情報に基づきます。"
    "補助金・助成金の公募期間や要件は変わることがあるため、"
    "申請前に必ず各制度の公式サイト・公募要領をご確認ください。"
)


def _is_mirasapo_url(url: str) -> bool:
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    return host == "mirasapo-plus.go.jp" or host.endswith(".mirasapo-plus.go.jp")


def _disclaimer_for_url(url: str) -> str:
    return _MIRASAPO_DISCLAIMER if _is_mirasapo_url(url) else _FREEE_DISCLAIMER


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


_TITLE_NOISE_RE = re.compile(r"[\s　_・,、。（）()\[\]]")


def _normalize_title(text: str) -> str:
    return _TITLE_NOISE_RE.sub("", text).lower()


# 各マニュアル冒頭の「- 出典：Googleドキュメント「◯◯」\n  <URL>」ブロック。
# そのファイル自身の原本URLなので、他マニュアルと取り違える余地がない
_SOURCE_HEADER_RE = re.compile(
    r"^[ \t]*[-*][ \t]*出典[：:][^\n]*\n(?:[^\n]*\n){0,2}?[ \t]*(https?://[^\s<>\"]+)",
    re.MULTILINE,
)
# GoogleドキュメントやDriveの識別子（?tab= 等のクエリ差を吸収して同一性を見る）
_DOC_ID_RE = re.compile(r"/(?:d|folders)/([A-Za-z0-9_-]{10,})")


def _own_source_url(file_name: str, handbook: Handbook) -> str:
    """そのマニュアル自身が冒頭に記載している原本URLを返す。"""
    cached = getattr(handbook, "_own_url_map", None)
    if cached is None:
        cached = {}
        for f in handbook.files:
            match = _SOURCE_HEADER_RE.search(f.content[:2000])
            if match:
                cached[f.name] = match.group(1)
        handbook._own_url_map = cached  # type: ignore[attr-defined]
    return cached.get(file_name, "")


def _manual_url_map(handbook: Handbook) -> dict[str, str]:
    """マニュアル名（正規化）→ 原本URL の対応表を「マニュアルリンク集」から作る。

    冒頭に出典URLを持たないファイルの補完に使う予備の経路。ひな形・freee・
    補助金のリンク集は社内マニュアルの原本ではないため対象外にする（誤補完防止）。
    """
    cached = getattr(handbook, "_manual_map", None)
    if cached is not None:
        return cached
    mapping: dict[str, str] = {}
    for f in handbook.files:
        if "マニュアルリンク集" not in f.name:
            continue
        for line in f.content.splitlines():
            if not line.lstrip().startswith("|"):
                continue  # 表の行だけを見る（見出し・注記の混入を防ぐ）
            match = _URL_RE.search(line)
            if not match:
                continue
            cells = [c.strip() for c in line[: match.start()].split("|") if c.strip()]
            title = cells[0] if cells else ""
            if title and len(title) < 60:
                mapping.setdefault(_normalize_title(title), match.group(0))
    handbook._manual_map = mapping  # type: ignore[attr-defined]
    return mapping


def _lookup_manual_url(file_name: str, handbook: Handbook) -> str:
    """マニュアルの原本URLを引く（自身の出典ヘッダ→リンク集の順）。"""
    own = _own_source_url(file_name, handbook)
    if own:
        return own
    mapping = _manual_url_map(handbook)
    key = _normalize_title(display_name(file_name))
    if not key:
        return ""
    if key in mapping:
        return mapping[key]
    # 「ライズ給与振込システム作業フロー(2025.11～)」のように注記付きで載っている
    # 場合に備え、マニュアル名がリンク集の見出しに含まれ、候補が1件のときだけ許す
    hits = [url for title, url in mapping.items() if key in title]
    return hits[0] if len(hits) == 1 else ""


def _url_belongs_to_file(url: str, file_name: str, handbook: Handbook) -> bool:
    """そのURLが、出典として挙げられたマニュアル自身に載っているURLか。

    「ハンドブックのどこかに実在する」だけでは、モデルが隣の行のURLを写した場合に
    別マニュアルの原本へ誘導してしまうため、出典ファイル本文との対応まで確認する。
    """
    target = next((f for f in handbook.files if f.name == file_name), None)
    if target is None:
        return False
    doc_id = _DOC_ID_RE.search(url)
    if doc_id:
        return doc_id.group(1) in target.content
    return url.rstrip("/") in {
        m.group(0).rstrip("/") for m in _URL_RE.finditer(target.content)
    }


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
        raw = match.group(0)
        # モデルが「**URL**」「`URL`」のようにMarkdownで囲んだ場合、末尾の記号は
        # URLの一部として取り込まれる（_URL_REは*や`を除外しない）。記号を切り離して
        # 素のURLで照合しないと、検証済みの正当なURLまで（URL省略）になってしまう
        url = raw.rstrip("*`")
        trailing = raw[len(url):]
        if any(url.startswith(prefix) for prefix in allowlist):
            return url + trailing
        return (url if _url_in_handbook(url, handbook) else "（URL省略）") + trailing

    return _URL_RE.sub(repl, text)


def display_name(file_name: str) -> str:
    """出典表示用のマニュアル名（内部ファイルの拡張子は見せない）。"""
    return file_name[:-3] if file_name.endswith(".md") else file_name


# ChatworkはMarkdownを表示できないため、モデルが混ぜた記法をプレーン表記へ変換する
_MD_BOLD_RE = re.compile(r"\*\*([^*\n]+?)\*\*|__([^_\n]+?)__")
_MD_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_HEADING_RE = re.compile(r"^(\s*)#{1,6}\s+", re.MULTILINE)
_MD_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)


def _strip_markdown(text: str) -> str:
    """Markdown記法をChatwork向けの表記に直す（箇条書きは「・」、強調・コードは中身のみ）。

    URL区間はプレースホルダへ退避してから変換する。Google DocsのID等には「__」や
    ハイフンがMarkdown記法と同じ文字列として正当に含まれるため、退避しないと
    実在するURLを破壊してしまう。URLを囲む**や`は記法としてURLの外に出し、
    通常の強調・コードのペアとして除去させる。
    """
    urls: list[str] = []

    def _shield(match: re.Match) -> str:
        url = match.group(0).rstrip("*`")
        trailing = match.group(0)[len(url):]
        urls.append(url)
        return f"\x00{len(urls) - 1}\x00{trailing}"

    text = _URL_RE.sub(_shield, text)
    text = _MD_BOLD_RE.sub(lambda m: m.group(1) or m.group(2) or "", text)
    text = _MD_CODE_RE.sub(r"\1", text)
    text = _MD_HEADING_RE.sub(r"\1", text)
    text = _MD_BULLET_RE.sub(r"\1・", text)
    for i, url in enumerate(urls):
        text = text.replace(f"\x00{i}\x00", url)
    return text


def sanitize_for_chatwork(text: str) -> str:
    """モデル由来文字列をChatwork表示用に整える。

    - Chatworkタグの無害化（[toall]等の注入防止。角括弧を全角へ）
    - Markdown記法の除去（Chatworkは表示できず記号がそのまま見えるため）
    """
    return _strip_markdown(text).replace("[", "［").replace("]", "］")


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
