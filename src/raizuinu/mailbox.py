"""メールサーバーの受信箱・送信済みを読む（IMAP・読み取り専用）。

営業メールの見張り（mailwatch.py）が、通知ルームの「先頭約400字」ではなくメールそのものを
読むための窓口（指示 2026-11-24）。Thunderbird が見ているのと同じサーバー上のフォルダを読む。

守ること:
- **読むだけ**。使う命令は LOGIN／LIST／EXAMINE（読み取り専用で開く）／UID SEARCH／UID FETCH／LOGOUT。
  既読にしない（本文は BODY.PEEK で取る）。印を付ける・移す・消す・書き込む命令と、メールの送信は
  置かない（`tests/test_mailbox.py` で使う命令の一覧を固定してある）
- パスワードは環境変数からだけ読む（設定には変数の名前だけを書く）。ログにも例外にも出さない
- 通信は暗号化する。証明書は通常どおり検証し、検証できないサーバーには、証明書の指紋を
  設定に書いたとき（`tls.pin_sha256`）だけ、その指紋と一致することを確かめてつなぐ
"""

from __future__ import annotations

import base64
import email
import email.policy
import hashlib
import html
import imaplib
import os
import re
import ssl
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Callable

JST = timezone(timedelta(hours=9))

AUTH, NETWORK, TLS, FOLDER = "auth", "network", "tls", "folder"

# 送信済みフォルダの名前の候補（サーバーが「送信済み」の印 \\Sent を付けていなければ名前で探す）
SENT_NAMES = ("sent", "sent messages", "sent items", "送信済み", "送信済みトレイ", "送信済みアイテム", "送信済メール")

# 届いたメールが入らないフォルダ（読まない）。最後の4つはサーバーが設定用に持つフォルダ
SKIP_NAMES = (
    "trash", "ごみ箱", "ゴミ箱", "deleted items", "junk", "spam", "迷惑メール", "drafts", "draft", "下書き",
    "templates", "テンプレート", "outbox", "送信トレイ", "autoreply", "trigger", "alias", "mailfilter",
)

MAX_FULL_BYTES = 15 * 1024 * 1024   # これより大きいメールは、先頭の一部だけ取る
PARTIAL_BYTES = 400 * 1024


class MailboxError(Exception):
    """受信箱を読めなかった。kind は auth（認証）／network（通信）／tls（証明書）／folder（フォルダ）。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


# --- フォルダ名（IMAPの修正UTF-7）---


def decode_folder(name: str) -> str:
    """「&kQFP4W4IMH8-」→「送信済み」。ASCIIの名前はそのまま。"""

    def repl(match: re.Match) -> str:
        chunk = match.group(1)
        if not chunk:
            return "&"
        data = chunk.replace(",", "/")
        try:
            return base64.b64decode(data + "=" * (-len(data) % 4)).decode("utf-16-be")
        except (ValueError, UnicodeDecodeError):
            return match.group(0)

    return re.sub(r"&([^-]*)-", repl, str(name or ""))


_LIST_RE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?:"(?P<sep>[^"]*)"|NIL)\s+(?P<name>.+)$')


def parse_list_line(line: bytes | str) -> dict[str, Any] | None:
    """LIST の1行 → {raw（命令に使う名前）, name（読める名前）, flags}。"""
    text = line.decode("ascii", "replace") if isinstance(line, bytes) else str(line)
    match = _LIST_RE.match(text.strip())
    if not match:
        return None
    raw = match.group("name").strip()
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return {"raw": raw, "name": decode_folder(raw), "flags": match.group("flags").split(), "sep": match.group("sep") or ""}


def _leaf(folder: dict) -> str:
    name = folder["name"].split(folder["sep"])[-1] if folder["sep"] else folder["name"]
    return name.strip().lower()


def _flagged(folder: dict, *flags: str) -> bool:
    return any(flag.lower() in flags for flag in folder["flags"])


def sent_folders(folders: list[dict], configured: str = "") -> list[dict]:
    """送信済みフォルダ。設定の名前があればそれだけ。無ければ、サーバーの印（\\Sent）か、よくある名前のものすべて。

    1つに絞らない（実例 2026-11-24: 「INBOX.送信済み」は空で、使われていたのは「INBOX.Sent」だった）。
    """
    if configured:
        return [f for f in folders if configured in (f["raw"], f["name"])]
    return [f for f in folders if _flagged(f, "\\sent") or _leaf(f) in SENT_NAMES]


def pick_sent_folder(folders: list[dict], configured: str = "") -> dict | None:
    """送信済みフォルダを1つ選ぶ（印のあるものを優先）。複数を読むときは sent_folders を使う。"""
    found = sent_folders(folders, configured)
    flagged = [f for f in found if _flagged(f, "\\sent")]
    return (flagged or found or [None])[0]


def other_folders(folders: list[dict], configured_sent: str = "", skip: Any = ()) -> list[dict]:
    """受信箱・送信済み以外で、届いたメールが振り分けられ得るフォルダ。

    メールソフトの振り分けで、届いたメールが受信箱から取引先別のフォルダへ移ることがある。
    ごみ箱・迷惑メール・下書きなどと、開けない印（\\Noselect）のフォルダは除く。
    """
    sent = {f["raw"] for f in sent_folders(folders, configured_sent)}
    skipped = set(SKIP_NAMES) | {str(name).strip().lower() for name in skip or ()}
    return [
        f for f in folders
        if f["raw"].upper() != "INBOX" and f["raw"] not in sent and not _flagged(f, "\\noselect", "\\drafts", "\\trash", "\\junk")
        and _leaf(f) not in skipped and f["name"].strip().lower() not in skipped
    ]


# --- メールの解釈 ---

_QUOTE_STARTS = (
    re.compile(r"^>"),
    re.compile(r"^-{2,}\s*(Original Message|Forwarded message|転送メッセージ|元のメッセージ)", re.I),
    re.compile(r"^-{5,}[^-\n]+-{3,}\s*$"),
    re.compile(r"^On .+ wrote:\s*$"),
    re.compile(r"^\d{4}[年/]\d{1,2}[月/]\d{1,2}日?.*(<[^>\s]+@[^>\s]+>|wrote|書きました|のメッセージ)\s*[:：]?\s*$"),
)
_HEADER_BLOCK_FIRST = re.compile(r"^(From|差出人|送信者)\s*[:：].+")
_HEADER_BLOCK_NEXT = re.compile(r"^(Sent|Date|送信日時|日時|To|宛先)\s*[:：]")


def split_quoted(text: str) -> tuple[str, str]:
    """本文を「今回書かれた部分」と「引用された過去のやり取り」に分ける。"""
    lines = str(text or "").splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if any(pattern.match(stripped) for pattern in _QUOTE_STARTS):
            return "\n".join(lines[:index]).rstrip(), "\n".join(lines[index:]).strip()
        if _HEADER_BLOCK_FIRST.match(stripped) and any(
            _HEADER_BLOCK_NEXT.match(nxt.strip()) for nxt in lines[index + 1: index + 5]
        ):
            return "\n".join(lines[:index]).rstrip(), "\n".join(lines[index:]).strip()
    return str(text or "").rstrip(), ""


def html_to_text(source: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", str(source or ""))
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    return html.unescape(text)


def _tidy(text: str) -> str:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n").replace("　\n", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _part_text(part: Any) -> str:
    try:
        return str(part.get_content())
    except (LookupError, UnicodeError, ValueError, KeyError):
        payload = part.get_payload(decode=True) or b""
        for charset in (part.get_content_charset() or "", "utf-8", "cp932", "iso-2022-jp"):
            if not charset:
                continue
            try:
                return payload.decode(charset)
            except (LookupError, UnicodeError):
                continue
        return payload.decode("utf-8", "replace")


def _addresses(message: Any, header: str) -> list[str]:
    values = [str(v) for v in message.get_all(header, [])]
    return [addr.lower() for _, addr in getaddresses(values) if "@" in addr]


def _ids(value: Any) -> list[str]:
    return re.findall(r"<[^<>\s]+>", str(value or ""))


def parse_message(raw: bytes, received: datetime | None = None, partial: bool = False) -> dict[str, Any]:
    """メール1通 → 差出人・宛先・CC・件名・本文（今回の部分と引用）・添付の名前・返信関係のID。"""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    senders = getaddresses([str(v) for v in message.get_all("From", [])])
    sender_name, sender = (senders[0][0].strip(), senders[0][1].lower()) if senders else ("", "")
    if received is None:
        try:
            received = parsedate_to_datetime(str(message.get("Date", "")))
        except (TypeError, ValueError):
            received = None
    try:
        written = parsedate_to_datetime(str(message.get("Date", "")))
        written = (written if written.tzinfo else written.replace(tzinfo=JST)).astimezone(JST)
    except (TypeError, ValueError):
        written = None
    if received is not None:
        received = (received if received.tzinfo else received.replace(tzinfo=JST)).astimezone(JST)

    body = ""
    try:
        part = message.get_body(preferencelist=("plain", "html"))
    except Exception:  # 壊れた構造のメール
        part = None
    if part is not None:
        body = _part_text(part)
        if part.get_content_type() == "text/html":
            body = html_to_text(body)
    names = []
    try:
        for attachment in message.iter_attachments():
            name = attachment.get_filename()
            if name:
                names.append(str(name))
    except Exception:
        pass
    fresh, quoted = split_quoted(_tidy(body))
    return {
        "sender": sender,
        "sender_name": sender_name,
        "to": _addresses(message, "To"),
        "cc": _addresses(message, "Cc"),
        "received": received,
        "written": written,  # 差出人が送った日時（フォルダを移しても変わらない）
        "subject": re.sub(r"\s+", " ", str(message.get("Subject", "") or "")).strip(),
        "attachments": names,
        "text": fresh,
        "quoted": quoted,
        "truncated": bool(partial),
        "message_id": (_ids(message.get("Message-ID")) or [""])[0],
        "references": _ids(message.get("In-Reply-To")) + _ids(message.get("References")),
    }


# --- 接続 ---


def tls_context(tls: dict | None) -> ssl.SSLContext:
    """通常は証明書を検証する。`pin_sha256` があるときだけ、検証の代わりに指紋の一致を確かめる。"""
    settings = tls or {}
    if settings.get("pin_sha256"):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            context.set_ciphers("DEFAULT@SECLEVEL=0")  # 古い証明書（鍵が短い・SHA-1）のサーバー用
        except ssl.SSLError:
            pass
        return context
    return ssl.create_default_context()


def fingerprint_sha256(der: bytes) -> str:
    return hashlib.sha256(der or b"").hexdigest()


def _same_fingerprint(a: str, b: str) -> bool:
    clean = lambda v: re.sub(r"[^0-9a-f]", "", str(v or "").lower())  # noqa: E731
    return bool(clean(a)) and clean(a) == clean(b)


def _connect(box: dict) -> Any:
    host, port = str(box["host"]), int(box.get("port", 993))
    tls = box.get("tls") or {}
    try:
        conn = imaplib.IMAP4_SSL(host, port, ssl_context=tls_context(tls), timeout=int(box.get("timeout", 30)))
    except ssl.SSLCertVerificationError as exc:
        raise MailboxError(TLS, f"{host} の証明書を検証できませんでした（{exc.verify_message}）") from None
    except ssl.SSLError as exc:
        raise MailboxError(TLS, f"{host} と暗号化通信を始められませんでした（{exc.reason}）") from None
    except OSError as exc:
        raise MailboxError(NETWORK, f"{host}:{port} に接続できませんでした（{type(exc).__name__}）") from None
    if tls.get("pin_sha256"):
        try:
            seen = fingerprint_sha256(conn.sock.getpeercert(binary_form=True))
        except Exception:
            seen = ""
        if not _same_fingerprint(seen, tls["pin_sha256"]):
            try:
                conn.shutdown()
            except Exception:
                pass
            raise MailboxError(TLS, f"{host} の証明書が、設定に控えた指紋と一致しません（つなぐのをやめました）")
    return conn


class MailboxReader:
    """1つのメールアドレスの受信箱と送信済みを、読み取り専用で読む。with で使う。"""

    def __init__(self, box: dict, password: str | None = None, connect: Callable[[dict], Any] | None = None) -> None:
        self._box = box
        self._password = password if password is not None else os.environ.get(str(box.get("password_env") or ""), "")
        self._connect = connect or _connect
        self._conn: Any = None
        self._folders: list[dict] | None = None
        self._opened = ""

    @property
    def name(self) -> str:
        return str(self._box.get("name") or self._box.get("address") or "")

    def configured(self) -> bool:
        return bool(self._password and self._box.get("host") and self._box.get("address"))

    def __enter__(self) -> "MailboxReader":
        if not self.configured():
            raise MailboxError(AUTH, f"{self.name} のメールのパスワードが設定されていません")
        self._conn = self._connect(self._box)
        user = str(self._box.get("user") or self._box["address"])
        try:
            self._conn.login(user, self._password)
        except imaplib.IMAP4.error:
            self._close()
            raise MailboxError(AUTH, f"{self.name} のメールにログインできませんでした（ユーザー名かパスワードが違います）") from None
        except OSError as exc:
            self._close()
            raise MailboxError(NETWORK, f"{self.name} のメールサーバーとの通信が切れました（{type(exc).__name__}）") from None
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._close()

    def _close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.logout()
        except Exception:
            pass
        self._conn = None

    # --- フォルダ ---

    def folders(self) -> list[dict]:
        if self._folders is None:
            status, data = self._call("list")
            self._folders = [f for f in (parse_list_line(line) for line in data or [] if line) if f]
        return self._folders

    def inbox(self) -> dict:
        return {"raw": "INBOX", "name": "INBOX", "flags": [], "sep": ""}

    def sent(self) -> list[dict]:
        """送信済みフォルダ（候補すべて）。"""
        return sent_folders(self.folders(), str(self._box.get("sent_folder") or ""))

    def others(self) -> list[dict]:
        """受信箱・送信済み以外で、届いたメールが振り分けられ得るフォルダ。"""
        return other_folders(self.folders(), str(self._box.get("sent_folder") or ""), self._box.get("skip_folders"))

    def open(self, folder: dict) -> dict[str, int]:
        """フォルダを読み取り専用で開く。→ {messages, uidvalidity, uidnext}"""
        quoted = '"' + folder["raw"].replace("\\", "\\\\").replace('"', '\\"') + '"'
        status, data = self._call("select", quoted, readonly=True)
        if status != "OK":
            raise MailboxError(FOLDER, f"{self.name} のフォルダ「{folder['name']}」を開けませんでした")
        self._opened = folder["raw"]
        info = {"messages": _to_int(data[0] if data else 0), "uidvalidity": 0, "uidnext": 0}
        for key in ("UIDVALIDITY", "UIDNEXT"):
            _, value = self._conn.response(key)
            info[key.lower()] = _to_int(value[0] if value else 0)
        return info

    def uids_after(self, after: int) -> list[int]:
        """開いているフォルダで、UID が after より大きいメール。古い順。"""
        status, data = self._call("uid", "SEARCH", None, f"UID {int(after) + 1}:*")
        found = sorted({_to_int(u) for u in (data[0] or b"").split()}) if status == "OK" and data else []
        return [uid for uid in found if uid > int(after)]  # 「n:*」は n より小さい最後の1通を返すことがある

    def last_uids(self, count: int) -> list[int]:
        status, data = self._call("uid", "SEARCH", None, "ALL")
        found = sorted({_to_int(u) for u in (data[0] or b"").split()}) if status == "OK" and data else []
        return found[-int(count):] if count else []

    def fetch(self, uid: int) -> dict[str, Any] | None:
        """開いているフォルダの1通を読む（既読にしない）。大きすぎるメールは先頭だけ。"""
        status, data = self._call("uid", "FETCH", str(int(uid)), "(RFC822.SIZE INTERNALDATE)")
        meta = _join_meta(data)
        if status != "OK" or not meta:
            return None
        size = _to_int((re.search(rb"RFC822\.SIZE (\d+)", meta) or [0, 0])[1])
        received = _internal_date(meta)
        partial = size > MAX_FULL_BYTES
        if partial:
            _, head = self._call("uid", "FETCH", str(int(uid)), "(BODY.PEEK[HEADER])")
            _, body = self._call("uid", "FETCH", str(int(uid)), f"(BODY.PEEK[TEXT]<0.{PARTIAL_BYTES}>)")
            raw = _literal(head) + _literal(body)
        else:
            _, whole = self._call("uid", "FETCH", str(int(uid)), "(BODY.PEEK[])")
            raw = _literal(whole)
        if not raw:
            return None
        mail = parse_message(raw, received=received, partial=partial)
        mail["uid"] = int(uid)
        mail["size"] = size
        return mail

    def newest_date(self, uid: int) -> datetime | None:
        """そのメールがサーバーに入った日時だけを取る（接続確認用。件名も本文も読まない）。"""
        status, data = self._call("uid", "FETCH", str(int(uid)), "(INTERNALDATE)")
        return _internal_date(_join_meta(data)) if status == "OK" else None

    def _call(self, method: str, *args: Any, **kwargs: Any) -> tuple[str, list]:
        try:
            status, data = getattr(self._conn, method)(*args, **kwargs)
        except imaplib.IMAP4.error as exc:
            raise MailboxError(FOLDER, f"{self.name} のメールサーバーが命令を受け付けませんでした（{method}）") from exc
        except OSError as exc:
            raise MailboxError(NETWORK, f"{self.name} のメールサーバーとの通信が切れました（{type(exc).__name__}）") from None
        return str(status), list(data or [])


def _to_int(value: Any) -> int:
    try:
        return int(value.decode() if isinstance(value, bytes) else value)
    except (TypeError, ValueError, AttributeError):
        return 0


def _join_meta(data: list) -> bytes:
    parts = []
    for entry in data or []:
        if isinstance(entry, tuple):
            parts.append(entry[0] if isinstance(entry[0], bytes) else b"")
        elif isinstance(entry, bytes):
            parts.append(entry)
    return b" ".join(parts)


def _literal(data: list) -> bytes:
    """FETCH の応答から本体のバイト列を取り出す。"""
    for entry in data or []:
        if isinstance(entry, tuple) and len(entry) > 1 and isinstance(entry[1], bytes):
            return entry[1]
    return b""


def _internal_date(meta: bytes) -> datetime | None:
    match = re.search(rb'INTERNALDATE "([^"]+)"', meta or b"")
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1).decode().strip(), "%d-%b-%Y %H:%M:%S %z").astimezone(JST)
    except ValueError:
        return None
