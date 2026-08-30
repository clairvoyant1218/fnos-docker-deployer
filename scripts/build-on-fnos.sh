#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
version="$(awk -F= '$1 == "version" {print $2; exit}' "$repo_root/manifest")"
[ -n "$version" ] || { echo '无法从 manifest 读取版本号' >&2; exit 1; }

command -v fnpack >/dev/null 2>&1 || { echo '未找到 fnpack，请在 fnOS 上运行本脚本。' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo '未找到 Python 3。' >&2; exit 1; }

chmod 0700 "$repo_root"/cmd/* "$repo_root"/app/scripts/*.py
chmod 0755 "$repo_root/app/ui/index.cgi"
chmod 0644 "$repo_root/app/ui/config" "$repo_root"/app/ui/images/*.png
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s "$repo_root/tests" -v
python3 - "$repo_root" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
for relative in ('app/scripts/worker.py', 'app/scripts/write_settings.py', 'app/ui/index.cgi'):
    path = root / relative
    compile(path.read_text(encoding='utf-8'), str(path), 'exec')
PY
find "$repo_root" -type d -name __pycache__ -prune -exec rm -rf -- {} +

mkdir -p "$repo_root/dist"
cd "$repo_root"
fnpack build --directory "$repo_root"
mv -f "$repo_root/fnos-docker-deployer.fpk" "$repo_root/dist/fnos-docker-deployer-${version}.fpk"
cd "$repo_root/dist"
sha256sum "fnos-docker-deployer-${version}.fpk" > SHA256SUMS
echo "构建完成：$repo_root/dist/fnos-docker-deployer-${version}.fpk"
