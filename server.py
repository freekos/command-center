#!/usr/bin/env python3
"""Personal multi-project command center (local server, 127.0.0.1).

Projects are EXPLICIT workspaces (no auto-scan). Each lives in config.json:
  { "projects": {
      "invictus": { "path": "~/Codebase/work/invictus", "tracker": "jira",
                    "jira": { "site": "...", "project": "IK", "email": "...",
                              "token": "...", "ticket_regex": "IK-\\d+" } },
      "visco":    { "path": "~/Codebase/work/visco", "tracker": "none" } } }

You add a project from the UI: pick its folder via the native macOS dialog;
the server analyses the folder (git repos + their remotes) and you confirm.
Each project is its own workspace — its own Jira key, configured per project.

Render depends on the project's tracker:
  jira  -> epics -> tasks (mine first) -> MRs
  none  -> repo -> open MRs + local worktrees
Loads async (fetch) so there is no page-reload feel.
config.json holds tokens -> it is chmod 600 and must stay local (never commit).
"""
import json, os, re, sys, subprocess, urllib.request, urllib.parse, base64, html, time, pathlib, threading, hmac, hashlib, secrets, http.cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).parent
CONFIG = HERE / "config.json"
LEGACY_ENV = HERE / ".env"
PORT = int(os.environ.get("CC_PORT", "8787"))
HOST = os.environ.get("CC_HOST", "")  # resolved in __main__ (config "bind" or 127.0.0.1)
TOKEN_URL = "https://id.atlassian.com/manage-profile/security/api-tokens"
DEFAULT_TICKET = r"[A-Z][A-Z0-9]+-\d+"
STATUS_COLOR = {"done": "#1f9d55", "indeterminate": "#d9a400", "new": "#6b7280"}
MR_COLOR = {"opened": "#0a7ef4", "merged": "#1f9d55", "closed": "#9ca3af", "locked": "#9ca3af"}

_CACHE = {}


def cached(key, ttl, fn):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (now, val)
    return val


def esc(s):
    return html.escape(str(s if s is not None else ""))


# ---------- config ----------
def load_config():
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text())
            cfg.setdefault("projects", {})
            return cfg
        except Exception:
            pass
    return {"projects": {}}


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    os.chmod(CONFIG, 0o600)


# ---------- auth (passcode gate; needed once the dashboard is reachable off-localhost) ----------
def auth_cfg():
    return load_config().get("auth") or {}


def auth_enabled():
    return bool(auth_cfg().get("passcode_sha256"))


def _server_secret():
    a = auth_cfg()
    if a.get("secret"):
        return a["secret"]
    cfg = load_config()
    s = secrets.token_hex(16)
    cfg.setdefault("auth", {})["secret"] = s
    save_config(cfg)
    return s


def session_token():
    return hmac.new(_server_secret().encode(), b"cc-session-v1", hashlib.sha256).hexdigest()


def check_passcode(pc):
    return bool(pc) and hashlib.sha256(pc.encode()).hexdigest() == auth_cfg().get("passcode_sha256")


def set_passcode(pc):
    cfg = load_config()
    a = cfg.setdefault("auth", {})
    a["passcode_sha256"] = hashlib.sha256(pc.encode()).hexdigest()
    a.setdefault("secret", secrets.token_hex(16))
    save_config(cfg)


def set_bind(host):
    cfg = load_config()
    cfg["bind"] = host
    save_config(cfg)


# ---------- repo discovery (within a project's folder) ----------
def parse_remote(url):
    """(vcs, path) from a git remote url; ('', '') if unknown."""
    if not url:
        return "", ""
    u = url.strip()
    for host, vcs in (("gitlab.com", "gitlab"), ("github.com", "github")):
        if host in u:
            tail = u.split(host, 1)[1].lstrip(":/")
            return vcs, tail[:-4] if tail.endswith(".git") else tail
    return "", ""


def git_repos_in(folder: pathlib.Path):
    """label -> repo dir, for the folder itself and immediate git subdirs."""
    repos = {}
    if (folder / ".git").exists():
        repos[folder.name] = folder
    if folder.is_dir():
        for sub in sorted(folder.iterdir()):
            if sub.is_dir() and not sub.name.startswith(".") and not sub.name.endswith(".worktrees") \
               and (sub / ".git").exists():
                repos[sub.name] = sub
    return repos


def analyze_folder(path):
    """Inspect a folder: its git repos and their remotes. For the add-project preview."""
    folder = pathlib.Path(os.path.expanduser(path))
    repos = []
    for label, rdir in git_repos_in(folder).items():
        url = subprocess.run(["git", "-C", str(rdir), "remote", "get-url", "origin"],
                             capture_output=True, text=True).stdout.strip()
        vcs, rpath = parse_remote(url)
        repos.append({"label": label, "vcs": vcs or "—", "path": rpath, "dir": str(rdir)})
    return {"name": folder.name, "path": str(folder), "repos": repos}


def build_project(name, pc):
    """Materialise one configured project into the runtime shape used by renderers."""
    folder = pathlib.Path(os.path.expanduser(pc.get("path", "")))
    entries = {}
    for label, rdir in git_repos_in(folder).items():
        url = subprocess.run(["git", "-C", str(rdir), "remote", "get-url", "origin"],
                             capture_output=True, text=True).stdout.strip()
        vcs, rpath = parse_remote(url)
        if rpath:
            entries[label] = {"vcs": vcs, "path": rpath, "dir": str(rdir)}
    jira = pc.get("jira") or {}
    pk = jira.get("project", "")
    # ticket regex is derived from the Jira project key (IK -> IK-\d+); no manual field.
    tr = jira.get("ticket_regex") or ((re.escape(pk) + r"-\d+") if pk else DEFAULT_TICKET)
    return {
        "name": name,
        "path": str(folder),
        "worktree_base": str(folder),
        "repos": entries,
        "tracker": pc.get("tracker", "none"),
        "jira_site": jira.get("site", ""),
        "jira_project": pk,
        "ticket_regex": tr,
        "email": jira.get("email", ""),
        "token": jira.get("token", ""),
    }


def discover_projects():
    cfg = load_config()
    return {name: build_project(name, pc) for name, pc in cfg.get("projects", {}).items()}


def get_projects():
    return cached("projects", 300, discover_projects)


def invalidate_projects():
    for k in [k for k in _CACHE if k == "projects" or k.startswith(("mrs:", "jira:"))]:
        _CACHE.pop(k, None)


# ---------- native folder picker (macOS; manual path elsewhere) ----------
def picker_supported():
    import sys
    return sys.platform == "darwin" and bool(__import__("shutil").which("osascript"))


def pick_folder():
    if not picker_supported():
        return None
    base = os.path.expanduser("~/Codebase")
    loc = base if os.path.isdir(base) else os.path.expanduser("~")
    script = (
        'tell application "System Events"\n'
        'activate\n'
        f'set f to choose folder with prompt "Выбери папку проекта" default location (POSIX file {json.dumps(loc)})\n'
        'end tell\n'
        'return POSIX path of f'
    )
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=180)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().rstrip("/")
    except Exception:
        pass
    return None


# ---------- Jira ----------
def jira(site, path, email, token, method="GET", body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(f"{site}{path}", data=data, method=method)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode())
    req.add_header("Accept", "application/json")
    if body:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())


def jira_search(site, jql, fields, email, token, limit=300):
    body = {"jql": jql, "fields": fields, "maxResults": limit}
    return jira(site, "/rest/api/3/search/jql", email, token, "POST", body).get("issues", [])


def fetch_jira(proj, email, token):
    site = proj["jira_site"]
    pk = proj["jira_project"]
    scope = f"project = {pk} AND " if pk else ""
    me = jira(site, "/rest/api/3/myself", email, token).get("displayName", "")
    mine = jira_search(site, f"{scope}assignee = currentUser() ORDER BY updated DESC",
                       ["issuetype", "parent"], email, token, 100)
    epic_keys = set()
    for i in mine:
        f = i["fields"]
        if (f.get("issuetype") or {}).get("name") in ("Эпик", "Epic"):
            epic_keys.add(i["key"])
        if f.get("parent"):
            epic_keys.add(f["parent"]["key"])
    if not epic_keys:
        return me, {}, []
    keys = ",".join(sorted(epic_keys))
    epics = {e["key"]: e["fields"].get("summary", "")
             for e in jira_search(site, f"key in ({keys})", ["summary"], email, token, 100)}
    tasks = []
    for c in jira_search(site, f"parent in ({keys}) ORDER BY status",
                         ["summary", "status", "issuetype", "assignee", "parent"], email, token, 400):
        f = c["fields"]
        st = f.get("status") or {}
        tasks.append({"key": c["key"], "summary": f.get("summary", ""), "status": st.get("name", ""),
                      "statusCategory": (st.get("statusCategory") or {}).get("key", "new"),
                      "assignee": (f.get("assignee") or {}).get("displayName", "—"),
                      "parent": (f.get("parent") or {}).get("key", "")})
    return me, epics, tasks


# ---------- VCS (GitLab via glab / GitHub via gh) ----------
def glab_mrs(path, state="all"):
    enc = path.replace("/", "%2F")
    try:
        out = subprocess.run(["glab", "api", f"projects/{enc}/merge_requests?per_page=100&scope=all&state={state}"],
                             capture_output=True, text=True, timeout=30).stdout
        arr = json.loads(out) if out.strip().startswith("[") else []
    except Exception:
        arr = []
    return [{"iid": m.get("iid"), "state": m.get("state"), "url": m.get("web_url", ""),
             "title": m.get("title", ""), "target": m.get("target_branch", ""),
             "branch": m.get("source_branch", "")} for m in arr]


def gh_prs(path):
    try:
        out = subprocess.run(["gh", "pr", "list", "--repo", path, "--state", "all", "--limit", "100",
                              "--json", "number,state,url,title,baseRefName,headRefName"],
                             capture_output=True, text=True, timeout=30).stdout
        arr = json.loads(out) if out.strip().startswith("[") else []
    except Exception:
        arr = []
    return [{"iid": m.get("number"), "state": m.get("state", "").lower().replace("open", "opened"),
             "url": m.get("url", ""), "title": m.get("title", ""),
             "target": m.get("baseRefName", ""), "branch": m.get("headRefName", "")} for m in arr]


def fetch_mrs(proj):
    """(mr_by_ticket, mr_by_repo) for the project's repos."""
    tre = re.compile(r"^(" + proj["ticket_regex"] + r")", re.I)
    by_ticket, by_repo = {}, {}
    for label, r in proj["repos"].items():
        mrs = glab_mrs(r["path"]) if r["vcs"] == "gitlab" else (gh_prs(r["path"]) if r["vcs"] == "github" else [])
        by_repo[label] = mrs
        for mr in mrs:
            mr["repo"] = label
            m = tre.match(mr.get("branch", "") or "")
            if m:
                by_ticket.setdefault(m.group(1).upper(), []).append(mr)
    return by_ticket, by_repo


# ---------- worktrees & prune ----------
def list_worktrees(proj):
    res = []
    for label, r in proj["repos"].items():
        d = r["dir"]
        out = subprocess.run(["git", "-C", d, "worktree", "list", "--porcelain"],
                             capture_output=True, text=True).stdout
        path = None
        for line in out.splitlines():
            if line.startswith("worktree "):
                path = line[9:]
            elif line.startswith("branch "):
                br = line[7:].replace("refs/heads/", "")
                if br not in ("main", "master") and path and path != d:
                    res.append((label, d, path, br))
    return res


def merged_branches(by_repo, label):
    return {m["branch"] for m in by_repo.get(label, []) if m["state"] == "merged"}


def prune_estimate(proj, by_repo):
    return sum(1 for label, d, path, br in list_worktrees(proj) if br in merged_branches(by_repo, label))


def do_prune(proj, by_repo):
    removed = []
    for label, d, path, br in list_worktrees(proj):
        if br not in merged_branches(by_repo, label):
            continue
        if subprocess.run(["git", "-C", path, "status", "--porcelain"],
                          capture_output=True, text=True).stdout.strip():
            continue  # dirty -> keep
        if subprocess.run(["git", "-C", d, "worktree", "remove", path],
                          capture_output=True, text=True).returncode == 0:
            removed.append(f"{label}/{br}")
    return removed


# ---------- render ----------
CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:20px}
h1{font-size:18px;margin:0 0 12px}
.top{display:flex;gap:8px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
select{background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:8px;padding:7px 11px;font-size:14px;min-width:180px}
.meta{color:#8b949e;font-size:12px;margin-bottom:12px}
.bar{display:flex;gap:10px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
.btn{background:#21262d;border:1px solid #30363d;color:#e6edf3;border-radius:8px;padding:7px 12px;font-size:13px;cursor:pointer;text-decoration:none;white-space:nowrap}
.btn:hover{background:#2b3340}.btn[disabled]{opacity:.6;cursor:default}
.btn.primary{background:#1f6feb;border-color:#1f6feb;color:#fff}.btn.primary:hover{background:#388bfd}
.btn.danger{background:#3d1d1d;border-color:#8a3b3b;color:#ffb3b3}.btn.danger:hover{background:#522424}
.epic,.repo{background:#161b22;border:1px solid #30363d;border-radius:12px;margin-bottom:12px;overflow:hidden}
.epic>summary,.repo>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:8px;padding:13px 16px;font-size:15px;font-weight:600;user-select:none}
.epic>summary::-webkit-details-marker,.repo>summary::-webkit-details-marker{display:none}
.epic>summary::before,.repo>summary::before{content:"▸";color:#8b949e;font-size:12px;transition:transform .15s}
.epic[open]>summary::before,.repo[open]>summary::before{transform:rotate(90deg)}
.epic>summary:hover,.repo>summary:hover{background:#1c2230}
.epic-title a{color:#58a6ff;text-decoration:none}
.count{margin-left:auto;color:#8b949e;font-size:12px;font-weight:400}
.epic-body,.repo-body{padding:0 16px 12px}
.empty-mine{color:#6b7280;font-size:12px;font-style:italic;padding:6px 0}
.others{margin-top:8px;border-top:1px solid #21262d}
.others>summary{list-style:none;cursor:pointer;display:flex;gap:6px;padding:7px 0 4px;color:#8b949e;font-size:12px;font-weight:500;user-select:none}
.others>summary::-webkit-details-marker{display:none}
.others>summary::before{content:"▸";font-size:10px;transition:transform .15s}
.others[open]>summary::before{transform:rotate(90deg)}
.task{border-top:1px solid #21262d}
.task>summary,.task.flat{list-style:none;display:flex;align-items:center;gap:8px;padding:7px 0;font-size:13px}
.task>summary{cursor:pointer}.task>summary::-webkit-details-marker{display:none}
.task>summary::before{content:"▸";color:#6b7280;font-size:10px;width:9px;flex:0 0 auto;transition:transform .15s}
.task[open]>summary::before{transform:rotate(90deg)}
.task.flat::before{content:"";width:9px;flex:0 0 auto}
.task>summary:hover{background:#1c2230}
.key{color:#58a6ff;text-decoration:none;font-weight:600;flex:0 0 auto}
.status{color:#fff;font-size:10px;padding:1px 7px;border-radius:8px;flex:0 0 auto;white-space:nowrap}
.t-sum{color:#c9d1d9;flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ind{flex:0 0 auto;font-size:12px;font-weight:600}.ind.none{color:#3b4048}
.task-mrs{display:flex;flex-direction:column;gap:5px;padding:2px 0 9px 17px}
.mr{display:inline-flex;align-items:center;gap:6px;text-decoration:none;border:1px solid;border-radius:7px;padding:3px 9px;font-size:12px;color:#e6edf3;background:#0d1117;width:max-content;max-width:100%}
.mr-state{color:#fff;font-size:10px;padding:0 6px;border-radius:7px;text-transform:uppercase}
.mr-repo{color:#c9d1d9;font-weight:600}.mr-iid{color:#8b949e}.mr-target{color:#6b7280;font-size:11px}.mr-review{color:#58a6ff}
.wt{display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px;border-top:1px solid #21262d}
.wt-br{color:#c9d1d9;flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wt-tag{font-size:10px;padding:0 6px;border-radius:8px;color:#fff}
.sub{color:#8b949e;font-size:11px;margin:8px 0 2px;text-transform:uppercase;letter-spacing:.04em}
.spin{color:#8b949e;font-size:13px;padding:18px 2px}
.loader{display:flex;align-items:center;gap:10px;color:#8b949e;font-size:13px;padding:26px 2px}
.loader .ring{width:16px;height:16px;border:2px solid #30363d;border-top-color:#58a6ff;border-radius:50%;animation:spin .7s linear infinite;flex:0 0 auto}
.skel{height:46px;border-radius:12px;margin-bottom:12px;background:linear-gradient(90deg,#161b22 25%,#1c2230 37%,#161b22 63%);background-size:400% 100%;animation:shimmer 1.3s ease infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes shimmer{0%{background-position:100% 0}100%{background-position:-100% 0}}
.hint{max-width:520px;margin:50px auto;text-align:center;color:#8b949e}
.hint h2{color:#e6edf3;font-size:16px}
/* modal */
.overlay{position:fixed;inset:0;background:rgba(1,4,9,.7);display:none;align-items:flex-start;justify-content:center;padding:50px 16px;z-index:50;overflow:auto}
.overlay.show{display:flex}
.modal{background:#161b22;border:1px solid #30363d;border-radius:14px;padding:22px;width:100%;max-width:520px}
.modal h2{margin:0 0 4px;font-size:16px}
.modal .desc{color:#8b949e;font-size:12px;margin:0 0 14px}
label{display:block;margin:12px 0 4px;font-size:12px;color:#8b949e}
input[type=text],input[type=email],input[type=password]{width:100%;background:#0d1117;border:1px solid #30363d;border-radius:8px;color:#e6edf3;padding:9px 11px;font-size:13px}
.path-box{background:#0d1117;border:1px dashed #30363d;border-radius:8px;padding:9px 11px;font-size:12px;color:#8b949e;word-break:break-all;margin-top:8px}
.repolist{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.repochip{background:#0d1117;border:1px solid #30363d;border-radius:7px;padding:3px 9px;font-size:12px}
.repochip .v{color:#8b949e;font-size:10px;margin-left:5px}
.check{display:flex;align-items:center;gap:8px;margin-top:16px;color:#c9d1d9;font-size:13px;cursor:pointer}
.check input{width:auto}
.modal-actions{display:flex;gap:10px;margin-top:20px;flex-wrap:wrap}
.modal-actions .spacer{flex:1}
a.link{color:#58a6ff}
.muted{color:#6b7280;font-size:11px}
#toast{position:fixed;top:14px;right:14px;background:#1f9d55;color:#fff;padding:9px 14px;border-radius:8px;font-size:13px;opacity:0;transform:translateY(-6px);transition:.2s;pointer-events:none;z-index:60}
#toast.show{opacity:1;transform:none}
@media (max-width:600px){
  body{padding:12px}
  h1{font-size:17px}
  .top{gap:8px}
  select{flex:1 1 auto;min-width:0;font-size:16px;padding:10px 12px}
  .top .btn{padding:10px 12px}
  .bar{gap:8px}
  .bar .btn{flex:1 1 auto;text-align:center}
  .epic>summary,.repo>summary{padding:14px 14px;font-size:15px}
  .epic-body,.repo-body{padding:0 12px 12px}
  .task>summary,.task.flat{padding:10px 0}
  .t-sum{white-space:normal}
  .mr{width:100%;flex-wrap:wrap}
  .overlay{padding:16px}
  .modal{padding:18px}
  input[type=text],input[type=email],input[type=password]{font-size:16px}
  .modal-actions{gap:8px}
  .modal-actions .btn{flex:1 1 auto;text-align:center}
}
"""


def mr_indicator(mrs):
    if not mrs:
        return '<span class="ind none">—</span>'
    c = "#0a7ef4" if any(m["state"] == "opened" for m in mrs) else ("#1f9d55" if all(m["state"] == "merged" for m in mrs) else "#8b949e")
    return f'<span class="ind" style="color:{c}">●{len(mrs)}</span>'


def mr_badge(m):
    c = MR_COLOR.get(m["state"], "#6b7280")
    return (f'<a class="mr" href="{esc(m["url"])}" target="_blank" rel="noopener" style="border-color:{c}">'
            f'<span class="mr-state" style="background:{c}">{esc(m["state"])}</span>'
            f'<span class="mr-repo">{esc(m["repo"])}</span><span class="mr-iid">!{esc(m["iid"])}</span>'
            f'<span class="mr-target">→ {esc(m["target"])}</span><span class="mr-review">Ревью ↗</span></a>')


def bar_html(prune_n):
    pb = (f'<button class="btn" onclick="prune(this)">🧹 Убрать смерженные worktree ({prune_n})</button>'
          if prune_n else '<span style="color:#6b7280;font-size:12px">нет worktree к очистке</span>')
    return f'<div class="bar"><button class="btn" onclick="load()">↻ Обновить</button>{pb}</div>'


def jira_content(proj, my_name, epics, tasks, by_ticket, prune_n):
    by_epic = {}
    for t in tasks:
        by_epic.setdefault(t["parent"], []).append(t)
    order = {"new": 0, "indeterminate": 1, "done": 2}
    secs = []
    for ek, et in sorted(epics.items()):
        ts = sorted(by_epic.get(ek, []), key=lambda x: order.get(x["statusCategory"], 1))
        done = sum(1 for x in ts if x["statusCategory"] == "done")
        mine = [t for t in ts if t["assignee"] == my_name]
        others = [t for t in ts if t["assignee"] != my_name]

        def trow(t):
            sc = STATUS_COLOR.get(t["statusCategory"], "#6b7280")
            mrs = by_ticket.get(t["key"], [])
            base = proj["jira_site"] + "/browse/"
            key = f'<a class="key" href="{base}{esc(t["key"])}" target="_blank" rel="noopener" onclick="event.stopPropagation()">{esc(t["key"])}</a>'
            line = f'{key}<span class="status" style="background:{sc}">{esc(t["status"])}</span><span class="t-sum">{esc(t["summary"])}</span>{mr_indicator(mrs)}'
            if mrs:
                return f'<details class="task"><summary>{line}</summary><div class="task-mrs">{"".join(mr_badge(m) for m in mrs)}</div></details>'
            return f'<div class="task flat">{line}</div>'
        my_rows = "".join(trow(t) for t in mine) or '<div class="empty-mine">нет моих задач</div>'
        ob = (f'<details class="others"><summary>Задачи других · {len(others)}</summary><div>{"".join(trow(t) for t in others)}</div></details>') if others else ""
        base = proj["jira_site"] + "/browse/"
        secs.append(f'<details class="epic"><summary><span class="epic-title"><a href="{base}{esc(ek)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">{esc(ek)}</a> {esc(et)}</span><span class="count">{done}/{len(ts)} done</span></summary><div class="epic-body">{my_rows}{ob}</div></details>')
    now = time.strftime("%H:%M %d.%m")
    return f'<div class="meta">{esc(proj["name"])} · Jira ({esc(my_name)}) + VCS · обновлено {now}</div>{bar_html(prune_n)}{"".join(secs) or "<div class=spin>нет эпиков</div>"}'


def vcs_content(proj, by_repo, prune_n):
    wts = {}
    for label, d, path, br in list_worktrees(proj):
        wts.setdefault(label, []).append((path, br))
    secs = []
    for label in proj["repos"]:
        mrs = by_repo.get(label, [])
        opened = [m for m in mrs if m["state"] == "opened"]
        merged = {m["branch"] for m in mrs if m["state"] == "merged"}
        wlist = wts.get(label, [])
        open_html = "".join(
            f'<a class="mr" href="{esc(m["url"])}" target="_blank" rel="noopener" style="border-color:#0a7ef4">'
            f'<span class="mr-state" style="background:#0a7ef4">opened</span><span class="mr-iid">!{esc(m["iid"])}</span>'
            f'<span class="t-sum">{esc(m["title"])}</span><span class="mr-review">Ревью ↗</span></a>' for m in opened) or '<div class="empty-mine">нет открытых MR</div>'
        wt_html = ""
        for path, br in wlist:
            tag = ('<span class="wt-tag" style="background:#1f9d55">merged</span>' if br in merged else '<span class="wt-tag" style="background:#6b7280">local</span>')
            wt_html += f'<div class="wt"><span class="wt-br">{esc(br)}</span>{tag}</div>'
        secs.append(f'<details class="repo"><summary><span class="epic-title">{esc(label)}</span>'
                    f'<span class="count">{len(opened)} MR · {len(wlist)} wt</span></summary>'
                    f'<div class="repo-body"><div class="sub">Открытые MR</div>{open_html}'
                    f'<div class="sub">Worktrees</div>{wt_html or "<div class=empty-mine>нет worktrees</div>"}</div></details>')
    now = time.strftime("%H:%M %d.%m")
    if not proj["repos"]:
        return f'<div class="meta">{esc(proj["name"])} · VCS (без Jira) · обновлено {now}</div>{bar_html(prune_n)}<div class="spin">в папке не найдено git-репозиториев с remote</div>'
    return f'<div class="meta">{esc(proj["name"])} · VCS (без Jira) · обновлено {now}</div>{bar_html(prune_n)}{"".join(secs)}'


def render_login(error=""):
    bn = f'<p style="color:#ffb3b3;font-size:13px;margin:0 0 10px">{esc(error)}</p>' if error else ""
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Командный центр — вход</title>
<style>{CSS}</style></head><body>
<div class="modal" style="max-width:360px;margin:16vh auto">
  <h2>Командный центр</h2>
  <p class="desc">Введи пасскод для доступа.</p>{bn}
  <form method="post" action="/login">
    <label>Пасскод</label><input type="password" name="passcode" autofocus required>
    <div class="modal-actions"><span class="spacer"></span><button class="btn primary">Войти</button></div>
  </form>
</div></body></html>"""


def render_shell(active, projects):
    dsite = load_config().get("default_jira_site") or ""  # optional, set in config.json
    opts = "".join(
        f'<option value="{esc(n)}"{" selected" if n == active else ""}>{esc(n)} · {esc(p["tracker"])}</option>'
        for n, p in sorted(projects.items()))
    select = f'<select id="proj" onchange="pick(this.value)">{opts}</select>' if projects else ""
    gear = '<button class="btn" onclick="openSettings()" title="Настройки проекта">⚙</button>' if projects else ""
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Командный центр</title>
<style>{CSS}</style></head><body>
<div id="toast"></div>
<h1>Командный центр</h1>
<div class="top">{select}{gear}<button class="btn primary" onclick="openAdd()">＋ Проект</button></div>
<div id="content"><div class="spin">загрузка…</div></div>

<div class="overlay" id="m-add"><div class="modal">
  <h2>Добавить проект</h2>
  <p class="desc">Выбери папку проекта — посмотрю, какие внутри git-репозитории.</p>
  <button class="btn" onclick="pickFolder()">📁 Выбрать папку…</button>
  <div class="muted" style="margin-top:8px">или путь вручную:</div>
  <div style="display:flex;gap:8px;margin-top:4px">
    <input type="text" id="ap-manual" placeholder="/Users/.../project" style="flex:1"
           onkeydown="if(event.key==='Enter')analyzeManual()">
    <button class="btn" onclick="analyzeManual()">→</button>
  </div>
  <div class="path-box" id="ap-path">папка не выбрана</div>
  <div id="ap-after" style="display:none">
    <label>Название проекта</label><input type="text" id="ap-name">
    <label>Найденные репозитории</label><div class="repolist" id="ap-repos"></div>
    <label class="check"><input type="checkbox" id="ap-jira" onchange="toggleJira('ap')"> Это Jira-проект (свой ключ)</label>
    <div id="ap-jira-fields" style="display:none">
      <label>Email (Atlassian)</label><input type="email" id="ap-email">
      <label>API-токен <a class="link" href="{TOKEN_URL}" target="_blank" rel="noopener">создать ↗</a></label>
      <input type="password" id="ap-token" placeholder="вставь токен">
      <label>Ключ проекта Jira <span class="muted">(опц. — пусто = все мои задачи)</span></label>
      <input type="text" id="ap-proj" placeholder="напр. IK">
      <label>Jira-сайт <span class="muted">(обычно менять не нужно)</span></label>
      <input type="text" id="ap-site">
    </div>
  </div>
  <div class="modal-actions">
    <button class="btn" onclick="hide('m-add')">Отмена</button><span class="spacer"></span>
    <button class="btn primary" id="ap-save" onclick="addProject()" disabled>Добавить</button>
  </div>
</div></div>

<div class="overlay" id="m-set"><div class="modal">
  <h2>Настройки · <span id="st-name"></span></h2>
  <p class="desc" id="st-path"></p>
  <label class="check"><input type="checkbox" id="st-jira" onchange="toggleJira('st')"> Jira-проект (свой ключ)</label>
  <div id="st-jira-fields" style="display:none">
    <label>Email (Atlassian)</label><input type="email" id="st-email">
    <label>API-токен <a class="link" href="{TOKEN_URL}" target="_blank" rel="noopener">создать ↗</a> <span class="muted">(пусто = не менять)</span></label>
    <input type="password" id="st-token" placeholder="••••••• оставь пустым чтобы не менять">
    <label>Ключ проекта Jira <span class="muted">(опц.)</span></label><input type="text" id="st-proj" placeholder="напр. IK">
    <label>Jira-сайт</label><input type="text" id="st-site">
  </div>
  <div class="modal-actions">
    <button class="btn danger" onclick="removeProject()">Удалить проект</button><span class="spacer"></span>
    <button class="btn" onclick="hide('m-set')">Отмена</button>
    <button class="btn primary" onclick="saveSettings()">Сохранить</button>
  </div>
</div></div>

<script>
let CUR={json.dumps(active)};
function toast(m){{const t=document.getElementById('toast');t.textContent=m;t.className='show';setTimeout(()=>t.className='',2600);}}
function show(id){{document.getElementById(id).classList.add('show');}}
function hide(id){{document.getElementById(id).classList.remove('show');}}
function pick(n){{CUR=n;localStorage.setItem('proj',n);const s=document.getElementById('proj');if(s)s.value=n;load();}}

async function load(){{
  const c=document.getElementById('content');
  if(!CUR){{c.innerHTML='<div class="hint"><h2>Пока нет проектов</h2><p>Нажми «＋ Проект» и выбери папку — дашборд сам найдёт репозитории.</p></div>';return;}}
  c.innerHTML='<div class="loader"><span class="ring"></span>Загружаю «'+CUR+'»…</div><div class="skel"></div><div class="skel"></div><div class="skel"></div>';
  const want=CUR;
  try{{const r=await fetch('/api/content?project='+encodeURIComponent(want));
    if(r.status===401){{location.href='/login';return;}}
    const html=await r.text();
    if(want!==CUR)return;            // user switched again mid-load — drop stale result
    c.innerHTML=html;}}
  catch(e){{if(want===CUR)c.innerHTML='<div class="spin">ошибка — нажми ↻</div>';}}
}}
async function prune(b){{b.disabled=true;const t=b.textContent;b.textContent='чищу…';
  try{{const r=await fetch('/api/prune?project='+encodeURIComponent(CUR),{{method:'POST'}});
    const d=await r.json();toast(d.message);document.getElementById('content').innerHTML=d.content;}}
  catch(e){{b.disabled=false;b.textContent=t;toast('ошибка');}}
}}
async function refreshProjects(selname){{
  const r=await fetch('/api/projects');const list=await r.json();
  const s=document.getElementById('proj');
  if(!list.length){{location.reload();return;}}
  // rebuild select (page may not have had one when empty)
  location.reload();
}}

function toggleJira(p){{document.getElementById(p+'-jira-fields').style.display=document.getElementById(p+'-jira').checked?'block':'none';}}

// ---- add project ----
function openAdd(){{
  document.getElementById('ap-path').textContent='папка не выбрана';
  document.getElementById('ap-after').style.display='none';
  document.getElementById('ap-save').disabled=true;
  for(const id of ['ap-name','ap-proj','ap-email','ap-token','ap-manual'])document.getElementById(id).value='';
  document.getElementById('ap-site').value={json.dumps(dsite)};
  document.getElementById('ap-jira').checked=false;toggleJira('ap');
  show('m-add');
}}
async function analyzeInto(path){{
  if(!path)return;
  document.getElementById('ap-path').textContent=path;
  try{{
    const a=await(await fetch('/api/analyze?path='+encodeURIComponent(path))).json();
    document.getElementById('ap-name').value=a.name;
    document.getElementById('ap-repos').innerHTML=a.repos.length
      ? a.repos.map(x=>'<span class="repochip">'+x.label+'<span class="v">'+x.vcs+'</span></span>').join('')
      : '<span class="muted">git-репозиториев не найдено (можно всё равно добавить)</span>';
    document.getElementById('ap-after').style.display='block';
    document.getElementById('ap-save').disabled=false;
  }}catch(e){{toast('не удалось прочитать папку');}}
}}
async function pickFolder(){{
  toast('Открываю выбор папки…');
  try{{const r=await fetch('/api/pick-folder');const d=await r.json();
    if(!d.supported){{toast('Нативный выбор только на macOS — вставь путь вручную');document.getElementById('ap-manual').focus();return;}}
    if(!d.path){{toast('отменено');return;}}
    analyzeInto(d.path);
  }}catch(e){{toast('ошибка выбора папки');}}
}}
function analyzeManual(){{analyzeInto(document.getElementById('ap-manual').value.trim());}}
async function addProject(){{
  const body={{
    name:document.getElementById('ap-name').value.trim(),
    path:document.getElementById('ap-path').textContent.trim(),
    jira:document.getElementById('ap-jira').checked?{{
      site:document.getElementById('ap-site').value.trim(),
      project:document.getElementById('ap-proj').value.trim(),
      email:document.getElementById('ap-email').value.trim(),
      token:document.getElementById('ap-token').value.trim()}}:null
  }};
  if(!body.name||!body.path){{toast('нужны название и папка');return;}}
  const r=await fetch('/api/add-project',{{method:'POST',body:JSON.stringify(body)}});
  const d=await r.json();
  if(d.ok){{localStorage.setItem('proj',body.name);hide('m-add');toast('Проект добавлен');location.reload();}}
  else toast(d.error||'ошибка');
}}

// ---- settings ----
async function openSettings(){{
  if(!CUR)return;
  const d=await(await fetch('/api/project?name='+encodeURIComponent(CUR))).json();
  document.getElementById('st-name').textContent=d.name;
  document.getElementById('st-path').textContent=d.path;
  const j=d.jira||{{}};
  document.getElementById('st-jira').checked=d.tracker==='jira';
  document.getElementById('st-proj').value=j.project||'';
  document.getElementById('st-email').value=j.email||'';
  document.getElementById('st-site').value=j.site||{json.dumps(dsite)};
  document.getElementById('st-token').value='';
  toggleJira('st');show('m-set');
}}
async function saveSettings(){{
  const body={{name:CUR,jira:document.getElementById('st-jira').checked?{{
    site:document.getElementById('st-site').value.trim(),
    project:document.getElementById('st-proj').value.trim(),
    email:document.getElementById('st-email').value.trim(),
    token:document.getElementById('st-token').value.trim()}}:null}};
  const r=await fetch('/api/project-jira',{{method:'POST',body:JSON.stringify(body)}});
  const d=await r.json();
  if(d.ok){{hide('m-set');toast('Сохранено');load();}}else toast(d.error||'ошибка');
}}
async function removeProject(){{
  if(!confirm('Убрать «'+CUR+'» из дашборда? (файлы на диске не трогаются)'))return;
  const r=await fetch('/api/remove-project',{{method:'POST',body:JSON.stringify({{name:CUR}})}});
  const d=await r.json();
  if(d.ok){{localStorage.removeItem('proj');hide('m-set');toast('Проект убран');location.reload();}}else toast(d.error||'ошибка');
}}

const saved=localStorage.getItem('proj');
const sel=document.getElementById('proj');
if(saved&&sel&&[...sel.options].some(o=>o.value===saved)){{CUR=saved;sel.value=saved;}}
load();
</script></body></html>"""


# ---------- build content for a project ----------
def build_content(proj):
    by_ticket, by_repo = cached(f"mrs:{proj['name']}", 60, lambda: fetch_mrs(proj))
    prune_n = prune_estimate(proj, by_repo)
    if proj["tracker"] == "jira" and proj.get("jira_site"):
        if not (proj.get("email") and proj.get("token")):
            return '<div class="spin">Jira не настроена для этого проекта — открой ⚙ и добавь ключ.</div>'
        my_name, epics, tasks = cached(f"jira:{proj['name']}", 60,
                                       lambda: fetch_jira(proj, proj["email"], proj["token"]))
        return jira_content(proj, my_name, epics, tasks, by_ticket, prune_n)
    return vcs_content(proj, by_repo, prune_n)


def warm_loop():
    """Pre-warm only Jira projects (the main ones); others load lazily on view."""
    while True:
        try:
            for n, p in get_projects().items():
                if p["tracker"] == "jira" and p.get("jira_site") and p.get("email") and p.get("token"):
                    cached(f"mrs:{n}", 60, lambda p=p: fetch_mrs(p))
                    cached(f"jira:{n}", 60, lambda p=p: fetch_jira(p, p["email"], p["token"]))
        except Exception:
            pass
        time.sleep(45)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, code=200, ctype="text/html; charset=utf-8", headers=None):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        for k, v in (headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False), code, "application/json; charset=utf-8")

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            return {}

    def _form(self):
        n = int(self.headers.get("Content-Length", 0))
        return urllib.parse.parse_qs(self.rfile.read(n).decode())

    def _authed(self):
        if not auth_enabled():
            return True
        c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        tok = c["cc_session"].value if "cc_session" in c else ""
        return hmac.compare_digest(tok, session_token())

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p, q = u.path, urllib.parse.parse_qs(u.query)

        if p == "/login":
            return self._send(render_login())
        if not self._authed():
            return self._send("auth required", 401) if p.startswith("/api/") else self._send(render_login())

        projects = get_projects()

        if p == "/api/content":
            name = (q.get("project") or [""])[0]
            proj = projects.get(name) or (next(iter(projects.values()), None))
            if not proj:
                return self._send('<div class="spin">нет проектов</div>')
            try:
                return self._send(build_content(proj))
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    _CACHE.pop(f"jira:{proj['name']}", None)
                    return self._send('<div class="spin">Jira-ключ неверный или истёк — открой ⚙ и обнови.</div>')
                return self._send(f'<div class="spin">Jira ошибка {e.code}</div>')
            except Exception as e:
                return self._send(f'<div class="spin">ошибка: {esc(e)}</div>')

        if p == "/api/projects":
            return self._json([{"name": n, "tracker": x["tracker"]} for n, x in sorted(projects.items())])

        if p == "/api/project":
            name = (q.get("name") or [""])[0]
            pc = load_config().get("projects", {}).get(name)
            if not pc:
                return self._json({"error": "не найден"}, 404)
            j = dict(pc.get("jira") or {})
            j.pop("token", None)  # never echo the token to the client
            return self._json({"name": name, "path": pc.get("path", ""),
                               "tracker": pc.get("tracker", "none"), "jira": j})

        if p == "/api/pick-folder":
            return self._json({"path": pick_folder(), "supported": picker_supported()})

        if p == "/api/analyze":
            return self._json(analyze_folder((q.get("path") or [""])[0]))

        # "/" -> the app shell
        active = next((n for n, x in sorted(projects.items()) if x["tracker"] == "jira"), None) \
            or next(iter(sorted(projects)), "")
        return self._send(render_shell(active, projects))

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)

        if u.path == "/login":
            pc = (self._form().get("passcode") or [""])[0]
            if check_passcode(pc):
                cookie = f"cc_session={session_token()}; HttpOnly; SameSite=Lax; Path=/; Max-Age=7776000"
                return self._send("", 303, headers=[("Location", "/"), ("Set-Cookie", cookie)])
            return self._send(render_login("Неверный пасскод"))

        if not self._authed():
            return self._send("auth required", 401)

        if u.path == "/api/add-project":
            d = self._body()
            name, path = (d.get("name") or "").strip(), (d.get("path") or "").strip()
            if not name or not path:
                return self._json({"error": "нужны название и папка"}, 400)
            cfg = load_config()
            pc = {"path": path}
            j = d.get("jira")
            if j and j.get("site") and j.get("token") and j.get("email"):
                pc["tracker"] = "jira"
                pc["jira"] = {"site": j["site"].strip(), "project": (j.get("project") or "").strip(),
                              "email": j["email"].strip(), "token": j["token"].strip()}
            else:
                pc["tracker"] = "none"
            cfg["projects"][name] = pc
            save_config(cfg)
            invalidate_projects()
            return self._json({"ok": True})

        if u.path == "/api/project-jira":
            d = self._body()
            name = (d.get("name") or "").strip()
            cfg = load_config()
            pc = cfg["projects"].get(name)
            if not pc:
                return self._json({"error": "не найден"}, 404)
            j = d.get("jira")
            if j and j.get("site"):
                old = pc.get("jira") or {}
                token = (j.get("token") or "").strip() or old.get("token", "")  # blank = keep
                pc["tracker"] = "jira"
                pc["jira"] = {"site": j["site"].strip(), "project": (j.get("project") or "").strip(),
                              "email": (j.get("email") or "").strip(), "token": token}
            else:
                pc["tracker"] = "none"
                pc.pop("jira", None)
            save_config(cfg)
            invalidate_projects()
            return self._json({"ok": True})

        if u.path == "/api/remove-project":
            name = (self._body().get("name") or "").strip()
            cfg = load_config()
            if cfg["projects"].pop(name, None) is None:
                return self._json({"error": "не найден"}, 404)
            save_config(cfg)
            invalidate_projects()
            return self._json({"ok": True})

        if u.path == "/api/prune":
            q = urllib.parse.parse_qs(u.query)
            proj = get_projects().get((q.get("project") or [""])[0])
            if not proj:
                return self._json({"message": "проект?", "content": ""})
            _, by_repo = cached(f"mrs:{proj['name']}", 60, lambda: fetch_mrs(proj))
            removed = do_prune(proj, by_repo)
            _CACHE.pop(f"mrs:{proj['name']}", None)
            msg = f"Убрано: {len(removed)} ({', '.join(removed)})" if removed else "Нечего убирать"
            try:
                content = build_content(proj)
            except Exception:
                content = ""
            return self._json({"message": msg, "content": content})

        self._send("not found", 404)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "passcode":
            import getpass
            pw = getpass.getpass("Новый пасскод: ").strip()
            if not pw:
                print("пусто — отмена"); sys.exit(1)
            set_passcode(pw); print("✓ пасскод задан"); sys.exit(0)
        if cmd == "bind":
            host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
            set_bind(host); print(f"✓ bind = {host}"); sys.exit(0)
        print("usage: server.py [passcode | bind <host>]"); sys.exit(1)

    host = HOST or load_config().get("bind") or "127.0.0.1"
    if host not in ("127.0.0.1", "localhost", "::1") and not auth_enabled():
        print(f"⚠ Отказ: bind={host} (доступ по сети) без пасскода. Сначала задай: cc passcode")
        sys.exit(1)
    threading.Thread(target=warm_loop, daemon=True).start()
    print(f"Command Center → http://{host}:{PORT}  (auth: {'on' if auth_enabled() else 'off'})")
    ThreadingHTTPServer((host, PORT), H).serve_forever()
