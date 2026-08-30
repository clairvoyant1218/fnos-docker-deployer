#!/usr/bin/env python3
import argparse
import ipaddress
import json
import os
import subprocess
import tempfile


def detect_lan_ip() -> str:
    try:
        output = subprocess.check_output(
            ["ip", "-o", "-4", "route", "show", "default"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        parts = output.split()
        if "src" in parts:
            return parts[parts.index("src") + 1]
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        pass
    return "127.0.0.1"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--lan-ip", default="")
    parser.add_argument("--settings", required=True)
    args = parser.parse_args()

    root = os.path.normpath(args.root)
    if root != "/vol1/Docker":
        raise SystemExit("当前版本只允许使用 /vol1/Docker")

    lan_ip = args.lan_ip.strip() or detect_lan_ip()
    ipaddress.ip_address(lan_ip)
    if not ipaddress.ip_address(lan_ip).is_private:
        raise SystemExit("默认绑定地址必须是局域网 IP")

    data = {
        "docker_root": root,
        "lan_ip": lan_ip,
        "version": 1,
    }
    os.makedirs(os.path.dirname(args.settings), mode=0o700, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix="settings.", dir=os.path.dirname(args.settings))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, args.settings)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


if __name__ == "__main__":
    main()
