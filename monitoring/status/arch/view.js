/* zorc architecture — overview and per-app detail.
   Routing is on the hash: no hash shows the fleet, #app-<name> shows one app.
   Both views are rendered from window.APPS (registry + live Coolify state),
   so nothing is hardcoded twice. */

const L = window.LOGOS || {};
const IMG = window.LOGO_IMG || {};
let APPS = window.APPS || [];
/* The snapshot carries what registry.yaml does not know: hostnames, build
   pack, exposed port, health path. Captured before APPS is replaced. */
const SNAP = {};
(window.APPS || []).forEach(a => { SNAP[a.n] = a; });

function mk(slug, size) {
  size = size || 14;
  const l = L[slug];
  if (!l) return "";
  const fill = l.color ? ` fill="${l.color}"` : "";
  return `<svg class="mk" width="${size}" height="${size}" viewBox="${l.vb}"${fill} role="img" aria-label="${l.name}">${l.body}</svg>`;
}
function mono(letters, color, size) {
  size = size || 14;
  return `<span class="mono" style="width:${size}px;height:${size}px;background:${color};color:#05070a;font-size:${Math.round(size * 0.5)}px">${letters}</span>`;
}
const INFISICAL = h => IMG.infisical
  ? `<span class="mk-infisical" style="height:${h || 15}px;width:${Math.round((h || 15) * 1.6)}px;background-image:url(${IMG.infisical})" role="img" aria-label="Infisical"></span>`
  : mono("IF", "#C9D1D9");
const LIVEKIT = h => IMG.livekit
  ? `<img class="mk-livekit" src="${IMG.livekit}" alt="LiveKit" style="height:${h || 13}px;width:auto">`
  : mono("LK", "#4C6FFF");

/* zorc's own services and generic concepts carry no third-party brand, so they
   get geometric marks in the theme's semantic colours rather than a fake logo. */
const GEO = {
  globe: c => `<svg class="mk" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="${c}" stroke-width="1.5"><circle cx="8" cy="8" r="6"/><path d="M2 8h12M8 2c1.8 2 1.8 10 0 12M8 2c-1.8 2-1.8 10 0 12"/></svg>`,
  ai: c => `<svg class="mk" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="${c}" stroke-width="1.6"><rect x="4.5" y="4.5" width="7" height="7" rx="1.2"/><path d="M8 1.5v3M8 11.5v3M1.5 8h3M11.5 8h3"/></svg>`,
  zorc: c => `<svg class="mk" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="${c}" stroke-width="1.5"><path d="M3 3h10L3 13h10"/></svg>`,
  server: c => `<svg class="mk" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="${c}" stroke-width="1.5"><rect x="2" y="3" width="12" height="4" rx="1"/><rect x="2" y="9" width="12" height="4" rx="1"/></svg>`,
  lock: c => `<svg class="mk" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="${c}" stroke-width="1.5"><rect x="3" y="7" width="10" height="7" rx="1.5"/><path d="M5.5 7V5a2.5 2.5 0 015 0v2"/></svg>`,
  none: c => `<svg class="mk" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="${c}" stroke-width="1.5"><circle cx="8" cy="8" r="6"/><path d="M4 12L12 4"/></svg>`,
};

const SHARED = [
  [mk("postgresql", 15), "PostgreSQL 18", "1000 MB", "One database and one role per app; no cross-app access, no exceptions. Apps get a dedicated instance from <code>database: true</code>, with credentials generated and injected rather than handled by the app author."],
  [mk("redis", 15), "Redis 7", "256 MB", "One logical DB index per app, recorded on the app's registry entry. Only infisical uses it. Treated as cache and queue, never a system of record."],
  [mk("traefikproxy", 15), "Traefik", "100 MB", "Managed by Coolify, one per node, routes purely by hostname. Healthchecks always target port 8080, and the final image needs curl or wget or the check fails even when the app is healthy."],
  [GEO.ai("#2fe6b8"), "ai-gateway", "384 MB", "Shared free-tier LLM proxy at <code>ai-gateway-internal:8080/auto/v1</code>, spreading across Groq, Google AI Studio and OpenRouter with failover. No app ever holds a provider key."],
  [GEO.zorc("#5eb1ff"), "zorc-search", "768 MB", "Internal web search over openserp, benchmark winner against SearXNG, degoog and Vane."],
  [INFISICAL(), "Infisical", "918 MB", "Self-hosted secret store, the intended default path for every app secret. Backed by its own Postgres and the shared Redis."],
  [mk("coolify", 15), "Coolify", "on servingz", "Orchestrates every <code>backend: coolify</code> node over root SSH, builds with Nixpacks, and owns Traefik. zorc drives it through its API rather than the dashboard."],
  [mk("github", 15), "GitHub", "source of truth", "Every app deploys from a repo and a branch. Private repos go through a GitHub App; <code>registry.yaml</code> is the single source of truth and each app entry is a reviewed PR."],
];

const EXTERNAL = [
  [mk("vercel", 15), "Vercel", "mcp.ivay.tech", "The original IVAY assistant, left in place on purpose. It serves a stale build &mdash; only <code>/</code> and <code>/debug</code> answer, while <code>/health</code>, <code>/ready</code>, <code>/version</code> and <code>/openapi.json</code> all 404 despite existing in source. The deployment sits in a Vercel account outside this one, so it cannot be inspected or moved from here."],
  [mk("neon", 15), "Neon", "the old assistant's database", "Holds 3 configured shops and 50 sessions behind that Vercel deployment. Not reachable from this account: <code>vercel env pull</code> returns only Vercel's own system variables. The rows were deliberately not migrated &mdash; the sessions are analytics, and storefront tokens regenerate at app install."],
  [mk("railway", 15), "Railway", "app.ivay.tech", "Broken. The Railway hostname itself returns 404 and no certificate is served, so HTTPS fails outright and visitors reach a dead link."],
  [mk("nginx", 15), "The IVAY pilot stack", "on hostinger-vps", "<code>ivay.tech</code> itself: nginx, api, postgres and contact containers running on the node but absent from registry.yaml. Working, yet zorc's budget can neither see nor protect it."],
  [LIVEKIT(15), "", "voice &middot; with Asterisk, on hostinger-vps", mk("asterisk", 12) + " Real-time voice and SIP for the IVAY caller work, on ports 5060 and 7880-7882. Outside zorc, like the rest of the pilot stack."],
  [mk("shopify", 15), "Shopify", "three storefronts", "test-for-ivay-gmbh, fechtklingen-dupe and turtle-beach, each with a storefront token auto-provisioned at app install."],
];

const NOTES = {
  "servingz": "Home network, no public IP. Holds the zorc git checkout, the shared Postgres and Redis, and the MCP server. Every public hostname on it arrives through cloudflared.",
  "hostinger-vps": "Public IPv4, so it takes direct ingress and real custom domains. Also runs the IVAY production stack outside zorc, which is why live memory looks healthier than the declared budget.",
  "contabo": "Added 2 November 2026. 4 vCPU, 7941 MB, 96 GB disk, public IPv4 only. Intended primary tenant is the Lifeline Veterinary Hospital platform, though no memory is fenced for it yet.",
  "rtx5090": "Not zorc-owned. Already runs 40+ of the user's own containers the budget cannot see, so the ceiling is a sanity limit on zorc's declared apps alone. AI and API backends only, never general-purpose apps.",
  "bitbots_gpu": "Personal AI workstation, shared and only partly zorc's. Runs its own voice and inference services alongside zorc's.",
  "jetson-thor": "Edge device. SSH arrives through Tailscale's own interception rather than authorized_keys, so confirm the trust boundary before putting anything privileged here.",
};
const GPUS = { rtx5090: "RTX 5090", bitbots_gpu: "2x RTX 3090", "jetson-thor": "Jetson AGX Thor" };

/* Built from registry.yaml at request time, so placement, memory and wiring
   are never stale. Falls back to the baked snapshot if the API is unreachable,
   which keeps the page useful rather than empty. */
let NODE_META = {}, NODE_ORDER = [];
function buildNodes(nodes) {
  NODE_META = {}; NODE_ORDER = [];
  Object.keys(nodes).forEach(n => {
    const v = nodes[n];
    const pub = !!v.has_public_ip;
    NODE_META[n] = {
      provider: v.provider || "unknown",
      backend: v.backend || "coolify",
      orch: (v.backend === "zorc-agent") ? "docker" : "coolify",
      role: v.is_control_plane ? "control plane" : null,
      gpu: GPUS[n] || null,
      pub: pub,
      net: pub ? "public IP" : (v.tailscale_ip ? "tunnel + tailnet" : "tailnet only"),
      addr: pub ? (v.ip + (v.tailscale_ip ? " &middot; tailnet " + v.tailscale_ip : ""))
                : (v.tailscale_ip ? "tailnet " + v.tailscale_ip : "tailnet only"),
      ceiling: Math.round((v.usable_mb || 0) * (v.max_utilisation || 0.8)) || 1,
      note: NOTES[n] || "",
    };
    NODE_ORDER.push(n);
  });
}


const byName = Object.fromEntries(APPS.map(a => [a.n, a]));
const slug = n => "app-" + n.replace(/[^A-Za-z0-9-]/g, "-");

function stateOf(a) {
  if (a.st === "running") return ["ok", "running"];
  if (a.st === "exited") return ["warn", "stopped"];
  if (a.st) return ["warn", a.st];
  return ["none", NODE_META[a.node] && NODE_META[a.node].backend === "zorc-agent" ? "zorc-agent" : "declared"];
}
function wires(a) {
  const out = [];
  if (a.db) out.push(`<span class="app-wires">${mk("postgresql", 11)}postgres</span>`);
  if (a.redis) out.push(`<span class="app-wires">${mk("redis", 11)}redis</span>`);
  if (a.ai) out.push(`<span class="app-wires">${GEO.ai("#2fe6b8")}ai</span>`);
  return out.join("");
}

/* ---------- overview ---------- */
function renderOverview() {
  const totals = {};
  NODE_ORDER.forEach(n => totals[n] = APPS.filter(a => a.node === n).reduce((s, a) => s + a.mb, 0));
  const declared = Object.values(totals).reduce((s, v) => s + v, 0);
  const gpu = totals.rtx5090 + totals.bitbots_gpu + totals["jetson-thor"];

  const nodes = NODE_ORDER.map(name => {
    const m = NODE_META[name];
    const apps = APPS.filter(a => a.node === name).sort((x, y) => y.mb - x.mb);
    const pct = Math.min(100, Math.round((totals[name] / m.ceiling) * 100));
    const cards = apps.map(a => {
      const [cls, label] = stateOf(a);
      const dom = a.urls.find(u => u.includes("zaindroid.me")) || a.urls[0];
      return `
      <a class="app" href="#${slug(a.n)}">
        <div class="app-top"><span class="app-name">${a.n}</span><span class="badge ${cls}">${label}</span></div>
        <div class="app-sub"><span class="mb">${a.mb.toLocaleString()} MB</span>${wires(a)}</div>
        ${dom ? `<div class="app-dom">${mk("cloudflare", 11)}${dom.replace(/^https?:\/\//, "")}</div>` : ""}
      </a>`;
    }).join("");
    return `
    <div class="group-title">${name} <span class="n">&mdash; ${apps.length} service${apps.length === 1 ? "" : "s"}</span></div>
    <div class="glass node">
      <div class="node-head">
        <div class="node-name">${mk(m.gpu ? "nvidia" : "ubuntu", 17)}${name}</div>
        <div class="node-meta">${m.addr}</div>
      </div>
      <div class="tags">
        <span class="tag">${m.provider}</span>
        <span class="tag">${mk(m.orch, 11)}${m.backend}</span>
        <span class="tag">${mk("tailscale", 11)}${m.net}</span>
        ${m.role ? `<span class="tag role">${GEO.zorc("#5eb1ff")}${m.role}</span>` : ""}
        ${m.gpu ? `<span class="tag">${mk("nvidia", 11)}${m.gpu}</span>` : ""}
      </div>
      <div class="bar-row">
        <div class="bar-label"><span>declared ${totals[name].toLocaleString()} MB</span><span>ceiling ${m.ceiling.toLocaleString()} MB &middot; ${pct}%</span></div>
        <div class="bar"><i class="${pct >= 95 ? "hot" : ""}" style="width:${pct}%"></i></div>
      </div>
      <div class="node-note">${m.note}</div>
      <div class="grid">${cards}</div>
    </div>`;
  }).join("");

  return `
  <div class="glass hero">
    <div class="orb"></div>
    <div class="hero-text">
      <h1>Six nodes, ${APPS.length} declared services, one control plane</h1>
      <div class="sub">
        Every public hostname enters through <b>Cloudflare</b>. Only <b>hostinger-vps</b> and
        <b>contabo</b> hold a public IP; everything else is reachable only over the
        <b>Cloudflare Tunnel</b> or the <b>tailnet</b>. <b>${declared.toLocaleString()}&nbsp;MB</b>
        declared across the fleet, <b>${gpu.toLocaleString()}&nbsp;MB</b> of it on the three GPU machines.
        <br><span class="hint">Select any service for how it is built, wired and reached.</span>
      </div>
    </div>
  </div>

  <div class="group-title">How a request reaches a container <span class="n">&mdash; the whole fleet</span></div>
  <div class="glass">${fleetDiagram(APPS, NODE_META, NODE_ORDER)}</div>

  ${nodes}

  <div class="group-title">Shared infrastructure <span class="n">&mdash; on servingz, budgeted separately</span></div>
  <div class="flat">${SHARED.map(([icon, name, mb, d]) => `
    <div class="glass svc"><div class="svc-name">${icon}<span>${name}</span><span class="svc-mb">${mb}</span></div>
    <div class="svc-detail">${d}</div></div>`).join("")}</div>

  <div class="group-title">Off platform <span class="n">&mdash; absent from registry.yaml, unprotected by its budget</span></div>
  <div class="flat">${EXTERNAL.map(([icon, name, mb, d]) => `
    <div class="glass svc offp"><div class="svc-name">${icon}<span>${name}</span><span class="svc-mb">${mb}</span></div>
    <div class="svc-detail">${d}</div></div>`).join("")}</div>

  <div class="footer-meta">
    Built from registry.yaml and live Coolify state, 2 November 2026. Theme tokens taken from monitoring/status/theme.css.
    <br>Brand marks are the official ones at their official colours, lightened only where a brand's own hex is unreadable on this background.
    <br>&ldquo;Declared&rdquo; is the registry budget, not live usage: the GPU nodes in particular run containers zorc does not know about.
  </div>`;
}

/* ---------- one app ---------- */
function renderApp(a) {
  const m = NODE_META[a.node];
  const [cls, label] = stateOf(a);
  const agent = m.backend === "zorc-agent";



  const fact = (icon, k, v) => `<div class="fact"><div class="fact-k">${icon}${k}</div><div class="fact-v">${v}</div></div>`;
  const repoUrl = a.repo ? "https://" + a.repo.replace(/^https?:\/\//, "") : null;

  return `
  <a class="back" href="#">${"&larr;"} All services</a>
  <div class="glass hero">
    <div class="orb ${cls === "ok" ? "" : cls}"></div>
    <div class="hero-text">
      <h1>${a.n}</h1>
      <div class="sub">
        <span class="badge ${cls}">${label}</span>
        &nbsp;${a.mb.toLocaleString()} MB on <b>${a.node}</b>${m.gpu ? ` (${m.gpu})` : ""}
        &middot; owned by <b>${a.owner || "unknown"}</b>
        ${a.crit ? ' &middot; <b style="color:var(--crit)">critical</b>' : ""}
      </div>
    </div>
  </div>

  <div class="group-title">How a request reaches it</div>
  <div class="glass">${appDiagram(a, m)}</div>

  <div class="group-title">Facts</div>
  <div class="glass facts">
    ${fact(mk(m.gpu ? "nvidia" : "ubuntu", 13), "Node", `${a.node} &mdash; ${m.provider}, ${m.net}`)}
    ${fact(mk(m.orch, 13), "Deployed by", agent
      ? "zorc-agent &mdash; direct Docker over SSH, Dockerfile only, no Traefik and no Nixpacks"
      : "Coolify &mdash; built with Nixpacks, fronted by Traefik")}
    ${fact(mk("docker", 13), "Memory", `${a.mb.toLocaleString()} MB declared in registry.yaml, enforced as a cgroup limit`)}
    ${fact(repoUrl ? mk("github", 13) : GEO.none("#566270"), "Source",
      repoUrl ? `<a href="${repoUrl}" target="_blank" rel="noopener">${a.repo}</a>, branch main` : "not declared in the registry")}
    ${fact(a.urls.length ? mk("cloudflare", 13) : GEO.lock("#566270"), "Reached at",
      a.urls.length ? a.urls.map(u => `<a href="${u}" target="_blank" rel="noopener">${u.replace(/^https?:\/\//, "")}</a>`).join("<br>")
                    : "no hostname &mdash; internal to the node")}
    ${fact(a.db ? mk("postgresql", 13) : GEO.none("#566270"), "Database",
      a.db ? "A dedicated Postgres instance, provisioned by <code>database: true</code>. Credentials are generated and injected; the app never handles them."
           : "None. <code>DATABASE_URL</code> is simply not set.")}
    ${a.redis ? fact(mk("redis", 13), "Cache", "One logical index on the shared Redis, set as <code>REDIS_URL</code>.") : ""}
    ${fact(a.ai ? GEO.ai("#2fe6b8") : GEO.none("#566270"), "AI",
      a.ai ? "Wired to the shared gateway at <code>ai-gateway-internal:8080/auto/v1</code>, which spreads across free providers and fails over. The app holds no provider key."
           : "None. No LLM credentials are injected.")}
    ${a.dep.length ? fact(GEO.zorc("#5eb1ff"), "Depends on",
      a.dep.map(d => byName[d] ? `<a href="#${slug(d)}">${d}</a>` : d).join(", ")) : ""}
    ${(() => { const r = APPS.filter(x => x.dep.includes(a.n));
      return r.length ? fact(GEO.zorc("#5eb1ff"), "Depended on by", r.map(x => `<a href="#${slug(x.n)}">${x.n}</a>`).join(", ")) : ""; })()}
  </div>

  <div class="group-title">Others on ${a.node}</div>
  <div class="grid">${APPS.filter(x => x.node === a.node && x.n !== a.n).sort((x, y) => y.mb - x.mb).map(x => {
    const [c, l] = stateOf(x);
    return `<a class="app" href="#${slug(x.n)}">
      <div class="app-top"><span class="app-name">${x.n}</span><span class="badge ${c}">${l}</span></div>
      <div class="app-sub"><span class="mb">${x.mb.toLocaleString()} MB</span>${wires(x)}</div></a>`;
  }).join("") || '<div class="node-note">Nothing else is declared on this node.</div>'}</div>`;
}

/* ---------- routing ---------- */
function route() {
  const h = (location.hash || "").replace(/^#/, "");
  const app = APPS.find(a => slug(a.n) === h);
  document.getElementById("view").innerHTML = app ? renderApp(app) : renderOverview();
  document.title = app ? `${app.n} — zorc Architecture` : "zorc Architecture";
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);

document.getElementById("legend").innerHTML = [
  ["cloudflare", "ingress"], ["coolify", "orchestration"], ["traefikproxy", "routing"],
  ["docker", "runtime"], ["postgresql", "database"], ["redis", "cache"],
  ["tailscale", "private net"], ["nvidia", "gpu"],
].map(([s, label]) => `<span>${mk(s, 12)}${label}</span>`).join("")
 + `<span>${GEO.ai("#2fe6b8")}ai gateway</span>`;

/* The snapshot shipped with the page is the fallback, not the source. */
function bootFromSnapshot() {
  buildNodes({
    "servingz": { provider: "self-hosted", backend: "coolify", is_control_plane: true,
      has_public_ip: false, tailscale_ip: "100.115.156.84", usable_mb: 27500, max_utilisation: 0.84 },
    "hostinger-vps": { provider: "hostinger", backend: "coolify", has_public_ip: true,
      ip: "2.25.105.110", tailscale_ip: "100.112.175.41", usable_mb: 6450, max_utilisation: 0.80 },
    "contabo": { provider: "contabo", backend: "coolify", has_public_ip: true,
      ip: "167.86.78.234", tailscale_ip: "100.105.206.126", usable_mb: 7041, max_utilisation: 0.80 },
    "rtx5090": { provider: "personal", backend: "zorc-agent", has_public_ip: false,
      tailscale_ip: "100.124.93.99", usable_mb: 125851, max_utilisation: 0.50 },
    "bitbots_gpu": { provider: "personal", backend: "zorc-agent", has_public_ip: false,
      tailscale_ip: "100.118.75.128", usable_mb: 126729, max_utilisation: 0.50 },
    "jetson-thor": { provider: "personal", backend: "zorc-agent", has_public_ip: false,
      tailscale_ip: "100.119.248.100", usable_mb: 123771, max_utilisation: 0.50 },
  });
}

(async function boot() {
  try {
    const r = await fetch("/api/architecture", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const d = await r.json();
    if (d.apps && d.apps.length) {
      APPS = d.apps;
      APPS.forEach(a => {
        const o = SNAP[a.n];
        if (!o) return;
        if (!a.urls || !a.urls.length) a.urls = o.urls || [];
        a.pack = a.pack || o.pack; a.port = a.port || o.port;
        a.health = a.health || o.health; a.branch = a.branch || o.branch;
        a.st = a.st || o.st;
      });
    }
    if (d.nodes && Object.keys(d.nodes).length) buildNodes(d.nodes);
    else bootFromSnapshot();
  } catch (e) {
    bootFromSnapshot();
    const v = document.getElementById("view");
    if (v) v.dataset.stale = "1";
  }
  route();

  /* Container state is the one thing worth a second round trip: it changes
     without any registry edit. Same endpoint the Apps tab uses. */
  try {
    const r = await fetch("/api/apps", { cache: "no-store" });
    if (!r.ok) return;
    const body = await r.json();
    const live = Array.isArray(body) ? body : (body.apps || []);
    const m = {};
    live.forEach(x => { if (x && x.name) m[x.name] = x; });
    let changed = false;
    APPS.forEach(a => {
      const x = m[a.n];
      if (!x) return;
      if (x.status) {
        const st = String(x.status).split(":")[0];
        if (st !== a.st) { a.st = st; changed = true; }
      }
      if (x.fqdn) {
        const urls = String(x.fqdn).split(",").map(u => u.trim()).filter(Boolean);
        if (urls.join() !== (a.urls || []).join()) { a.urls = urls; changed = true; }
      }
    });
    if (changed) route();
  } catch (e) { /* keep what we rendered */ }
})();
