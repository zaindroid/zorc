/* Flow diagrams in the ByteByteGo idiom: dashed orthogonal connectors, a
   numbered badge on every hop, dashed group containers with a pill title, and
   brand marks carrying the meaning. Each edge is labelled with the protocol or
   payload that actually crosses it, so the picture states a mechanism instead
   of listing names. Palette stays the status page's. */

const DG = (() => {
  const L = window.LOGOS || {};
  const C = { line: "#566270", dim: "#90a0ac", text: "#dce6ec", faint: "#566270",
              accent: "#5eb1ff", ok: "#2fe6b8", warn: "#ffb454", crit: "#ff5c72" };

  function logoG(slug, x, y, size) {
    const l = L[slug];
    if (!l) return "";
    const vbw = parseFloat(l.vb.split(/[\s,]+/)[2]) || 24;
    const k = size / vbw;
    const fill = l.color ? ` fill="${l.color}"` : "";
    return `<g transform="translate(${x},${y}) scale(${k.toFixed(4)})"${fill}>${l.body}</g>`;
  }
  const GEOP = {
    globe: '<circle cx="8" cy="8" r="6"/><path d="M2 8h12M8 2c1.8 2 1.8 10 0 12M8 2c-1.8 2-1.8 10 0 12"/>',
    ai: '<rect x="4.5" y="4.5" width="7" height="7" rx="1.2"/><path d="M8 1.5v3M8 11.5v3M1.5 8h3M11.5 8h3"/>',
    zorc: '<path d="M3 3h10L3 13h10"/>',
    server: '<rect x="2" y="3" width="12" height="4" rx="1"/><rect x="2" y="9" width="12" height="4" rx="1"/><path d="M4.5 5h.01M4.5 11h.01"/>',
    lock: '<rect x="3" y="7" width="10" height="7" rx="1.5"/><path d="M5.5 7V5a2.5 2.5 0 015 0v2"/>',
    user: '<circle cx="8" cy="5.5" r="2.6"/><path d="M2.8 13.5a5.2 5.2 0 0110.4 0"/>',
  };
  function geoG(name, color, x, y, size) {
    const k = size / 16;
    return `<g transform="translate(${x},${y}) scale(${k.toFixed(4)})" fill="none" stroke="${color}" stroke-width="1.5" stroke-linecap="round">${GEOP[name] || ""}</g>`;
  }
  const icon = (spec, x, y, size) =>
    !spec ? "" : spec.geo ? geoG(spec.geo, spec.color || C.dim, x, y, size) : logoG(spec, x, y, size);
  const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const txt = (x, y, s, fill, size, anchor, weight) =>
    `<text x="${x}" y="${y}" fill="${fill}" font-family="ui-monospace,monospace" font-size="${size}"
      ${anchor ? `text-anchor="${anchor}"` : ""} ${weight ? `font-weight="${weight}"` : ""}>${esc(s)}</text>`;

  /* A pill label, as the reference uses for every group heading. */
  function pill(cx, y, text, color) {
    const w = text.length * 6.1 + 22;
    return `<g>
      <rect x="${cx - w / 2}" y="${y}" width="${w}" height="19" rx="9.5" fill="${color}"/>
      ${txt(cx, y + 13.5, text, "#05070a", 10, "middle", "700")}</g>`;
  }
  /* Dashed container with its title pill straddling the top edge. */
  function group(g) {
    return `<g>
      <rect x="${g.x}" y="${g.y}" width="${g.w}" height="${g.h}" rx="12"
        fill="rgba(255,255,255,0.016)" stroke="${g.color}" stroke-width="1"
        stroke-dasharray="5 4" opacity="0.9"/>
      ${pill(g.x + g.w / 2, g.y - 9.5, g.title, g.color)}</g>`;
  }

  /* A component box: mark on the left, name, then up to two detail lines.
     `fill` makes it a solid hero box, as the reference does for its Server. */
  function box(b) {
    const stroke = b.fill ? b.fill : (b.accent || "rgba(255,255,255,0.15)");
    const bg = b.fill ? b.fill : "rgba(255,255,255,0.05)";
    const title = b.fill ? "#05070a" : (b.accent || C.text);
    const sub = b.fill ? "rgba(5,7,10,0.72)" : C.faint;
    const lines = (b.sub || "").split("\n").filter(Boolean).slice(0, 2);
    return `<g>
      <rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="10" fill="${bg}" stroke="${stroke}" stroke-width="1"/>
      ${b.icon ? icon(b.icon, b.x + 11, b.y + 10, 16) : ""}
      ${txt(b.x + (b.icon ? 34 : 11), b.y + 22, b.title, title, 11.5, null, "600")}
      ${lines.map((s, i) => txt(b.x + 11, b.y + 38 + i * 11.5, s, sub, 8.6)).join("")}
    </g>`;
  }

  /* Numbered badge, the device that makes the reference readable. */
  function badge(x, y, n, color) {
    return `<g>
      <circle cx="${x}" cy="${y}" r="8.5" fill="${color}"/>
      ${txt(x, y + 3.4, String(n), "#05070a", 9.5, "middle", "700")}</g>`;
  }

  const side = (b, s) => ({
    r: [b.x + b.w, b.y + b.h / 2], l: [b.x, b.y + b.h / 2],
    t: [b.x + b.w / 2, b.y], b: [b.x + b.w / 2, b.y + b.h],
  }[s]);

  /* Orthogonal dashed connector with a numbered badge and a label.
     mode: "h" straight across, "v" straight down, "hv" across then down,
     "vh" down then across, "around" out of the bottom, under, and back up. */
  function conn(a, b, o) {
    o = o || {};
    const color = o.color || C.line;
    let d, bx, by;
    if (o.mode === "v") {
      const [x1, y1] = side(a, "b"), [x2, y2] = side(b, "t");
      d = `M${x1} ${y1}V${y2}`; bx = x1; by = (y1 + y2) / 2;
    } else if (o.mode === "vh") {
      const [x1, y1] = side(a, "b"), [x2, y2] = side(b, "l");
      d = `M${x1} ${y1}V${y2}H${x2}`; bx = x1; by = (y1 + y2) / 2;
    } else if (o.mode === "hv") {
      const [x1, y1] = side(a, "r"), [x2, y2] = side(b, "t");
      d = `M${x1} ${y1}H${x2}V${y2}`; bx = (x1 + x2) / 2; by = y1;
    } else if (o.mode === "around") {
      const [x1, y1] = side(a, "b"), [x2, y2] = side(b, "b");
      const dip = o.dip || 34;
      d = `M${x1} ${y1}V${y1 + dip}H${x2}V${y2}`; bx = (x1 + x2) / 2; by = y1 + dip;
    } else {
      const [x1, y1] = side(a, "r"), [x2, y2] = side(b, "l");
      d = y1 === y2 ? `M${x1} ${y1}H${x2}` : `M${x1} ${y1}H${(x1 + x2) / 2}V${y2}H${x2}`;
      bx = (x1 + x2) / 2; by = y1 === y2 ? y1 : (y1 + y2) / 2;
    }
    const lx = bx;
    const ly = o.labelY !== undefined ? o.labelY : (o.labelAt === "below" ? by + 26 : by - 14);
    return `<g>
      <path d="${d}" fill="none" stroke="${color}" stroke-width="1.2" stroke-dasharray="5 4"
        marker-end="url(#dg-arrow)" opacity="0.95"/>
      ${o.n ? badge(bx, by, o.n, color) : ""}
      ${o.label ? txt(lx, ly, o.label, C.faint, 8.6, "middle") : ""}
    </g>`;
  }

  /* Fan-out as an orthogonal bus rather than N overlapping elbows: one trunk
     down out of the source, a horizontal run, then a short drop into the top of
     each target. Without this every edge leaves the same point and the badges
     stack on top of one another. */
  function bus(from, targets, o) {
    o = o || {};
    const trunkX = from.x + from.w / 2, trunkY = from.y + from.h;
    const busY = o.busY;
    const xs = targets.map(t => t.box.x + t.box.w / 2);
    const minX = Math.min(trunkX, ...xs), maxX = Math.max(trunkX, ...xs);
    let out = `<path d="M${trunkX} ${trunkY}V${busY}" fill="none" stroke="${o.color || C.line}"
      stroke-width="1.2" stroke-dasharray="5 4"/>`;
    if (targets.length > 1 || xs[0] !== trunkX)
      out += `<path d="M${minX} ${busY}H${maxX}" fill="none" stroke="${o.color || C.line}"
        stroke-width="1.2" stroke-dasharray="5 4"/>`;
    targets.forEach(t => {
      const x = t.box.x + t.box.w / 2, c = t.color || C.line;
      out += `<path d="M${x} ${busY}V${t.box.y}" fill="none" stroke="${c}" stroke-width="1.2"
        stroke-dasharray="5 4" marker-end="url(#dg-arrow)"/>`;
      out += badge(x, busY + (t.box.y - busY) / 2, t.n, c);
      if (t.label) out += txt(x, t.box.y - 8, t.label, C.faint, 8.6, "middle");
    });
    return out;
  }

  function wrap(w, h, inner, minw) {
    return `<div class="dg"><svg viewBox="0 0 ${w} ${h}" style="min-width:${minw}px" role="img"
      aria-label="Architecture flow diagram">
      <defs><marker id="dg-arrow" markerWidth="7" markerHeight="7" refX="6.2" refY="3.5" orient="auto">
        <path d="M0 0l6 3.5L0 7z" fill="${C.line}"/></marker></defs>${inner}</svg></div>`;
  }
  return { box, conn, group, pill, badge, bus, wrap, icon, txt, C };
})();

/* ---------- one app ---------- */

/* Dependency name -> brand mark. Anything absent falls back to a geometric
   mark rather than borrowing an unrelated logo. */
const STACK_LOGO = {
  next: "nextdotjs", react: "react", vue: "vuedotjs", svelte: "svelte",
  "@angular/core": "angular", astro: "astro", remix: "remix", vite: "vite",
  tailwindcss: "tailwindcss", "htmx.org": null, alpinejs: null,
  express: "express", fastify: "fastify", "@nestjs/core": "nestjs",
  "socket.io": "socketdotio", koa: null, hono: null, ws: null,
  "@modelcontextprotocol/sdk": null, "discord.js": null, "node-telegram-bot-api": null,
  "@prisma/client": "prisma", prisma: "prisma", "drizzle-orm": null, typeorm: null,
  sequelize: null, pg: "postgresql", mongoose: "mongodb", "better-sqlite3": "sqlite",
  redis: "redis", ioredis: "redis", "@libsql/client": "sqlite",
  fastapi: "fastapi", flask: "flask", django: "django", uvicorn: "python",
  gunicorn: "python", streamlit: "python", aiohttp: "python", starlette: "python",
  sqlalchemy: "python", psycopg: "postgresql", asyncpg: "postgresql",
  pydantic: "python", alembic: "python", duckdb: null, gradio: "python", jinja2: "python",
  typescript: "typescript", esbuild: null, webpack: null,
};
const LANG_LOGO = { node: "nodedotjs", python: "python", go: "go", static: "nginx" };
const PRETTY = {
  "@angular/core": "Angular", "@nestjs/core": "NestJS", "@prisma/client": "Prisma",
  "socket.io": "Socket.IO", tailwindcss: "Tailwind", "better-sqlite3": "SQLite",
  "@modelcontextprotocol/sdk": "MCP SDK", "@libsql/client": "libSQL", next: "Next.js",
  pg: "node-postgres", ws: "WebSocket", "drizzle-orm": "Drizzle", "htmx.org": "htmx",
  jinja2: "Jinja2", fastapi: "FastAPI", psycopg: "psycopg", asyncpg: "asyncpg",
};
const pretty = d => PRETTY[d] || d.charAt(0).toUpperCase() + d.slice(1);

function appDiagram(a, meta) {
  const C = DG.C;
  const agent = meta.backend === "zorc-agent";
  const st = (window.STACK || {})[a.n] || { lang: "unknown", fe: [], be: [], data: [], bits: [] };
  const B = [], G = [], E = [], BUS = [];
  const W = 128, H = 56, GAP = 46, x0 = 26;

  /* Deploy time is a different lifecycle from a request, so it gets its own
     band and unnumbered arrows instead of joining the request's sequence. */
  const bY = 34;
  const repoShort = (a.repo || "").replace("github.com/", "") || "not declared";
  const src = { x: x0, y: bY + 22, w: 168, h: H, title: "GitHub",
    sub: repoShort + "\nbranch " + (a.branch || "main"), icon: "github" };
  const packTitle = a.pack === "dockerfile" ? "Dockerfile" : a.pack === "nixpacks" ? "Nixpacks" : "build";
  const packSub = a.pack === "dockerfile" ? "image recipe in the repo"
    : a.pack === "nixpacks" ? "Coolify detects\nthe toolchain" : "not recorded";
  const pack = { x: x0 + 168 + GAP, y: bY + 22, w: 150, h: H, title: packTitle, sub: packSub,
    icon: a.pack === "dockerfile" ? "docker" : { geo: "server", color: C.warn } };
  const img = { x: x0 + 168 + GAP + 150 + GAP, y: bY + 22, w: 150, h: H,
    title: "image", sub: "runs on " + a.node, icon: "docker" };
  B.push(src, pack, img);
  E.push([src, pack, { label: "push", color: C.warn }]);
  E.push([pack, img, { label: "build", color: C.warn }]);
  G.push({ x: x0 - 16, y: bY, w: 168 + GAP + 150 + GAP + 150 + 32, h: H + 44,
    title: "DEPLOY TIME", color: C.warn });

  /* Request time. */
  const gy = bY + H + 96;
  const chain = [{ title: "User", sub: "browser, agent", icon: { geo: "user" } }];
  if (a.urls.length) {
    chain.push({ title: "Cloudflare", sub: "DNS, TLS\nAccess policy", icon: "cloudflare" });
    chain.push(meta.pub
      ? { title: "A record", sub: "direct origin\n" + (meta.addr || "").split("&middot;")[0].trim(),
          icon: { geo: "server", color: C.accent } }
      : { title: "cloudflared", sub: "tunnel\nno public IP", icon: "cloudflare" });
    if (!agent) chain.push({ title: "Traefik", sub: "routes by hostname\nhealthcheck " + (a.health || "/health"),
      icon: "traefikproxy" });
  } else {
    chain.push({ title: "no hostname", sub: "internal to the node", icon: { geo: "lock" } });
  }
  const step = W + GAP, firstChain = B.length;
  chain.forEach((c, i) => B.push(Object.assign(c, { x: x0 + i * step, y: gy + 22, w: W, h: H })));
  const ingressW = chain.length * step - GAP + 32;
  G.push({ x: x0 - 16, y: gy, w: ingressW, h: H + 44,
    title: a.urls.length ? "REQUEST TIME" : "NOT EXPOSED", color: C.accent });
  const port = a.port || "8080";
  const labels = a.urls.length
    ? ["request", meta.pub ? "TLS, policy" : "through tunnel", "to :" + port]
    : [""];
  for (let i = 0; i < chain.length - 1; i++)
    E.push([B[firstChain + i], B[firstChain + i + 1],
      { n: i + 1, label: labels[i] || "", color: C.accent, labelY: gy + 16 }]);

  /* The container itself. */
  const appY = gy + H + 104;
  const app = { x: x0 + (chain.length - 1) * step, y: appY, w: W + 40, h: H + 6, title: a.n,
    sub: a.mb.toLocaleString() + " MB" + (agent ? "\ndynamic port" : "\nlistens on :" + port),
    icon: LANG_LOGO[st.lang] || "docker", fill: C.ok };
  B.push(app);
  E.push([B[firstChain + chain.length - 1], app,
    { n: chain.length, label: agent ? "direct" : "proxied", mode: "v", color: C.ok }]);
  E.push([img, app, { label: "deployed as", color: C.warn, mode: "vh", labelY: appY - 12 }]);

  let bottom = appY + app.h;

  /* What runs inside: only what the repository's manifest actually declares. */
  const cells = [];
  st.fe.forEach(d => cells.push([d, "frontend", C.accent]));
  st.be.forEach(d => cells.push([d, "backend", C.ok]));
  st.data.forEach(d => cells.push([d, "data access", C.crit]));
  const sY = appY + app.h + 94;
  if (cells.length) {
    const bw = 148, bgap = 18;
    const perRow = Math.min(cells.length, 4);
    const totalW = perRow * bw + (perRow - 1) * bgap;
    const sx = Math.max(x0, app.x + app.w / 2 - totalW / 2);
    const rows = Math.ceil(cells.length / perRow);
    cells.forEach((cell, i) => {
      const r = Math.floor(i / perRow), c = i % perRow;
      B.push({ x: sx + c * (bw + bgap), y: sY + r * 68, w: bw, h: 50,
        title: pretty(cell[0]), sub: cell[1],
        icon: STACK_LOGO[cell[0]] || { geo: "zorc", color: cell[2] }, accent: cell[2] });
    });
    G.push({ x: sx - 16, y: sY - 24, w: totalW + 32, h: rows * 68 - 18 + 44,
      title: "INSIDE THE CONTAINER · " + st.lang.toUpperCase(), color: C.ok });
    E.push([app, { x: sx + totalW / 2 - 1, y: sY - 24, w: 2, h: 2 },
      { label: "runs", mode: "v", color: C.ok }]);
    bottom = sY + rows * 68 + 26;
  } else {
    const why = st.lang === "unknown"
      ? "repository not readable from this account,\nor no manifest at its root"
      : st.lang + " app, no framework named in the manifest";
    const b = { x: app.x - 30, y: sY, w: 240, h: 50, title: "stack not detected", sub: why,
      icon: { geo: "lock" } };
    B.push(b);
    E.push([app, b, { mode: "v", color: C.faint }]);
    bottom = sY + 50 + 18;
  }

  /* Integrations. */
  const ints = [];
  if (a.db) ints.push({ title: "Postgres", sub: "dedicated instance\nown database + role",
    icon: "postgresql", label: "SQL :5432", color: C.accent });
  if (a.redis) ints.push({ title: "Redis", sub: "one logical index", icon: "redis",
    label: "RESP :6379", color: C.crit });
  if (a.ai) ints.push({ title: "ai-gateway", sub: "Groq, Google, OpenRouter\npicked with failover",
    icon: { geo: "ai", color: C.ok }, label: "POST /auto/v1", color: C.ok });
  a.dep.forEach(d => ints.push({ title: d, sub: "declared dependency",
    icon: { geo: "zorc", color: C.accent }, label: "depends on", color: C.accent }));

  if (ints.length) {
    const iy = bottom + 84, iw = 158, igap = 22;
    const totalW = ints.length * iw + (ints.length - 1) * igap;
    const ix = Math.max(x0, app.x + app.w / 2 - totalW / 2);
    G.push({ x: ix - 16, y: iy - 24, w: totalW + 32, h: H + 44, title: "INTEGRATIONS", color: C.ok });
    const targets = ints.map((s, i) => {
      const b = { x: ix + i * (iw + igap), y: iy, w: iw, h: H, title: s.title, sub: s.sub, icon: s.icon };
      B.push(b);
      return { box: b, n: chain.length + 1 + i, label: s.label, color: s.color };
    });
    BUS.push([{ x: app.x, y: bottom - 50, w: app.w, h: 50 }, targets, { busY: iy - 44, color: C.ok }]);
    bottom = iy + H + 42;
  }

  const width = Math.max(ingressW + x0, ...B.map(b => b.x + b.w)) + 26;
  const inner = G.map(DG.group).join("")
    + E.map(e => DG.conn(e[0], e[1], e[2])).join("")
    + BUS.map(b => DG.bus(b[0], b[1], b[2])).join("")
    + B.map(DG.box).join("");
  return DG.wrap(width, bottom + 14, inner, Math.min(width, 820));
}

/* ---------- the fleet ---------- */
function fleetDiagram(APPS, NODE_META, NODE_ORDER) {
  const C = DG.C;
  const B = [], G = [], E = [];
  const H = 52, gap = 64;
  const tunnelNodes = NODE_ORDER.filter(n => !NODE_META[n].pub);
  const directNodes = NODE_ORDER.filter(n => NODE_META[n].pub);
  const nodesTop = 46;
  const fleetH = (tunnelNodes.length + directNodes.length) * gap;
  const mid = nodesTop + fleetH / 2 - H / 2;

  const user = { x: 12, y: mid, w: 104, h: H, title: "User", sub: "browser, agent", icon: { geo: "user" } };
  const cf = { x: 158, y: mid, w: 128, h: H, title: "Cloudflare", sub: "DNS, TLS\nAccess policy", icon: "cloudflare" };
  B.push(user, cf);
  E.push([user, cf, { n: 1, label: "HTTPS 443" }]);

  const tY = nodesTop + (tunnelNodes.length * gap) / 2 - H / 2;
  const dY = nodesTop + tunnelNodes.length * gap + (directNodes.length * gap) / 2 - H / 2;
  const tun = { x: 330, y: tY, w: 132, h: H, title: "cloudflared", sub: "tunnel\nfor nodes with no IP", icon: "cloudflare" };
  const dir = { x: 330, y: dY, w: 132, h: H, title: "A record", sub: "direct to the node", icon: { geo: "server", color: C.accent } };
  B.push(tun, dir);
  E.push([cf, tun, { n: 2, label: "tunnel" }], [cf, dir, { n: 2, label: "origin" }]);

  const nodeBoxes = {};
  NODE_ORDER.forEach((n, i) => {
    const m = NODE_META[n];
    const apps = APPS.filter(a => a.node === n);
    const mb = apps.reduce((s, a) => s + a.mb, 0);
    const b = { x: 506, y: nodesTop + i * gap, w: 176, h: H, title: n,
      sub: `${apps.length} services · ${mb.toLocaleString()} MB\n${m.backend}${m.gpu ? " · GPU" : ""}`,
      icon: m.gpu ? "nvidia" : "ubuntu", accent: m.role ? C.accent : null };
    nodeBoxes[n] = b; B.push(b);
    E.push([m.pub ? dir : tun, b, { n: 3, color: m.pub ? C.accent : C.line }]);
  });
  G.push({ x: 492, y: nodesTop - 22, w: 204, h: fleetH + 10, title: "NODES", color: C.accent });

  const shared = [
    { title: "Traefik", sub: "one per node\nroutes by hostname", icon: "traefikproxy", label: "HTTP" },
    { title: "Postgres 18", sub: `${APPS.filter(a => a.db).length} dedicated instances`, icon: "postgresql", label: "SQL :5432" },
    { title: "Redis 7", sub: "one index per app", icon: "redis", label: "RESP :6379" },
    { title: "ai-gateway", sub: `${APPS.filter(a => a.ai).length} apps · free providers`, icon: { geo: "ai", color: C.ok }, label: "/auto/v1" },
  ];
  const sx = 790, sTop = nodesTop + fleetH / 2 - (shared.length * 60 - 8) / 2;
  shared.forEach((s, i) => {
    const b = { x: sx, y: Math.max(nodesTop - 6, sTop + i * 60), w: 168, h: H, title: s.title, sub: s.sub, icon: s.icon };
    B.push(b);
    E.push([nodeBoxes.servingz, b, { n: 4, color: C.line }]);
  });
  const sh = Math.max(...B.filter(b => b.x === sx).map(b => b.y + b.h));
  G.push({ x: sx - 14, y: Math.max(nodesTop - 28, sTop - 22), w: 196, h: sh - Math.max(nodesTop - 6, sTop) + 30,
    title: "SHARED, ON SERVINGZ", color: C.ok });

  const width = Math.max(...B.map(b => b.x + b.w)) + 26;
  const height = Math.max(...B.map(b => b.y + b.h), ...G.map(g => g.y + g.h)) + 20;
  const inner = G.map(DG.group).join("") + E.map(e => DG.conn(e[0], e[1], e[2])).join("") + B.map(DG.box).join("");
  return DG.wrap(width, height, inner, Math.min(width, 880));
}
