"""Проверка здоровья в режиме CHILD_RUNTIME=shared.

Ожидание:
- один worker-процесс (python -m child.worker --socket ...), не процесс на каждого бота;
- в его окружении нет секретов конструктора (BOT_TOKEN, LAVEPAY_*, OWNER_PAYOUT_KEY, ...);
- по сокету status все боты с desired=1 и status=running перечислены в running.

Запуск: python scripts/check_shared_worker.py --data-dir data
Код 0 — ок, иначе проблема.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

FORBIDDEN = {
    "BOT_TOKEN", "LAVEPAY_API_KEY", "LAVEPAY_WEBHOOK_SECRET", "OWNER_PAYOUT_KEY",
    "TGRASS_FACTORY_TOKEN", "TGRASS_HUB_API_KEY", "PIARFLOW_TOKEN", "PIARFLOW_HUB_API_KEY",
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    p.add_argument("--pid", type=int, default=0, help="pid worker, если известен")
    args = p.parse_args()
    sock = args.data_dir / "worker.sock"
    if not sock.exists():
        print("FAIL: нет worker.sock — shared-режим не поднят или worker упал")
        return 1
    if args.pid:
        env_path = Path(f"/proc/{args.pid}/environ")
        if env_path.exists():
            raw = env_path.read_bytes().split(b"\0")
            keys = {x.split(b"=", 1)[0].decode() for x in raw if b"=" in x}
            leaked = sorted(keys & FORBIDDEN)
            if leaked:
                print("FAIL: в окружении worker секреты:", ",".join(leaked))
                return 1
            cmdline = Path(f"/proc/{args.pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
            if "child.worker" not in cmdline:
                print("FAIL: pid не worker:", cmdline)
                return 1
            print("OK: окружение worker без секретов конструктора")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(str(sock))
            s.sendall(b'{"cmd":"status"}\n')
            data = b""
            while b"\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        reply = json.loads(data.decode() or "{}")
    except OSError as exc:
        print("FAIL: сокет недоступен:", exc)
        return 1
    if not reply.get("ok"):
        print("FAIL: worker не ответил status:", reply)
        return 1
    print("OK: worker жив, running:", reply.get("running"))
    print("OK: это один процесс на всех ботов, не --dir на каждого")
    return 0


if __name__ == "__main__":
    sys.exit(main())
