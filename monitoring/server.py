"""servingz status + approvals server.

Serves the existing static status page (written by watchdog.py) plus a
dynamic /approvals dashboard that lets a human review and act on pending
"production deploy" approval gates from CI, without touching GitHub's UI.

Both live behind the same Cloudflare Access policy already applied to
status.zaindroid.me — this process only binds 127.0.0.1, same as before.
"""
import json
import re
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
STATUS_DIR = BASE_DIR / "status"
REGISTRY_PATH = BASE_DIR.parent / "registry.yaml"
TOKEN_PATH = BASE_DIR / "secrets" / "github_approvals.json"

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
<html><head><meta charset="utf-8"><title>servingz approvals</title>
<style>
body { font-family: -apple-system, sans-serif; background:#111; color:#eee; margin:2rem; }
h1 { font-size:1.4rem; margin-bottom:0.25rem; }
a.back { color:#888; font-size:0.85rem; text-decoration:none; }
a.back:hover { text-decoration:underline; }
.empty { color:#888; margin-top:2rem; }
.card { background:#1a1a1a; border:1px solid #333; border-radius:8px; padding:1rem 1.25rem; margin-top:1.25rem; max-width:900px; }
.card h2 { margin:0 0 0.5rem 0; font-size:1.05rem; }
.card .sha { color:#888; font-family:monospace; font-size:0.85rem; }
.preview-label { color:#999; font-size:0.8rem; margin:1rem 0 0.4rem; }
.preview-frame { width:100%; height:480px; border:1px solid #333; border-radius:6px; background:#fff; }
.preview-missing { color:#888; font-size:0.85rem; padding:1rem; border:1px dashed #333; border-radius:6px; margin-top:0.5rem; }
.links { margin:0.75rem 0; display:flex; gap:1rem; flex-wrap:wrap; }
.links a { color:#6fb3ff; font-size:0.85rem; text-decoration:none; }
.links a:hover { text-decoration:underline; }
.actions { margin-top:1rem; display:flex; gap:0.75rem; }
button { border:none; border-radius:6px; padding:0.5rem 1.1rem; font-size:0.9rem; cursor:pointer; }
.approve { background:#2e7d32; color:#fff; }
.approve:hover { background:#388e3c; }
.deny { background:#8b2e2e; color:#fff; }
.deny:hover { background:#a03838; }
.meta { color:#666; font-size:0.8rem; margin-top:1rem; }
button:disabled { opacity:0.5; cursor:default; }
</style></head>
<body>
<a class="back" href="/">&larr; status</a>
<h1>Pending production approvals</h1>
<div id="list"><div class="empty">Loading&hellip;</div></div>

<script>
async function load() {
  const list = document.getElementById('list');
  let items;
  try {
    items = await (await fetch('/api/approvals')).json();
  } catch (e) {
    list.innerHTML = '<div class="empty">Failed to load — retrying&hellip;</div>';
    return;
  }
  if (!items.length) {
    list.innerHTML = '<div class="empty">Nothing waiting on you right now.</div>';
    return;
  }
  list.innerHTML = items.map(renderCard).join('');
}

function renderCard(item) {
  const links = [];
  if (item.staging_url) links.push(`<a href="${item.staging_url}" target="_blank">Open staging in new tab &#8599;</a>`);
  if (item.compare_url) links.push(`<a href="${item.compare_url}" target="_blank">Code diff</a>`);
  if (item.run_url) links.push(`<a href="${item.run_url}" target="_blank">CI run</a>`);
  links.push(`<a href="${item.issue_url}" target="_blank">GitHub issue</a>`);

  const preview = item.staging_url
    ? `<div class="preview-label">Live preview — this is the exact build you're approving:</div>
       <iframe class="preview-frame" src="${item.staging_url}" loading="lazy"></iframe>`
    : `<div class="preview-missing">No staging URL reported for this app — check the CI run.</div>`;

  return `
    <div class="card" id="card-${item.repo.replace('/', '-')}-${item.issue_number}">
      <h2>${item.app}</h2>
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
  card.querySelectorAll('button').forEach(b => b.disabled = true);
  try {
    const res = await fetch('/api/approvals/decide', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({repo, issue_number, decision}),
    });
    if (!res.ok) throw new Error(await res.text());
    card.innerHTML = `<div class="meta">Recorded "${decision}" — CI will pick it up within ~10s.</div>`;
  } catch (e) {
    card.querySelectorAll('button').forEach(b => b.disabled = false);
    alert('Failed to record decision: ' + e.message);
  }
}

load();
setInterval(load, 10000);
</script>
</body></html>"""


@app.get("/approvals", response_class=HTMLResponse)
def approvals_page():
    return APPROVALS_HTML
