#!/usr/bin/env python3
"""生成客户专属访问链接（`/studio?phone=<11位手机号>&code=<32位hex>`）。

校验码 = `md5(手机号 + client_secret)`，**不带时间戳**：链接长期有效、可以转发，
要作废就轮换 secret。secret 的读取顺序与后端 `novelvideo.studio_access` 完全一致：

    --secret  >  环境变量 STUDIO_ACCESS_SECRET  >  secret 文件里的 client_secret=

正因如此，这个脚本既能直接在服务器上跑（读 /data/studio-access.secret），
也能在本地显式传 secret 生成同一条链接。

用法：
    # 服务器上（默认读 /data/studio-access.secret）
    python3 scripts/studio_link.py --phone 13800000000 --base https://example.com

    # 一条命令生成多个人的链接（每行一条，方便复制）
    python3 scripts/studio_link.py --phone 13800000000 --phone 13900000000 \\
        --base https://example.com

    # 本地临时生成（显式给 secret；base 也可以从 STUDIO_BASE_URL 读）
    STUDIO_ACCESS_SECRET=xxx python3 scripts/studio_link.py --phone 13800000000

退出码：0 成功；2 参数/配置错误（手机号不合法、找不到 secret）。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

PHONE_RE = re.compile(r"^\d{11}$")
DEFAULT_SECRET_FILE = "/data/studio-access.secret"


def normalize_phone(raw: str) -> str:
    """去掉空格/连字符/+86 前缀，返回 11 位手机号或空串。"""
    text = re.sub(r"[\s\-]", "", str(raw or ""))
    if text.startswith("+"):
        text = text[1:]
    if len(text) == 13 and text.startswith("86"):
        text = text[2:]
    return text if PHONE_RE.match(text) else ""


def read_secret_file(path: Path) -> str:
    """从 `key=value` 风格的 secret 文件里取 `client_secret`。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip().lower() == "client_secret":
            return value.strip()
    return ""


def resolve_secret(explicit: str, secret_file: str) -> str:
    if explicit.strip():
        return explicit.strip()
    from_env = os.environ.get("STUDIO_ACCESS_SECRET", "").strip()
    if from_env:
        return from_env
    return read_secret_file(Path(secret_file))


def client_code(phone: str, secret: str) -> str:
    """与后端 `studio_access.expected_code` 保持同一算法。"""
    return hashlib.md5(f"{phone}{secret}".encode("utf-8")).hexdigest()


def build_link(phone: str, code: str, base: str) -> str:
    root = base.strip().rstrip("/")
    return f"{root}/studio?phone={phone}&code={code}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成客户专属访问链接（/studio?phone=&code=）",
    )
    parser.add_argument(
        "--phone",
        action="append",
        required=True,
        metavar="13800000000",
        help="11 位手机号；可重复传入以一次生成多条链接",
    )
    parser.add_argument(
        "--base",
        default=os.environ.get("STUDIO_BASE_URL", "").strip(),
        help="站点根地址，例如 https://example.com（默认读 STUDIO_BASE_URL；留空则只输出路径）",
    )
    parser.add_argument(
        "--secret",
        default="",
        help="client secret；不传则读 STUDIO_ACCESS_SECRET 或 secret 文件",
    )
    parser.add_argument(
        "--secret-file",
        default=(
            os.environ.get("STUDIO_ACCESS_SECRET_FILE", "").strip() or DEFAULT_SECRET_FILE
        ),
        help=f"secret 文件路径（默认 {DEFAULT_SECRET_FILE}）",
    )
    return parser


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)

    secret = resolve_secret(args.secret, args.secret_file)
    if not secret:
        print(
            "找不到 client secret：请用 --secret，或设置 STUDIO_ACCESS_SECRET，"
            f"或在 {args.secret_file} 里写 client_secret=...",
            file=sys.stderr,
        )
        return 2

    for raw in args.phone:
        phone = normalize_phone(raw)
        if not phone:
            print(f"手机号必须是 11 位数字：{raw}", file=sys.stderr)
            return 2
        print(build_link(phone, client_code(phone, secret), args.base))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
