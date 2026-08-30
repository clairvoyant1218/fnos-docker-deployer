#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import os
import secrets
import subprocess
import sys
import urllib.parse
import uuid
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
WORKER = APP_ROOT / "scripts" / "worker.py"
DEFAULT_ROOT = Path("/vol1/Docker")
STATE_ROOT = DEFAULT_ROOT / ".fnos-docker-deployer"
SETTINGS_PATH = STATE_ROOT / "settings.json"
JOBS_ROOT = STATE_ROOT / "jobs"
CSRF_PATH = STATE_ROOT / "csrf-token"
AI_CONFIG_PATH = STATE_ROOT / "ai.json"


def e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def response(body: str, status: str = "200 OK") -> None:
    print(f"Status: {status}")
    print("Content-Type: text/html; charset=utf-8")
    print("Cache-Control: no-store")
    print("X-Content-Type-Options: nosniff")
    print("X-Frame-Options: SAMEORIGIN")
    print("Referrer-Policy: no-referrer")
    print("Content-Security-Policy: default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'self'")
    print("")
    print(body)


def redirect(location: str) -> None:
    print("Status: 303 See Other")
    print(f"Location: {location}")
    print("Cache-Control: no-store")
    print("")


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return default


def settings() -> dict[str, Any]:
    data = load_json(SETTINGS_PATH, {})
    return {
        "docker_root": data.get("docker_root", str(DEFAULT_ROOT)),
        "lan_ip": data.get("lan_ip", "127.0.0.1"),
    }


def csrf_token() -> str:
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    if CSRF_PATH.is_file():
        return CSRF_PATH.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    fd = os.open(CSRF_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
    return token


def parse_post() -> dict[str, str]:
    try:
        length = int(os.environ.get("CONTENT_LENGTH", "0"))
    except ValueError:
        length = 0
    if length < 1 or length > 131072:
        raise ValueError("请求内容为空或过大")
    body = sys.stdin.buffer.read(length).decode("utf-8")
    parsed = urllib.parse.parse_qs(body, keep_blank_values=True, strict_parsing=False)
    return {key: values[-1] for key, values in parsed.items()}


def bool_field(data: dict[str, str], key: str) -> bool:
    return data.get(key) in ("1", "true", "on", "yes")


def save_ai_config(data: dict[str, str]) -> None:
    base_url = data.get("ai_base_url", "").strip().rstrip("/")
    model = data.get("ai_model", "").strip()
    api_key = data.get("ai_api_key", "")
    if not base_url.startswith(("https://", "http://")) or len(base_url) > 1000:
        raise ValueError("AI 中转地址必须是有效的 HTTP(S) 地址")
    if not model or len(model) > 200:
        raise ValueError("请填写模型名")
    old = load_json(AI_CONFIG_PATH, {})
    if not api_key:
        api_key = str(old.get("api_key", ""))
    if not api_key:
        raise ValueError("首次设置必须填写 API Key")
    payload = {"base_url": base_url, "model": model, "api_key": api_key}
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp_path = AI_CONFIG_PATH.with_suffix(".tmp")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp_path, AI_CONFIG_PATH)


def submit_job(action: str, title: str, spec: dict[str, Any]) -> str:
    JOBS_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    job_id = uuid.uuid4().hex
    job_path = JOBS_ROOT / f"{job_id}.job.json"
    status_path = JOBS_ROOT / f"{job_id}.status.json"
    log_path = JOBS_ROOT / f"{job_id}.log"
    payload = {
        "id": job_id,
        "action": action,
        "title": title,
        "settings": str(SETTINGS_PATH),
        "ai_config": str(AI_CONFIG_PATH),
        "status": str(status_path),
        "spec": spec,
    }
    fd = os.open(job_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    log_handle = open(log_path, "ab", buffering=0)
    subprocess.Popen(
        [sys.executable, str(WORKER), str(job_path)],
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    log_handle.close()
    return job_id


def project_status(name: str) -> tuple[str, str]:
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={name}", "--format", "{{.Status}}"],
            text=True,
            capture_output=True,
            timeout=5,
        )
        states = [line for line in result.stdout.splitlines() if line]
        if not states:
            return "未创建", "muted"
        if all(line.startswith("Up") for line in states):
            return "运行中", "ok"
        if any(line.startswith("Up") for line in states):
            return "部分运行", "warn"
        return "已停止", "off"
    except (OSError, subprocess.SubprocessError):
        return "未知", "warn"


def list_projects(root: Path) -> list[dict[str, Any]]:
    projects = []
    try:
        entries = sorted(root.iterdir(), key=lambda path: path.name.lower())
    except OSError:
        return projects
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        metadata_path = entry / ".fnos-deployer.json"
        if not metadata_path.is_file():
            continue
        metadata = load_json(metadata_path, {})
        state, css = project_status(entry.name)
        metadata.update({"status": state, "status_css": css, "dir": str(entry)})
        projects.append(metadata)
    return projects


def list_jobs() -> list[dict[str, Any]]:
    jobs = []
    try:
        paths = sorted(JOBS_ROOT.glob("*.status.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:8]
    except OSError:
        return jobs
    for path in paths:
        item = load_json(path, {})
        item["id"] = item.get("id", path.name.split(".")[0])
        jobs.append(item)
    return jobs


STYLE = """
:root{color-scheme:dark;--bg:#0b1020;--panel:#141b2d;--panel2:#19243a;--line:#2b3955;--text:#eef4ff;--muted:#9eb0ca;--blue:#4f8cff;--green:#39d98a;--amber:#ffbe55;--red:#ff6b7a}*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#0b1020,#10182a);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;color:var(--text)}main{max-width:1120px;margin:auto;padding:28px 22px 64px}h1{font-size:28px;margin:0 0 7px}.sub{color:var(--muted);margin:0 0 24px}.hero,.panel,.card{background:rgba(20,27,45,.94);border:1px solid var(--line);border-radius:16px}.hero{padding:22px;margin-bottom:18px;display:flex;gap:16px;justify-content:space-between;align-items:center}.tag{display:inline-block;border-radius:999px;padding:5px 10px;background:#203459;color:#aaccff;font-size:13px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}.panel{padding:20px;margin:18px 0}.card{padding:17px}.card h3{margin:0 0 8px}.status{font-size:13px;border-radius:99px;padding:4px 9px}.status.ok{background:#163b31;color:#72e6b1}.status.warn{background:#43351e;color:#ffd079}.status.off,.status.muted{background:#283248;color:#b8c5d9}.status.failed{background:#4b2530;color:#ff9aaa}.status.success{background:#163b31;color:#72e6b1}.status.running{background:#233b68;color:#91b9ff}details{border-top:1px solid var(--line);padding-top:14px;margin-top:14px}summary{cursor:pointer;color:#bcd2ff;font-weight:600}label{display:block;font-size:14px;color:#c9d5e8;margin:14px 0 6px}input,textarea,select{width:100%;background:#0d1424;border:1px solid #344563;border-radius:9px;color:var(--text);padding:10px 11px;font:inherit}textarea{min-height:86px;resize:vertical}.check{display:flex;gap:8px;align-items:flex-start;color:#cbd8ec}.check input{width:auto;margin-top:3px}.hint{font-size:12px;color:var(--muted);margin-top:5px}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:15px}button,.button{border:0;border-radius:9px;background:var(--blue);color:white;padding:9px 14px;font-weight:600;cursor:pointer;text-decoration:none;font-size:14px}.secondary{background:#293750;color:#d7e4f8}.danger{background:#74313c}.cols{display:grid;grid-template-columns:1fr 1fr;gap:12px}.path{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;color:#c9dcff;word-break:break-all}.empty{color:var(--muted);text-align:center;padding:22px}nav{display:flex;gap:9px;margin:18px 0}.notice{border-left:4px solid var(--amber);padding:10px 13px;background:#2c251a;color:#ffe0a3;border-radius:8px}pre{white-space:pre-wrap;word-break:break-word;background:#080d18;border:1px solid var(--line);padding:14px;border-radius:10px;max-height:460px;overflow:auto}@media(max-width:700px){.cols{grid-template-columns:1fr}.hero{align-items:flex-start;flex-direction:column}}
"""


def page(title: str, content: str, refresh: int | None = None) -> str:
    refresh_tag = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{refresh_tag}<title>{e(title)}</title><style>{STYLE}</style></head><body><main>{content}</main></body></html>"""


def dashboard(message: str = "") -> str:
    cfg = settings()
    token = csrf_token()
    ai_cfg = load_json(AI_CONFIG_PATH, {})
    ai_ready = bool(ai_cfg.get("base_url") and ai_cfg.get("model") and ai_cfg.get("api_key"))
    ai_state = "已配置" if ai_ready else "未配置"
    root = Path(cfg["docker_root"])
    projects = list_projects(root)
    jobs = list_jobs()
    cards = []
    for item in projects:
        name = item.get("project", Path(item["dir"]).name)
        source = item.get("image") if item.get("kind") == "image" else item.get("repo")
        cards.append(f"""
<article class="card"><div style="display:flex;justify-content:space-between;gap:8px"><h3>{e(name)}</h3><span class="status {e(item['status_css'])}">{e(item['status'])}</span></div><div class="hint">{e('镜像项目' if item.get('kind') == 'image' else 'GitHub Compose')}</div><p class="path">{e(source or '')}</p><p class="hint">{e(item['dir'])}</p><form method="post" class="actions"><input type="hidden" name="csrf" value="{e(token)}"><input type="hidden" name="action" value="project_action"><input type="hidden" name="project" value="{e(name)}"><button name="project_action" value="start">启动</button><button class="secondary" name="project_action" value="restart">重启</button><button class="secondary" name="project_action" value="update">拉取更新</button><button class="danger" name="project_action" value="stop">停止</button></form></article>""")
    project_html = "".join(cards) or '<div class="empty">还没有通过本工具创建项目。</div>'
    job_rows = []
    for item in jobs:
        state = item.get("state", "running")
        labels = {"running": "进行中", "success": "已完成", "failed": "失败"}
        job_rows.append(f'<article class="card"><div style="display:flex;justify-content:space-between"><strong>{e(item.get("title","任务"))}</strong><span class="status {e(state)}">{e(labels.get(state,state))}</span></div><div class="actions"><a class="button secondary" href="?job={e(item["id"])}">查看日志</a></div></article>')
    jobs_html = "".join(job_rows) or '<div class="empty">暂无部署任务。</div>'
    msg = f'<p class="notice">{e(message)}</p>' if message else ""
    content = f"""
<section class="hero"><div><h1>飞牛 Docker 部署器</h1><p class="sub">任意镜像 · GitHub Compose · 中文分步部署</p><span class="tag">项目根目录 {e(cfg['docker_root'])}</span> <span class="tag">默认局域网 IP {e(cfg['lan_ip'])}</span></div><div class="tag">root 管理 Docker</div></section>{msg}
<section class="panel"><h2>新建项目</h2><p class="sub">选择一种来源。提交后在后台拉取，不需要停留在页面等待。</p><div class="grid">
<article class="card"><h3>方式一：镜像地址</h3><p class="hint">适用于 Docker Hub、GHCR 和其他镜像仓库。</p><form method="post"><input type="hidden" name="csrf" value="{e(token)}"><input type="hidden" name="action" value="deploy_image"><label>第 1 步 · 项目名</label><input name="project" required pattern="[A-Za-z0-9][A-Za-z0-9_.-]{{0,62}}" placeholder="例如 filebrowser"><label>第 2 步 · 镜像地址</label><input name="image" required placeholder="例如 ghcr.io/作者/镜像:版本"><div class="cols"><div><label>端口映射</label><textarea name="ports" placeholder="8088:80&#10;每行一个"></textarea><div class="hint">只写两段时自动绑定局域网 IP。</div></div><div><label>目录/数据卷映射</label><textarea name="volumes" placeholder="/vol1:/data&#10;my_data:/config"></textarea></div></div><label>环境变量</label><textarea name="environment" placeholder="TZ=Asia/Shanghai&#10;KEY=value"></textarea><details><summary>高级设置</summary><label>启动命令</label><input name="command" placeholder="例如 --port 8080"><label>设备映射</label><textarea name="devices" placeholder="/dev/dri:/dev/dri"></textarea><div class="cols"><div><label>网络模式</label><select name="network"><option value="bridge">bridge（推荐）</option><option value="host">host</option><option value="none">none</option></select></div><div><label>自动重启</label><select name="restart"><option value="unless-stopped">unless-stopped</option><option value="always">always</option><option value="on-failure">on-failure</option><option value="no">no</option></select></div></div><label class="check"><input type="checkbox" name="run_as_root" value="1">容器内使用 root 用户（0:0）</label><label class="check"><input type="checkbox" name="privileged" value="1">启用特权模式（可访问更多宿主机设备，风险较高）</label><label class="check"><input type="checkbox" name="allow_public" value="1">允许端口绑定 0.0.0.0/全部接口（不会自动配置公网或路由器转发）</label></details><div class="actions"><button type="submit">开始部署镜像</button></div></form></article>
<article class="card"><h3>方式二：GitHub Compose</h3><p class="hint">当前版本支持无需登录即可访问的公开仓库。</p><form method="post"><input type="hidden" name="csrf" value="{e(token)}"><input type="hidden" name="action" value="deploy_github"><label>第 1 步 · 项目名</label><input name="project" required pattern="[A-Za-z0-9][A-Za-z0-9_.-]{{0,62}}" placeholder="例如 my-app"><label>第 2 步 · GitHub 仓库</label><input name="repo" required placeholder="https://github.com/作者/仓库"><label>第 3 步 · 环境变量（可留空）</label><textarea name="environment" placeholder="TZ=Asia/Shanghai&#10;PASSWORD=只保存在NAS"></textarea><details><summary>高级设置</summary><label>分支或标签</label><input name="ref" placeholder="留空使用默认分支"><label>Compose 相对路径</label><input name="compose_path" placeholder="例如 deploy/docker-compose.yml"></details><div class="notice" style="margin-top:14px">仓库自带的 Compose 可能开放端口、挂载系统目录或启用特权权限。部署前请先阅读仓库说明。</div><div class="actions"><button type="submit">拉取并部署</button></div></form></article>
</div></section>
<section class="panel"><h2>AI 辅助安装 <span class="tag">{e(ai_state)}</span></h2><p class="sub">可选功能。AI 只生成部署草稿，绝不会自动执行 root 操作；必须由你核对后手工提交上面的部署表单。</p><div class="grid">
<article class="card"><h3>连接 OpenAI 兼容中转</h3><form method="post"><input type="hidden" name="csrf" value="{e(token)}"><input type="hidden" name="action" value="save_ai"><label>API 基础地址</label><input name="ai_base_url" required value="{e(ai_cfg.get('base_url',''))}" placeholder="https://example.com/v1"><label>模型名</label><input name="ai_model" required value="{e(ai_cfg.get('model',''))}" placeholder="例如 gpt-5-mini"><label>API Key</label><input type="password" name="ai_api_key" placeholder="{e('已保存，留空保持不变' if ai_ready else '首次设置必须填写')}"><p class="hint">密钥只保存在 NAS 的私有设置文件，权限 0600，不写入项目日志或 GitHub。</p><div class="actions"><button type="submit">保存 AI 设置</button></div></form></article>
<article class="card"><h3>生成部署草稿</h3><form method="post"><input type="hidden" name="csrf" value="{e(token)}"><input type="hidden" name="action" value="ai_assist"><label>镜像或 GitHub 地址</label><input name="ai_source" required placeholder="镜像名或 https://github.com/作者/仓库"><label>你想怎样部署</label><textarea name="ai_goal" required placeholder="例如：中文界面，端口 8080，数据放 /vol1/Docker/my-app，不开放公网"></textarea><div class="notice" style="margin-top:14px">AI 结果可能出错。部署前仍需核对上游 README、镜像版本、端口、目录和权限。</div><div class="actions"><button type="submit">让 AI 生成草稿</button></div></form></article>
</div></section>
<section class="panel"><h2>我的项目</h2><div class="grid">{project_html}</div></section>
<section class="panel"><h2>最近任务</h2><div class="grid">{jobs_html}</div></section>
"""
    return page("飞牛 Docker 部署器", content)


def job_page(job_id: str) -> str:
    if not job_id.isalnum() or len(job_id) != 32:
        return page("无效任务", '<div class="panel"><h2>任务编号无效</h2><a class="button" href="?">返回</a></div>')
    status = load_json(JOBS_ROOT / f"{job_id}.status.json", {"state": "running", "title": "正在启动任务"})
    try:
        log = (JOBS_ROOT / f"{job_id}.log").read_text(encoding="utf-8", errors="replace")[-50000:]
    except OSError:
        log = "任务日志尚未生成。"
    state = status.get("state", "running")
    refresh = 3 if state == "running" else None
    content = f'<section class="hero"><div><h1>{e(status.get("title","部署任务"))}</h1><span class="status {e(state)}">{e(state)}</span></div><a class="button secondary" href="?">返回首页</a></section><section class="panel"><pre>{e(log)}</pre>'
    if status.get("error"):
        content += f'<p class="notice">{e(status["error"])}</p>'
    content += "</section>"
    return page("部署任务", content, refresh)


def main() -> None:
    is_admin = os.environ.get("HTTP_X_TRIM_ISADMIN", "").lower()
    if is_admin in ("false", "0", "no"):
        response(page("无权限", '<div class="panel"><h2>仅管理员可使用此应用</h2></div>'), "403 Forbidden")
        return
    if os.environ.get("REQUEST_METHOD", "GET").upper() == "POST":
        try:
            data = parse_post()
            if not secrets.compare_digest(data.get("csrf", ""), csrf_token()):
                raise ValueError("页面令牌已失效，请返回首页重新提交")
            action = data.get("action", "")
            if action == "save_ai":
                save_ai_config(data)
                redirect("?")
                return
            if action == "deploy_image":
                spec = {
                    "project": data.get("project", ""), "image": data.get("image", ""),
                    "ports": data.get("ports", ""), "volumes": data.get("volumes", ""),
                    "environment": data.get("environment", ""), "command": data.get("command", ""),
                    "devices": data.get("devices", ""), "network": data.get("network", "bridge"),
                    "restart": data.get("restart", "unless-stopped"), "run_as_root": bool_field(data, "run_as_root"),
                    "privileged": bool_field(data, "privileged"), "allow_public": bool_field(data, "allow_public"),
                }
                job_id = submit_job("deploy_image", f"部署镜像：{spec['project']}", spec)
            elif action == "deploy_github":
                spec = {
                    "project": data.get("project", ""), "repo": data.get("repo", ""),
                    "ref": data.get("ref", ""), "compose_path": data.get("compose_path", ""),
                    "environment": data.get("environment", ""),
                }
                job_id = submit_job("deploy_github", f"部署 GitHub：{spec['project']}", spec)
            elif action == "project_action":
                spec = {"project": data.get("project", ""), "project_action": data.get("project_action", "")}
                job_id = submit_job("project_action", f"项目操作：{spec['project']}", spec)
            elif action == "ai_assist":
                spec = {"source": data.get("ai_source", ""), "goal": data.get("ai_goal", "")}
                job_id = submit_job("ai_assist", "AI 部署草稿", spec)
            else:
                raise ValueError("未知操作")
            redirect("?job=" + job_id)
            return
        except Exception as exc:
            response(dashboard(f"提交失败：{exc}"), "400 Bad Request")
            return
    query = urllib.parse.parse_qs(os.environ.get("QUERY_STRING", ""))
    if query.get("job"):
        response(job_page(query["job"][-1]))
    else:
        response(dashboard())


if __name__ == "__main__":
    main()
