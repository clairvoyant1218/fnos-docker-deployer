#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
GITHUB_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


class DeployError(RuntimeError):
    pass


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_json(path: Path, data: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def run(cmd: list[str], cwd: Path | None = None) -> None:
    printable = " ".join(shlex.quote(item) for item in cmd)
    print(f"$ {printable}", flush=True)
    process = subprocess.run(cmd, cwd=cwd, text=True)
    if process.returncode != 0:
        raise DeployError(f"命令执行失败，退出码 {process.returncode}")


def lines(value: str) -> list[str]:
    result = []
    for raw in value.replace("\r", "").split("\n"):
        item = raw.strip()
        if item and not item.startswith("#"):
            result.append(item)
    return result


def validate_project(name: str) -> str:
    if not PROJECT_RE.fullmatch(name):
        raise DeployError("项目名只能包含字母、数字、点、下划线和短横线，最长 63 个字符")
    return name


def safe_project_dir(root: Path, name: str) -> Path:
    project_dir = (root / validate_project(name)).resolve()
    if project_dir.parent != root.resolve():
        raise DeployError("项目目录超出 /vol1/Docker")
    return project_dir


def parse_env(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in lines(value):
        if "=" not in item:
            raise DeployError(f"环境变量格式错误：{item}")
        key, val = item.split("=", 1)
        key = key.strip()
        if not ENV_RE.fullmatch(key):
            raise DeployError(f"环境变量名不合法：{key}")
        result[key] = val
    return result


def write_env(path: Path, env: dict[str, str]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix="env.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for key, value in env.items():
                escaped = value.replace("\\", "\\\\").replace("\n", "\\n")
                handle.write(f"{key}={escaped}\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def normalize_ports(value: str, lan_ip: str, allow_public: bool) -> list[str]:
    result = []
    ipaddress.ip_address(lan_ip)
    for item in lines(value):
        if not re.fullmatch(r"[A-Za-z0-9.:[\]-]+(?:/(?:tcp|udp))?", item):
            raise DeployError(f"端口格式不合法：{item}")
        base = item.rsplit("/", 1)[0]
        colon_count = base.count(":")
        if colon_count == 1:
            item = f"{lan_ip}:{item}"
        elif colon_count < 1:
            raise DeployError(f"端口应写成 主机端口:容器端口：{item}")
        if (item.startswith("0.0.0.0:") or item.startswith("::")) and not allow_public:
            raise DeployError("默认禁止绑定全部网络接口；确有需要时请勾选公开绑定确认")
        result.append(item)
    return result


def normalize_volumes(value: str) -> list[str]:
    result = []
    for item in lines(value):
        if "\x00" in item or "\n" in item:
            raise DeployError("目录映射包含非法字符")
        parts = item.split(":")
        if len(parts) not in (2, 3) or not parts[1].startswith("/"):
            raise DeployError(f"目录映射格式错误：{item}")
        if len(parts) == 3 and parts[2] not in ("ro", "rw", "z", "Z"):
            raise DeployError(f"目录映射模式不支持：{item}")
        result.append(item)
    return result


def normalize_devices(value: str) -> list[str]:
    result = []
    for item in lines(value):
        parts = item.split(":")
        if len(parts) not in (1, 2, 3) or not all(p.startswith("/dev/") or p in ("rwm", "rw", "r") for p in parts):
            raise DeployError(f"设备映射格式错误：{item}")
        result.append(item)
    return result


def deploy_image(spec: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    root = Path(settings["docker_root"])
    name = validate_project(str(spec.get("project", "")).strip())
    image = str(spec.get("image", "")).strip()
    if not IMAGE_RE.fullmatch(image):
        raise DeployError("镜像地址格式不合法")

    project_dir = safe_project_dir(root, name)
    metadata_path = project_dir / ".fnos-deployer.json"
    if project_dir.exists() and not metadata_path.exists():
        raise DeployError("同名目录已存在但不是本工具创建的项目，已停止以避免覆盖")
    if metadata_path.exists():
        raise DeployError("同名项目已存在，请使用更新操作")
    project_dir.mkdir(parents=True, mode=0o700)

    env = parse_env(str(spec.get("environment", "")))
    if env:
        write_env(project_dir / ".env", env)

    network_mode = str(spec.get("network", "bridge"))
    if network_mode not in ("bridge", "host", "none"):
        raise DeployError("网络模式不支持")
    restart = str(spec.get("restart", "unless-stopped"))
    if restart not in ("no", "always", "on-failure", "unless-stopped"):
        raise DeployError("重启策略不支持")

    service: dict[str, Any] = {
        "image": image,
        "container_name": name,
        "restart": restart,
    }
    if network_mode != "bridge":
        service["network_mode"] = network_mode
    ports = normalize_ports(
        str(spec.get("ports", "")),
        str(settings["lan_ip"]),
        bool(spec.get("allow_public", False)),
    )
    if ports and network_mode == "bridge":
        service["ports"] = ports
    volumes = normalize_volumes(str(spec.get("volumes", "")))
    if volumes:
        service["volumes"] = volumes
    devices = normalize_devices(str(spec.get("devices", "")))
    if devices:
        service["devices"] = devices
    if env:
        service["env_file"] = [".env"]
    command = str(spec.get("command", "")).strip()
    if command:
        service["command"] = shlex.split(command)
    if bool(spec.get("run_as_root", False)):
        service["user"] = "0:0"
    if bool(spec.get("privileged", False)):
        service["privileged"] = True

    compose: dict[str, Any] = {
        "name": name,
        "services": {"app": service},
    }
    named_volumes = {}
    for volume in volumes:
        source = volume.split(":", 1)[0]
        if not source.startswith(("/", ".")):
            named_volumes[source] = {}
    if named_volumes:
        compose["volumes"] = named_volumes
    compose_path = project_dir / "compose.yaml"
    with compose_path.open("w", encoding="utf-8") as handle:
        json.dump(compose, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.chmod(compose_path, 0o600)

    metadata = {
        "version": 1,
        "kind": "image",
        "project": name,
        "image": image,
        "compose_file": str(compose_path),
        "created_at": now(),
        "allow_public": bool(spec.get("allow_public", False)),
        "run_as_root": bool(spec.get("run_as_root", False)),
        "privileged": bool(spec.get("privileged", False)),
    }
    atomic_json(metadata_path, metadata)
    try:
        run(["docker", "compose", "-p", name, "-f", str(compose_path), "pull"], cwd=project_dir)
        run(["docker", "compose", "-p", name, "-f", str(compose_path), "up", "-d"], cwd=project_dir)
    except Exception:
        metadata["last_error_at"] = now()
        atomic_json(metadata_path, metadata)
        raise
    return metadata


def find_compose(source: Path, requested: str) -> Path:
    if requested:
        candidate = (source / requested).resolve()
        if source.resolve() not in candidate.parents:
            raise DeployError("Compose 路径超出仓库目录")
        if not candidate.is_file():
            raise DeployError(f"没有找到指定的 Compose 文件：{requested}")
        return candidate
    names = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
    for directory in (source, source / "docker", source / "deploy", source / "deployment"):
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return candidate.resolve()
    raise DeployError("仓库中未找到 Compose 文件；请在高级设置中填写相对路径")


def deploy_github(spec: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    root = Path(settings["docker_root"])
    name = validate_project(str(spec.get("project", "")).strip())
    repo = str(spec.get("repo", "")).strip()
    match = GITHUB_RE.fullmatch(repo)
    if not match:
        raise DeployError("当前版本只接受公开的 https://github.com/作者/仓库 地址")
    repo = f"https://github.com/{match.group(1)}/{match.group(2)}.git"
    ref = str(spec.get("ref", "")).strip()
    if ref and not REF_RE.fullmatch(ref):
        raise DeployError("分支或标签名称不合法")

    project_dir = safe_project_dir(root, name)
    metadata_path = project_dir / ".fnos-deployer.json"
    if project_dir.exists():
        raise DeployError("同名目录或项目已存在，已停止以避免覆盖")
    project_dir.mkdir(parents=True, mode=0o700)
    source = project_dir / "source"
    clone = ["git", "clone", "--depth", "1"]
    if ref:
        clone += ["--branch", ref]
    clone += [repo, str(source)]
    run(clone, cwd=project_dir)

    compose_path = find_compose(source, str(spec.get("compose_path", "")).strip())
    env = parse_env(str(spec.get("environment", "")))
    if env:
        write_env(compose_path.parent / ".env", env)

    metadata = {
        "version": 1,
        "kind": "github",
        "project": name,
        "repo": repo,
        "ref": ref,
        "compose_file": str(compose_path),
        "created_at": now(),
    }
    atomic_json(metadata_path, metadata)
    try:
        run(
            ["docker", "compose", "-p", name, "-f", str(compose_path), "up", "-d", "--build", "--pull", "always"],
            cwd=compose_path.parent,
        )
    except Exception:
        metadata["last_error_at"] = now()
        atomic_json(metadata_path, metadata)
        raise
    return metadata


def project_action(spec: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    root = Path(settings["docker_root"])
    name = validate_project(str(spec.get("project", "")).strip())
    action = str(spec.get("project_action", ""))
    if action not in ("start", "stop", "restart", "update"):
        raise DeployError("不支持的项目操作")
    project_dir = safe_project_dir(root, name)
    metadata_path = project_dir / ".fnos-deployer.json"
    if not metadata_path.is_file():
        raise DeployError("项目记录不存在")
    metadata = load_json(metadata_path)
    compose_path = Path(metadata["compose_file"]).resolve()
    if project_dir.resolve() not in compose_path.parents:
        raise DeployError("Compose 路径超出项目目录")
    if not compose_path.is_file():
        raise DeployError("Compose 文件不存在")

    base = ["docker", "compose", "-p", name, "-f", str(compose_path)]
    if action == "start":
        run(base + ["up", "-d"], cwd=compose_path.parent)
    elif action == "stop":
        run(base + ["stop"], cwd=compose_path.parent)
    elif action == "restart":
        run(base + ["restart"], cwd=compose_path.parent)
    else:
        if metadata.get("kind") == "github":
            source = project_dir / "source"
            run(["git", "pull", "--ff-only"], cwd=source)
            compose_path = find_compose(source, os.path.relpath(compose_path, source))
        run(base + ["up", "-d", "--build", "--pull", "always"], cwd=compose_path.parent)
    metadata["last_action"] = action
    metadata["last_action_at"] = now()
    atomic_json(metadata_path, metadata)
    return metadata


def ai_assist(spec: dict[str, Any], ai_config_path: Path) -> dict[str, Any]:
    config = load_json(ai_config_path)
    base_url = str(config.get("base_url", "")).strip().rstrip("/")
    api_key = str(config.get("api_key", ""))
    model = str(config.get("model", "")).strip()
    if not base_url or not api_key or not model:
        raise DeployError("请先在首页保存 AI 中转地址、API Key 和模型名")

    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise DeployError("AI 中转地址必须是有效的 HTTP(S) 地址")
    if parsed.scheme == "http":
        host = parsed.hostname
        try:
            local_http = host in ("localhost", "127.0.0.1", "::1") or ipaddress.ip_address(host).is_private
        except ValueError:
            local_http = False
        if not local_http:
            raise DeployError("公网 AI 中转地址必须使用 HTTPS")

    endpoint = base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"
    source = str(spec.get("source", "")).strip()
    goal = str(spec.get("goal", "")).strip()
    if not source or not goal:
        raise DeployError("请填写镜像/仓库地址和部署目标")
    if len(source) > 1000 or len(goal) > 8000:
        raise DeployError("AI 辅助输入过长")

    system_prompt = """你是 fnOS Docker 部署顾问。请用简体中文输出安全、可复核的部署草稿，不要声称已执行任何操作。目标系统使用 Docker Compose，项目保存在 /vol1/Docker。优先给出固定镜像版本；端口默认绑定 NAS 局域网 IP；明确列出端口、卷、环境变量、设备、网络、root 和 privileged 是否需要。不要编造未知的必填参数，无法确认时标注需要查阅上游 README。最后给出可复制到分步表单的字段清单。"""
    user_prompt = f"来源：{source}\n用户目标：{goal}"
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    print("正在请求 AI 生成部署草稿……", flush=True)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read(4 * 1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise DeployError(f"AI 中转返回 HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise DeployError(f"AI 请求失败：{exc}") from exc
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise DeployError("AI 返回格式不是兼容的 Chat Completions 响应") from exc
    if not isinstance(content, str) or not content.strip():
        raise DeployError("AI 没有返回可用内容")
    print("\n===== AI 部署草稿（尚未执行）=====\n", flush=True)
    print(content.strip(), flush=True)
    print("\n===== 草稿结束，请人工核对后再填入部署表单 =====", flush=True)
    return {"kind": "ai_draft", "source": source, "model": model, "generated_at": now()}


def main() -> int:
    if len(sys.argv) != 2:
        print("用法：worker.py JOB.json", file=sys.stderr)
        return 2
    job_path = Path(sys.argv[1]).resolve()
    job = load_json(job_path)
    settings = load_json(Path(job["settings"]))
    status_path = Path(job["status"])
    status = {
        "id": job["id"],
        "state": "running",
        "title": job.get("title", "部署任务"),
        "started_at": now(),
    }
    atomic_json(status_path, status)
    try:
        action = job["action"]
        if action == "deploy_image":
            result = deploy_image(job["spec"], settings)
        elif action == "deploy_github":
            result = deploy_github(job["spec"], settings)
        elif action == "project_action":
            result = project_action(job["spec"], settings)
        elif action == "ai_assist":
            result = ai_assist(job["spec"], Path(job["ai_config"]))
        else:
            raise DeployError("未知任务类型")
        status.update({"state": "success", "finished_at": now(), "result": result})
        atomic_json(status_path, status)
        print("任务完成。", flush=True)
        return 0
    except Exception as exc:
        status.update({"state": "failed", "finished_at": now(), "error": str(exc)})
        atomic_json(status_path, status)
        print(f"错误：{exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
