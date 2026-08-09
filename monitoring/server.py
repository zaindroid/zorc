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
from fastapi.responses import HTMLResponse, JSONResponse, Response
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


@app.get("/theme.css")
def theme_css():
    return Response((STATUS_DIR / "theme.css").read_text(), media_type="text/css")


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
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <a class="brand" href="/"><span class="pip" id="brand-pip"></span> SERVINGZ</a>
    <div class="tabs">
      <a href="/">Status</a>
      <a href="/approvals" class="active">Approvals</a>
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
async function load() {
  const list = document.getElementById('list');
  const orb = document.getElementById('hero-orb');
  const title = document.getElementById('hero-title');
  let items;
  try {
    items = await (await fetch('/api/approvals', { cache: 'no-store' })).json();
  } catch (e) {
    title.textContent = "Can't reach the approvals service";
    orb.className = 'orb crit';
    return;
  }

  if (!items.length) {
    orb.className = 'orb ok';
    title.textContent = 'Nothing waiting on you';
    list.innerHTML = '<div class="empty">All caught up — new deploys will show up here the moment they pass staging + regression.</div>';
    return;
  }

  orb.className = 'orb warn';
  title.textContent = items.length === 1
    ? '1 deploy waiting on your review'
    : `${items.length} deploys waiting on your review`;

  // Don't clobber cards mid-decision (buttons disabled) on the poll tick.
  const existing = new Set([...list.querySelectorAll('.card[data-pending]')].map(el => el.id));
  list.innerHTML = items.map(item => renderCard(item, existing)).join('');
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

load();
setInterval(load, 10000);
</script>
</body>
</html>"""


@app.get("/approvals", response_class=HTMLResponse)
def approvals_page():
    return APPROVALS_HTML
