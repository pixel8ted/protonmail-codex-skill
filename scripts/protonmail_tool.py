#!/usr/bin/env python3
"""Small Proton Mail Bridge IMAP/SMTP helper.

Reads connection settings from PROTONMAIL_* environment variables by default.
Designed for local Proton Mail Bridge use, not direct Proton account login.
"""

from __future__ import annotations

import argparse
import email
import getpass
import html
import imaplib
import os
import re
import shlex
import smtplib
import ssl
import sys
from datetime import datetime
from email.message import EmailMessage
from email.policy import default
from email.utils import formatdate, make_msgid


DEFAULT_IMAP_PORT = 1143
DEFAULT_SMTP_PORT = 1025
DEFAULT_CONFIG = "~/.config/codex_skills/protonmail-bridge.env"
FALLBACK_CONFIGS = ("~/.config/codex_skills/proton-bridge.env",)
CONFIG_VALUES: dict[str, str] | None = None


def parse_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    expanded = os.path.expanduser(path)
    if not os.path.exists(expanded):
        return values
    with open(expanded, "r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            if key:
                values[key] = value
    return values


def env(name: str, default_value: str | None = None) -> str | None:
    if name in os.environ:
        return os.environ[name]
    global CONFIG_VALUES
    if CONFIG_VALUES is None:
        config_path = os.environ.get("PROTONMAIL_CONFIG", DEFAULT_CONFIG)
        CONFIG_VALUES = parse_env_file(config_path)
        if not CONFIG_VALUES and "PROTONMAIL_CONFIG" not in os.environ:
            for fallback in FALLBACK_CONFIGS:
                CONFIG_VALUES = parse_env_file(fallback)
                if CONFIG_VALUES:
                    break
    return CONFIG_VALUES.get(name, default_value)


def is_local_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def tls_context(host: str, verify: bool) -> ssl.SSLContext:
    if verify:
        return ssl.create_default_context()
    if not is_local_host(host):
        raise SystemExit("Refusing unverified TLS for non-local host.")
    return ssl._create_unverified_context()


def credentials(args: argparse.Namespace) -> tuple[str, str]:
    username = args.username or env("PROTONMAIL_USERNAME")
    password = env("PROTONMAIL_PASSWORD")
    if not username:
        username = input("Bridge username: ").strip()
    if not password:
        password = getpass.getpass("Bridge password: ")
    return username, password


def connect_imap(args: argparse.Namespace) -> imaplib.IMAP4:
    host = args.imap_host or env("PROTONMAIL_IMAP_HOST", "127.0.0.1")
    port = int(args.imap_port or env("PROTONMAIL_IMAP_PORT", str(DEFAULT_IMAP_PORT)))
    username, password = credentials(args)
    imap = imaplib.IMAP4(host, port)
    if not args.no_starttls:
        imap.starttls(ssl_context=tls_context(host, not args.local_bridge_tls))
    imap.login(username, password)
    return imap


def connect_smtp(args: argparse.Namespace) -> smtplib.SMTP:
    host = args.smtp_host or env("PROTONMAIL_SMTP_HOST", "127.0.0.1")
    port = int(args.smtp_port or env("PROTONMAIL_SMTP_PORT", str(DEFAULT_SMTP_PORT)))
    username, password = credentials(args)
    smtp = smtplib.SMTP(host, port, timeout=30)
    smtp.ehlo()
    if not args.no_starttls:
        smtp.starttls(context=tls_context(host, not args.local_bridge_tls))
        smtp.ehlo()
    smtp.login(username, password)
    return smtp


def decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    decoded: list[str] = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?</\\1>", " ", value)
    value = re.sub(r"(?i)<br\\s*/?>", "\n", value)
    value = re.sub(r"(?i)</p>", "\n\n", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"[ \t]+", " ", value).strip()


def message_text(msg: email.message.EmailMessage) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = (part.get_content_disposition() or "").lower()
            if disposition == "attachment":
                continue
            try:
                content = part.get_content()
            except Exception:
                continue
            if content_type == "text/plain":
                plain_parts.append(str(content))
            elif content_type == "text/html":
                html_parts.append(html_to_text(str(content)))
    else:
        content = msg.get_content()
        if msg.get_content_type() == "text/html":
            html_parts.append(html_to_text(str(content)))
        else:
            plain_parts.append(str(content))
    return "\n\n".join(plain_parts or html_parts).strip()


def attachment_summary(msg: email.message.EmailMessage) -> list[dict[str, str | int]]:
    attachments = []
    for part in msg.walk():
        if part.get_content_disposition() == "attachment":
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                {
                    "filename": part.get_filename() or "(unnamed)",
                    "content_type": part.get_content_type(),
                    "size": len(payload),
                }
            )
    return attachments


def imap_date(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d")
    return parsed.strftime("%d-%b-%Y")


def search_tokens(query: str) -> list[str]:
    if not query:
        return ["ALL"]
    tokens = []
    for raw in shlex.split(query):
        key, sep, value = raw.partition(":")
        key = key.lower()
        if not sep:
            if raw.lower() in {"unseen", "seen", "flagged"}:
                tokens.append(raw.upper())
            else:
                tokens.extend(["TEXT", value_or_raw(raw)])
            continue
        if key == "from":
            tokens.extend(["FROM", value])
        elif key == "to":
            tokens.extend(["TO", value])
        elif key == "subject":
            tokens.extend(["SUBJECT", value])
        elif key == "body":
            tokens.extend(["BODY", value])
        elif key == "text":
            tokens.extend(["TEXT", value])
        elif key == "since":
            tokens.extend(["SINCE", imap_date(value)])
        elif key == "before":
            tokens.extend(["BEFORE", imap_date(value)])
        else:
            tokens.extend(["TEXT", raw])
    return tokens or ["ALL"]


def value_or_raw(raw: str) -> str:
    return raw


def list_folders(args: argparse.Namespace) -> int:
    imap = connect_imap(args)
    try:
        status, folders = imap.list()
        if status != "OK":
            raise SystemExit(f"LIST failed: {status}")
        for folder in folders or []:
            print(folder.decode(errors="replace"))
    finally:
        imap.logout()
    return 0


def search(args: argparse.Namespace) -> int:
    imap = connect_imap(args)
    try:
        imap.select(args.mailbox, readonly=True)
        status, data = imap.uid("SEARCH", None, *search_tokens(args.query))
        if status != "OK":
            raise SystemExit(f"SEARCH failed: {status}")
        uids = data[0].decode().split()
        for uid in uids[-args.limit :]:
            status, msg_data = imap.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)] RFC822.SIZE)")
            if status != "OK":
                continue
            header_bytes = next(item[1] for item in msg_data if isinstance(item, tuple))
            msg = email.message_from_bytes(header_bytes, policy=default)
            print(f"UID: {uid}")
            print(f"Date: {decode_header_value(msg.get('Date'))}")
            print(f"From: {decode_header_value(msg.get('From'))}")
            print(f"To: {decode_header_value(msg.get('To'))}")
            print(f"Subject: {decode_header_value(msg.get('Subject'))}")
            print()
    finally:
        imap.logout()
    return 0


def read(args: argparse.Namespace) -> int:
    imap = connect_imap(args)
    try:
        imap.select(args.mailbox, readonly=True)
        status, msg_data = imap.uid("FETCH", str(args.uid), "(BODY.PEEK[])")
        if status != "OK":
            raise SystemExit(f"FETCH failed: {status}")
        raw = next(item[1] for item in msg_data if isinstance(item, tuple))
        msg = email.message_from_bytes(raw, policy=default)
        print(f"Date: {decode_header_value(msg.get('Date'))}")
        print(f"From: {decode_header_value(msg.get('From'))}")
        print(f"To: {decode_header_value(msg.get('To'))}")
        print(f"Cc: {decode_header_value(msg.get('Cc'))}")
        print(f"Subject: {decode_header_value(msg.get('Subject'))}")
        print(f"Message-ID: {decode_header_value(msg.get('Message-ID'))}")
        attachments = attachment_summary(msg)
        if attachments:
            print("Attachments:")
            for item in attachments:
                print(f"- {item['filename']} ({item['content_type']}, {item['size']} bytes)")
        print("\n--- Body ---\n")
        print(message_text(msg))
    finally:
        imap.logout()
    return 0


def move(args: argparse.Namespace) -> int:
    imap = connect_imap(args)
    moved = 0
    try:
        imap.select(args.mailbox, readonly=False)
        for uid in args.uid:
            status, _ = imap.uid("COPY", str(uid), args.destination)
            if status != "OK":
                print(f"UID {uid}: copy to {args.destination} failed", file=sys.stderr)
                continue
            status, _ = imap.uid("STORE", str(uid), "+FLAGS.SILENT", r"(\Deleted)")
            if status != "OK":
                print(f"UID {uid}: copied but could not mark original deleted", file=sys.stderr)
                continue
            moved += 1
        if moved and not args.no_expunge:
            imap.expunge()
    finally:
        imap.logout()
    print(f"Moved {moved} message(s) from {args.mailbox} to {args.destination}")
    return 0 if moved == len(args.uid) else 1


def read_body(args: argparse.Namespace) -> str:
    if args.body:
        return args.body
    if args.body_file:
        with open(args.body_file, "r", encoding="utf-8") as handle:
            return handle.read()
    return sys.stdin.read()


def send(args: argparse.Namespace) -> int:
    username, _ = credentials(args)
    from_addr = args.from_addr or env("PROTONMAIL_FROM") or username
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(args.to)
    if args.cc:
        msg["Cc"] = ", ".join(args.cc)
    if args.bcc:
        msg["Bcc"] = ", ".join(args.bcc)
    if args.reply_to:
        msg["Reply-To"] = args.reply_to
    msg["Subject"] = args.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(read_body(args))

    recipients = args.to + (args.cc or []) + (args.bcc or [])
    smtp = connect_smtp(args)
    try:
        smtp.send_message(msg, from_addr=from_addr, to_addrs=recipients)
    finally:
        smtp.quit()
    print(f"Sent message to {', '.join(args.to)}")
    return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--username", help="Bridge username. Prefer PROTONMAIL_USERNAME.")
    parser.add_argument("--imap-host", help="IMAP host. Defaults to PROTONMAIL_IMAP_HOST or 127.0.0.1.")
    parser.add_argument("--imap-port", type=int, help="IMAP port. Defaults to PROTONMAIL_IMAP_PORT or 1143.")
    parser.add_argument("--smtp-host", help="SMTP host. Defaults to PROTONMAIL_SMTP_HOST or 127.0.0.1.")
    parser.add_argument("--smtp-port", type=int, help="SMTP port. Defaults to PROTONMAIL_SMTP_PORT or 1025.")
    parser.add_argument("--no-starttls", action="store_true", help="Disable STARTTLS.")
    parser.add_argument(
        "--local-bridge-tls",
        action="store_true",
        default=True,
        help="Allow local self-signed Bridge TLS certificates for localhost.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Query/read/send mail through Proton Mail Bridge.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    folder_parser = subparsers.add_parser("list-folders")
    add_common(folder_parser)
    folder_parser.set_defaults(func=list_folders)

    search_parser = subparsers.add_parser("search")
    add_common(search_parser)
    search_parser.add_argument("--mailbox", default="INBOX")
    search_parser.add_argument("--query", default="")
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.set_defaults(func=search)

    read_parser = subparsers.add_parser("read")
    add_common(read_parser)
    read_parser.add_argument("--mailbox", default="INBOX")
    read_parser.add_argument("--uid", required=True)
    read_parser.set_defaults(func=read)

    move_parser = subparsers.add_parser("move")
    add_common(move_parser)
    move_parser.add_argument("--mailbox", default="INBOX")
    move_parser.add_argument("--destination", default="Trash")
    move_parser.add_argument("--uid", action="append", required=True)
    move_parser.add_argument("--no-expunge", action="store_true", help="Copy and mark deleted without expunging.")
    move_parser.set_defaults(func=move)

    send_parser = subparsers.add_parser("send")
    add_common(send_parser)
    send_parser.add_argument("--from", dest="from_addr")
    send_parser.add_argument("--to", action="append", required=True)
    send_parser.add_argument("--cc", action="append")
    send_parser.add_argument("--bcc", action="append")
    send_parser.add_argument("--reply-to")
    send_parser.add_argument("--subject", required=True)
    body_group = send_parser.add_mutually_exclusive_group()
    body_group.add_argument("--body")
    body_group.add_argument("--body-file")
    send_parser.set_defaults(func=send)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
