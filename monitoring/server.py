"""servingz status + approvals server.

Serves the existing static status page (written by watchdog.py) plus a
dynamic /approvals dashboard that lets a human review and act on pending
"production deploy" approval gates from CI, without touching GitHub's UI.

Both live behind the same Cloudflare Access policy already applied to
status.zaindroid.me — this process only binds 127.0.0.1, same as before.
"""
import json
import re
import sys
import threading
import uuid
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
STATUS_DIR = BASE_DIR / "status"
REGISTRY_PATH = BASE_DIR.parent / "registry.yaml"
TOKEN_PATH = BASE_DIR / "secrets" / "github_approvals.json"

sys.path.insert(0, str(BASE_DIR.parent / "deploy"))
import agent as deploy_agent  # noqa: E402

DEPLOY_JOBS: dict[str, dict] = {}  # in-memory job status, fine for a single-operator platform

FIELD_RE = re.compile(r"^(App|Commit|Staging|Compare|Run): (.+)$", re.MULTILINE)

app = FastAPI()


def github_token() -> str:
    return json.loads(TOKEN_PATH.read_text())["token"]


def apps_with_repos() -> list[dict]:
    data = yaml.safe_load(REGISTRY_PATH.read_text())
    out = []
    for a in data.get("apps", []):
        repo = a.get("repo", "")
        if repo.startswith("github.com/"):
            out.append({"name": a["name"], "repo": repo[len("github.com/"):]})
    return out


@app.get("/", response_class=HTMLResponse)
def status_page():
    return (STATUS_DIR / "index.html").read_text()


@app.get("/status.json")
def status_json():
    return JSONResponse(json.loads((STATUS_DIR / "status.json").read_text()))


@app.get("/theme.css")
def theme_css():
    return Response((STATUS_DIR / "theme.css").read_text(), media_type="text/css")


@app.get("/effects.js")
def effects_js():
    return Response((STATUS_DIR / "effects.js").read_text(), media_type="application/javascript")


class DeployRequest(BaseModel):
    owner_repo: str  # "owner/repo"
    name: str
    git_branch: str = "main"


def _run_deploy_job(job_id: str, req: DeployRequest):
    DEPLOY_JOBS[job_id]["status"] = "running"
    try:
        result = deploy_agent.deploy(owner_repo=req.owner_repo, name=req.name, git_branch=req.git_branch)
        DEPLOY_JOBS[job_id].update(status="done", result=result)
    except deploy_agent.DeployError as e:
        DEPLOY_JOBS[job_id].update(status="failed", error={"step": e.step, "reason": e.reason})
    except Exception as e:  # noqa: BLE001 -- surface anything unexpected to the dashboard, don't swallow
        DEPLOY_JOBS[job_id].update(status="failed", error={"step": "unexpected", "reason": str(e)})


@app.post("/api/deploy")
def start_deploy(req: DeployRequest):
    job_id = uuid.uuid4().hex[:12]
    DEPLOY_JOBS[job_id] = {"status": "queued", "owner_repo": req.owner_repo, "name": req.name}
    threading.Thread(target=_run_deploy_job, args=(job_id, req), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/deploy/{job_id}")
def get_deploy_status(job_id: str):
    job = DEPLOY_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job id")
    return job


@app.get("/api/approvals")
def list_approvals():
    headers = {
        "Authorization": f"Bearer {github_token()}",
        "Accept": "application/vnd.github+json",
    }
    out = []
    with httpx.Client(timeout=10) as client:
        for a in apps_with_repos():
            r = client.get(
                f"https://api.github.com/repos/{a['repo']}/issues",
                headers=headers,
                params={"state": "open"},
            )
            r.raise_for_status()
            for issue in r.json():
                if not issue["title"].startswith("Approve production deploy"):
                    continue
                # manual-approval@v1 posts the custom issue-body as the
                # FIRST COMMENT, not the issue body itself — the issue body
                # is always its own fixed "pending manual review" template.
                cr = client.get(
                    issue["comments_url"],
                    headers=headers,
                    params={"per_page": 1},
                )
                cr.raise_for_status()
                comments = cr.json()
                review_text = comments[0]["body"] if comments else ""
                fields = dict(FIELD_RE.findall(review_text))
                out.append({
                    "app": a["name"],
                    "repo": a["repo"],
                    "issue_number": issue["number"],
                    "title": issue["title"],
                    "commit": fields.get("Commit", ""),
                    "staging_url": fields.get("Staging", ""),
                    "compare_url": fields.get("Compare", ""),
                    "run_url": fields.get("Run", ""),
                    "issue_url": issue["html_url"],
                    "created_at": issue["created_at"],
                })
    out.sort(key=lambda x: x["created_at"])
    return out


class Decision(BaseModel):
    repo: str
    issue_number: int
    decision: str


@app.post("/api/approvals/decide")
def decide(d: Decision):
    if d.decision not in ("approved", "denied"):
        raise HTTPException(400, "decision must be 'approved' or 'denied'")
    known_repos = {a["repo"] for a in apps_with_repos()}
    if d.repo not in known_repos:
        raise HTTPException(403, "repo is not a registered app")

    headers = {
        "Authorization": f"Bearer {github_token()}",
        "Accept": "application/vnd.github+json",
    }
    with httpx.Client(timeout=10) as client:
        r = client.post(
            f"https://api.github.com/repos/{d.repo}/issues/{d.issue_number}/comments",
            headers=headers,
            json={"body": d.decision},
        )
        r.raise_for_status()
    return {"ok": True}


APPROVALS_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>servingz — approvals</title>
<link rel="stylesheet" href="/theme.css">
<script src="/effects.js"></script>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <a class="brand" href="/"><span class="pip" id="brand-pip"></span> <span id="brand-text">SERVINGZ</span></a>
    <div class="tabs">
      <a href="/" data-scramble>Status</a>
      <a href="/approvals" class="active" data-scramble>Approvals</a>
      <a href="/deploy" data-scramble>Deploy</a>
    </div>
  </div>

  <div class="glass hero" id="hero">
    <div class="orb ok" id="hero-orb"></div>
    <div class="hero-text">
      <h1 id="hero-title">Loading&hellip;</h1>
      <div class="sub" id="hero-sub">Anything waiting on a human shows up here, with a live preview of exactly what would ship.</div>
    </div>
  </div>

  <div id="list"></div>
</div>

<script>
let lastTitle = null;
function setTitle(titleEl, text) {
  if (text !== lastTitle) { zorcEffects.decodeIn(titleEl, text); lastTitle = text; }
}

async function load() {
  const list = document.getElementById('list');
  const orb = document.getElementById('hero-orb');
  const title = document.getElementById('hero-title');
  let items;
  try {
    items = await (await fetch('/api/approvals', { cache: 'no-store' })).json();
  } catch (e) {
    setTitle(title, "Can't reach the approvals service");
    orb.className = 'orb crit';
    return;
  }

  if (!items.length) {
    orb.className = 'orb ok';
    setTitle(title, 'Nothing waiting on you');
    list.innerHTML = '<div class="empty">All caught up — new deploys will show up here the moment they pass staging + regression.</div>';
    return;
  }

  orb.className = 'orb warn';
  setTitle(title, items.length === 1
    ? '1 deploy waiting on your review'
    : `${items.length} deploys waiting on your review`);

  // Don't clobber cards mid-decision (buttons disabled) on the poll tick.
  const existing = new Set([...list.querySelectorAll('.card[data-pending]')].map(el => el.id));
  list.innerHTML = items.map(item => renderCard(item, existing)).join('');
  zorcEffects.wireScrambleHovers(list);
}

function cardId(item) {
  return `card-${item.repo.replace('/', '-')}-${item.issue_number}`;
}

function renderCard(item, skip) {
  if (skip.has(cardId(item))) return document.getElementById(cardId(item)).outerHTML;

  const links = [];
  if (item.staging_url) links.push(`<a href="${item.staging_url}" target="_blank">Open staging in new tab &#8599;</a>`);
  if (item.compare_url) links.push(`<a href="${item.compare_url}" target="_blank">Code diff</a>`);
  if (item.run_url) links.push(`<a href="${item.run_url}" target="_blank">CI run</a>`);
  links.push(`<a href="${item.issue_url}" target="_blank">GitHub issue</a>`);

  const preview = item.staging_url
    ? `<div class="preview-label">Live preview &mdash; this is the exact build you're approving:</div>
       <iframe class="preview-frame" src="${item.staging_url}" loading="lazy"></iframe>`
    : `<div class="preview-missing">No staging URL reported for this app &mdash; check the CI run.</div>`;

  return `
    <div class="glass card" id="${cardId(item)}">
      <h2 data-scramble>${item.app}</h2>
      <div class="sha">commit ${item.commit || '(unknown)'}</div>
      ${preview}
      <div class="links">${links.join('')}</div>
      <div class="actions">
        <button class="approve" onclick="decide('${item.repo}', ${item.issue_number}, 'approved', this)">Approve &amp; deploy</button>
        <button class="deny" onclick="decide('${item.repo}', ${item.issue_number}, 'denied', this)">Deny</button>
      </div>
    </div>`;
}

async function decide(repo, issue_number, decision, btn) {
  const card = btn.closest('.card');
  card.setAttribute('data-pending', '1');
  card.querySelectorAll('button').forEach(b => b.disabled = true);
  try {
    const res = await fetch('/api/approvals/decide', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({repo, issue_number, decision}),
    });
    if (!res.ok) throw new Error(await res.text());
    const word = decision === 'approved' ? 'Approved' : 'Denied';
    card.innerHTML = `<h2>${card.querySelector('h2').textContent}</h2><div class="resolved">${word} &mdash; CI will pick it up within ~10s.</div>`;
  } catch (e) {
    card.removeAttribute('data-pending');
    card.querySelectorAll('button').forEach(b => b.disabled = false);
    alert('Failed to record decision: ' + e.message);
  }
}

const brandEl = document.getElementById('brand-text');
zorcEffects.decodeIn(brandEl, 'SERVINGZ', { stagger: 55, dur: 300 });
zorcEffects.shimmer(brandEl);
zorcEffects.wireScrambleHovers(document.querySelector('.tabs'));

load();
setInterval(load, 10000);
</script>
</body>
</html>"""


@app.get("/approvals", response_class=HTMLResponse)
def approvals_page():
    return APPROVALS_HTML


DEPLOY_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>servingz — deploy</title>
<link rel="stylesheet" href="/theme.css">
<script src="/effects.js"></script>
<style>
.form-card { padding: 1.3rem 1.5rem; max-width: 560px; }
.field { margin-top: 1rem; }
.field label { display: block; font-size: 0.78rem; color: var(--text-dim); margin-bottom: 0.4rem; letter-spacing: 0.04em; }
.field input {
  width: 100%; background: rgba(255,255,255,0.04); border: 1px solid var(--border);
  border-radius: 8px; padding: 0.6rem 0.8rem; color: var(--text); font-family: var(--font); font-size: 0.9rem;
}
.field input:focus { outline: none; border-color: var(--accent); }
.field .hint { color: var(--text-faint); font-size: 0.72rem; margin-top: 0.35rem; }
.deploy-btn {
  margin-top: 1.3rem; background: rgba(94,177,255,0.14); color: var(--accent);
  box-shadow: inset 0 0 0 1px rgba(94,177,255,0.4);
}
.deploy-btn:hover { background: rgba(94,177,255,0.22); }
.log-card { padding: 1.1rem 1.4rem; max-width: 560px; margin-top: 1.1rem; }
.log-line { font-size: 0.82rem; padding: 0.3rem 0; border-bottom: 1px solid var(--border); display: flex; gap: 0.6rem; align-items: baseline; }
.log-line:last-child { border-bottom: none; }
.log-ok { color: var(--ok); }
.log-fail { color: var(--crit); }
.log-pending { color: var(--text-faint); }
.result-box { margin-top: 1rem; padding: 0.9rem 1rem; border-radius: 8px; font-size: 0.85rem; line-height: 1.6; }
.result-box.ok { background: rgba(47,230,184,0.08); box-shadow: inset 0 0 0 1px rgba(47,230,184,0.3); color: var(--text); }
.result-box.fail { background: rgba(255,92,114,0.08); box-shadow: inset 0 0 0 1px rgba(255,92,114,0.3); color: var(--text); }
.result-box a { color: var(--accent); }
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <a class="brand" href="/"><span class="pip" id="brand-pip"></span> <span id="brand-text">SERVINGZ</span></a>
    <div class="tabs">
      <a href="/" data-scramble>Status</a>
      <a href="/approvals" data-scramble>Approvals</a>
      <a href="/deploy" class="active" data-scramble>Deploy</a>
    </div>
  </div>

  <div class="glass hero" id="hero">
    <div class="orb ok" id="hero-orb"></div>
    <div class="hero-text">
      <h1 id="hero-title">Hand over a repo</h1>
      <div class="sub">No Dockerfile required for most apps &mdash; classification and resource
      checks are deterministic (no LLM), Nixpacks handles the build. Static sites get flagged
      for Cloudflare Pages instead of landing on this node.</div>
    </div>
  </div>

  <div class="glass form-card">
    <div class="field">
      <label>GitHub repo (owner/name)</label>
      <input id="repo" placeholder="zaindroid/my-new-app" autocomplete="off">
      <div class="hint">Private repos work too, via the same GitHub App already connected.</div>
    </div>
    <div class="field">
      <label>App name</label>
      <input id="name" placeholder="my-new-app" autocomplete="off">
      <div class="hint">Becomes the subdomain: name.zaindroid.me</div>
    </div>
    <div class="field">
      <label>Branch</label>
      <input id="branch" value="main" autocomplete="off">
    </div>
    <button class="deploy-btn" id="deployBtn" onclick="startDeploy()">Deploy</button>
  </div>

  <div class="glass log-card" id="logCard" style="display:none">
    <div id="logLines"></div>
    <div id="resultBox"></div>
  </div>
</div>

<script>
const STEP_LABELS = {
  clone: 'Clone repository',
  classify: 'Classify (static vs. app, language)',
  budget_check: 'Check registry.yaml budget',
  create_coolify_app: 'Create Coolify resource',
  create_dns_record: 'Create DNS record',
  add_tunnel_route: 'Wire Cloudflare Tunnel route',
  deploy_to_pages: 'Deploy to Cloudflare Pages',
  add_pages_custom_domain: 'Attach custom domain (Pages)',
  register_app: 'Register in registry.yaml',
  commit_and_push: 'Commit + push',
};

async function startDeploy() {
  const owner_repo = document.getElementById('repo').value.trim();
  const name = document.getElementById('name').value.trim();
  const git_branch = document.getElementById('branch').value.trim() || 'main';
  if (!owner_repo || !name) { alert('Repo and name are both required.'); return; }

  document.getElementById('deployBtn').disabled = true;
  document.getElementById('logCard').style.display = 'block';
  document.getElementById('logLines').innerHTML = '<div class="log-line log-pending">Starting&hellip;</div>';
  document.getElementById('resultBox').innerHTML = '';

  let job;
  try {
    const res = await fetch('/api/deploy', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({owner_repo, name, git_branch}),
    });
    if (!res.ok) throw new Error(await res.text());
    job = await res.json();
  } catch (e) {
    document.getElementById('logLines').innerHTML = `<div class="log-line log-fail">Failed to start: ${e.message}</div>`;
    document.getElementById('deployBtn').disabled = false;
    return;
  }
  poll(job.job_id);
}

async function poll(jobId) {
  const res = await fetch(`/api/deploy/${jobId}`, { cache: 'no-store' });
  const job = await res.json();
  renderLog(job);
  if (job.status === 'done' || job.status === 'failed') {
    document.getElementById('deployBtn').disabled = false;
    return;
  }
  setTimeout(() => poll(jobId), 1500);
}

function renderLog(job) {
  const log = (job.result && job.result.log) || (job.error ? [{step: job.error.step, ok: false}] : []);
  const lines = log.map(l => {
    const cls = l.ok ? 'log-ok' : 'log-fail';
    const icon = l.ok ? '\\u2713' : '\\u2717';
    return `<div class="log-line ${cls}">${icon} ${STEP_LABELS[l.step] || l.step}</div>`;
  });
  if (job.status === 'running' || job.status === 'queued') {
    lines.push('<div class="log-line log-pending">&hellip;</div>');
  }
  document.getElementById('logLines').innerHTML = lines.join('');

  const box = document.getElementById('resultBox');
  if (job.status === 'done') {
    const r = job.result;
    if (r.status === 'needs_manual_step') {
      box.innerHTML = `<div class="result-box fail">${r.message}</div>`;
    } else {
      box.innerHTML = `<div class="result-box ok">${r.message}<br><a href="https://${r.domain}" target="_blank">https://${r.domain}</a></div>`;
    }
  } else if (job.status === 'failed') {
    box.innerHTML = `<div class="result-box fail">Failed at <b>${STEP_LABELS[job.error.step] || job.error.step}</b>: ${job.error.reason}</div>`;
  }
}

const brandEl = document.getElementById('brand-text');
zorcEffects.decodeIn(brandEl, 'SERVINGZ', { stagger: 55, dur: 300 });
zorcEffects.shimmer(brandEl);
zorcEffects.wireScrambleHovers(document.querySelector('.tabs'));
</script>
</body>
</html>"""


@app.get("/deploy", response_class=HTMLResponse)
def deploy_page():
    return DEPLOY_HTML
