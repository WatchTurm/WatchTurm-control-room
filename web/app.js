/* WatchTurm Control Room
   Copyright 2026 Mateusz Zadrożny. Licensed under Apache-2.0.
   See LICENSE. Developed within WatchTurm initiative.
   UI-first: Overview, Environments, Release History, Ticket Tracker, Statistics, Runbooks.
   - Uses existing styles.css “premium” classes (envCard, compareBtn, panel, table, docCard…)
   - Left nav: groups + leaves; submenu only for selected leaf
   - Views: overview | env | releases
   - Env cards: click toggles details
   - Compare: select up to 2 envs, order by env list (alpha,beta,dev,qa,uat,prod)
   - Read-only: no Promote or write actions
*/

const DEFAULT_JSON_URL = "../data/latest.json"; // web/ -> data/
const RELEASE_HISTORY_URL = "../data/release_history.json"; // web/ -> data/
// Snapshot API server - configurable via window.SNAPSHOT_API_BASE or auto-detect
const SNAPSHOT_API_BASE = (() => {
  // Allow override via global variable (useful for production)
  if (window.SNAPSHOT_API_BASE) {
    return window.SNAPSHOT_API_BASE;
  }
  // Auto-detect: if running on localhost, use localhost:8001, otherwise use relative path
  if (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1') {
    return "http://localhost:8001";
  }
  // Production: assume API is on same domain
  return "/api";
})();


// ---------- UI preferences (Enterprise Hardening) ----------
const UI_DEFAULTS = {
  theme: "dark", // dark | light
  lang: "EN",    // EN | PL (labels only)
};

let uiTheme = (() => {
  try { return localStorage.getItem("roc_theme") || UI_DEFAULTS.theme; } catch { return UI_DEFAULTS.theme; }
})();
let uiLang = (() => {
  try { return localStorage.getItem("roc_lang") || UI_DEFAULTS.lang; } catch { return UI_DEFAULTS.lang; }
})();

function applyTheme() {
  const t = (uiTheme === "light") ? "light" : "dark";
  uiTheme = t;
  document.documentElement.setAttribute("data-theme", t);
  try { localStorage.setItem("roc_theme", t); } catch {}
}

function applyLang() {
  uiLang = (uiLang === "PL") ? "PL" : "EN";
  try { localStorage.setItem("roc_lang", uiLang); } catch {}
}

// small i18n helper for core UI labels only (data is not translated)
const T = {
  EN: {
    openLatest: "Open latest.json",
    theme: "Theme",
    dark: "Dark",
    light: "Light",
    lang: "Language",
  },
  PL: {
    openLatest: "Otwórz latest.json",
    theme: "Motyw",
    dark: "Ciemny",
    light: "Jasny",
    lang: "Język",
  },
};

function tt(key) {
  const dict = T[uiLang] || T.EN;
  return dict[key] || (T.EN[key] || key);
}

// Debounce helper
function debounce(fn, ms=150) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

// Safe JSON response parser with error handling
async function safeJsonResponse(res) {
  try {
    const text = await res.text().catch(() => '');
    if (!res.ok) {
      console.error(`[API Error] ${res.status} ${res.statusText}:`, text.substring(0, 200));
      let msg = res.statusText || 'Request failed';
      try {
        const parsed = text.trim() ? JSON.parse(text) : null;
        if (parsed && typeof parsed.message === 'string') msg = parsed.message;
      } catch (_) {}
      return { _error: true, _status: res.status, message: msg };
    }
    if (!text.trim()) {
      console.warn('[API Warning] Empty response body');
      return null;
    }
    return JSON.parse(text);
  } catch (e) {
    // Try to get response text for debugging
    let responseText = '';
    try {
      const cloned = res.clone();
      responseText = await cloned.text().catch(() => '');
    } catch (_) {}
    
    console.error('[JSON Parse Error]', e, 'Response preview:', responseText.substring(0, 200));
    return null;
  }
}

// Safe API fetch with network error handling
async function safeApiFetch(url, options = {}) {
  try {
    const res = await fetch(url, options);
    return await safeJsonResponse(res);
  } catch (e) {
    // Network error (connection refused, timeout, etc.)
    if (e instanceof TypeError && e.message.includes('fetch')) {
      console.error('[Network Error]', e.message);
      return { 
        _error: true, 
        _errorType: 'network',
        message: `Cannot connect to API server. Is it running at ${url}?`
      };
    }
    console.error('[API Fetch Error]', e);
    return { 
      _error: true, 
      _errorType: 'unknown',
      message: e.message || 'Unknown error'
    };
  }
}

// ---------- Left nav tree (UI only) ----------
// NAV_TREE: Now built dynamically from tenant config (groups + projects)
// Fallback to empty if no config
function buildNavTree() {
  if (!window.AdminConfig) return [];
  const cfg = AdminConfig.load();
  if (!cfg || !Array.isArray(cfg.groups)) return [];
  
  return cfg.groups.map(g => {
    if (g.type === "group") {
      const children = (g.projects || []).map(p => ({
        type: "leaf",
        key: p.key || "",
        label: p.name || p.key || "",
      }));
      // If group has only one child, flatten it to a leaf (no dropdown)
      if (children.length === 1) {
        return children[0];
      }
      // If group has no children, skip it
      if (children.length === 0) {
        return null;
      }
      return {
        type: "group",
        key: g.key || "",
        label: g.name || g.key || "",
        children,
      };
    } else {
      // Standalone project (not in a group)
      return {
        type: "leaf",
        key: g.key || "",
        label: g.name || g.key || "",
      };
    }
  }).filter(Boolean); // Remove null entries (empty groups)
}

// No hardcoded projects - nav comes from AdminConfig (localStorage) or appData only
const NAV_TREE_LEGACY = [];

// Map UI leaf keys -> project keys from latest.json (projects[].key)
const LEAF_TO_PROJECT_KEY = {
  TAP2: "TAP2",
  B2C: "PO1_B2C",
  TCBP_MFES: "TCBP_MFES",
  TCBP_ADAPTERS: "TCBP_ADAPTERS",
};

// ---------- State ----------

// --- Env quick links (MVP0.5 UI-only) ---
let envLinksOpenKey = null; // e.g. "PO1V8:qa"

// --- Tools: Release History (Stage 2) ---
let releaseHistoryData = null;
let releaseHistoryLoadError = "";
let releaseHistoryLoading = false;

const historyFilters = {
  project: "ALL",
  envs: new Set(),
  q: "",
  includeBootstrap: true,
  // Advanced search mode (default: expanded/visible)
  advancedMode: true,
  dateFrom: "",
  dateTo: "",
  repo: "",
  tag: "",
  deployer: "",
  // View mode: "list" or "calendar"
  viewMode: "list",
  // Default view limit (show 10, can expand to 30)
  defaultLimit: 10,
  visibleLimit: 10, // Current visible limit (10 or 30) - persists across renders
  // Calendar: selected day (YYYY-MM-DD) - shows events for that day below calendar
  selectedCalendarDay: null,
  // Calendar: show all events for selected day (false = first 20 only)
  calendarDayShowAll: false,
};

// --- Right sidebar docs ---
// Sidebar content is PER PROJECT and only visible on project views (Environments/Releases).
// There are no longer any built-in mock documents. Everything comes from tenant
// configuration (admin-config.js). If no docs are configured for a platform,
// the section shows "No data" plus an admin-only "+ Add" button.
const PROJECT_DOCS = {}; // kept for backward compatibility, but not used anymore.

// Helper: resolve documents for a project from tenant config (if available).
function getProjectDocs(proj) {
  const key = proj?.key || proj?.name || "";
  let docs = [];

  try {
    if (window.AdminConfig) {
      const cfg = AdminConfig.load();
      const projs = Array.isArray(cfg?.projects) ? cfg.projects : [];
      const m = projs.find((p) => (p && String(p.key || "").toUpperCase()) === String(key || "").toUpperCase());
      if (m && Array.isArray(m.docs) && m.docs.length) {
        docs = m.docs;
      }
    }
  } catch (_) {
    // Non-fatal: treat as no docs
  }

  return docs;
}


// Release history comes from release_history.json / snapshot API (no mock data).



function normKey(x) {
  return String(x ?? "").trim().toLowerCase();
}

function getEnvLinksFor(projectKey, envKey) {
  // First, try tenant config (admin-configured)
  if (window.AdminConfig) {
    const cfg = AdminConfig.load();
    if (cfg && Array.isArray(cfg.groups)) {
      const allProjects = cfg.groups.flatMap(g => g.type === "group" ? (g.projects || []) : [g]).filter(p => p.type !== "group");
      for (const proj of allProjects) {
        if (normKey(proj.key) !== normKey(projectKey)) continue;
        const envs = Array.isArray(proj.environments) ? proj.environments : [];
        for (const env of envs) {
          if (normKey(env.key) === normKey(envKey)) {
            const cmsUrl = env.cmsUrl || env.url || "";
            if (cmsUrl) {
              return {
                cms: {
                  label: env.cmsLabel || env.linkLabel || "CMS",
                  url: cmsUrl,
                },
              };
            }
            return null;
          }
        }
      }
    }
  }
  
  return null;
}

function isProjectSidebarAllowed() {
  // Right sidebar only on Environments view (not on Runbooks - runbooks are a standalone tool).
  const proj = getCurrentProject();
  if (!proj) return false;
  return currentView === "env";
}

function applySidebarVisibility() {
  const sidebar = el("sidebar");
  const toggle = el("sidebarToggle");

  const allowed = isProjectSidebarAllowed();

  if (toggle) toggle.style.display = allowed ? "" : "none";
  if (sidebar) sidebar.style.display = allowed ? "" : "none";

  if (!allowed) {
    sidebarOpen = false;
    if (sidebar) sidebar.classList.remove("open");
    return;
  }

  // allowed → apply current open/close state
  setSidebar(sidebarOpen);
}


// --- Overview: Parameters & Logs env selector (UI-only) ---
let paramsEnvByProject = Object.create(null); // { [projectKey]: "prod"|"uat"|"qa"|"dev" }

let appData = { projects: [] };

// --- Global UI error trap (prevents blank screens; shows actionable message) ---
if (!window.__releaseOpsErrorTrapInstalled) {
  window.__releaseOpsErrorTrapInstalled = true;
  window.addEventListener('error', (ev) => {
    try {
      const msg = (ev && (ev.message || (ev.error && ev.error.message))) || 'Unknown error';
      console.error('[UI ERROR]', ev.error || ev);
      const host = document.getElementById('app');
      const bannerId = 'globalErrorBanner';
      if (host && !document.getElementById(bannerId)) {
        const d = document.createElement('div');
        d.id = bannerId;
        d.style.position = 'fixed';
        d.style.bottom = '16px';
        d.style.left = '16px';
        d.style.right = '16px';
        d.style.zIndex = '9999';
        d.style.padding = '12px 14px';
        d.style.borderRadius = '12px';
        d.style.border = '1px solid rgba(255, 99, 132, 0.45)';
        d.style.background = 'rgba(255, 99, 132, 0.10)';
        d.style.backdropFilter = 'blur(10px)';
        d.style.fontFamily = 'system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial';
        d.style.color = 'inherit';
        d.innerHTML = `<strong>UI error</strong>: ${escapeHtml(msg)}. Open DevTools console for details.`;
        host.appendChild(d);
      }
    } catch (_) {}
  });
}

let currentLeafKey = null;
let runbooksSelectedProjectKey = null; // when Runbooks (tool) is open: selected platform key
let runbooksSelectedRunbook = null;    // "scope" | "drift" | "readiness" when one is selected
let runbooksScopeBaselineMode = "specific"; // "specific" | "prefix" - scope: use exact branch or latest by prefix
let runbooksTicketPrefixes = [];       // ticket prefixes for runbooks (from project or user-added)
let currentView = "overview"; // overview | env | releases | history | ticket | setup

let expandedGroupKey = null;      // PO1 / TCBP
let expandedLeafKey = null;     // submenu shows only for this leaf

let sidebarOpen = false;

// env/details/compare
let detailsOpenEnvKey = null;

// compare selection: allow selecting more than 2 envs, but UI compare view is still pair-based.
const MAX_COMPARE_SELECT = 4;
let compareSelected = new Set(); // env keys (max MAX_COMPARE_SELECT)
let compareOnlyMismatches = false; // compare filter
let comparePair = null; // { aKey, bKey } currently compared (must be subset of compareSelected)


// ---------- Helpers ----------
const el = (id) => document.getElementById(id);

// -----------------------------
// Safety / resilience (avoid blank screens)
// -----------------------------

function normalizeAppDataShape(raw) {
  const d = raw && typeof raw === "object" ? raw : {};
  if (!Array.isArray(d.projects)) d.projects = [];
  if (!Array.isArray(d.warnings)) d.warnings = [];
  if (!d.integrations || typeof d.integrations !== "object") d.integrations = {};
  if (!d.observability || typeof d.observability !== "object") d.observability = {};
  if (!Array.isArray(d.observability.summary)) d.observability.summary = [];
  if (!Array.isArray(d.observability.warnings)) d.observability.warnings = [];
  return d;
}

function renderFatal(err) {
  try {
    console.error("[FATAL UI] render failed", err);

    // NOTE: index.html uses <main class="main"> without id="main".
    // We intentionally target the real main container to avoid silent blank screens.
    const main =
      document.querySelector("main.main") ||
      document.querySelector("main") ||
      document.body;

    if (!main) return;

    main.innerHTML = `
      <div class="pageHeader">
        <div>
          <div class="pageTitle">Something went wrong</div>
          <div class="pageSubtitle">The dashboard failed to render this view. The page is still usable (you can switch tabs), but this view needs a small fix.</div>
        </div>
      </div>
      <div class="panel" style="margin-top:12px;">
        <div class="panelTitle">Error</div>
        <div class="muted" style="margin-top:8px; white-space:pre-wrap;">${escapeHtml(String(err && err.stack ? err.stack : err))}</div>
        <div class="muted" style="margin-top:8px;">Tip: open DevTools Console for the full stack trace.</div>
      </div>
    `;
  } catch (_) {
    // last resort: do nothing
  }
}

window.addEventListener("error", (e) => renderFatal(e.error || e.message));
window.addEventListener("unhandledrejection", (e) => renderFatal(e.reason || e));

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (m) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[m])
  );
}

/** Prefer vX.X.X in tag display (e.g. "repo-v0.0.107" → "v0.0.107") */
function preferVersionTag(tag) {
  if (!tag || tag === "-") return tag;
  const m = String(tag).match(/v\d+\.\d+\.\d+/);
  return m ? m[0] : tag;
}

// For safe usage inside HTML attributes (href, title, data-*, ...)
function escapeAttr(s) {
  // escapeHtml already escapes quotes, which is sufficient for our needs here
  return escapeHtml(s);
}

// Data-quality warnings (component/env). We keep this very small in UI: an icon + tooltip.
function renderWarningsIcon(warnings) {
  const arr = Array.isArray(warnings) ? warnings : [];
  if (!arr.length) return "";

  const lines = arr.map((w) => {
    // Backward compatible:
    // - old: { code, message }
    // - new (Stage 8.1): { reason, source, scope, project, env, component }
    const code = (w && (w.code || w.reason)) ? String(w.code || w.reason) : "";
    const msg = (w && (w.message || w.detail || w.source)) ? String(w.message || w.detail || w.source) : "";
    return code ? `${code}${msg ? ": " + msg : ""}`.trim() : msg;
  }).filter(Boolean);

  const title = lines.length ? lines.join("\n") : "Data warnings";
  return `<span class="warnIcon" title="${escapeAttr(title)}" aria-label="Warnings">⚠</span>`;
}

function getRootWarnings() {
  return Array.isArray(appData?.warnings) ? appData.warnings : [];
}

function normalizeKeyLoose(v) {
  return String(v || "").trim().toLowerCase();
}
// Backward-compat alias.
// Some UI paths still call normalizeKey() (older name); keep it to avoid runtime crashes.
function normalizeKey(v) {
  return normalizeKeyLoose(v);
}

function warningsForEnv(projectKey, envKey) {
  const p = normalizeKeyLoose(projectKey);
  const e = normalizeKeyLoose(envKey);
  const root = getRootWarnings();
  if (!root.length) return [];

  return root.filter((w) => {
    if (!w) return false;
    const wp = normalizeKeyLoose(w.project || w.projectKey);
    const we = normalizeKeyLoose(w.env || w.envKey);
    // project match: either explicitly matches, or warning is global (no project)
    const projOk = !wp || wp === p;
    // env match: either explicitly matches, or warning is broader scope
    const envOk = !we || we === e;
    return projOk && envOk;
  });
}

// Dedupe link list (prevents repeated Kustomization/Branch/Commit links)
function dedupeLinks(links) {
  const out = [];
  const seen = new Set();
  if (!Array.isArray(links)) return out;
  for (const l of links) {
    if (!l) continue;
    const url = String(l.url || '').trim();
    if (!url) continue;
    const label = String(l.label || l.type || 'Link').trim() || 'Link';
    const key = url;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ url, label });
  }
  return out;
}

function prettyLinkLabel(label) {
  const s = String(label || "").trim();
  const k = s.toLowerCase();
  if (!s) return "Link";
  if (k.includes("kustom")) return "Infra";
  if (k.includes("commit")) return "Commit";
  if (k.includes("branch")) return "Branch";
  if (k.includes("teamcity") || k.includes("build")) return "Build";
  return s;
}

function renderSourceLinks(links) {
  if (!Array.isArray(links) || links.length === 0) return "";

  const base = dedupeLinks(links).map((l) => ({
    url: l.url,
    label: prettyLinkLabel(l.label),
  }));
  if (!base.length) return "";

  const priority = {
    commit: 1,
    pr: 1,
    kustomization: 2,
    infra: 2,
    teamcity: 3,
    build: 3,
    branch: 4,
    compare: 5,
    link: 99,
  };

  const byLabel = new Map();
  for (const l of base) {
    const key = String(l.label || "link").trim().toLowerCase();
    if (!byLabel.has(key)) byLabel.set(key, l);
  }

  const clean = Array.from(byLabel.values()).sort((a, b) => {
    const pa = priority[String(a.label || "link").trim().toLowerCase()] ?? 99;
    const pb = priority[String(b.label || "link").trim().toLowerCase()] ?? 99;
    return pa - pb;
  });

  const html = clean
    .slice(0, 4)
    .map(
      (l) =>
        `<a class="srcLink" href="${escapeAttr(l.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(l.label)}</a>`
    )
    .join(" ");
  return `<span class="srcLinks">${html}</span>`;
}


function fmtDate(iso) {
  if (!iso) return "";
 
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
 
  return d.toLocaleString("pl-PL", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  });
}

function fmtAgo(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  const diffMs = Date.now() - d.getTime();
  const sec = Math.floor(diffMs / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const h = Math.floor(min / 60);
  if (h < 48) return `${h}h ago`;
  const days = Math.floor(h / 24);
  return `${days}d ago`;
}

// Pick the newest valid timestamp from a list of candidates.
// Accepts ISO strings (or anything Date() can parse). Returns the original best string.
function pickBestDate(...candidates) {
  let bestRaw = "";
  let bestTs = -1;
  for (const c of candidates.flat()) {
    if (!c) continue;
    const raw = String(c).trim();
    if (!raw) continue;
    const d = new Date(raw);
    if (Number.isNaN(d.getTime())) continue;
    const ts = d.getTime();
    if (ts > bestTs) {
      bestTs = ts;
      bestRaw = raw;
    }
  }
  return bestRaw;
}

function fmtWhen(iso) {
  if (!iso) return "-";
  const abs = fmtDate(iso);
  const ago = fmtAgo(iso);
  // MVP1 UX: show only 'time ago' in-line; full date/time is available on hover (title).
  return `<span class="when" title="${escapeAttr(abs)}">${escapeHtml(ago)}</span>`;
}

function normalizeTag(tag) {
  if (!tag) return "";
  return String(tag).trim().replace(/^v\.(?=\d)/, "v").replace(/-v\.(?=\d)/, "-v");
}

function extractBuildFromTag(tag) {
  const t = normalizeTag(tag);
  const m = t.match(/v\d+\.\d+\.(\d+)$/);
  return m ? m[1] : "";
}

// tolerant repo list: env.components (your JSON) + fallbacks
function getEnvRepos(env) {
  return env?.components || env?.services || env?.repositories || env?.repos || [];
}

// Env-level summary fallback (when env.lastDeploy/deployer/build are missing)
function deriveEnvSummaryFromRepos(env) {
  const repos = getEnvRepos(env);
  let best = null;
  let bestTs = "";
  for (const r of repos) {
    const ts = String(r?.deployedAt || r?.buildFinishedAt || "").trim();
    if (ts && (!bestTs || ts > bestTs)) {
      bestTs = ts;
      best = r;
    }
  }
  const build = (env?.build || env?.version || "").trim()
    || (best?.build ? String(best.build) : "")
    || extractBuildFromTag(best?.tag || best?.imageTag || "");
  const by = (env?.deployer || env?.deployedBy || "").trim() || (best ? String(best.deployer || best.deployedBy || best.triggeredBy || "") : "");
  const last = (env?.lastDeploy || env?.lastDeployedAt || "").trim() || bestTs;
  return { build, by, last };
}


function getProjectByLeaf(leafKey) {
  const projectKey = LEAF_TO_PROJECT_KEY[leafKey] || leafKey;
  return appData.projects?.find((p) => p.key === projectKey || p.name === projectKey) || null;
}

function getCurrentProject() {
  if (currentView === "releases" && (runbooksSelectedProjectKey != null || currentLeafKey != null)) {
    return getProjectByLeaf(runbooksSelectedProjectKey || currentLeafKey);
  }
  return getProjectByLeaf(currentLeafKey);
}

function getEnvList(project) {
  if (!project) return [];
  return Array.isArray(project.environments) ? project.environments : (project.envs || []);
}


// stable ordering for compare: by envs array order
function sortSelectedByEnvOrder(project, keys) {
  const envs = getEnvList(project);
  const order = new Map(envs.map((e, idx) => [String(e.key), idx]));
  return [...keys].sort((a, b) => (order.get(a) ?? 999) - (order.get(b) ?? 999));
}

function findParentGroupKey(leafKey) {
  const navTree = buildNavTree();
  for (const n of (navTree.length > 0 ? navTree : NAV_TREE_LEGACY)) {
    if (n.type === "group" && n.children?.some((c) => c.key === leafKey)) return n.key;
  }
  return null;
}


// ---------- URL hash management for deep-linking ----------
function updateUrlHash() {
  let hash = "";
  if (currentView === "env" && currentLeafKey && detailsOpenEnvKey) {
    // Format: #platform:env (e.g., #TAP2:qa)
    hash = `#${currentLeafKey}:${detailsOpenEnvKey}`;
  } else if (currentView === "env" && currentLeafKey) {
    // Format: #platform (e.g., #TAP2)
    hash = `#${currentLeafKey}`;
  } else if (currentView === "overview") {
    hash = "#overview";
  } else if (currentView === "releases") {
    hash = "#runbooks";
  } else if (currentView === "history") {
    hash = "#history";
  } else if (currentView === "ticket") {
    hash = "#ticket";
  } else if (currentView === "setup") {
    hash = "#setup";
  }
  if (window.location.hash !== hash) {
    window.location.hash = hash;
  }
}

function parseUrlHash() {
  const hash = window.location.hash.slice(1); // Remove #
  if (!hash) return;
  
  // Check for platform:env format (e.g., TAP2:qa)
  const match = hash.match(/^([^:]+):(.+)$/);
  if (match) {
    const [, platformKey, envKey] = match;
    const projectKey = LEAF_TO_PROJECT_KEY[platformKey] || platformKey;
    // Wait for appData to be loaded before navigating
    if (appData && appData.projects) {
      goToEnvDetails(projectKey, envKey);
    }
    return;
  }
  
  // Check for simple platform format (e.g., TAP2)
  if (hash && LEAF_TO_PROJECT_KEY[hash]) {
    const projectKey = LEAF_TO_PROJECT_KEY[hash];
    if (appData && appData.projects) {
      goToProjectEnvs(projectKey);
    }
    return;
  }
  
  // Check for view names
  if (hash === "overview") {
    setView("overview");
  } else if (hash === "runbooks") {
    setView("releases");
  } else if (hash === "history") {
    setView("history");
  } else if (hash === "ticket") {
    setView("ticket");
  } else if (hash === "setup") {
    setView("setup");
  }
}

function goToProjectEnvs(projectKey) {
  const leaf = Object.keys(LEAF_TO_PROJECT_KEY).find(k => LEAF_TO_PROJECT_KEY[k] === projectKey) || projectKey;
  currentLeafKey = leaf;
  expandedGroupKey = findParentGroupKey(leaf);
  expandedLeafKey = leaf;
  currentView = "env";
  compareSelected = new Set();
  comparePair = null;
  detailsOpenEnvKey = null;
  setSidebar(false);
  // Update URL hash for deep-linking
  updateUrlHash();
  render();
}

function goToEnvDetails(projectKey, envKey) {
  const leaf = Object.keys(LEAF_TO_PROJECT_KEY).find(k => LEAF_TO_PROJECT_KEY[k] === projectKey) || projectKey;
  currentLeafKey = leaf;
  expandedGroupKey = findParentGroupKey(leaf);
  expandedLeafKey = leaf;
  currentView = "env";
  compareSelected = new Set();
  comparePair = null;
  detailsOpenEnvKey = envKey; // Open the specific environment details
  setSidebar(false);
  // Update URL hash for deep-linking
  updateUrlHash();
  render();
  // Scroll to details panel after render
  requestAnimationFrame(() => {
    const d = el("details");
    if (d && d.innerHTML.trim()) d.scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

// ---------- Boot ----------


// ---------- Datadog runtime health (optional backend proxy) ----------
async function refreshDatadogHealth() {
  try {
    const dd = appData?.integrations?.datadog;
    if (!dd || !dd.enabled) return;

    // If snapshot provides a proxy URL, use it; otherwise default to local backend.
    const url = dd.proxyBaseUrl || "http://localhost:8001/api/datadog/health";

    const res = await fetch(url, { cache: "no-store" });
    const j = await safeJsonResponse(res);
    if (!j) {
      throw new Error(`Failed to parse response (HTTP ${res.status})`);
    }

    // Success: set live status only (never overwrite snapshot connected/reason)
    if (dd) {
      dd.liveConnected = true;
      dd.liveReason = "ok";
    dd.liveCheckedAt = new Date().toISOString();
    }

  } catch (e) {
    const dd = appData?.integrations?.datadog;
    if (dd) {
      // Failure: set live status only (never overwrite snapshot connected/reason)
      dd.liveConnected = false;
      dd.liveReason = (e && e.message) ? e.message : String(e);
      dd.liveCheckedAt = new Date().toISOString();
    }
  }
}

async function boot() {
  // Apply persisted UI preferences early
  try { applyTheme(); applyLang(); } catch {}
  
  // Load main data with improved error handling
  try {
    const res = await fetch(DEFAULT_JSON_URL, { cache: "no-store" });
    const jsonData = await safeJsonResponse(res);
    if (!jsonData || (jsonData._error === true)) {
      const errorMsg = (res?.status === 404 || jsonData?._status === 404)
        ? "Data file not found. Please run the snapshot generator first."
        : (jsonData?.message || `Failed to load data (HTTP ${res?.status || "?"}). The server may be unavailable or the file is corrupted.`);
      throw new Error(errorMsg);
    }
    appData = jsonData;
  } catch (e) {
    console.error('[FATAL] Failed to load data:', e);
    // Show user-friendly error message
    const main = document.querySelector("main.main") || document.body;
    if (main && !main.querySelector("#dataLoadError")) {
      const errorDiv = document.createElement("div");
      errorDiv.id = "dataLoadError";
      errorDiv.className = "panel";
      errorDiv.style.cssText = "margin: 20px; padding: 20px; border: 1px solid rgba(255, 99, 132, 0.45); background: rgba(255, 99, 132, 0.10);";
      errorDiv.innerHTML = `
        <div style="font-weight: 600; margin-bottom: 8px; color: var(--text);">Failed to Load Data</div>
        <div style="color: var(--muted); margin-bottom: 12px;">${escapeHtml(e.message || "Unknown error")}</div>
        <button class="btn btnPrimary" onclick="location.reload()" style="margin-top: 8px;">Retry</button>
      `;
      main.insertBefore(errorDiv, main.firstChild);
    }
    appData = { projects: [] };
  }

  // Ensure missing optional fields never crash the UI
  appData = normalizeAppDataShape(appData);

  
  // Onboarding banner (dismissible, first-time only)
  try {
    const banner = document.getElementById("onboardingBanner");
    if (banner && !localStorage.getItem("roc_setup_banner_dismissed")) {
      banner.classList.remove("hidden");
      banner.innerHTML = `
        <span>First time? <a href="#" data-goto-setup>Read the Setup guide</a> for step-by-step configuration.</span>
        <button type="button" aria-label="Dismiss" onclick="this.closest('.onboardingBanner').classList.add('hidden'); localStorage.setItem('roc_setup_banner_dismissed','1');">×</button>
      `;
      banner.querySelector("[data-goto-setup]")?.addEventListener("click", (e) => {
        e.preventDefault();
        setView("setup");
        banner.classList.add("hidden");
        try { localStorage.setItem("roc_setup_banner_dismissed", "1"); } catch (_) {}
      });
    }
  } catch (_) {}

  // Parse URL hash for deep-linking (before setting default view)
  parseUrlHash();
  
  // Start on Overview (no project selected) if no hash was parsed
  if (currentView === "overview") {
    currentLeafKey = null;
    expandedGroupKey = null;
    expandedLeafKey = null;
  } else {
    expandedGroupKey = findParentGroupKey(currentLeafKey);
    expandedLeafKey = currentLeafKey;
  }

  render();
  
  // Listen for hash changes (browser back/forward)
  window.addEventListener("hashchange", () => {
    parseUrlHash();
    render();
  });

  // Event delegation: Mini History row click (works even after Overview re-renders)
  const labelByProjectKey = { TAP2: "TAP2.0", PO1V8: "PO1 (PO1v8)", B2C: "B2C (PO1v13)", TCBP_MFES: "TCBP → MFEs", TCBP_ADAPTERS: "TCBP → Adapters", LCW: "LCW", BS: "Booking Services", PO1_B2C: "B2C (PO1v13)" };
  document.addEventListener("click", (e) => {
    if (currentView !== "overview") return;
    if (!e.target.closest(".miniHistoryContainer")) return;
    const row = e.target.closest(".historyRow");
    if (!row) return;
    if (e.target.closest("a") || e.target.closest("button")) return;
    const raw = row.getAttribute("data-hevent");
    if (!raw) return;
    const drawer = el("historyDrawer");
    const drawerBody = el("historyDrawerBody");
    if (!drawer || !drawerBody) return;
    let ev;
    try { ev = JSON.parse(raw); } catch { return; }
    const parts = [];
    const kv = (k, v) => `<div class='historyKV'><div class='muted'>${escapeHtml(k)}</div><div class='mono'>${escapeHtml(String(v || "-"))}</div></div>`;
    parts.push(kv("Platform", labelByProjectKey[ev.p] || ev.p));
    parts.push(kv("Environment", ev.e));
    parts.push(kv("Component", ev.c));
    parts.push(kv("From", ev.f));
    parts.push(kv("To", ev.t));
    parts.push(kv("By", ev.by));
    parts.push(kv("When", ev.at ? fmtDate(ev.at) : "-"));
    const baseLinks = [];
    if (ev.commitUrl) baseLinks.push({ url: ev.commitUrl, label: "Commit" });
    if (ev.kustomizationUrl) baseLinks.push({ url: ev.kustomizationUrl, label: "Kustomization" });
    if (Array.isArray(ev.links)) { for (const l of ev.links) { if (l?.url) baseLinks.push({ url: l.url, label: (l.label || l.type || "Link") }); } }
    const linksHtml = renderSourceLinks(baseLinks);
    if (linksHtml) parts.push(`<div class='historyKV'><div class='muted'>Links</div><div class='historyDrawerLinks'>${linksHtml}</div></div>`);
    if (ev.warnings && Array.isArray(ev.warnings) && ev.warnings.length) {
      const w = ev.warnings.map((w) => { const code = w?.code ? String(w.code) : ""; const msg = w?.message ? String(w.message) : "Warning"; return `<div class='warnRow'><span class='warnDot'></span><span>${escapeHtml(code ? `${code}: ${msg}` : msg)}</span></div>`; }).join("");
      parts.push(`<div class='historyKV'><div class='muted'>Warnings</div><div class='warnList'>${w}</div></div>`);
    }
    drawerBody.innerHTML = parts.join("");
    drawer.classList.remove("hidden");
  });

  // Start snapshot status polling
  startSnapshotStatusPolling();
}

// ---------- Snapshot Status Polling ----------
let snapshotStatusInterval = null;
let snapshotStatus = null;
let statsSelectedView = "overview"; // overview | deployers | projects | environments | components | perproject
let statsSelectedWindow = "30"; // "7" | "30"

async function fetchSnapshotStatus() {
  try {
    const data = await safeApiFetch(`${SNAPSHOT_API_BASE}/api/snapshot/status`, { cache: "no-store" });
    if (data && !data._error) {
      snapshotStatus = data;
      updateSnapshotStatusUI();
    } else if (data && data._error) {
      // API unavailable - set status to show error
      snapshotStatus = { 
        running: false, 
        _apiError: true,
        _errorMessage: data.message || "API server unavailable"
      };
      updateSnapshotStatusUI();
    } else {
      snapshotStatus = null;
      updateSnapshotStatusUI();
    }
  } catch (e) {
    // API server might not be running - that's OK
    console.warn("[Snapshot Status] Error:", e);
    snapshotStatus = { 
      running: false, 
      _apiError: true,
      _errorMessage: "Failed to connect to API server"
    };
    updateSnapshotStatusUI();
  }
}

function updateSnapshotStatusUI() {
  // Trigger a re-render of the header to include snapshot status
  // This ensures the status is always shown when header is rendered
  if (typeof renderHeader === "function") {
    renderHeader();
  }
}

function startSnapshotStatusPolling() {
  // Fetch immediately
  fetchSnapshotStatus();
  
  // Then poll every 30 seconds
  if (snapshotStatusInterval) {
    clearInterval(snapshotStatusInterval);
  }
  snapshotStatusInterval = setInterval(fetchSnapshotStatus, 30000);
}

// ---------- Sidebar (right overlay) ----------
function setSidebar(open) {
  sidebarOpen = !!open;

  const sidebar = el("sidebar");
  const toggle = el("sidebarToggle");

  if (sidebar) {
    sidebar.classList.toggle("open", sidebarOpen);
    sidebar.setAttribute("aria-hidden", sidebarOpen ? "false" : "true");
    if (sidebarOpen) {
      // When opening the sidebar, make sure it is visible in the viewport.
      try {
        requestAnimationFrame(() => {
          sidebar.scrollIntoView({ behavior: "smooth", block: "start" });
        });
      } catch (_) {
        // non-fatal
      }
    }
  }
  if (toggle) {
    toggle.classList.toggle("open", sidebarOpen);
    toggle.textContent = sidebarOpen ? "›" : "‹";
    toggle.setAttribute("aria-label", sidebarOpen ? "Close sidebar" : "Open sidebar");
  }
}

function bindSidebarToggle() {
  const toggle = el("sidebarToggle");
  if (!toggle) return;
  toggle.onclick = () => {
    setSidebar(!sidebarOpen);
    renderRightSidebar();
  };

  // apply initial state (fixes arrow direction on first load)
  setSidebar(sidebarOpen);
}

function bindToolsNav() {
  // Bind once (guard), so render() can call it many times safely
  document.querySelectorAll(".navItem[data-view]").forEach((btn) => {
    if (btn.dataset.bound === "1") return;
    btn.dataset.bound = "1";

    btn.addEventListener("click", () => {
      setView(btn.getAttribute("data-view")); // "history" | "ticket"
    });
  });

  // Active state
  document.querySelectorAll(".navItem[data-view]").forEach((btn) => {
    const view = btn.getAttribute("data-view");
    btn.classList.toggle("active", currentView === view);
  });
}


// ---------- Header ----------
// ---------- Header ----------
function renderHeader() {
  const top = el("topBar");
  if (!top) return;

  const proj = getCurrentProject();
  const lastRefresh =
    proj?.generatedAt ? fmtDate(proj.generatedAt)
    : appData?.generatedAt ? fmtDate(appData.generatedAt)
    : "-";

  let title = "";
  if (currentView === "history") title = "Release History";
  else if (currentView === "ticket") title = "Ticket Tracker";
  else if (currentView === "stats") title = "Statistics";
  else if (currentView === "setup") title = "Setup";
  else if (currentView === "overview") title = "Overview";
  else if (currentView === "releases") title = "Runbooks";
  else title = `Platform: ${proj?.name || proj?.key || currentLeafKey || ""}`.trim();

  const theme = (uiTheme === "light") ? "light" : "dark";

  // Stage 8.2: integrations + reminders/signals (trust signals)
  const rootWarnings = getRootWarnings();
  const signalCount = rootWarnings.length;
  const dd = appData?.integrations?.datadog;
  const ddEnabled = !!dd?.enabled;

  const ddSnapshotConnected = !!dd?.connected;           // snapshot truth
  const ddLiveKnown = typeof dd?.liveConnected === "boolean";
  const ddLiveConnected = !!dd?.liveConnected;           // runtime truth (proxy)
  const ddHasObservabilityData = (appData?.observability?.summary || []).length > 0;

  // Badge logic: if enabled, check both connection status and data availability
  let badgeText = "N/A";
  let badgeClass = "muted";
  if (ddEnabled) {
    if (ddSnapshotConnected) {
      badgeText = "Connected";
      badgeClass = "ok";
    } else if (ddHasObservabilityData) {
      // Data exists but snapshot connection status is false
      badgeText = "Data available";
      badgeClass = "ok";
    } else {
      badgeText = "Offline";
      badgeClass = "warn";
    }
  }

  const ddTitle = !ddEnabled
    ? "Datadog: Not configured"
    : [
        `Datadog (${dd?.site || ""})`,
        `Snapshot connection: ${ddSnapshotConnected ? "Connected" : "Not connected"}`,
        ddHasObservabilityData ? "Observability data: Available" : "Observability data: None",
        ddLiveKnown ? `Live proxy: ${ddLiveConnected ? "OK" : "Offline"}` : "Live proxy: not checked",
        ddLiveKnown && !ddLiveConnected && dd?.liveReason ? `Live reason: ${dd.liveReason}` : ""
      ].filter(Boolean).join("\n");

  // Top-level reminders/signals badge (root + observability warnings; Overview panel also shows computed reminders)
  let warningsBadgeLabel = "N/A";
  let warningsBadgeClass = "muted";
  let warningsBadgeTitle = "No reminders or signals. Open the Reminders & signals panel on Overview for deployment hints (e.g. stale deploy, QA vs UAT diff).";

  if (signalCount > 0) {
    warningsBadgeLabel = String(signalCount);
    warningsBadgeClass = "warn";
    warningsBadgeTitle = `${signalCount} reminder(s) or signal(s).\n` +
      "Open the Reminders & signals panel on Overview for details (stale deploy, QA vs UAT diff, etc.).";
  }

  const topSubText = currentView === "setup" ? "Step-by-step configuration guide" : "Last refresh: " + lastRefresh;
  const badgesHtml = currentView === "setup"
    ? ""
    : `<span class="statusBadge ${warningsBadgeClass}" title="${escapeAttr(warningsBadgeTitle)}">Signals: ${escapeHtml(warningsBadgeLabel)}</span>`;

  // Snapshot status HTML (if available)
  let snapshotStatusHtml = "";
  if (snapshotStatus) {
    // Check for API error
    if (snapshotStatus._apiError) {
      snapshotStatusHtml = `
        <div class="snapshotStatus" style="display: flex; align-items: center; gap: 12px; font-size: 12px; margin-right: 12px;">
          <span class="muted" style="color: var(--warn);" title="${escapeAttr(snapshotStatus._errorMessage || 'API server unavailable')}">
            ⚠️ API unavailable
          </span>
        </div>
      `;
    } else {
      const running = snapshotStatus.running;
      const minutesUntil = snapshotStatus.minutesUntilNextRun || 0;
      const progress = snapshotStatus.progress || {};
      
      if (running) {
        const step = progress.step || "Running...";
        const progressPct = progress.progress || 0;
        const etaMinutes = progress.etaMinutes;
        // Handle negative or zero ETA - show a better message when taking longer than expected
        let etaText = "Running...";
        if (etaMinutes !== undefined && etaMinutes !== null) {
          if (etaMinutes <= 0) {
            etaText = "Taking longer than expected...";
          } else {
            etaText = `~${etaMinutes} min left`;
          }
        }
        snapshotStatusHtml = `
          <div class="snapshotStatus" style="display: flex; align-items: center; gap: 12px; font-size: 12px; margin-right: 12px;">
            <span class="muted">Snapshot: ${escapeHtml(step)}</span>
            <span class="muted" style="font-size: 11px;">${escapeHtml(etaText)}</span>
            <button class="btn ghost" style="font-size: 11px; padding: 4px 8px;" disabled>Running...</button>
          </div>
        `;
      } else {
        const nextRunText = minutesUntil > 0 
          ? `Next snapshot in ${minutesUntil} min${minutesUntil !== 1 ? 's' : ''}`
          : "Next snapshot soon";
        snapshotStatusHtml = `
          <div class="snapshotStatus" style="display: flex; align-items: center; gap: 12px; font-size: 12px; margin-right: 12px;">
            <span class="muted">${nextRunText}</span>
            <button class="btn ghost" id="triggerSnapshotBtn" style="font-size: 11px; padding: 4px 8px;" type="button">Run Now</button>
          </div>
        `;
      }
    }
  }

  top.innerHTML = `
    <div class="topLeft">
      <div class="topTitle">${escapeHtml(title)}</div>
      <div class="topSub">${escapeHtml(topSubText)}</div>
    </div>

    <div class="topRight">
      ${snapshotStatusHtml}
      <div class="topBadges">
${badgesHtml}
      </div>
      <div class="seg" role="group" aria-label="Theme">
        <button type="button" class="segBtn ${theme === "dark" ? "active" : ""}" id="themeDark">${escapeHtml(tt("dark"))}</button>
        <button type="button" class="segBtn ${theme === "light" ? "active" : ""}" id="themeLight">${escapeHtml(tt("light"))}</button>
      </div>

      <button class="btn ghost" type="button" id="openLatestBtn">${escapeHtml(tt("openLatest"))}</button>
    </div>
  `;

  const openBtn = el("openLatestBtn");
  if (openBtn) openBtn.onclick = () => window.open(DEFAULT_JSON_URL, "_blank");
  
  // Add click handler for snapshot trigger button (if present)
  const triggerBtn = el("triggerSnapshotBtn");
  if (triggerBtn) {
    triggerBtn.onclick = async () => {
      triggerBtn.disabled = true;
      triggerBtn.textContent = "Triggering...";
      try {
    const data = await safeApiFetch(`${SNAPSHOT_API_BASE}/api/snapshot/trigger`, { method: "POST" });
    if (data && data._error) {
      alert(`Failed to trigger snapshot: ${data.message || 'API server unavailable'}`);
      triggerBtn.disabled = false;
      triggerBtn.textContent = "Run Now";
      return;
    }
        if (data && data.success) {
          triggerBtn.textContent = "Triggered!";
          // Refresh status immediately
          setTimeout(() => fetchSnapshotStatus(), 1000);
        } else {
          triggerBtn.textContent = "Failed";
          setTimeout(() => {
            triggerBtn.disabled = false;
            triggerBtn.textContent = "Run Now";
          }, 2000);
        }
      } catch (e) {
        triggerBtn.textContent = "Error";
        setTimeout(() => {
          triggerBtn.disabled = false;
          triggerBtn.textContent = "Run Now";
        }, 2000);
      }
    };
  }

  const td = el("themeDark");
  const tl = el("themeLight");
  if (td) td.onclick = () => { uiTheme = "dark"; applyTheme(); render(); };
  if (tl) tl.onclick = () => { uiTheme = "light"; applyTheme(); render(); };
}


// ---------- View switch ----------
function setView(view) {
  // Reset ticket state when leaving ticket view
  if (currentView === "ticket" && view !== "ticket") {
    ticketQuery = "";
    ticketFilterHasPRs = false;
    ticketFilterStage = "";
  }
  
  // Reset ticket state when entering ticket view (ensure clean state)
  if (view === "ticket" && currentView !== "ticket") {
    ticketQuery = "";
    ticketFilterHasPRs = false;
    ticketFilterStage = "";
  }

  // Reset history search when leaving history view
  if (currentView === "history" && view !== "history") {
    historyFilters.q = "";
  }

  currentView = view;
  
  // Scroll to top on every view change (mainScroll = scroll container)
  const mainScrollEl = document.querySelector(".mainScroll");
  if (mainScrollEl) mainScrollEl.scrollTo({ top: 0, behavior: "instant" });

  // reset UI state that should not leak between views
  envLinksOpenKey = null;
  detailsOpenEnvKey = null;

  compareSelected = new Set();
  compareOnlyMismatches = false;
  comparePair = null;


  // Right sidebar should never "stick" when you leave project views
  if (!isProjectSidebarAllowed()) {
    setSidebar(false);
  }

  // Update URL hash for deep-linking
  updateUrlHash();
  render();
}


function selectLeaf(leafKey) {
  currentLeafKey = leafKey;
  expandedLeafKey = leafKey;
  expandedGroupKey = findParentGroupKey(leafKey);

  // switching project should start "clean" (no sticky UI)
  setSidebar(false);
  // Ensure we switch to env view (default view for platforms)
  currentView = "env";
  setView("env");
}


// ---------- Left nav ----------


function renderLeftNav() {
  const list = el("projectsList");
  if (!list) return;

  const ovBtn = el("navOverviewBtn");
  if (ovBtn) {
    ovBtn.classList.toggle("active", currentView === "overview");
   ovBtn.onclick = () => openOverview();

  }

  const renderLeaf = (leaf, indent = 0) => {
    const isProjectView = currentView === "env";
    const isActiveLeaf = isProjectView && leaf.key === currentLeafKey;
    const showSubmenu = isProjectView && expandedLeafKey === leaf.key;

    const subEnvActive = isActiveLeaf && currentView === "env";

    return `
      <div class="projectItem ${isActiveLeaf ? "activeProject" : ""}" style="${indent ? `margin-left:${indent}px;` : ""}">
        <button class="navItem ${isActiveLeaf ? "active" : ""}"
                data-leaf="${escapeHtml(leaf.key)}"
                type="button">
          ${escapeHtml(leaf.label)}
        </button>

        <div class="projectSub ${showSubmenu ? "open" : ""}">
          <button class="subItem ${subEnvActive ? "active" : ""}"
                  data-sub="env"
                  data-leaf="${escapeHtml(leaf.key)}"
                  type="button">Environments</button>
        </div>
      </div>
    `;
  };

  const navTree = buildNavTree();
  list.innerHTML = (navTree.length > 0 ? navTree : NAV_TREE_LEGACY).map((node) => {
    if (node.type === "leaf") return renderLeaf(node, 0);

    const isOpen = expandedGroupKey === node.key;
    return `
      <div class="navGroup">
        <button class="navItem groupItem ${isOpen ? "open" : ""}"
                data-group="${escapeHtml(node.key)}"
                type="button">
          <span>${escapeHtml(node.label)}</span>
          <span class="caret">${isOpen ? "▾" : "▸"}</span>
        </button>

        <div class="groupChildren ${isOpen ? "open" : ""}">
          ${node.children.map((c) => renderLeaf(c, 10)).join("")}
        </div>
      </div>
    `;
  }).join("");

  const ddAny = (appData?.observability?.summary || []).length > 0;

  // group expand/collapse
  list.querySelectorAll("button[data-group]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const g = btn.getAttribute("data-group");
      expandedGroupKey = (expandedGroupKey === g) ? null : g;
      renderLeftNav();
    });
  });

  // leaf click => select leaf + ensure parent group expanded
  list.querySelectorAll("button[data-leaf]:not([data-sub])").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const leafKey = btn.getAttribute("data-leaf");
      const parent = findParentGroupKey(leafKey);
      if (parent) expandedGroupKey = parent;
      selectLeaf(leafKey);
    });
  });

  // submenu click => view switch
  list.querySelectorAll("button[data-sub]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const leafKey = btn.getAttribute("data-leaf");
      const sub = btn.getAttribute("data-sub"); // env | releases
      currentLeafKey = leafKey;
      expandedLeafKey = leafKey;
      setView(sub);
    });
  });

  const setupSection = document.getElementById("navSectionSetup");
  if (setupSection) {
    setupSection.style.display = "";
    setupSection.querySelectorAll(".navItem[data-view='setup']").forEach((btn) => {
      btn.classList.toggle("active", currentView === "setup");
    });
  }
}


// ---------- Overview ----------
function getAllEnvs() {
  const projects = Array.isArray(appData.projects) ? appData.projects : [];
  const all = [];
  for (const p of projects) {
    const envs = Array.isArray(p.environments) ? p.environments : [];
    for (const e of envs) all.push({ project: p, env: e });
  }
  return all;
}

function normalizeNote(note) {
  if (note == null) return "";
  if (typeof note === "string") return note;
  if (typeof note === "number" || typeof note === "boolean") return String(note);
  if (Array.isArray(note)) {
    return note.map(normalizeNote).filter(Boolean).join(" • ");
  }
  if (typeof note === "object") {
    const msg =
      note.message ??
      note.text ??
      note.title ??
      note.reason ??
      note.note ??
      note.error ??
      note.description ??
      "";
    if (msg) return String(msg);
    try {
      return JSON.stringify(note);
    } catch {
      return String(note);
    }
  }
  return String(note);
}

function renderOverview() {
  const wrap = el("overviewContent");
  if (!wrap) return;

  try {

  const projects = Array.isArray(appData.projects) ? appData.projects : [];

  // Datadog availability flag (used to label the Parameters & logs panel)
  // NOTE: there is a similarly named variable inside renderLeftNav(), but it is
  // not visible here.
  const ddAny = (appData?.observability?.summary || []).length > 0;

  // ---------- Stage model (overview uses universal pipeline only) ----------
  const STAGES = ["dev", "qa", "uat", "prod"];
  const STAGE_LABEL = { dev: "DEV", qa: "QA", uat: "UAT", prod: "PROD" };

  // ---------- Parameters & Logs (mock) env selector ----------
  const PARAM_ENVS = ["prod", "uat", "qa", "dev"];
  const PARAM_ENV_LABEL = { prod: "PROD", uat: "UAT", qa: "QA", dev: "DEV" };

  const normalizeKey = (v) => String(v || "").toLowerCase();

  const getParamEnvFor = (projectKey) => {
    const k = String(projectKey || "");
    const v = String(paramsEnvByProject[k] || "prod").toLowerCase();
    return PARAM_ENVS.includes(v) ? v : "prod";
  };

  const setParamEnvFor = (projectKey, envKey) => {
    const k = String(projectKey || "");
    const v = String(envKey || "").toLowerCase();
    paramsEnvByProject[k] = PARAM_ENVS.includes(v) ? v : "prod";
  };

  // Stage aliases (helps avoid false "N/A" when env keys are e.g. "green"/"orange")
  // DEV stays an aggregate lane.
  const stageEnvKeys = {
    dev: ["dev", "develop", "alpha", "beta"],
    qa: ["qa", "green", "orange"],
    uat: ["uat", "yellow"],
    prod: ["prod", "production"],
  };

  const getStageEnvs = (project, stage) => {
    const envs = getEnvList(project);
    const wanted = stageEnvKeys[stage] || [stage];
    return envs.filter(e => {
      const k = normalizeKey(e.key || e.name);
      return wanted.some(w => k === w || k.endsWith("-" + w) || k.includes(w)); // tolerant
    });
  };

  const envSeverity = (env) => {
    const status = normalizeKey(env?.status);
    const argo = normalizeKey(env?.argoStatus);
    const notes = Array.isArray(env?.notes) ? env.notes.length : 0;
    const warns = Array.isArray(env?.warnings) ? env.warnings.length : 0;

    if (status === "blocked") return "bad";
    if (argo.includes("degraded") || argo.includes("outofsync")) return "warn";
    if (warns > 0) return "warn";
    if (notes > 0) return "warn";
    if (status === "healthy" || status === "ok" || status === "synced") return "ok";
    if (!status) return "warn";
    return "warn";
  };

  const stageSeverity = (project, stage) => {
    const envs = getStageEnvs(project, stage);
    if (!envs.length) return "na";
    const sev = envs.map(envSeverity);
    if (sev.includes("bad")) return "bad";
    if (sev.includes("warn")) return "warn";
    return "ok";
  };

  const stageVersion = (project, stage) => {
    const envs = getStageEnvs(project, stage);
    if (!envs.length) return "-";
    // pick "most relevant": prefer exact env (qa/uat/prod) or plain "dev" for dev lane
    const pick = (() => {
      const wanted = stageEnvKeys[stage] || [stage];
      const envsByKey = new Map(envs.map(e => [normalizeKey(e.key || e.name), e]));
      for (const w of wanted) {
        for (const [k, e] of envsByKey.entries()) {
          if (k === w || k.endsWith("-" + w)) return e;
        }
      }
      return envs[0];
    })();

    // tolerant fields
    const v =
      pick?.build || pick?.version || pick?.tag || pick?.imageTag ||
      pick?.branch || pick?.releaseBranch ||
      (pick?.lastDeploy && normalizeKey(pick.lastDeploy).includes("release/") ? pick.lastDeploy : null) ||
      "-";
    return String(v).replace(/^v(?=\d)/i, ""); // keep pretty, drop leading v if present
  };

  // ---------- Sticky alerts (1h) ----------
  const STICKY_KEY = "roc_sticky_alerts_v1";
  const STICKY_TTL_MS = 60 * 60 * 1000;

  const loadSticky = () => {
    try {
      const raw = localStorage.getItem(STICKY_KEY);
      if (!raw) return [];
      const arr = JSON.parse(raw);
      if (!Array.isArray(arr)) return [];
      const now = Date.now();
      return arr.filter(x => x && x.expiresAt && x.expiresAt > now);
    } catch {
      return [];
    }
  };

  const saveSticky = (arr) => {
    try {
      localStorage.setItem(STICKY_KEY, JSON.stringify(arr.slice(0, 30)));
    } catch {}
  };

  // Build "live" alert sources: globalAlerts + blocked envs + argo degraded/outofsync notes
  const globalAlerts = Array.isArray(appData.globalAlerts) ? appData.globalAlerts : [];
  const allEnvs = getAllEnvs();

  const liveAlerts = [];

  // global alerts -> alert events
  for (const a of globalAlerts) {
    const level = normalizeKey(a.level || "info");
    liveAlerts.push({
      kind: "alert",
      level: level === "high" || level === "bad" || level === "error" ? "bad" : (level === "warn" ? "warn" : "warn"),
      ts: a.ts || a.time || appData.generatedAt || new Date().toISOString(),
      title: a.title || "Alert",
      message: a.message || "",
      key: `global:${a.id || a.title || a.message}`.slice(0, 180),
      projectKey: null,
      envKey: null,
    });
  }

  // env alerts (blocked / degraded / outofsync)
  for (const { project, env } of allEnvs) {
    const sev = envSeverity(env);
    if (sev === "bad" || sev === "warn") {
      const envName = env.name || env.key || "env";
      const projName = project.name || project.key || "Platform";
      const note = normalizeNote(env.note || env.alert || env.message) || (Array.isArray(env.notes) ? env.notes[0] : "") || env.argoStatus || "";
      const title =
        sev === "bad" ? `Blocked in ${projName}` : `Warning in ${projName}`;
      const msg =
        sev === "bad" ? `${envName}: ${note || "Blocked"}` : `${envName}: ${note || "Needs attention"}`;

      liveAlerts.push({
        kind: "alert",
        level: sev,
        ts: env.lastDeploy || env.lastDeployedAt || appData.generatedAt || new Date().toISOString(),
        title,
        message: msg,
        key: `env:${project.key}:${normalizeKey(env.key || env.name)}:${sev}`.slice(0, 180),
        projectKey: project.key,
        envKey: env.key || env.name || "",
      });
    }
  }

  // keep sticky for alerts (so they don't disappear instantly)
  let sticky = loadSticky();

  // remove sticky env alerts that are resolved (env no longer bad/warn)
  const currentEnvSev = new Map();
  for (const { project, env } of allEnvs) {
    currentEnvSev.set(`env:${project.key}:${normalizeKey(env.key || env.name)}`, envSeverity(env));
  }
  sticky = sticky.filter(a => {
    if (a.kind !== "alert") return false;
    if (a.key?.startsWith("env:")) {
      const parts = String(a.key).split(":");
      const mapKey = `env:${parts[1]}:${parts[2]}`;
      const sev = currentEnvSev.get(mapKey);
      return sev === "bad" || sev === "warn";
    }
    return true; // global alerts expire by TTL only
  });

  // add new live alerts to sticky list (by key)
  const stickyKeys = new Set(sticky.map(x => x.key));
  const nowMs = Date.now();
  for (const a of liveAlerts) {
    if (a.kind === "alert" && !stickyKeys.has(a.key)) {
      sticky.push({ ...a, createdAt: nowMs, expiresAt: nowMs + STICKY_TTL_MS });
      stickyKeys.add(a.key);
    }
  }
  // keep only non-expired
  sticky = sticky.filter(x => x.expiresAt > nowMs);
  saveSticky(sticky);

  const section2 = ""; // Datadog News removed in open-source version

  // ---------- Reminders & signals (root warnings + observability warnings + computed reminders) ----------
  const STALE_DAYS = 14;
  const staleNowMs = Date.now();
  const rootWarnings = getRootWarnings();

  const computedReminders = [];
  for (const { project, env } of allEnvs) {
    const projName = project.name || project.key || "Platform";
    const envName = env.name || env.key || "env";
    const lastDeploy = env.lastDeploy || env.lastDeployedAt || env.lastDeploymentAt || null;
    if (lastDeploy) {
      const deployMs = new Date(lastDeploy).getTime();
      const daysAgo = Math.floor((staleNowMs - deployMs) / (24 * 60 * 60 * 1000));
      if (daysAgo >= STALE_DAYS) {
        computedReminders.push({
          reason: "Stale deploy",
          msg: `No deployment on ${projName} / ${envName} for ${daysAgo} days - worth checking.`,
          scope: "env",
          project: project.key,
          env: env.key,
          level: "warn",
          source: "reminder",
        });
      }
    }
  }
  for (const p of projects) {
    const qaEnvs = getStageEnvs(p, "qa");
    const uatEnvs = getStageEnvs(p, "uat");
    if (!qaEnvs.length || !uatEnvs.length) continue;
    const qaEnv = qaEnvs[0];
    const uatEnv = uatEnvs[0];
    const qaComps = Array.isArray(qaEnv.components) ? qaEnv.components : [];
    const uatComps = Array.isArray(uatEnv.components) ? uatEnv.components : [];
    const qaByRepo = {};
    const uatByRepo = {};
    for (const c of qaComps) {
      const repo = c.repo || c.name || "";
      if (repo) qaByRepo[repo] = c.tag || c.build || c.imageTag || "";
    }
    for (const c of uatComps) {
      const repo = c.repo || c.name || "";
      if (repo) uatByRepo[repo] = c.tag || c.build || c.imageTag || "";
    }
    const allRepos = new Set([...Object.keys(qaByRepo), ...Object.keys(uatByRepo)]);
    let diffCount = 0;
    let onlyDiffRepo = null;
    for (const repo of allRepos) {
      const qaV = qaByRepo[repo] || "";
      const uatV = uatByRepo[repo] || "";
      if (qaV !== uatV) {
        diffCount++;
        onlyDiffRepo = repo;
      }
    }
    if (diffCount === 1 && onlyDiffRepo) {
      const projName = p.name || p.key || "Platform";
      computedReminders.push({
        reason: "QA vs UAT diff",
        msg: `Only difference between QA and UAT in ${projName} is repo ${onlyDiffRepo}. Consider Drift runbook to verify back-merges.`,
        scope: "project",
        project: p.key,
        env: null,
        level: "warn",
        source: "reminder",
      });
    }
  }

  // Runbook suggestions (contextual hints to run specific runbooks)
  const projectsWithRepos = projects.filter((p) => {
    const envs = (p.environments || []);
    const comps = envs.flatMap((e) => e.components || []);
    return comps.length >= 2;
  });
  if (projectsWithRepos.length) {
    computedReminders.push({
      reason: "Runbook: Drift",
      msg: "Consider running Drift runbook to check for unmerged commits in release branches (hotfixes not back-merged to main).",
      scope: "global",
      project: null,
      env: null,
      level: "info",
      source: "reminder",
    });
    computedReminders.push({
      reason: "Runbook: Scope",
      msg: "Consider running Scope runbook to compare baseline vs default branch before release (commits, tickets).",
      scope: "global",
      project: null,
      env: null,
      level: "info",
      source: "reminder",
    });
    computedReminders.push({
      reason: "Runbook: Release Diff",
      msg: "Consider running Release Diff runbook to compare two releases (older vs newer) - added commits and tickets per repo.",
      scope: "global",
      project: null,
      env: null,
      level: "info",
      source: "reminder",
    });
  }

  const normRoot = rootWarnings.map((w) => ({
    reason: String(w?.reason || w?.code || "warning"),
    msg: String(w?.message || w?.detail || w?.msg || "").trim() || String(w?.reason || w?.code || ""),
    scope: w?.scope || (w?.env ? "env" : "global"),
    project: w?.project || w?.projectKey,
    env: w?.env || w?.envKey,
    component: w?.component,
    level: String(w?.level || "warn").toLowerCase(),
    source: String(w?.source || "snapshot"),
  }));
  const signals = [...normRoot, ...computedReminders];
  const topSignals = signals.slice(0, 8);
  const signalHtml = topSignals.map((w) => {
    const lvl = String(w?.level || "info").toLowerCase();
    const css = (lvl === "warning" || lvl === "warn") ? "warn" : (lvl === "error" || lvl === "bad") ? "bad" : "ok";
    const where = [w?.project, w?.env, w?.component].filter(Boolean).join(" / ");
    const reason = String(w?.reason || "signal");
    const displayMsg = String(w?.msg || "").trim() || reason;
    return `
      <div class="warnItem ${css}" title="${escapeAttr(w?.source ? `Source: ${w.source}` : "")}">
        <div class="warnTop">
          <div class="warnTitle">${escapeHtml(reason)}</div>
          <div class="warnMeta">${where ? escapeHtml(where) + " • " : ""}${escapeHtml(displayMsg)}</div>
        </div>
      </div>
    `;
  }).join("");

  const sectionWarnings = `
    <div class="panel">
      <div class="ov2H">Reminders &amp; signals</div>
      ${signals.length ? `
        <div class="warnList">${signalHtml}</div>
        <div class="muted" style="margin-top:8px;">Showing ${Math.min(8, signals.length)} of ${signals.length}. Deployment hints and runbook suggestions.</div>
      ` : `<div class="muted">No reminders or signals.</div>`}
    </div>
  `;

  // Mini History now uses Release History data (see section3 below)
  // No localStorage needed - reusing Release History end-to-end

  // ---------- Mock params (deterministic) ----------
  const hash01 = (str) => {
    const h = cryptoHash(str);
    return (h % 10000) / 10000;
  };

  const mockMetricsFor = (projectKey) => {
    const a = hash01(projectKey + ":a");
    const b = hash01(projectKey + ":b");
    const c = hash01(projectKey + ":c");
    const d = hash01(projectKey + ":d");
    const cpu = Math.round(25 + a * 65);          // %
    const mem = Math.round(30 + b * 60);          // %
    const pods = Math.round(3 + c * 14);          // count
    const err = (d * 2.5).toFixed(2);             // %
    const p95 = Math.round(120 + (1 - a) * 900);  // ms
    return { cpu, mem, pods, err, p95 };
  };

  // slightly varied numbers per env (still deterministic)
  const mockMetricsForEnv = (projectKey, envKey) => {
    const base = mockMetricsFor(projectKey);
    const shift = envKey === "prod" ? 1.0 : envKey === "uat" ? 0.92 : envKey === "qa" ? 0.86 : 0.80;
    return {
      cpu: Math.max(1, Math.min(99, Math.round(base.cpu * shift))),
      mem: Math.max(1, Math.min(99, Math.round(base.mem * shift))),
      pods: Math.max(1, Math.round(base.pods * (envKey === "prod" ? 1.0 : 0.9))),
      err: (parseFloat(base.err) * (envKey === "prod" ? 1.0 : 0.85)).toFixed(2),
      p95: Math.max(20, Math.round(base.p95 * (envKey === "prod" ? 1.0 : 0.9))),
    };
  };

  const getDatadogMetricsForEnv = (projectKey, envKey) => {
    const items = appData?.observability?.summary || [];
    const hit = items.find((it) => (
      String(it.projectKey || "") === String(projectKey) &&
      String(it.envKey || "").toLowerCase() === String(envKey).toLowerCase()
    ));
    if (!hit || !hit.metrics) return null;
    return {
      cpu: hit.metrics.cpuPct,
      mem: hit.metrics.memPct,
      pods: hit.metrics.pods,
      err: hit.metrics.errorRatePct,
      // snapshot uses `p95ms` (older patches used `p95Ms`)
      p95: (hit.metrics.p95ms !== undefined ? hit.metrics.p95ms : hit.metrics.p95Ms),
      _tags: hit.usedTags || [],
      _note: hit.note || "",
      _windowMinutes: (hit.meta && hit.meta.minutes) || null,
    };
  };

  // ---------- Render helpers ----------
  const sevToClass = (sev) => (sev === "bad" ? "bad" : sev === "na" ? "na" : "ok");
  const sevToLabel = (sev) => (sev === "bad" ? "Blocked" : sev === "na" ? "No signal" : "Healthy");

  // ---------- SVG "pizza" platform wheel (environments as slices) ----------
const platformWheelSvg = (project) => {
  const envs = getEnvList(project);
  const N = envs.length || 1;
  const cx = 60, cy = 60, r = 54, ir = 32;
  const total = 2 * Math.PI;
  const gap = 0.012;
  let start = -Math.PI / 2;

  // text radius: between inner and outer ring
  const tr = (ir + r) / 2;

  const slices = envs.map((env, idx) => {
    const end = start + total / N;
    
    // Determine severity from env status
    const sev = envSeverity(env);
    const cls = sevToClass(sev);

    const a0 = start + gap;
    const a1 = end - gap;

    const path = donutSlicePath(cx, cy, r, ir, a0, a1);

    // mid-angle for label placement
    const mid = (a0 + a1) / 2;
    const lx = cx + tr * Math.cos(mid);
    const ly = cy + tr * Math.sin(mid);

    // rotate text so it's tangent-ish but readable
    let rot = (mid * 180) / Math.PI + 90;
    // keep upright (avoid upside-down labels)
    if (rot > 90 && rot < 270) rot += 180;

    const envName = env.name || env.key || "";
    const envKey = String(env.key || "").toLowerCase();
    const title = `${envName} • ${sevToLabel(sev)}`;
    const dataKey = escapeHtml(project.key || "");
    const dataEnv = escapeAttr(envKey);

    // Use environment name as label (shortened if needed)
    const label = envName.length > 8 ? envName.slice(0, 8) : envName;

    const slice = `
      <g class="wheelGroup">
        <path class="wheelSlice ${cls}" d="${path}" tabindex="0"
              data-project="${dataKey}" data-env="${dataEnv}">
          <title>${escapeHtml(title)}</title>
        </path>

        <text class="wheelLabel"
              x="${lx.toFixed(2)}" y="${ly.toFixed(2)}"
              text-anchor="middle" dominant-baseline="middle"
              transform="rotate(${rot.toFixed(2)} ${lx.toFixed(2)} ${ly.toFixed(2)})">
          ${escapeHtml(label)}
        </text>
      </g>
    `;
    start = end;
    return slice;
  }).join("");

  const projectName = project.name || project.key || "Platform";
  const projectKey = escapeHtml(project.key || "");

  return `
    <svg class="wheel" viewBox="0 0 120 120" role="img" aria-label="${escapeHtml(projectName)} platform radar">
      ${slices}
      <circle class="wheelHole" cx="60" cy="60" r="30"></circle>
 <text class="wheelNum"
      x="60" y="66"
      text-anchor="middle"
      font-size="44"
      font-weight="900"
      fill="#fff">
        ${envs.length}
</text>
    </svg>
  `;
};
// Short labels for pizza (UI-only)
const PIZZA_LABELS = {
  "Booking Services": "BS",
  "B2C (PO1v13)": "B2C",
  "PO1 (PO1v8)": "PO1",
  "TCBP → MFEs": "MFEs",
  "TAP2.0": "TAP2",
  "TCBP → Adapters": "Adapters",
  // możesz dopisywać kolejne:
  // "TAP2.0": "TAP2",
};

// helper: choose best label for a project
const getPizzaLabel = (p) => {
  const name = String(p?.name || "").trim();
  const key  = String(p?.key  || "").trim();

  // 1) exact match by name
  if (name && PIZZA_LABELS[name]) return PIZZA_LABELS[name];

  // 2) exact match by key (jeśli wolisz mapować po key)
  if (key && PIZZA_LABELS[key]) return PIZZA_LABELS[key];

  // 3) fallback: krótkie nazwy bez mapki
  const raw = name || key || "";
  return raw.length > 10 ? raw.slice(0, 10) : raw;
};


const section1 = `
  <div class="stageGrid">
    ${projects.map(p => `
      <div class="stageCard">
        <div class="stageTop">
          <div class="stageName">${escapeHtml(p.name || p.key || "Platform")}</div>
        </div>
        ${platformWheelSvg(p)}
      </div>
    `).join("")}
  </div>

  <div class="pipelineHint">click a slice → environment details</div>
`;



  // Mini History: Preview of Release History (last 10 events, same data/UI/modal)
  // Reuse Release History data and rendering completely
  const labelByProjectKey = {
    TAP2: "TAP2.0",
    PO1V8: "PO1 (PO1v8)",
    B2C: "B2C (PO1v13)",
    TCBP_MFES: "TCBP → MFEs",
    TCBP_ADAPTERS: "TCBP → Adapters",
    LCW: "LCW",
    BS: "Booking Services",
  };

  // Load release history data if not already loaded (ensures Mini History works on first render)
  let miniHistoryEvents = [];
  let miniHistoryLoading = false;
  let miniHistoryError = null;
  
  if (!releaseHistoryData && !releaseHistoryLoading) {
    // Start loading in background, but render loading state
    miniHistoryLoading = true;
    ensureReleaseHistoryLoaded()
      .then(() => {
        miniHistoryLoading = false;
        // Re-render Overview to show loaded data
        if (currentView === "overview") renderOverview();
      })
      .catch((e) => {
        miniHistoryLoading = false;
        miniHistoryError = String(e?.message || e);
        // Re-render Overview to show error
        if (currentView === "overview") renderOverview();
      });
  } else if (releaseHistoryLoading) {
    miniHistoryLoading = true;
  } else if (releaseHistoryLoadError) {
    miniHistoryError = releaseHistoryLoadError;
  }

  // Get events from Release History data (same source as Release History view)
  if (releaseHistoryData) {
    if (releaseHistoryData?.format === "append-only") {
      miniHistoryEvents = releaseHistoryData.events || [];
    } else if (releaseHistoryData?.format === "legacy") {
      const legacyData = releaseHistoryData.data;
      const projectsObj = legacyData?.projects && typeof legacyData.projects === "object" ? legacyData.projects : {};
      Object.keys(projectsObj).forEach((pKey) => {
        const entry = projectsObj[pKey];
        const events = Array.isArray(entry) ? entry : (Array.isArray(entry?.events) ? entry.events : []);
        for (const ev of events) {
          miniHistoryEvents.push({
            ...ev,
            _projectKey: pKey,
            _projectLabel: labelByProjectKey[pKey] || pKey,
          });
        }
      });
    } else {
      const projectsObj = releaseHistoryData?.projects && typeof releaseHistoryData.projects === "object" ? releaseHistoryData.projects : {};
      Object.keys(projectsObj).forEach((pKey) => {
        const entry = projectsObj[pKey];
        const events = Array.isArray(entry) ? entry : (Array.isArray(entry?.events) ? entry.events : []);
        for (const ev of events) {
          miniHistoryEvents.push({
            ...ev,
            _projectKey: pKey,
            _projectLabel: labelByProjectKey[pKey] || pKey,
          });
        }
      });
    }
  }

  // Sort by timestamp (most recent first) - same as Release History
  miniHistoryEvents.sort((a, b) => {
    const ta = new Date(a.at || a.time || 0).getTime();
    const tb = new Date(b.at || b.time || 0).getTime();
    return tb - ta;
  });

  // Limit to last 10 events
  const limitedEvents = miniHistoryEvents.slice(0, 10);

  // Reuse the SAME renderEventRow function from Release History
  const renderEventRow = (ev, idx, opts = {}) => {
    const envKey = String(ev.envKey || ev.env || "");
    const envName = String(ev.envName || envKey || "-");
    const comp = String(ev.component || "-");
    const fromTagRaw = String(ev.fromTag || "-");
    const toTagRaw = String(ev.toTag || "-");
    const fromTag = preferVersionTag(fromTagRaw);
    const toTag = preferVersionTag(toTagRaw);
    const by = String(ev.by || "-");
    const at = String(ev.at || ev.time || "");
    const ago = fmtAgo(at);
    const abs = at ? fmtDate(at) : "-";
    const kind = String(ev.kind || "TAG_CHANGE").toUpperCase();

    const links = Array.isArray(ev.links) ? ev.links : [];
    const commitUrl = ev.commitUrl || ev.commitURL || "";
    const kustUrl = ev.kustomizationUrl || ev.kustomizationURL || "";
    const baseLinks = [
      commitUrl ? { url: commitUrl, label: 'Commit' } : null,
      kustUrl ? { url: kustUrl, label: 'Kustomization' } : null,
      ...links.map((l) => l?.url ? ({ url: l.url, label: (l.label || l.type || 'Link') }) : null)
    ].filter(Boolean);

    const linkHtml = renderSourceLinks(baseLinks);
    const warnIcon = renderWarningsIcon(ev.warnings);
    const nested = opts.nested ? " nested" : "";

    return `
      <div class="historyRow${nested}" data-hevent="${escapeAttr(JSON.stringify({
        p: ev._projectKey,
        e: String(ev.envKey || ev.env || ""),
        c: String(ev.component || ""),
        f: String(ev.fromTag || ""),
        t: String(ev.toTag || ""),
        by: String(ev.by || ""),
        at: String(ev.at || ev.time || ""),
        commitUrl: String(ev.commitUrl || ev.commitURL || ""),
        kustomizationUrl: String(ev.kustomizationUrl || ev.kustomizationURL || ""),
        links: Array.isArray(ev.links) ? ev.links : [],
        warnings: ev.warnings || [],
      }))}">
        <div class="historyRowTop">
          <div class="historyRowLeft">
            <span class="pill softPill historyRowLeftEnv">${escapeHtml(envName)}</span>
            <span class="pill infoPill historyRowLeftPlatform">${escapeHtml(ev._projectLabel)}</span>
            <div class="historyRowLeftMain">
              <span class="historyComp">${escapeHtml(comp)}</span>
              ${kind !== "TAG_CHANGE" ? `<span class="pill">${escapeHtml(kind)}</span>` : ""}
              ${warnIcon}
            </div>
          </div>

          <div class="historyRowMid mono">
            <span class="fromTag" title="${escapeAttr(fromTagRaw)}">${escapeHtml(fromTag)}</span>
            <span class="arrow">→</span>
            <span class="toTag" title="${escapeAttr(toTagRaw)}">${escapeHtml(toTag)}</span>
          </div>

          <div class="historyRowRight">
            <span class="muted">by</span> <span class="historyBy">${escapeHtml(by)}</span>
            <span class="muted" title="${escapeAttr(abs)}">· ${escapeHtml(ago)}</span>
            <span class="historyLinks">${linkHtml || ""}</span>
          </div>
        </div>
      </div>
    `;
  };

  let historyRows;
  if (miniHistoryLoading) {
    historyRows = `<div class="muted" style="padding:12px; text-align:center;">Loading deployment events…</div>`;
  } else if (miniHistoryError) {
    historyRows = `<div class="muted" style="padding:12px;">Unable to load deployment events</div>`;
  } else if (limitedEvents.length > 0) {
    historyRows = limitedEvents.map((ev, idx) => renderEventRow(ev, idx)).join("");
  } else {
    historyRows = `<div class="muted" style="padding:12px;">No deployment events available</div>`;
  }

  const section3 = `
    <div class="panel">
      <div class="ov2H">Mini history (last 10 events)</div>
      <div class="historySectionBody miniHistoryContainer">
        ${historyRows}
      </div>
      <div class="miniHistoryFooter">
        <a href="#" onclick="setView('history'); return false;" class="miniHistoryLink">View full Release History →</a>
      </div>
    </div>
  `;

  // Initialize drawer for Mini History (same as Release History)
  // This ensures the drawer works even if Release History view hasn't been opened yet
  setTimeout(() => {
    const drawer = el("historyDrawer");
    const drawerBody = el("historyDrawerBody");
    
    // Create drawer if it doesn't exist (normally created in renderHistory)
    if (!drawer) {
      const drawerHtml = `
        <div id="historyDrawer" class="historyDrawer hidden">
          <div class="historyDrawerBackdrop" data-hdrawer-close="1"></div>
          <div class="historyDrawerPanel">
            <div class="historyDrawerHeader">
              <div class="historyDrawerTitle">Event details</div>
              <button type="button" class="chip" data-hdrawer-close="1">Close</button>
            </div>
            <div id="historyDrawerBody" class="historyDrawerBody"></div>
          </div>
        </div>
      `;
      document.body.insertAdjacentHTML("beforeend", drawerHtml);
    }
    
    // Bind drawer handlers (same logic as renderHistory)
    const miniDrawer = el("historyDrawer");
    const miniDrawerBody = el("historyDrawerBody");
    if (!miniDrawer || !miniDrawerBody) return;
    
    const closeDrawer = () => {
      miniDrawer.classList.add("hidden");
      miniDrawerBody.innerHTML = "";
    };
    
    document.querySelectorAll("[data-hdrawer-close]").forEach((x) => {
      x.onclick = closeDrawer;
    });
    
    // Bind click handlers for .historyRow elements in Mini History
    // Use the same handler pattern as Release History
    const handleMiniHistoryRowClick = (e) => {
      // Don't open drawer if clicking on a link or button
      if (e.target.closest('a') || e.target.closest('button')) return;
      
      const row = e.currentTarget || e.target.closest('.historyRow');
      if (!row) return;
      
      const raw = row.getAttribute("data-hevent");
      if (!raw || !miniDrawer || !miniDrawerBody) return;
      let ev;
      try { ev = JSON.parse(raw); } catch { return; }

      const parts = [];
      const kv = (k, v) => `<div class='historyKV'><div class='muted'>${escapeHtml(k)}</div><div class='mono'>${escapeHtml(String(v || "-"))}</div></div>`;

      parts.push(kv("Platform", labelByProjectKey[ev.p] || ev.p));
      parts.push(kv("Environment", ev.e));
      parts.push(kv("Component", ev.c));
      parts.push(kv("From", ev.f));
      parts.push(kv("To", ev.t));
      parts.push(kv("By", ev.by));
      parts.push(kv("When", ev.at ? fmtDate(ev.at) : "-"));

      const baseLinks = [];
      if (ev.commitUrl) baseLinks.push({ url: ev.commitUrl, label: 'Commit' });
      if (ev.kustomizationUrl) baseLinks.push({ url: ev.kustomizationUrl, label: 'Kustomization' });
      if (Array.isArray(ev.links)) {
        for (const l of ev.links) {
          if (l?.url) baseLinks.push({ url: l.url, label: (l.label || l.type || 'Link') });
        }
      }
      const linksHtml = renderSourceLinks(baseLinks);
      if (linksHtml) {
        parts.push(`<div class='historyKV'><div class='muted'>Links</div><div class='historyDrawerLinks'>${linksHtml}</div></div>`);
      }

      if (ev.warnings && Array.isArray(ev.warnings) && ev.warnings.length) {
        const w = ev.warnings.map((w) => {
          const code = w?.code ? String(w.code) : "";
          const msg = w?.message ? String(w.message) : "Warning";
          const text = code ? `${code}: ${msg}` : msg;
          return `<div class='warnRow'><span class='warnDot'></span><span>${escapeHtml(text)}</span></div>`;
        }).join("");
        parts.push(`<div class='historyKV'><div class='muted'>Warnings</div><div class='warnList'>${w}</div></div>`);
      }

      miniDrawerBody.innerHTML = parts.join("");
      miniDrawer.classList.remove("hidden");
    };
    
    // Bind handlers to Mini History rows
    document.querySelectorAll(".miniHistoryContainer .historyRow").forEach((row) => {
      row.onclick = null;
      row.addEventListener('click', handleMiniHistoryRowClick);
    });
  }, 100); // Slightly longer delay to ensure DOM is ready

  // ---------- Parameters & logs (Datadog) - removed in open-source version ----------
  const paramsHtml = "";
  const section4 = ""; // Parameters & logs (Datadog) removed in open-source version

  // ---------- Integrations status panel ----------
  const integrations = appData?.integrations || {};
  const integrationList = [
    {
      key: "teamcity",
      name: "TeamCity",
      data: integrations.teamcity || {},
    },
    {
      key: "github",
      name: "GitHub",
      data: integrations.github || {},
    },
    {
      key: "jira",
      name: "Jira",
      data: integrations.jira || {},
    },
  ];

  const integrationsHtml = integrationList.map((int) => {
    const d = int.data;
    const enabled = !!d.enabled;
    // Snapshot is source of truth for connection status
    const snapshotConnected = !!d.connected;
    const snapshotReason = d.reason || "";
    const lastFetch = d.lastFetch || null;
    const coverage = d.coverage || {};

    let statusClass = "muted";
    let statusText = "Disabled";
    if (enabled) {
      // Prioritize snapshot status (source of truth)
      if (snapshotConnected) {
        statusClass = "ok";
        statusText = "Connected";
      } else {
        statusClass = "warn";
        statusText = "Error";
      }
    }

    const coverageText = (() => {
      if (int.key === "teamcity") {
        const comps = coverage.components || 0;
        return comps > 0 ? `${comps} components` : "No data";
      } else if (int.key === "github" || int.key === "jira") {
        const tickets = coverage.tickets || 0;
        const windowDays = coverage.windowDays || null;
        if (tickets > 0) {
          return windowDays ? `${tickets} tickets (last ${windowDays} days)` : `${tickets} tickets`;
        }
        return windowDays ? `0 tickets (last ${windowDays} days)` : "No data";
      }
      return "No data";
    })();

    // Build status details: snapshot first, then live check if available
    let statusDetails = [];
    statusDetails.push(`<span class="muted">Status:</span> ${escapeHtml(snapshotConnected ? "Connected" : "Error")}`);
    if (snapshotReason && snapshotReason !== "ok") {
      statusDetails.push(`<span class="muted">(${escapeHtml(snapshotReason)})</span>`);
    }
    
    // Show live check status separately (if checked) without overriding snapshot
    return `
      <div class="integrationItem">
        <div class="integrationHeader">
          <div class="integrationName">${escapeHtml(int.name)}</div>
          <span class="statusBadge ${statusClass}">${escapeHtml(statusText)}</span>
        </div>
        <div class="integrationDetails">
          ${enabled ? `
            <div class="integrationDetail">
              ${statusDetails.join(" ")}
            </div>
            ${lastFetch ? `
              <div class="integrationDetail">
                <span class="muted">Last fetch:</span> ${escapeHtml(fmtDate(lastFetch))}
              </div>
            ` : ""}
            <div class="integrationDetail">
              <span class="muted">Coverage:</span> ${escapeHtml(coverageText)}
            </div>
          ` : `
            <div class="integrationDetail muted">Not configured</div>
          `}
        </div>
      </div>
    `;
  }).join("");

  const sectionIntegrations = `
    <div class="panel">
      <div class="ov2H">Integrations status</div>
      <div class="integrationsList">${integrationsHtml || `<div class="muted">No data</div>`}</div>
    </div>
  `;

  wrap.innerHTML = `
    <div class="ov2Grid">
      <div class="ov2Left">
        <div class="panel">
          <div class="ov2H">Pipeline radar</div>
          ${section1}
        </div>
        ${section3}
        ${sectionIntegrations}
      </div>
      <div class="ov2Right">
        ${section2}
        ${sectionWarnings}
        ${section4}
      </div>
    </div>
  `;

  // bind slice clicks
  wrap.querySelectorAll(".wheelSlice").forEach(pathEl => {
    const handler = () => {
      const pk = pathEl.getAttribute("data-project");
      const envKey = pathEl.getAttribute("data-env");
      if (pk && envKey) {
        // Navigate directly to the specific environment details
        goToEnvDetails(pk, envKey);
      } else if (pk) {
        // Fallback: if no env key, navigate to platform view
        goToProjectEnvs(pk);
      }
    };
    pathEl.addEventListener("click", handler);
    pathEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") handler();
    });
  });

  // ---------- Parameters dropdown bindings ----------
  const closeAllParamMenus = () => {
    wrap.querySelectorAll(".paramEnv.open").forEach((dd) => dd.classList.remove("open"));
    wrap.querySelectorAll(".paramEnvPill[aria-expanded='true']").forEach((b) =>
      b.setAttribute("aria-expanded", "false")
    );
  };

  // toggle menu
  wrap.querySelectorAll("[data-param-pill]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const key = btn.getAttribute("data-param-pill");
      const dd = wrap.querySelector(`.paramEnv[data-env-dd="${CSS.escape(key)}"]`);
      if (!dd) return;

      const isOpen = dd.classList.contains("open");
      closeAllParamMenus();
      if (!isOpen) {
        dd.classList.add("open");
        btn.setAttribute("aria-expanded", "true");
      }
    });
  });

  // choose env option
  wrap.querySelectorAll("[data-param-proj][data-param-env]").forEach((item) => {
    item.addEventListener("click", (e) => {
      e.stopPropagation();
      const pk = item.getAttribute("data-param-proj");
      const env = item.getAttribute("data-param-env");
      setParamEnvFor(pk, env);
      renderOverview(); // rerender updates pill + metrics
    });
  });

  // clicking background closes menu
  wrap.addEventListener("click", closeAllParamMenus);

  } catch (err) {
    console.error("Overview render failed:", err);
    try {
      wrap.innerHTML = `
        <div class="card" style="padding:16px;">
          <div class="row" style="justify-content:space-between; align-items:center;">
            <div>
              <h3 style="margin:0 0 6px 0;">Overview failed to render</h3>
              <div class="muted">Open DevTools console for details. This is usually caused by an unexpected snapshot shape.</div>
            </div>
            <button class="btn" onclick="location.reload()">Reload</button>
          </div>
        </div>
      `;
    } catch {}
  }
}


// --- small pure helpers for overview v2 ---
function cryptoHash(str) {
  // deterministic small hash (no crypto API needed)
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h += (h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24);
  }
  return Math.abs(h >>> 0);
}

function donutSlicePath(cx, cy, rOuter, rInner, a0, a1) {
  const polar = (r, a) => [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  const [x0, y0] = polar(rOuter, a0);
  const [x1, y1] = polar(rOuter, a1);
  const [x2, y2] = polar(rInner, a1);
  const [x3, y3] = polar(rInner, a0);
  const large = (a1 - a0) > Math.PI ? 1 : 0;
  return [
    `M ${x0.toFixed(3)} ${y0.toFixed(3)}`,
    `A ${rOuter} ${rOuter} 0 ${large} 1 ${x1.toFixed(3)} ${y1.toFixed(3)}`,
    `L ${x2.toFixed(3)} ${y2.toFixed(3)}`,
    `A ${rInner} ${rInner} 0 ${large} 0 ${x3.toFixed(3)} ${y3.toFixed(3)}`,
    "Z",
  ].join(" ");
}

function openOverview() {
  // close compare/details and sidebar
  compareSelected = new Set();
  detailsOpenEnvKey = null;
  envLinksOpenKey = null;
  setSidebar(false);

  currentView = "overview";
  currentLeafKey = null;
  expandedLeafKey = null;
  expandedGroupKey = null;

  render();
}


// ---------- Environments (premium cards) ----------
function renderEnvCards() {
  const row = el("envRow");
  if (!row) return;

  const proj = getCurrentProject();
  const envs = getEnvList(proj);

  if (!envs.length) {
    row.innerHTML = `<div class="muted">No data</div>`;
    return;
  }

  row.innerHTML = envs.map((env) => {
    const status = String(env.status || "unknown").toLowerCase();
    const pillClass = status === "healthy" ? "healthy" : status === "warn" ? "warn" : status === "blocked" ? "blocked" : "unknown";

    const sum = deriveEnvSummaryFromRepos(env);
    const last = sum.last ? fmtWhen(sum.last) : "-";
    const by = sum.by || "-";
    const build = sum.build || "-";

    const notes = Array.isArray(env.notes) ? env.notes : [];
    const noteText = normalizeNote(notes[0] ?? env.note ?? env.alert ?? env.message);
    const hasNote = !!noteText;

    const isSelected = compareSelected.has(env.key);
    const cardSelectedClass = isSelected ? "selected" : "";

    // Stage 8.2: merge per-env warnings (if present) + root snapshot warnings scoped to this env
    const rootEnvWarnings = warningsForEnv(proj?.key, env.key);
    const mergedWarnings = ([]).concat(Array.isArray(env.warnings) ? env.warnings : [], rootEnvWarnings);
    const envWarnIcon = renderWarningsIcon(mergedWarnings);

    const projectKey = proj?.key || proj?.name || "";
    const links = getEnvLinksFor(projectKey, env.key);
    const openKey = `${String(projectKey)}:${String(env.key)}`;
    const linksOpen = envLinksOpenKey === openKey;

    // Build links HTML only if config exists for this env
    // IMPORTANT: This must be an INLINE expandable list (accordion-like) - not a floating popup.
    let linksHtml = "";
    if (links) {
      const cmsUrl = links.cms?.url || "";
      const cmsLabel = links.cms?.label || "CMS";

      const fe = links.fe && typeof links.fe === "object" ? links.fe : null;
      const feRows = [];
      if (fe) {
        for (const label of Object.keys(fe)) {
          const url = fe[label];
          if (!url) continue;
          feRows.push(`
            <a class="envLinkRow" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">
              <span class="envLinkLabel">${escapeHtml(label)}</span>
            </a>
          `);
        }
      }

      linksHtml = `
        <div class="envLinks ${linksOpen ? "open" : ""}" data-links="${escapeHtml(openKey)}">
          <div class="envLinksHead">Quick links</div>

          <div class="envLinksSection">
            <div class="envLinksTitle">CMS</div>
            ${cmsUrl ? `
              <a class="envLinkRow" href="${escapeHtml(cmsUrl)}" target="_blank" rel="noopener noreferrer">
                <span class="envLinkLabel">${escapeHtml(cmsLabel)}</span>
              </a>
            ` : `<div class="envLinksEmpty">(no CMS link)</div>`}
          </div>

          ${feRows.length ? `
            <div class="envLinksSection">
              <div class="envLinksTitle">Frontends</div>
              ${feRows.join("")}
            </div>
          ` : ``}
        </div>
      `;
    }

    return `
      <div class="envCard ${cardSelectedClass}" data-env="${escapeHtml(env.key)}">
        <div class="envTop">
          ${links ? `
            <button
              class="envName envNameBtn"
              data-envlinks="${escapeHtml(openKey)}"
              type="button"
              aria-expanded="${linksOpen ? "true" : "false"}"
              title="Show quick links"
            >
              <span class="envNameText">${escapeHtml(env.name || env.key)}</span>
              <span class="envNameCaret">${linksOpen ? "▴" : "▾"}</span>
            </button>
          ` : `
            <div class="envName">${escapeHtml(env.name || env.key)}</div>
          `}

          <div class="pill ${pillClass}">${escapeHtml(String(env.status || "UNKNOWN").toUpperCase())}</div>
        </div>

        <div class="envLines">
          <div class="line oneLine"><b>Last deploy:</b> ${last}</div>
          <div class="line oneLine"><b>By:</b> ${escapeHtml(by)}</div>
          <div class="line oneLine"><b>Build:</b> ${escapeHtml(build)}</div>
        </div>

        ${linksHtml}

${hasNote ? `
  <div class="note">
    <span class="dot"></span>
    ${env.sourceUrl ? `
      <a class="noteLink" href="${escapeHtml(env.sourceUrl)}" target="_blank" rel="noopener noreferrer">
        ${escapeHtml(noteText)}
      </a>
    ` : `
      <div>${escapeHtml(noteText)}</div>
    `}
  </div>
        ` : ""}

        <div class="envBottom">
          <button class="linkBtn" data-details="${escapeHtml(env.key)}" type="button">Click to view details</button>
          <button class="compareBtn ${isSelected ? "on" : ""}" data-compare="${escapeHtml(env.key)}" type="button">
            ${isSelected ? "Selected" : "Compare"}
          </button>
        </div>
      </div>
    `;
  }).join("");

  // Env name -> toggle links (MVP0.5)
  row.querySelectorAll('button[data-envlinks]').forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const k = btn.getAttribute("data-envlinks");
      envLinksOpenKey = (envLinksOpenKey === k) ? null : k;
      renderEnvCards(); // re-render only env cards (safe + minimal)
    });
  });

  // Prevent card-level click-through when clicking real links
  row.querySelectorAll(".envLinks a").forEach((a) => {
    a.addEventListener("click", (ev) => {
      ev.stopPropagation();
    });
  });

  // Details toggle
  row.querySelectorAll("button[data-details]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const k = btn.getAttribute("data-details");
      if (!k) return;

      // Exit compare mode if active
      if (compareSelected.size >= 2) {
        compareSelected = new Set();
        comparePair = null;
      }

      detailsOpenEnvKey = (detailsOpenEnvKey === k) ? null : k;
      updateUrlHash(); // Update URL when opening/closing details
      render();
      // Make the details panel visible immediately (prevents "it did nothing" feeling)
      requestAnimationFrame(() => {
        const d = el("details");
        if (d && d.innerHTML.trim()) d.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
  });

  // Compare toggle
  row.querySelectorAll("button[data-compare]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const k = btn.getAttribute("data-compare");
      toggleCompare(k);
    });
  });

  // Card click (enterprise UX): open env details by clicking anywhere on the card
  row.querySelectorAll(".envCard").forEach((card) => {
    card.addEventListener("click", () => {
      const k = card.getAttribute("data-env");
      if (!k) return;

      // Exit compare mode if active
      if (compareSelected.size >= 2) {
        compareSelected = new Set();
        comparePair = null;
      }

      detailsOpenEnvKey = (detailsOpenEnvKey === k) ? null : k;
      render();
      // Make the details panel visible immediately (prevents "it did nothing" feeling)
      requestAnimationFrame(() => {
        const d = el("details");
        if (d && d.innerHTML.trim()) d.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
  });

}

function syncComparePair(proj, set) {
  const ordered = sortSelectedByEnvOrder(proj, set);
  if (ordered.length < 2) {
    comparePair = null;
    return;
  }

  // if current pair is invalid or not fully selected, pick first two
  if (!comparePair || !set.has(comparePair.aKey) || !set.has(comparePair.bKey) || comparePair.aKey === comparePair.bKey) {
    comparePair = { aKey: ordered[0], bKey: ordered[1] };
    return;
  }

  // keep pair in env order for stable UX
  const pairOrdered = sortSelectedByEnvOrder(proj, [comparePair.aKey, comparePair.bKey]);
  comparePair = { aKey: pairOrdered[0], bKey: pairOrdered[1] };
}

function toggleCompare(envKey) {
  const proj = getCurrentProject();
  const set = new Set(compareSelected);

  if (set.has(envKey)) {
    set.delete(envKey);
  } else {
    // allow selecting up to MAX_COMPARE_SELECT envs
    if (set.size >= MAX_COMPARE_SELECT) {
      // remove first in env-order to keep UX stable
      const ordered = sortSelectedByEnvOrder(proj, set);
      set.delete(ordered[0]);
    }
    set.add(envKey);
  }

  // keep state ordered logically
  const orderedNow = sortSelectedByEnvOrder(proj, set);
  compareSelected = new Set(orderedNow);
  // entering compare mode should close env details (clean UX)
  if (compareSelected.size >= 2) detailsOpenEnvKey = null;

  render();
}

// ---------- Details panel ----------

function renderEnvDetails() {
  const host = el("details");
  if (!host) return;

  const proj = getCurrentProject();
  const envs = getEnvList(proj);

  if (!detailsOpenEnvKey) {
    host.innerHTML = "";
    return;
  }

  const env = envs.find((e) => String(e.key) === String(detailsOpenEnvKey));
  if (!env) {
    host.innerHTML = "";
    return;
  }

  const repos = getEnvRepos(env);

  // Env-level timestamp (best-effort): prefer env.lastDeploy, otherwise derive from components.
  const envLastDeploy = pickBestDate(
    env.lastDeploy,
    env.lastDeployed,
    env.deployedAt,
    env.updatedAt,
    ...repos.map((r) => r?.deployedAt || r?.buildFinishedAt || r?.updatedAt)
  );

  // Snapshot timestamp (when this data was observed)
  const snapshotGeneratedAt = proj?.generatedAt || appData?.generatedAt || "";

  host.innerHTML = `
    <div class="panel">
      <div class="panelTop">
        <div>
          <h3 class="panelTitle">${escapeHtml(env.name || env.key)}</h3>
          <div class="panelMeta">
            <b>Status:</b> ${escapeHtml(String(env.status || "-"))} ${renderWarningsIcon(env.warnings)}
            &nbsp;•&nbsp;
            <b>Build:</b> ${escapeHtml(env.build || env.version || "-")}
            ${envLastDeploy ? `&nbsp;•&nbsp;<b>Last deployed:</b> <span title="${escapeAttr(fmtDate(envLastDeploy))}">${escapeHtml(fmtAgo(envLastDeploy))}</span> <span class="muted">(${escapeHtml(fmtDate(envLastDeploy))})</span>` : ""}
            ${snapshotGeneratedAt ? `&nbsp;•&nbsp;<b>Snapshot:</b> <span class="muted">${escapeHtml(fmtDate(snapshotGeneratedAt))}</span>` : ""}
          </div>
        </div>
        <button class="btn" id="btnCloseDetails" type="button">Close</button>
      </div>

      ${repos.length ? `
        <table class="table">
          <thead>
            <tr>
             <th>Repository</th>
<th>Build / Branch / Deployer</th>
</tr>
</thead>
<tbody>
  ${repos.map((r) => {
    const prettyFromTag = (r.tag || r.imageTag || "").match(/^(.*)-v\d+\.\d+\.\d+$/)?.[1];

    const repoLabel =
      prettyFromTag ||
      r.name ||
      r.repo ||
      r.key ||
      (r.image ? r.image.split("/").pop() : "-");

    const deployer = r.deployer || r.deployedBy || r.triggeredBy || "";

    const warnIcon = renderWarningsIcon(r.warnings);

    const repoHtml = r.repoUrl
      ? `<a class="cellLink" href="${escapeAttr(r.repoUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(repoLabel)}</a>${warnIcon}`
      : `<b>${escapeHtml(repoLabel)}</b>${warnIcon}`;

    const buildText = (r.build || "").trim() || extractBuildFromTag(r.tag || r.imageTag || "") || "-";
    const buildHtml = r.buildUrl
      ? `<a class="cellLink" href="${escapeAttr(r.buildUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(buildText)}</a>`
      : escapeHtml(buildText);

    const branchText = r.branch ? String(r.branch) : "";
    const branchHtml = branchText
      ? (r.branchUrl
          ? `<a class="cellLink" href="${escapeAttr(r.branchUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(branchText)}</a>`
          : escapeHtml(branchText))
      : "";

    const infraHtml = r.kustomizationUrl
      ? `<a class="cellLink muted" href="${escapeAttr(r.kustomizationUrl)}" target="_blank" rel="noopener noreferrer">infra</a>`
      : "";

    // Traceability (best-effort; some fields may be missing depending on integration coverage)
    const commitUrl = String(
      r.commitUrl ||
      r.commitURL ||
      r.deployedCommitUrl ||
      r.deployedCommitURL ||
      r.deployerCommitUrl ||
      r.deployerCommitURL ||
      ""
    );
    const commitHtml = commitUrl
      ? `<a class="cellLink muted" href="${escapeAttr(commitUrl)}" target="_blank" rel="noopener noreferrer">commit</a>`
      : "";

    const rightParts = [];
    rightParts.push(`<span class="srcMain">${buildHtml}</span>`);
    if (branchHtml) rightParts.push(branchHtml);

    // Date (best-effort) - show clear timestamp: relative time + absolute time
    const rowWhen = pickBestDate(r.deployedAt, r.buildFinishedAt, r.updatedAt);
    if (rowWhen) {
      const absTime = fmtDate(rowWhen);
      const relTime = fmtAgo(rowWhen);
      rightParts.push(`<span title="${escapeAttr(absTime)}">${escapeHtml(relTime)}</span> <span class="muted">(${escapeHtml(absTime)})</span>`);
    }

    if (deployer) rightParts.push(`<span class="muted">by</span> ${escapeHtml(deployer)}`);

    // Source block (traceability)
    const sources = [];
    if (commitHtml) sources.push(commitHtml.replace('>commit<', '>commit<'));
    if (infraHtml) sources.push(infraHtml.replace('>infra<', '>infra<'));
    const srcBlock = sources.length ? `<span class="srcBlock">${sources.join(" ")}</span>` : "";
    if (srcBlock) rightParts.push(srcBlock);

    const rightColHtml = rightParts.join(" <span class=\"cvSep\">•</span> ");

    return `
      <tr class="envDetailRow" data-env-repo="${escapeAttr(JSON.stringify({
        repo: repoLabel,
        repoUrl: r.repoUrl || "",
        build: buildText,
        buildUrl: r.buildUrl || "",
        branch: branchText,
        branchUrl: r.branchUrl || "",
        tag: r.tag || r.imageTag || "",
        image: r.image || "",
        deployer: deployer,
        deployedAt: r.deployedAt || "",
        buildFinishedAt: r.buildFinishedAt || "",
        updatedAt: r.updatedAt || "",
        commitUrl: commitUrl,
        kustomizationUrl: r.kustomizationUrl || "",
        warnings: r.warnings || [],
      }))}">
        <td>${repoHtml}</td>
        <td>${rightColHtml}</td>
      </tr>
    `;
  }).join("")}
</tbody>

        </table>
      ` : `
        <div class="muted" style="margin-top:10px;">No data</div>
      `}
    </div>

    <div id="envDetailDrawer" class="historyDrawer hidden">
      <div class="historyDrawerBackdrop" data-envdrawer-close="1"></div>
      <div class="historyDrawerPanel">
        <div class="historyDrawerHeader">
          <div class="historyDrawerTitle">Repository details</div>
          <button type="button" class="chip" data-envdrawer-close="1">Close</button>
        </div>
        <div id="envDetailDrawerBody" class="historyDrawerBody"></div>
      </div>
    </div>
  `;

  const close = el("btnCloseDetails");
  if (close) close.onclick = () => {
    detailsOpenEnvKey = null;
    render();
  };

  // Repository details drawer
  const drawer = el("envDetailDrawer");
  const drawerBody = el("envDetailDrawerBody");
  const closeDrawer = () => {
    if (!drawer) return;
    drawer.classList.add("hidden");
    if (drawerBody) drawerBody.innerHTML = "";
  };
  document.querySelectorAll("[data-envdrawer-close]").forEach((x) => {
    x.onclick = closeDrawer;
  });
  document.querySelectorAll(".envDetailRow").forEach((row) => {
    row.onclick = (e) => {
      // Don't open drawer if clicking on a link
      if (e.target.tagName === "A") return;
      const raw = row.getAttribute("data-env-repo");
      if (!raw || !drawer || !drawerBody) return;
      let repo;
      try { repo = JSON.parse(raw); } catch { return; }

      const parts = [];
      const kv = (k, v) => `<div class='historyKV'><div class='muted'>${escapeHtml(k)}</div><div class='mono'>${escapeHtml(String(v || "-"))}</div></div>`;

      parts.push(kv("Repository", repo.repo));
      if (repo.build) parts.push(kv("Build", repo.build));
      if (repo.branch) parts.push(kv("Branch", repo.branch));
      if (repo.tag) parts.push(kv("Tag", repo.tag));
      if (repo.image) parts.push(kv("Image", repo.image));
      if (repo.deployer) parts.push(kv("Deployer", repo.deployer));
      
      const when = pickBestDate(repo.deployedAt, repo.buildFinishedAt, repo.updatedAt);
      if (when) parts.push(kv("Deployed", fmtDate(when)));

      const baseLinks = [];
      if (repo.repoUrl) baseLinks.push({ url: repo.repoUrl, label: 'Repository' });
      if (repo.buildUrl) baseLinks.push({ url: repo.buildUrl, label: 'Build' });
      if (repo.branchUrl) baseLinks.push({ url: repo.branchUrl, label: 'Branch' });
      if (repo.commitUrl) baseLinks.push({ url: repo.commitUrl, label: 'Commit' });
      if (repo.kustomizationUrl) baseLinks.push({ url: repo.kustomizationUrl, label: 'Kustomization' });
      const linksHtml = renderSourceLinks(baseLinks);
      if (linksHtml) {
        parts.push(`<div class='historyKV'><div class='muted'>Links</div><div class='historyDrawerLinks'>${linksHtml}</div></div>`);
      }

      if (repo.warnings && Array.isArray(repo.warnings) && repo.warnings.length) {
        const w = repo.warnings.map((w) => {
          const code = w?.code ? String(w.code) : "";
          const msg = w?.message ? String(w.message) : "Warning";
          const text = code ? `${code}: ${msg}` : msg;
          return `<div class='warnRow'><span class='warnDot'></span><span>${escapeHtml(text)}</span></div>`;
        }).join("");
        parts.push(`<div class='historyKV'><div class='muted'>Warnings</div><div class='warnList'>${w}</div></div>`);
      }

      drawerBody.innerHTML = parts.join("");
      drawer.classList.remove("hidden");
    };
  });
}

// ---------- Compare ----------
function renderCompareBar() {
  const bar = el("compareBar");
  if (!bar) return;

  const proj = getCurrentProject();
  if (!proj) { bar.innerHTML = ""; return; }

  const envs = getEnvList(proj);
  const ordered = sortSelectedByEnvOrder(proj, compareSelected);

  // Only show compare UI when we have at least 2 selected envs
  if (ordered.length < 2) {
    bar.innerHTML = "";
    return;
  }

  const selectedCount = ordered.length;

  const chipHtml = ordered.map((k) => {
    const env = envs.find((e) => String(e.key) === String(k));
    const label = env?.name || k;
    return `
      <button class="compareChip" type="button" data-chip="${escapeHtml(k)}" title="Remove from compare">
        <span class="compareChipLabel">${escapeHtml(label)}</span>
        <span class="compareChipX">×</span>
      </button>
    `;
  }).join("");

  bar.innerHTML = `
    <div class="panel comparePanel">
      <div class="panelTop compareTop">
        <div>
          <h3 class="panelTitle">Compare</h3>
          <div class="panelMeta">
            <div class="compareChips">${chipHtml}</div>
            <div class="compareMetaLine">
              <span class="muted">Selected: ${selectedCount}/4</span>
              <label class="cmpToggle"><input type="checkbox" id="cmpOnlyDiff" ${compareOnlyMismatches ? "checked" : ""} /><span>Only mismatches</span></label>
            </div>
          </div>
        </div>
        <div class="compareActions">
          <button class="btn" id="btnCompareClear" type="button">Clear</button>
        </div>
      </div>
    </div>
  `;

  // remove single env from compare
  bar.querySelectorAll("button[data-chip]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const k = btn.getAttribute("data-chip");
      const set = new Set(compareSelected);
      set.delete(k);
      compareSelected = new Set(sortSelectedByEnvOrder(proj, set));

      render();
    });
  });

  const clear = el("btnCompareClear");
  if (clear) clear.onclick = () => {
    compareSelected = new Set();
    render();
  };

  const only = el("cmpOnlyDiff");
  if (only) only.onchange = () => {
    compareOnlyMismatches = !!only.checked;
    render();
  };

}



function renderCompare() {
  const host = el("compare");
  if (!host) return;

  const proj = getCurrentProject();
  const envs = getEnvList(proj);

  const ordered = sortSelectedByEnvOrder(proj, compareSelected);
  if (ordered.length < 2) {
    host.innerHTML = "";
    return;
  }

  const selectedEnvs = ordered
    .map((k) => envs.find((e) => e.key === k))
    .filter(Boolean);

  if (selectedEnvs.length < 2) {
    host.innerHTML = "";
    return;
  }

  // Build repo maps per env
  // IMPORTANT: must match the same "repoLabel" logic used in env details.
  const nameOf = (r) => {
    if (!r) return "";
    const prettyFromTag = (r.tag || r.imageTag || "").match(/^(.*)-v\d+\.\d+\.\d+$/)?.[1];
    return String(
      prettyFromTag ||
      r.name ||
      r.repo ||
      r.key ||
      (r.image ? String(r.image).split("/").pop() : "")
    ).trim();
  };
  const envRepoMaps = selectedEnvs.map((env) => {
    const repos = getEnvRepos(env);
    return new Map(repos.map((r) => [nameOf(r), r]));
  });

  const allRepoNames = [...new Set(envRepoMaps.flatMap((m) => [...m.keys()]))]
    .filter(Boolean)
    .sort((x, y) => x.localeCompare(y));

  const repoParts = (r) => {
    if (!r) {
      return {
        build: "-",
        branch: "-",
        deployer: "-",
        buildUrl: "",
        branchUrl: "",
        repoUrl: "",
        kustomizationUrl: "",
        commitUrl: "",
        warnings: [],
        _missing: true,
      };
    }

    const build = String((r.build ?? "")).trim() || extractBuildFromTag(r.tag || r.imageTag || "") || "-";
    const branch = r.branch ? String(r.branch) : "-";
    const deployer = String(r.deployer || r.deployedBy || r.triggeredBy || "-");

    const commitUrl = String(
      r.commitUrl ||
      r.commitURL ||
      r.deployedCommitUrl ||
      r.deployedCommitURL ||
      r.deployerCommitUrl ||
      r.deployerCommitURL ||
      ""
    );

    return {
      build,
      branch,
      deployer,
      buildUrl: String(r.buildUrl || ""),
      branchUrl: String(r.branchUrl || ""),
      repoUrl: String(r.repoUrl || ""),
      kustomizationUrl: String(r.kustomizationUrl || ""),
      commitUrl,
      warnings: Array.isArray(r.warnings) ? r.warnings : [],
      _missing: false,
    };
  };

  const partsDiff = (arr, key) => {
    const vals = arr.map(x => x[key]);
    const base = vals[0];
    return vals.some(v => v !== base);
  };

  const rowClass = (vals) => {
    const base = vals[0];
    const ok = vals.every(v => v === base);
    return ok ? "rowMatched" : "rowUnmatched";
  };


  const isRowMatched = (repoName) => {
    const parts = envRepoMaps.map((m) => repoParts(m.get(repoName)));
    const fullVals = parts.map((p) => `${p.build} • ${p.branch}`);
    return rowClass(fullVals) === "rowMatched";
  };

  const visibleRepoNames = compareOnlyMismatches
    ? allRepoNames.filter((rn) => !isRowMatched(rn))
    : allRepoNames;

  
  const totalRepos = allRepoNames.length;
  const mismatchedRepoNames = allRepoNames.filter((rn) => !isRowMatched(rn));
  const mismatchedCount = mismatchedRepoNames.length;

  const missingByEnv = selectedEnvs.map((env, i) => {
    const m = envRepoMaps[i];
    let c = 0;
    for (const rn of allRepoNames) {
      if (!m.has(rn)) {
        const existsElsewhere = envRepoMaps.some((mm, j) => j !== i && mm.has(rn));
        if (existsElsewhere) c++;
      }
    }
    return { name: env.name || env.key, count: c };
  });

  const showingCount = visibleRepoNames.length;

  const compareSummaryHtml = (() => {
    const pills = [];
    pills.push(`<span class="pill infoPill">Repos: <b>${totalRepos}</b></span>`);
    pills.push(`<span class="pill mismatchPill">Mismatches: <b>${mismatchedCount}</b></span>`);
    if (compareOnlyMismatches) pills.push(`<span class="pill softPill">Showing: <b>${showingCount}</b></span>`);
    for (const x of missingByEnv) {
      if (x.count) pills.push(`<span class="pill missingPill">Missing in ${escapeHtml(x.name)}: <b>${x.count}</b></span>`);
    }
    return pills.length ? `<div class="compareSummary">${pills.join("")}</div>` : "";
  })();


  host.innerHTML = `
    <div class="panel">
      <div class="panelTop">
        <div>
          <h3 class="panelTitle">Repositories</h3>
          <div class="panelMeta">
            Comparing <b>${escapeHtml(selectedEnvs.map(e => e.name || e.key).join(" • "))}</b>
          </div>
          ${compareSummaryHtml}
        </div>
      </div>

      ${visibleRepoNames.length ? `
        <div class="compareTableWrap">
          <table class="table">
            <thead>
              <tr>
                <th>Repository</th>
                ${selectedEnvs.map((e, idx) => `<th class="envCol ${idx === 0 ? "envColFirst" : ""}">${escapeHtml(e.name || e.key)}</th>`).join("")}
              </tr>
            </thead>
            <tbody>
              ${visibleRepoNames.map((repoName) => {
                const parts = envRepoMaps.map((m) => repoParts(m.get(repoName)));
                const fullVals = parts.map((p) => `${p.build} • ${p.branch}`);
                const cls = rowClass(fullVals);

                const buildDiff = partsDiff(parts, "build");
                const branchDiff = partsDiff(parts, "branch");

                const linkOrText = (text, url, extraCls) => {
                  const safeText = escapeHtml(text);
                  const safeUrl = (url || "").trim();
                  if (safeUrl) {
                    return `<a class="cellLink ${extraCls || ""}" href="${escapeHtml(safeUrl)}" target="_blank" rel="noopener noreferrer">${safeText}</a>`;
                  }
                  return `<span class="${extraCls || ""}">${safeText}</span>`;
                };

                const cellHtml = (p) => {
                  if (p._missing) return `<span class="cv missing">-</span>`;
                  const bCls = buildDiff ? "diff" : "";
                  const brCls = branchDiff ? "diff" : "";

                  const warn = renderWarningsIcon(p.warnings);

                  // Truncate long branch names visually but keep full value in title tooltip.
                  const fullBranch = String(p.branch || "-");
                  const maxBranchLen = 42;
                  const shortBranch = fullBranch.length > maxBranchLen
                    ? fullBranch.slice(0, maxBranchLen - 1) + "…"
                    : fullBranch;

                  const buildEl = linkOrText(p.build, p.buildUrl, `cv build ${bCls}`);
                  const branchEl = `<span class="cv branch ${brCls}" title="${escapeAttr(fullBranch)}">${escapeHtml(shortBranch)}</span>`;
                  const dep = (p.deployer && p.deployer !== "-") ? `<span class="cvSep">•</span><span class="muted" style="font-size:12px;">by ${escapeHtml(p.deployer)}</span>` : "";
                  const infra = p.kustomizationUrl ? `<span class="cvSep">•</span><a class="cellLink" href="${escapeHtml(p.kustomizationUrl)}" target="_blank" rel="noopener noreferrer">infra</a>` : "";
                  const commit = p.commitUrl ? `<span class="cvSep">•</span><a class="cellLink" href="${escapeHtml(p.commitUrl)}" target="_blank" rel="noopener noreferrer">commit</a>` : "";

                  return `${warn}${buildEl}<span class="cvSep">•</span>${branchEl}${dep}${commit}${infra}`;
                };

                return `
                  <tr class="${cls}">
                    <td>
                      <div class="repoCell">
                        ${(() => {
                          const anyRepoUrl = parts.map(p => p.repoUrl).find(u => (u || "").trim());
                          if (anyRepoUrl) {
                            return `<a class="cellLink" href="${escapeHtml(anyRepoUrl)}" target="_blank" rel="noopener noreferrer"><b>${escapeHtml(repoName)}</b></a>`;
                          }
                          return `<b>${escapeHtml(repoName)}</b>`;
                        })()}
                        <span class="matchPill ${cls === "rowMatched" ? "match" : "mismatch"}">
                          ${cls === "rowMatched" ? "MATCHED" : "MISMATCH"}
                        </span>
                      </div>
                    </td>
                    ${parts.map((p, idx) => `<td class="compareCell envCol ${idx === 0 ? "envColFirst" : ""}">${cellHtml(p)}</td>`).join("")}
                  </tr>
                `;
              }).join("")}
            </tbody>
          </table>
        </div>
      ` : `
        <div class="muted" style="margin-top:10px;">No data</div>
      `}
    </div>
  `;
}


// ---------- Runbooks (read-only checks, placeholder framework) ----------
let runbookAbortController = null;
let runbookPreservedHtml = null; // preserve result pane across re-renders so output is not wiped

function renderReleases() {
  const wrap = el("releasesContent");
  if (!wrap) return;

  // Preserve active result pane before overwriting (avoids "runs then turns off" when re-render happens)
  runbookPreservedHtml = null;
  if (runbooksSelectedRunbook) {
    const paneId = runbooksSelectedRunbook === "scope" ? "runbookScopeBody" : runbooksSelectedRunbook === "drift" ? "runbookDriftBody" : "runbookReleaseDiffBody";
    const pane = el(paneId);
    const html = pane && pane.innerHTML ? pane.innerHTML.trim() : "";
    if (html && !/Running\s+(scope|drift|release diff)/i.test(html)) runbookPreservedHtml = html;
  }

  const projects = Array.isArray(appData.projects) ? appData.projects : [];
  const proj = getCurrentProject();
  const projKey = proj ? (proj.key || proj.name || "") : "";
  // Sync ticket prefixes from project when project changes (incl. first load)
  if (projKey && runbooksSelectedProjectKey !== projKey) {
    runbooksSelectedProjectKey = projKey;
    runbooksTicketPrefixes = Array.isArray(proj?.ticketPrefixes) && proj.ticketPrefixes.length ? [...proj.ticketPrefixes] : [""];
  }
  const envs = proj ? getEnvList(proj) : [];
  const allRepos = envs.flatMap((e) => getEnvRepos(e));
  const repoNames = [...new Set(allRepos.map((r) => r.repo || r.name || "").filter(Boolean))].sort();

  const runbookDescriptions = {
    scope: {
      title: "Scope checker",
      short: "Commits and tickets between baseline and default branch.",
      what: "Lists commits and ticket IDs that appear between the selected release baseline and the default branch, for each repository. Use it to see what changed since the release was cut.",
      tip: "If a branch is not found in a repository, the result will show a warning and that repo will have 0 commits - fix the baseline or head ref, or ensure the branch exists in that repo.",
    },
    drift: {
      title: "Drift / back-merge checker",
      short: "Release branch vs default - hotfixes not back-merged.",
      what: "Checks if the release branch has commits (or tickets) that are not yet on the default branch - e.g. hotfixes merged to release but not back-merged to main. Shows what might need to be merged down.",
      tip: "Repos with drift are listed with commit counts and tickets; use this to decide what to back-merge.",
    },
    "release-diff": {
      title: "Release diff (older → newer)",
      short: "Older → newer: +commits, +tickets. Newer vs older release.",
      what: "Compares two release branches (e.g. release/0.21.0 vs release/0.23.0) per repository. Shows added commits (in Y, not in X), removed commits (in X, not in Y), and extracted tickets and PR refs. Use it for quick release notes or to see what changed between versions.",
      tip: "Both refs must exist in each repo. Use the release notes raw block to copy-paste a summary.",
    },
  };

  const runbooks = [
    { id: "scope", label: "Scope checker", desc: runbookDescriptions.scope.short },
    { id: "drift", label: "Drift / back-merge", desc: runbookDescriptions.drift.short },
    { id: "release-diff", label: "Release diff", desc: runbookDescriptions["release-diff"].short },
  ];

  const selectedDesc = runbooksSelectedRunbook ? runbookDescriptions[runbooksSelectedRunbook] : null;

  wrap.innerHTML = `
    <div class="relLayout">
      <aside class="relSidebar">
        <nav class="relNav" aria-label="Runbooks">
          ${runbooks.map((rb) => {
            const active = runbooksSelectedRunbook === rb.id;
            return `<button class="relNavItem ${active ? "active" : ""}" type="button" data-runbook="${escapeAttr(rb.id)}" title="${escapeAttr(rb.desc)}">${escapeHtml(rb.label)}</button>`;
          }).join("")}
        </nav>
      </aside>
      <div class="relMain">
        ${runbooksSelectedRunbook ? `
          <div class="relInstructionBlock" role="region" aria-label="Runbook instructions">
            <h2 class="relInstructionTitle">What this runbook does</h2>
            <p class="relInstructionWhat">${selectedDesc ? escapeHtml(selectedDesc.what) : ""}</p>
            <p class="relInstructionTip muted">${selectedDesc ? escapeHtml(selectedDesc.tip) : ""}</p>
          </div>
          <div class="relConfigBlock">
            <h2 class="relConfigTitle">Configuration</h2>
            <div class="relConfigGrid">
              <div class="relConfigRow">
                <label class="relConfigLabel" for="relPlatformDropdown">Platforms</label>
                <select id="relPlatformDropdown" class="relConfigSelect relPlatformSelect" aria-label="Select platform">
                  <option value="">Select platform…</option>
                  ${projects.map((p) => {
                    const key = p.key || p.name || "";
                    const label = p.name || p.key || "Platform";
                    const sel = key === projKey ? " selected" : "";
                    return `<option value="${escapeAttr(key)}"${sel}>${escapeHtml(label)}</option>`;
                  }).join("")}
                </select>
              </div>
              <div class="relConfigRow">
                <label class="relConfigLabel">Repositories</label>
                <div class="relConfigRepos" id="relConfigRepos">
                  ${repoNames.length ? repoNames.map((repo) => `<label class="relConfigCheck"><input type="checkbox" checked data-repo="${escapeAttr(repo)}"> ${escapeHtml(repo)}</label>`).join("") : `<span class="muted">${projKey ? "No repositories in this platform." : "Select a platform to list repositories."}</span>`}
                </div>
              </div>
              ${runbooksSelectedRunbook === "scope" ? `
              <div class="relConfigRow">
                <span class="relConfigLabel">Baseline</span>
                <div class="relScopeModeRadios">
                  <label class="relConfigCheck"><input type="radio" name="scopeBaselineMode" value="specific" id="relScopeModeSpecific" ${runbooksScopeBaselineMode === "specific" ? "checked" : ""}> Specific branch</label>
                  <label class="relConfigCheck"><input type="radio" name="scopeBaselineMode" value="prefix" id="relScopeModePrefix" ${runbooksScopeBaselineMode === "prefix" ? "checked" : ""}> Latest by prefix</label>
                </div>
              </div>
              ` : ""}
              ${(runbooksSelectedRunbook !== "scope" || runbooksScopeBaselineMode === "specific") && runbooksSelectedRunbook !== "release-diff" ? `
              <div class="relConfigRow">
                <label class="relConfigLabel" for="relConfigBaseline">Baseline / release ref</label>
                <input type="text" id="relConfigBaseline" class="relConfigInput" placeholder="e.g. release/1.0" value="release/1.0" aria-label="Baseline ref">
                <div class="relInputHint muted">Branch or tag to compare against.</div>
              </div>
              ` : ""}
              ${runbooksSelectedRunbook === "scope" && runbooksScopeBaselineMode === "prefix" ? `
              <div class="relConfigRow">
                <label class="relConfigLabel" for="relLatestBranchesPrefix">Branch prefix</label>
                <input type="text" id="relLatestBranchesPrefix" class="relConfigInput" placeholder="e.g. release" value="release" aria-label="Branch prefix" style="max-width:160px;">
                <div class="relInputHint muted">Prefix to find latest branches. Use Fetch to load them.</div>
                <button type="button" class="btn" id="btnFetchLatestBranches">Fetch latest branches</button>
              </div>
              <div id="runbookLatestBranches" class="relLatestBranchesResult" style="margin-top:6px;"></div>
              ` : ""}
              ${runbooksSelectedRunbook === "release-diff" ? `
              <div class="relConfigRow">
                <label class="relConfigLabel" for="relConfigReleaseX">Newer release</label>
                <input type="text" id="relConfigReleaseX" class="relConfigInput" placeholder="e.g. release/0.23.0" value="" aria-label="Newer release">
                <div class="relInputHint muted">Branch or tag of the newer release. We compare older → newer (+commits, +tickets).</div>
              </div>
              <div class="relConfigRow">
                <label class="relConfigLabel" for="relConfigReleaseY">Older release</label>
                <input type="text" id="relConfigReleaseY" class="relConfigInput" placeholder="e.g. release/0.21.0" value="" aria-label="Older release">
                <div class="relInputHint muted">Branch or tag of the older release.</div>
              </div>
              ` : ""}
              ${runbooksSelectedRunbook !== "release-diff" ? `
              <div class="relConfigRow">
                <label class="relConfigLabel" for="relConfigHead">Default / head ref</label>
                <input type="text" id="relConfigHead" class="relConfigInput" placeholder="e.g. main" value="main" aria-label="Head ref">
                <div class="relInputHint muted">Default branch to compare against (e.g. main, develop).</div>
              </div>
              ` : ""}
              <div class="relConfigRow">
                <label class="relConfigLabel">Ticket prefixes</label>
                <div id="relConfigTicketPrefixes" class="relConfigPrefixList">
                  ${(runbooksTicketPrefixes.length ? runbooksTicketPrefixes : [""]).map((p, i) => `
                    <div class="relConfigPrefixItem" data-prefix-index="${i}">
                      <input type="text" class="relConfigInput relConfigInputSmall" placeholder="e.g. PROJ" value="${escapeHtml(String(p || ""))}" data-prefix-value aria-label="Ticket prefix ${i + 1}">
                      <button type="button" class="btn ghost relConfigPrefixRemove" data-remove-prefix="${i}" title="Remove" ${(runbooksTicketPrefixes.length || 1) <= 1 ? "disabled" : ""}>×</button>
                    </div>
                  `).join("")}
                </div>
                <button type="button" class="btn ghost" id="relConfigAddPrefix" style="margin-top:6px; font-size:12px;">+ Add prefix</button>
                <div class="relInputHint muted">Case-insensitive. Empty = match all. Loaded from project config when available.</div>
              </div>
            </div>
          </div>
          <div class="relRunBlock">
            <button class="btn primary" type="button" id="btnRunCurrentRunbook">Run check</button>
            <button class="btn" type="button" id="btnRunCancel" style="display:none">Cancel</button>
            <div class="muted relHint">Read-only. No branches or PRs are created.</div>
          </div>
          <div class="relResultsBlock" id="relResultsBlock">
            <div id="runbookScopeBody" class="relResultPane" style="display:${runbooksSelectedRunbook === "scope" ? "block" : "none"}"></div>
            <div id="runbookDriftBody" class="relResultPane" style="display:${runbooksSelectedRunbook === "drift" ? "block" : "none"}"></div>
            <div id="runbookReleaseDiffBody" class="relResultPane" style="display:${runbooksSelectedRunbook === "release-diff" ? "block" : "none"}"></div>
          </div>
        ` : `
          <div class="relEmpty">
            <p class="relEmptyTitle">Select a runbook</p>
            <p class="relEmptyDesc muted">Choose a runbook from the list on the left. Then set platform, repositories, branches and ticket prefix, and run the check.</p>
          </div>
        `}
      </div>
    </div>
  `;

  // Bind platform dropdown (in config; do not clear selected runbook on change)
  const relDropdown = el("relPlatformDropdown");
  if (relDropdown) {
    relDropdown.addEventListener("change", () => {
      runbooksSelectedProjectKey = relDropdown.value || null;
      const proj = runbooksSelectedProjectKey ? (projects.find(p => (p.key || p.name) === runbooksSelectedProjectKey) || null) : null;
      runbooksTicketPrefixes = Array.isArray(proj?.ticketPrefixes) && proj.ticketPrefixes.length ? [...proj.ticketPrefixes] : [""];
      render();
    });
  }

  // Bind Add/Remove prefix buttons (delegated)
  if (!wrap.dataset.prefixDelegated) {
    wrap.dataset.prefixDelegated = "1";
    wrap.addEventListener("click", (e) => {
      const addBtn = e.target.closest("#relConfigAddPrefix");
      if (addBtn) {
        const prefixContainer = el("relConfigTicketPrefixes");
        if (prefixContainer) {
          const inputs = prefixContainer.querySelectorAll("[data-prefix-value]");
          const values = Array.from(inputs).map((i) => (i.value || "").trim());
          runbooksTicketPrefixes = values.length ? [...values, ""] : [""];
          render();
        }
        return;
      }
      const remBtn = e.target.closest(".relConfigPrefixRemove");
      if (remBtn && !remBtn.disabled) {
        const idx = parseInt(remBtn.getAttribute("data-remove-prefix"), 10);
        const prefixContainer = el("relConfigTicketPrefixes");
        if (prefixContainer && !isNaN(idx)) {
          const inputs = prefixContainer.querySelectorAll("[data-prefix-value]");
          const values = Array.from(inputs).map((i) => (i.value || "").trim()).filter((_, i) => i !== idx);
          runbooksTicketPrefixes = values.length ? values : [""];
          render();
        }
        return;
      }
    });
  }

  // Scope baseline mode: specific branch vs latest by prefix
  const scopeModeSpecific = el("relScopeModeSpecific");
  const scopeModePrefix = el("relScopeModePrefix");
  if (scopeModeSpecific) scopeModeSpecific.addEventListener("change", () => { runbooksScopeBaselineMode = "specific"; render(); });
  if (scopeModePrefix) scopeModePrefix.addEventListener("change", () => { runbooksScopeBaselineMode = "prefix"; render(); });

  // Bind runbook list
  wrap.querySelectorAll(".relNavItem").forEach((btn) => {
    btn.addEventListener("click", () => {
      runbooksSelectedRunbook = btn.getAttribute("data-runbook");
      render();
    });
  });

  // Bind Run and Cancel via delegation (wrap survives re-renders; buttons are recreated)
  if (!wrap.dataset.runDelegated) {
    wrap.dataset.runDelegated = "1";
    wrap.addEventListener("click", (e) => {
      const runBtn = e.target.closest("#btnRunCurrentRunbook");
      if (runBtn && runbooksSelectedRunbook) {
        if (runbooksSelectedRunbook === "scope") runScopeRunbook();
        else if (runbooksSelectedRunbook === "drift") runDriftRunbook();
        else if (runbooksSelectedRunbook === "release-diff") runReleaseDiffRunbook();
        return;
      }
      const cancelBtn = e.target.closest("#btnRunCancel");
      if (cancelBtn && runbookAbortController) {
        runbookAbortController.abort();
        cancelBtn.style.display = "none";
      }
    });
  }

  const btnFetchLatest = el("btnFetchLatestBranches");
  if (btnFetchLatest && runbooksSelectedRunbook === "scope") {
    btnFetchLatest.onclick = () => fetchLatestBranches();
  }

  if (runbooksSelectedRunbook) {
    const scopePane = el("runbookScopeBody");
    const driftPane = el("runbookDriftBody");
    const diffPane = el("runbookReleaseDiffBody");
    if (scopePane) scopePane.style.display = runbooksSelectedRunbook === "scope" ? "block" : "none";
    if (driftPane) driftPane.style.display = runbooksSelectedRunbook === "drift" ? "block" : "none";
    if (diffPane) diffPane.style.display = runbooksSelectedRunbook === "release-diff" ? "block" : "none";
    const activePane = runbooksSelectedRunbook === "scope" ? scopePane : runbooksSelectedRunbook === "drift" ? driftPane : diffPane;
    if (runbookPreservedHtml && activePane) {
      activePane.innerHTML = runbookPreservedHtml;
      runbookPreservedHtml = null;
    } else {
      if (scopePane && !scopePane.innerHTML.trim()) scopePane.innerHTML = `<div class="muted" style="font-size:12px;">Run the check to see scope between baseline and default branch.</div>`;
      if (driftPane && !driftPane.innerHTML.trim()) driftPane.innerHTML = `<div class="muted" style="font-size:12px;">Run the check to see drift between release and default branch.</div>`;
      if (diffPane && !diffPane.innerHTML.trim()) diffPane.innerHTML = `<div class="muted" style="font-size:12px;">Run the check to see +commits and +tickets (older → newer).</div>`;
    }
  }
}


// --- Runbooks client helpers ---

function getCurrentProjectKey() {
  const proj = getCurrentProject();
  return proj?.key || proj?.name || currentLeafKey || "";
}

// Store runbook data for modal display
let lastRunbookData = null;
let lastRunbookType = null;

function renderRunbookTable(containerId, title, rows, columns, summaryHtml, warnings, fullData = null, runbookType = null, rawBlock = null, getRowClass = null) {
  const host = el(containerId);
  if (!host) return;

  // Store for modal
  if (fullData) {
    lastRunbookData = fullData;
    lastRunbookType = runbookType;
  }

  if (!rows || !rows.length) {
    host.innerHTML = `<div class="muted" style="font-size:12px;">No data returned.</div>`;
    return;
  }

  const header = columns.map(c => {
    const cellCls = c.cellClass ? ` class="${escapeAttr(c.cellClass)}"` : "";
    return `<th${cellCls}>${escapeHtml(c.label)}</th>`;
  }).join("");
  const strikeStyle = "text-decoration:line-through;opacity:0.65";
  const rowBgStyle = "background:rgba(255,100,100,.08)";
  const TICKETS_TRUNCATE_LEN = 60;
  const body = rows.map(r => {
    const rowClass = typeof getRowClass === "function" ? getRowClass(r) : "";
    const cls = rowClass ? ` class="${escapeAttr(rowClass)}"` : "";
    const isStrike = runbookType === "release-diff" && r.refsAvailable === false;
    const dataRefs = isStrike ? ' data-refs-available="false" title="Branch(es) not found in this repo"' : "";
    const trStyle = isStrike ? ` style="${rowBgStyle}"` : "";
    const tdStyle = isStrike ? ` style="${strikeStyle}"` : "";
    const aStyle = isStrike ? ` style="${strikeStyle}"` : "";
    return `<tr${cls}${dataRefs}${trStyle}>
      ${columns.map(c => {
        let val = String(r[c.key] ?? "");
        const url = c.urlKey && r[c.urlKey];
        const cellCls = c.cellClass ? ` class="${escapeAttr(c.cellClass)}"` : "";
        if (url) return `<td${tdStyle}${cellCls}><a class="cellLink" href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer"${aStyle}>${escapeHtml(val)}</a></td>`;
        if (c.truncateTickets && val.length > TICKETS_TRUNCATE_LEN) {
          const short = val.slice(0, TICKETS_TRUNCATE_LEN) + "…";
          return `<td${tdStyle}${cellCls}><span class="ticketsTruncated" title="${escapeAttr(val)}">${escapeHtml(short)}</span> <button type="button" class="btnLink ticketsViewAll" data-runbook-type="${escapeAttr(runbookType || "")}">View all</button></td>`;
        }
        return `<td${tdStyle}${cellCls}>${escapeHtml(val)}</td>`;
      }).join("")}
    </tr>`;
  }).join("");

  const viewDetailsBtn = fullData ? `
    <button class="btn ghost" type="button" id="btnViewDetails_${containerId}" style="margin-top:8px; font-size:11px;">
      View Detailed Output
    </button>
  ` : "";

  const warningsHtml = Array.isArray(warnings) && warnings.length
    ? `
    <div class="relWarningsBlock" role="alert">
      <div class="relWarningsTitle">Issues / messages</div>
      <ul class="relWarningsList">
        ${warnings.map((w) => {
          const msg = String(w || "").trim();
          const friendly = msg.replace(/baseline\/head ref not fully available/i, "branch or ref not available")
            .replace(/\(baseline=([^,]+), head=([^)]+)\)/i, "- baseline: $1, head: $2. If a branch was not found in this repository, create it or fix the ref name.");
          return `<li>${escapeHtml(friendly || msg)}</li>`;
        }).join("")}
      </ul>
    </div>
    `
    : "";

  host.innerHTML = `
    <div class="panel" style="margin-top:8px;">
      <div class="ov2H">${escapeHtml(title)}</div>
      ${warningsHtml}
      <div style="margin-top:6px; overflow:auto;">
        <table class="table compact runbookTable">
          <thead><tr>${header}</tr></thead>
          <tbody>${body}</tbody>
        </table>
      </div>
      ${summaryHtml || ""}
      ${viewDetailsBtn}
      ${rawBlock || ""}
    </div>
  `;

  // Bind view details button if present
  if (fullData) {
    const btn = el(`btnViewDetails_${containerId}`);
    if (btn) {
      btn.onclick = () => showRunbookModal(runbookType || 'unknown');
    }
  }
  // Bind "View all" buttons for truncated tickets
  host.querySelectorAll(".ticketsViewAll").forEach((b) => {
    b.onclick = () => showRunbookModal(runbookType || 'unknown');
  });
}

// Expose modal functions globally
window.showRunbookModal = showRunbookModal;
window.closeRunbookModal = closeRunbookModal;

function showRunbookModal(type) {
  if (!lastRunbookData) return;

  const modalId = "runbookModal";
  let modal = el(modalId);
  if (!modal) {
    modal = document.createElement("div");
    modal.id = modalId;
    modal.className = "modal";
    modal.style.cssText = "display:none; position:fixed; z-index:1000; left:0; top:0; width:100%; height:100%; background:rgba(0,0,0,0.7);";
    document.body.appendChild(modal);
  }

  let content = "";
  const data = lastRunbookData;

  if (type === "scope") {
    content = formatScopeOutput(data);
  } else if (type === "drift") {
    content = formatDriftOutput(data);
  } else if (type === "release-diff") {
    content = formatReleaseDiffOutput(data);
  } else if (type === "readiness") {
    content = formatReadinessOutput(data);
  } else {
    content = `<pre>${escapeHtml(JSON.stringify(data, null, 2))}</pre>`;
  }

  modal.innerHTML = `
    <div style="background:var(--bg); margin:5% auto; padding:20px; border-radius:8px; max-width:90%; max-height:85vh; overflow:auto; position:relative;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px;">
        <h3 style="margin:0;">Runbook Detailed Output</h3>
        <button class="btn ghost" id="btnCloseModal" style="font-size:18px; padding:4px 12px;">×</button>
      </div>
      <div style="font-family:monospace; font-size:12px; line-height:1.6; white-space:pre-wrap; background:var(--bg2); padding:16px; border-radius:4px; overflow:auto;">
${content}
      </div>
    </div>
  `;
  modal.style.display = "block";
  
  // Close on button click
  const closeBtn = el("btnCloseModal");
  if (closeBtn) closeBtn.onclick = closeRunbookModal;
  
  // Close on backdrop click
  modal.onclick = (e) => {
    if (e.target === modal) closeRunbookModal();
  };
}

function closeRunbookModal() {
  const modal = el("runbookModal");
  if (modal) modal.style.display = "none";
}

function formatScopeOutput(data) {
  const repos = data.repos || [];
  const summary = data.summary || {};
  const warnings = data.warnings || [];

  let out = "────────────────────────────────────────────────────────────────────────\n";
  out += "Scope between Release & Default Branch\n";
  out += "────────────────────────────────────────────────────────────────────────\n\n";

  out += "• Collecting latest release branches...\n";
  for (const repo of repos) {
    const baseline = repo.baselineRef || "-";
    const head = repo.headRef || "main";
    const status = repo.baselineExists && repo.headExists ? "✓" : "✗";
    out += `   ${status} ${repo.repo.padEnd(35)} → baseline: ${baseline.padEnd(20)} head: ${head}\n`;
  }
  out += "\n";

  out += "────────────────────────────────────────────────────────────────────────\n";
  out += "Repositories Scan\n";
  out += "────────────────────────────────────────────────────────────────────────\n";
  for (const repo of repos) {
    out += `\n▶ ${repo.repo}\n`;
    if (!repo.baselineExists || !repo.headExists) {
      out += `   WARN: Baseline/head ref not fully available (baseline=${repo.baselineRef || '-'}, head=${repo.headRef || '-'})\n`;
      continue;
    }
    const tickets = repo.tickets || [];
    const commitCount = repo.commitCount || 0;
    if (tickets.length > 0 || commitCount > 0) {
      out += `   Tickets: ${tickets.join(", ")}\n`;
      out += `   Total commits: ${commitCount}\n`;
    } else {
      out += `   OK: No new commits detected.\n`;
    }
  }

  out += "\n────────────────────────────────────────────────────────────────────────\n";
  out += "Summary\n";
  out += "────────────────────────────────────────────────────────────────────────\n";
  const uniqueTickets = summary.uniqueTickets || [];
  out += `Unique tickets: ${uniqueTickets.length}\n`;
  out += `Total commits: ${summary.totalCommits || 0}\n`;

  if (warnings.length > 0) {
    out += "\nWarnings:\n";
    for (const w of warnings) {
      out += `   • ${w}\n`;
    }
  }

  return out;
}

function formatDriftOutput(data) {
  const repos = data.repos || [];
  const summary = data.summary || {};
  const warnings = data.warnings || [];

  let out = "────────────────────────────────────────────────────────────────────────\n";
  out += "Drift / Back-merge Checker\n";
  out += "────────────────────────────────────────────────────────────────────────\n\n";

  out += "• Checking for commits in release branches not present in default branch...\n\n";

  out += "────────────────────────────────────────────────────────────────────────\n";
  out += "Repositories Scan\n";
  out += "────────────────────────────────────────────────────────────────────────\n";
  for (const repo of repos) {
    out += `\n▶ ${repo.repo}\n`;
    if (!repo.releaseExists || !repo.mainExists) {
      out += `   ⚠️  Release/main ref not fully available (release=${repo.releaseRef || '-'}, main=${repo.mainRef || '-'})\n`;
      continue;
    }
    const hasDrift = repo.hasDrift || false;
    const tickets = repo.tickets || [];
    const commitCount = repo.commitCount || 0;
    if (hasDrift) {
      out += `   DRIFT DETECTED: ${commitCount} commits in release not in main\n`;
      if (tickets.length > 0) {
        out += `   Tickets on release-only commits: ${tickets.join(", ")}\n`;
      }
    } else {
      out += `   OK: No drift detected.\n`;
    }
  }

  out += "\n────────────────────────────────────────────────────────────────────────\n";
  out += "Summary\n";
  out += "────────────────────────────────────────────────────────────────────────\n";
  const uniqueTickets = summary.uniqueTickets || [];
  out += `Unique tickets on release-only commits: ${uniqueTickets.length}\n`;
  out += `Total drift commits: ${summary.totalDriftCommits || 0}\n`;

  if (warnings.length > 0) {
    out += "\nWarnings:\n";
    for (const w of warnings) {
      out += `   • ${w}\n`;
    }
  }

  return out;
}

function formatReleaseDiffOutput(data) {
  const repos = data.repos || [];
  const summary = data.summary || {};
  const warnings = data.warnings || [];
  const older = data.releaseRefA || "old";
  const newer = data.releaseRefB || "new";

  let out = "────────────────────────────────────────────────────────────────────────\n";
  out += "Release diff (older → newer)\n";
  out += "────────────────────────────────────────────────────────────────────────\n\n";
  out += `Comparing: ${older} → ${newer}\n\n`;

  out += "────────────────────────────────────────────────────────────────────────\n";
  out += "Added (older → newer)\n";
  out += "────────────────────────────────────────────────────────────────────────\n";
  for (const r of repos) {
    const a = r.added || {};
    const n = a.commitCount ?? 0;
    const t = (a.tickets || []).join(", ") || "-";
    const p = (a.prs || []).join(", ") || "-";
    out += `  ${r.repo}: +${n} commits | tickets: ${t} | PRs: ${p}\n`;
  }

  out += "\n────────────────────────────────────────────────────────────────────────\n";
  out += "Summary\n";
  out += "────────────────────────────────────────────────────────────────────────\n";
  out += `+${summary.totalAdded ?? 0} commits, +${(summary.addedTickets || []).length} tickets\n`;
  out += `Tickets: ${(summary.addedTickets || []).join(", ") || "-"}\n`;

  if (warnings.length > 0) {
    out += "\nWarnings:\n";
    for (const w of warnings) out += `   • ${w}\n`;
  }
  return out;
}

function formatReadinessOutput(data) {
  const repos = data.repos || [];
  const warnings = data.warnings || [];

  let out = "────────────────────────────────────────────────────────────────────────\n";
  out += "Release Readiness / Ref Validator\n";
  out += "────────────────────────────────────────────────────────────────────────\n\n";

  out += "• Validating baseline and head refs for all repos...\n\n";

  out += "────────────────────────────────────────────────────────────────────────\n";
  out += "Repositories Validation\n";
  out += "────────────────────────────────────────────────────────────────────────\n";
  for (const repo of repos) {
    out += `\n▶ ${repo.repo}\n`;
    const status = repo.status || "unknown";
    const baseline = repo.baselineRef || "-";
    const head = repo.headRef || "main";
    const statusIcon = status === "ok" ? "OK" : "WARN";
    out += `   ${statusIcon} Status: ${status.toUpperCase()}\n`;
    out += `   Baseline: ${baseline} ${repo.baselineExists ? "✓" : "✗"}\n`;
    out += `   Head: ${head} ${repo.headExists ? "✓" : "✗"}\n`;
    if (repo.messages && repo.messages.length > 0) {
      for (const msg of repo.messages) {
        out += `   • ${msg}\n`;
      }
    }
  }

  const overallStatus = data.status || "unknown";
  out += "\n────────────────────────────────────────────────────────────────────────\n";
  out += "Overall Status\n";
  out += "────────────────────────────────────────────────────────────────────────\n";
  out += `Status: ${overallStatus.toUpperCase()}\n`;

  if (warnings.length > 0) {
    out += "\nWarnings:\n";
    for (const w of warnings) {
      out += `   • ${w}\n`;
    }
  }

  return out;
}

async function fetchLatestBranches() {
  const projectKey = getCurrentProjectKey();
  const prefixEl = el("relLatestBranchesPrefix");
  const prefix = (prefixEl && prefixEl.value ? prefixEl.value.trim() : "") || "release";
  const host = el("runbookLatestBranches");
  if (!host) return;
  host.innerHTML = `<div class="muted" style="font-size:12px;">Fetching latest branches…</div>`;

  try {
    const data = await safeApiFetch(`${SNAPSHOT_API_BASE}/api/runbooks/latest-branches`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ projectKey, prefix }),
    });
    if (!data || data._error) {
      host.innerHTML = `<div class="relApiError" role="alert"><strong>API Error</strong>: ${escapeHtml(data?.message || "Cannot connect to API server")}.</div>`;
      return;
    }
    if (data.status !== "ok") {
      host.innerHTML = `<div class="muted" style="font-size:12px;">Failed: ${escapeHtml(data?.message || "Unknown error")}</div>`;
      return;
    }
    const repos = data.repos || [];
    const warnings = data.warnings || [];
    const header = "<tr><th>Repository</th><th>Latest branch</th></tr>";
    const body = repos.map((r) => `<tr><td>${escapeHtml(r.repo || "")}</td><td>${escapeHtml(r.branch || "-")}</td></tr>`).join("");
    host.innerHTML = `
      <div class="panel" style="margin-top:10px;">
        <div class="ov2H">Latest branches (prefix: ${escapeHtml(prefix)})</div>
        <div style="margin-top:6px; overflow:auto;">
          <table class="table compact">
            <thead>${header}</thead>
            <tbody>${body}</tbody>
          </table>
        </div>
        ${warnings.length ? `<div class="muted" style="margin-top:6px; font-size:11px;">${warnings.map((w) => escapeHtml(w)).join("<br>")}</div>` : ""}
      </div>
    `;
  } catch (e) {
    host.innerHTML = `<div class="muted" style="font-size:12px;">Error: ${escapeHtml(e && (e.message || e) || String(e))}</div>`;
  }
}

function getRunbookConfigFromUI() {
  const baselineEl = el("relConfigBaseline");
  const headEl = el("relConfigHead");
  const prefixContainer = el("relConfigTicketPrefixes");
  const baselineRef = (baselineEl && baselineEl.value ? baselineEl.value.trim() : "") || null;
  const headRef = (headEl && headEl.value ? headEl.value.trim() : "") || "main";
  const parts = [];
  if (prefixContainer) {
    prefixContainer.querySelectorAll("[data-prefix-value]").forEach((inp) => {
      const v = (inp.value || "").trim().replace(/[^\w]/g, "");
      if (v) parts.push(v);
    });
  }
  let ticketRegex = null;
  if (parts.length) ticketRegex = "(?i)(?:" + parts.join("|") + ")[-\\s]?\\d+";
  return { baselineRef, headRef, ticketRegex };
}

async function runScopeRunbook() {
  const projectKey = getCurrentProjectKey();
  const hostId = "runbookScopeBody";
  const host = el(hostId);
  const btnCancel = el("btnRunCancel");
  if (host) host.innerHTML = `<div class="muted" style="font-size:12px;">Running scope check…</div>`;
  if (btnCancel) btnCancel.style.display = "inline-block";

  runbookAbortController = new AbortController();
  const { headRef, ticketRegex } = getRunbookConfigFromUI();
  const usePrefix = runbooksScopeBaselineMode === "prefix";
  const baselineRef = usePrefix ? null : (el("relConfigBaseline") && el("relConfigBaseline").value ? el("relConfigBaseline").value.trim() : null) || null;
  const baselinePrefix = usePrefix ? (el("relLatestBranchesPrefix") && el("relLatestBranchesPrefix").value ? el("relLatestBranchesPrefix").value.trim() : null) || "release" : null;

  try {
    const body = {
      projectKey,
      ...(baselineRef ? { baselineRef } : {}),
      ...(baselinePrefix ? { baselinePrefix } : {}),
      ...(headRef ? { headRef } : {}),
      ...(ticketRegex ? { ticketRegex } : {}),
    };
    const data = await safeApiFetch(`${SNAPSHOT_API_BASE}/api/runbooks/scope`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: runbookAbortController.signal,
    });
    if (!data || data._error) {
      if (host) host.innerHTML = `<div class="relApiError" role="alert"><strong>API Error</strong>: ${escapeHtml(data?.message || "Cannot connect to API server")}. Make sure the snapshot API is running (see Configuration or start the API server).</div>`;
      return;
    }
    if (data.status !== "ok") {
      if (host) host.innerHTML = `<div class="muted" style="font-size:12px;">Failed: ${escapeHtml(data?.message || "Unknown error")}</div>`;
      return;
    }

    const rows = (data.repos || []).map(r => ({
      repo: r.repo,
      baselineRef: r.baselineRef || "-",
      headRef: r.headRef || "-",
      commitCount: r.commitCount != null ? String(r.commitCount) : "-",
      compareUrl: r.compareUrl || "",
      tickets: (r.tickets || []).join(", "),
    }));

    const summaryHtml = `
      <div class="muted" style="margin-top:6px; font-size:12px;">
        Unique tickets: ${(data.summary?.uniqueTickets || []).length || 0}. 
        Total commits: ${data.summary?.totalCommits ?? 0}.
      </div>
    `;

    renderRunbookTable(
      hostId,
      "Scope between baseline and default branch",
      rows,
      [
        { key: "repo", label: "Repository" },
        { key: "baselineRef", label: "Baseline ref" },
        { key: "headRef", label: "Head ref" },
        { key: "commitCount", label: "Commits", urlKey: "compareUrl" },
        { key: "tickets", label: "Tickets", cellClass: "runbookCellTickets", truncateTickets: true },
      ],
      summaryHtml,
      data.warnings || [],
      data,
      "scope"
    );
  } catch (e) {
    if (e && e.name === "AbortError") {
      if (host) host.innerHTML = `<div class="muted" style="font-size:12px;">Cancelled.</div>`;
    } else if (host) host.innerHTML = `<div class="muted" style="font-size:12px;">Error: ${escapeHtml(e && (e.message || e) || String(e))}</div>`;
  } finally {
    runbookAbortController = null;
    if (btnCancel) btnCancel.style.display = "none";
  }
}

async function runDriftRunbook() {
  const projectKey = getCurrentProjectKey();
  const hostId = "runbookDriftBody";
  const host = el(hostId);
  const btnCancel = el("btnRunCancel");
  if (host) host.innerHTML = `<div class="muted" style="font-size:12px;">Running drift check…</div>`;
  if (btnCancel) btnCancel.style.display = "inline-block";

  runbookAbortController = new AbortController();
  const { baselineRef, headRef, ticketRegex } = getRunbookConfigFromUI();

  try {
    const body = {
      projectKey,
      ...(baselineRef ? { baselineRef } : {}),
      ...(headRef ? { headRef } : {}),
      ...(ticketRegex ? { ticketRegex } : {}),
    };
    const data = await safeApiFetch(`${SNAPSHOT_API_BASE}/api/runbooks/drift`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: runbookAbortController.signal,
    });
    if (!data || data._error) {
      if (host) host.innerHTML = `<div class="relApiError" role="alert"><strong>API Error</strong>: ${escapeHtml(data?.message || "Cannot connect to API server")}. Make sure the snapshot API is running (see Configuration or start the API server).</div>`;
      return;
    }
    if (data.status !== "ok") {
      if (host) host.innerHTML = `<div class="muted" style="font-size:12px;">Failed: ${escapeHtml(data?.message || "Unknown error")}</div>`;
      return;
    }

    const rows = (data.repos || []).map(r => ({
      repo: r.repo,
      releaseRef: r.releaseRef || "-",
      mainRef: r.mainRef || "-",
      hasDrift: r.hasDrift ? "YES" : "no",
      commitCount: r.commitCount != null ? String(r.commitCount) : "-",
      compareUrl: r.compareUrl || "",
      tickets: (r.tickets || []).join(", "),
    }));

    const summaryHtml = `
      <div class="muted" style="margin-top:6px; font-size:12px;">
        Unique tickets on release-only commits: ${(data.summary?.uniqueTickets || []).length || 0}. 
        Total drift commits: ${data.summary?.totalDriftCommits ?? 0}.
      </div>
    `;

    renderRunbookTable(
      hostId,
      "Drift (release contains changes not in default branch)",
      rows,
      [
        { key: "repo", label: "Repository" },
        { key: "releaseRef", label: "Release ref" },
        { key: "mainRef", label: "Default ref" },
        { key: "hasDrift", label: "Drift?" },
        { key: "commitCount", label: "Drift commits", urlKey: "compareUrl" },
        { key: "tickets", label: "Tickets", cellClass: "runbookCellTickets", truncateTickets: true },
      ],
      summaryHtml,
      data.warnings || [],
      data,
      "drift"
    );
  } catch (e) {
    if (e && e.name === "AbortError") {
      if (host) host.innerHTML = `<div class="muted" style="font-size:12px;">Cancelled.</div>`;
    } else if (host) host.innerHTML = `<div class="muted" style="font-size:12px;">Error: ${escapeHtml(e && (e.message || e) || String(e))}</div>`;
  } finally {
    runbookAbortController = null;
    if (btnCancel) btnCancel.style.display = "none";
  }
}

async function runReleaseDiffRunbook() {
  const projectKey = getCurrentProjectKey();
  const hostId = "runbookReleaseDiffBody";
  let host = el(hostId);
  const btnCancel = el("btnRunCancel");
  if (host) host.innerHTML = `<div class="muted" style="font-size:12px;">Running release diff…</div>`;
  if (btnCancel) btnCancel.style.display = "inline-block";

  const releaseXEl = el("relConfigReleaseX");
  const releaseYEl = el("relConfigReleaseY");
  const releaseRefA = (releaseXEl && releaseXEl.value ? releaseXEl.value.trim() : "") || null;
  const releaseRefB = (releaseYEl && releaseYEl.value ? releaseYEl.value.trim() : "") || null;
  const { ticketRegex } = getRunbookConfigFromUI();

  if (!releaseRefA || !releaseRefB) {
    host = el(hostId);
    if (host) host.innerHTML = `<div class="relApiError" role="alert">Set <b>Newer release</b> and <b>Older release</b> to run the diff.</div>`;
    if (btnCancel) btnCancel.style.display = "none";
    return;
  }

  runbookAbortController = new AbortController();
  try {
    const body = {
      projectKey,
      releaseRefA: releaseRefB,
      releaseRefB: releaseRefA,
      ...(ticketRegex ? { ticketRegex } : {}),
    };
    const data = await safeApiFetch(`${SNAPSHOT_API_BASE}/api/runbooks/release-diff`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: runbookAbortController.signal,
    });
    host = el(hostId);
    if (!data || data._error) {
      if (host) host.innerHTML = `<div class="relApiError" role="alert"><strong>API Error</strong>: ${escapeHtml(data?.message || "Cannot connect to API server")}. Make sure the snapshot API is running (see Configuration or start the API server).</div>`;
      const rb = el("relResultsBlock");
      if (rb && rb.scrollIntoView) rb.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    if (data.status !== "ok") {
      if (host) host.innerHTML = `<div class="relApiError" role="alert">Failed: ${escapeHtml(data?.message || "Unknown error")}</div>`;
      const rb = el("relResultsBlock");
      if (rb && rb.scrollIntoView) rb.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }

    const repos = data.repos || [];
    const summary = data.summary || {};
    const addedTickets = summary.addedTickets || [];
    const totalAdded = summary.totalAdded ?? 0;
    const olderRef = data.releaseRefA || "";
    const newerRef = data.releaseRefB || "";
    const warningsList = data.warnings || [];
    const refMissingRepos = new Set();
    for (const w of warningsList) {
      const s = String(w || "").trim();
      if (!/refs not fully available/i.test(s)) continue;
      const idx = s.indexOf(":");
      if (idx !== -1) refMissingRepos.add(s.slice(0, idx).trim());
    }

    const rows = repos.map((r) => {
      const a = r.added || {};
      const tickets = a.tickets || [];
      const n = a.commitCount != null ? a.commitCount : 0;
      const fromApi = r.refsAvailable !== false;
      const fromWarnings = !refMissingRepos.has(String(r.repo || "").trim());
      const refsAvailable = fromApi && fromWarnings;
      return {
        repo: r.repo,
        releaseNewer: r.releaseB || "-",
        releaseOlder: r.releaseA || "-",
        addedTickets: tickets.length ? `+${tickets.length}` : "0",
        addedTicketsList: tickets.join(", ") || "-",
        commits: String(n),
        compareUrl: a.compareUrl || "",
        refsAvailable,
      };
    });

    const summaryHtml = `
      <div class="muted" style="margin-top:6px; font-size:12px;">
        Older → newer: +${totalAdded} commits, +${addedTickets.length} tickets.
      </div>
    `;

    let rawLines = [];
    rawLines.push(`Release diff: ${olderRef || "old"} → ${newerRef || "new"}`);
    rawLines.push("");
    rawLines.push("## Added (older → newer)");
    for (const r of repos) {
      const a = r.added || {};
      const n = a.commitCount ?? 0;
      const t = (a.tickets || []).join(", ") || "-";
      const p = (a.prs || []).join(", ") || "-";
      rawLines.push(`  ${r.repo}: +${n} commits | tickets: ${t} | PRs: ${p}`);
    }
    rawLines.push("");
    rawLines.push("## Summary");
    rawLines.push(`  +${totalAdded} commits, +${addedTickets.length} tickets`);
    rawLines.push(`  Tickets: ${(addedTickets || []).join(", ") || "-"}`);
    const rawBlock = `
      <div class="relRawBlock" style="margin-top:12px;">
        <div class="ov2H" style="font-size:12px;">Release notes raw</div>
        <pre class="relRawPre" style="margin-top:6px; padding:10px; background:var(--bg2); border-radius:4px; font-size:11px; overflow:auto; max-height:280px;">${escapeHtml(rawLines.join("\n"))}</pre>
      </div>
    `;

    renderRunbookTable(
      hostId,
      "Release diff (older → newer)",
      rows,
      [
        { key: "repo", label: "Repository" },
        { key: "releaseNewer", label: "Newer release" },
        { key: "releaseOlder", label: "Older release" },
        { key: "addedTicketsList", label: "+Tickets", cellClass: "runbookCellTickets", truncateTickets: true },
        { key: "commits", label: "+Commits", urlKey: "compareUrl" },
      ],
      summaryHtml,
      data.warnings || [],
      data,
      "release-diff",
      rawBlock,
      (row) => (row.refsAvailable === false ? "relRowStrikethrough" : "")
    );
    const resultPane = el(hostId);
    if (resultPane) {
      if (/Running\s+release diff/i.test(resultPane.innerHTML)) {
        resultPane.innerHTML = `<div class="relApiError" role="alert"><strong>Run completed</strong> but output was not shown (possible re-render). Try running again.</div>`;
      }
      if (typeof resultPane.scrollIntoView === "function") {
        resultPane.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    }
    const resultsBlock = el("relResultsBlock");
    if (resultsBlock && typeof resultsBlock.scrollIntoView === "function") {
      resultsBlock.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  } catch (e) {
    host = el(hostId);
    if (e && e.name === "AbortError") {
      if (host) host.innerHTML = `<div class="relApiError" role="alert">Cancelled.</div>`;
    } else if (host) host.innerHTML = `<div class="relApiError" role="alert">Error: ${escapeHtml(e && (e.message || e) || String(e))}</div>`;
    const rb = el("relResultsBlock");
    if (rb && rb.scrollIntoView) rb.scrollIntoView({ behavior: "smooth", block: "start" });
  } finally {
    runbookAbortController = null;
    if (btnCancel) btnCancel.style.display = "none";
  }
}

async function runReadinessRunbook() {
  const projectKey = getCurrentProjectKey();
  const hostId = "runbookReadinessBody";
  const host = el(hostId);
  const btnCancel = el("btnRunCancel");
  if (host) host.innerHTML = `<div class="muted" style="font-size:12px;">Running readiness check…</div>`;
  if (btnCancel) btnCancel.style.display = "inline-block";

  runbookAbortController = new AbortController();
  const { baselineRef, headRef, ticketRegex } = getRunbookConfigFromUI();

  try {
    const body = {
      projectKey,
      ...(baselineRef ? { baselineRef } : {}),
      ...(headRef ? { headRef } : {}),
      ...(ticketRegex ? { ticketRegex } : {}),
    };
    const data = await safeApiFetch(`${SNAPSHOT_API_BASE}/api/runbooks/readiness`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: runbookAbortController.signal,
    });
    if (!data || data._error) {
      if (host) host.innerHTML = `<div class="relApiError" role="alert"><strong>API Error</strong>: ${escapeHtml(data?.message || "Cannot connect to API server")}. Make sure the snapshot API is running (see Configuration or start the API server).</div>`;
      return;
    }
    if (data.status !== "ok" && data.status !== "warn") {
      if (host) host.innerHTML = `<div class="muted" style="font-size:12px;">Failed: ${escapeHtml(data?.message || "Unknown error")}</div>`;
      return;
    }

    const rows = (data.repos || []).map(r => ({
      repo: r.repo,
      baselineRef: r.baselineRef || "-",
      headRef: r.headRef || "-",
      baselineExists: r.baselineExists ? "yes" : "no",
      headExists: r.headExists ? "yes" : "no",
      status: r.status || "ok",
      messages: (r.messages || []).join(" | "),
    }));

    const summaryHtml = `
      <div class="muted" style="margin-top:6px; font-size:12px;">
        Overall status: ${escapeHtml(data.status || "ok")}.
      </div>
    `;

    renderRunbookTable(
      hostId,
      "Release readiness / ref validator",
      rows,
      [
        { key: "repo", label: "Repository" },
        { key: "baselineRef", label: "Baseline ref" },
        { key: "headRef", label: "Head ref" },
        { key: "baselineExists", label: "Baseline exists" },
        { key: "headExists", label: "Head exists" },
        { key: "status", label: "Status" },
        { key: "messages", label: "Messages" },
      ],
      summaryHtml,
      data.warnings || [],
      data,
      "readiness"
    );
  } catch (e) {
    if (e && e.name === "AbortError") {
      if (host) host.innerHTML = `<div class="muted" style="font-size:12px;">Cancelled.</div>`;
    } else if (host) host.innerHTML = `<div class="muted" style="font-size:12px;">Error: ${escapeHtml(e && (e.message || e) || String(e))}</div>`;
  } finally {
    runbookAbortController = null;
    if (btnCancel) btnCancel.style.display = "none";
  }
}
// ---------- Right sidebar (Documents / Alerts) ----------
function renderRightSidebar() {
  const inner = el("rightInner");
  if (!inner) return;

  const proj = getCurrentProject();
  const envs = getEnvList(proj);

  // Default: Documents + Alerts (per project)
  const projKey = proj?.key || proj?.name || "";
  const docs = getProjectDocs(proj);

  const docsHtml = docs.length
    ? docs.map((d, idx) => {
        const href = d.href || "#";
        const safeHref = escapeHtml(href);
        const safeText = escapeHtml(d.text || "Document");
        const isPlaceholder = href === "#";
        return `
          <div class="docRow" data-doc-index="${idx}">
            <a class="docCard" href="${safeHref}" ${isPlaceholder ? 'onclick="return false;"' : 'target="_blank" rel="noopener noreferrer"'}>
              <div class="docIcon">📄</div>
              <div class="docText">${safeText}</div>
            </a>
            ${isPlaceholder ? "" : `
              <div class="docActions">
                <button type="button" class="btnTiny docActionBtn" data-doc-index="${idx}" data-doc-action="edit">Edit</button>
                <button type="button" class="btnTiny docActionBtn" data-doc-index="${idx}" data-doc-action="delete">Delete</button>
              </div>
            `}
          </div>
        `;
      }).join("")
    : `<div class="muted" style="margin-top:8px;">No data</div>`;

  // Global alerts (includes Datadog monitors, Argo notes, etc.)
  const globalAlerts = Array.isArray(appData.globalAlerts) ? appData.globalAlerts : [];
  const alerts = globalAlerts.slice(0, 8);

  const alertsHtml = alerts.length
    ? alerts.map((a) => {
        const title = escapeHtml(a.title || "Alert");
        const msg = escapeHtml(a.message || "");
        const level = normalizeKey(a.level || a.severity || "warn");
        const badge = level === "bad" || level === "high" || level === "error"
          ? "BAD"
          : (level === "info" ? "INFO" : "WARN");
        const url = a.url ? escapeHtml(a.url) : "";
        return `
          <div class="alert" style="margin-top:10px;">
            <div style="display:flex; align-items:center; justify-content:space-between; gap:10px;">
              <div style="font-weight:600;">${title}</div>
              <div class="chip" style="font-size:11px; padding:3px 8px; border-radius:999px;">${badge}</div>
            </div>
            ${msg ? `<div class="muted" style="margin-top:6px; font-size:12px; line-height:1.35;">${msg}</div>` : ``}
            ${url ? `<div style="margin-top:6px;"><a class="cellLink" href="${url}" target="_blank" rel="noopener noreferrer">Open source</a></div>` : ``}
          </div>
        `;
      }).join("")
    : `<div class="muted" style="margin-top:8px;">No data</div>`;

  // Admin-only inline controls for managing per-project documents.
  const isAdmin = (() => {
    try {
      if (window.AdminConfig && AdminConfig.isAdminMode()) return true;
      const h = (window.location.hostname || "").toLowerCase();
      if (h === "localhost" || h === "127.0.0.1") return true;
      const q = new URLSearchParams(window.location.search || "");
      if (q.get("admin") === "1" || q.get("admin") === "true") return true;
    } catch (_) {}
    return false;
  })();

  inner.innerHTML = `
    <div class="rightSectionTitle">DOCUMENTS</div>
    ${docsHtml}
    ${isAdmin ? `
      <div class="muted" style="margin-top:8px; font-size:11px;">
        Admin: manage documents for this platform.
      </div>
      <div style="display:flex; gap:8px; margin-top:6px;">
        <button type="button" class="btn btnSmall" id="btnAddDoc">+ Add</button>
      </div>
    ` : ""}
  `;

  if (isAdmin && window.AdminConfig && projKey) {
    const addBtn = el("btnAddDoc");
    if (addBtn) {
      addBtn.onclick = () => {
        try {
          const title = prompt("Document title (e.g. Release process):");
          if (!title || !title.trim()) return;
          const href = prompt("Document URL (SharePoint, GitHub README, internal docs):", "https://");
          if (!href || !href.trim()) return;

          const cfg = AdminConfig.load() || AdminConfig.defaultConfig();
          cfg.projects = Array.isArray(cfg.projects) ? cfg.projects : [];
          const matchKey = String(projKey || "").toUpperCase();
          let p = cfg.projects.find((pr) => pr && String(pr.key || "").toUpperCase() === matchKey);
          if (!p) {
            // If the project is missing from config, create a minimal shell so docs are persisted.
            p = { key: projKey, name: proj?.name || projKey, environments: [], services: [], docs: [] };
            cfg.projects.push(p);
          }
          p.docs = Array.isArray(p.docs) ? p.docs : [];
          p.docs.push({ text: title.trim(), href: href.trim() });
          AdminConfig.save(cfg);
          renderRightSidebar();
        } catch (e) {
          console.error("Failed to add project document:", e);
          alert("Failed to save document. See console for details.");
        }
      };
    }

    // Edit / delete existing documents (admin only)
    inner.querySelectorAll(".docActionBtn[data-doc-index]").forEach((btn) => {
      btn.addEventListener("click", () => {
        try {
          const idx = parseInt(btn.getAttribute("data-doc-index"), 10);
          if (Number.isNaN(idx)) return;

          const action = btn.getAttribute("data-doc-action") || "edit";

          const cfg = AdminConfig.load() || AdminConfig.defaultConfig();
          cfg.projects = Array.isArray(cfg.projects) ? cfg.projects : [];
          const matchKey = String(projKey || "").toUpperCase();
          let p = cfg.projects.find((pr) => pr && String(pr.key || "").toUpperCase() === matchKey);
          if (!p) {
            // If the project is missing from config, create shell with current docs as starting point
            p = { key: projKey, name: proj?.name || projKey, environments: [], services: [], docs: [] };
            cfg.projects.push(p);
          }

          const currentDocs = docs.slice(); // docs from getProjectDocs()
          const existing = currentDocs[idx] || { text: "", href: "" };

          if (action === "delete") {
            const confirmRemove = window.confirm("Remove this document from this platform?");
            if (!confirmRemove) return;
            currentDocs.splice(idx, 1);
          } else {
            // Edit: allow changing title and URL
            const newTitle = window.prompt("Document title:", existing.text || "");
            if (!newTitle || !newTitle.trim()) return;
            const newHref = window.prompt("Document URL:", existing.href || "https://");
            if (!newHref || !newHref.trim()) return;
            currentDocs[idx] = { text: newTitle.trim(), href: newHref.trim() };
          }

          p.docs = currentDocs;
          AdminConfig.save(cfg);
          renderRightSidebar();
        } catch (e) {
          console.error("Failed to update project document:", e);
          alert("Failed to update document. See console for details.");
        }
      });
    });
  }
}

// ---------- Admin / Configuration ----------
const ADMIN_API_BASE = (() => {
  if (typeof window === "undefined" || !window.location) return "http://127.0.0.1:8001";
  const h = (window.location.hostname || "").toLowerCase();
  if (h === "localhost" || h === "127.0.0.1") return "http://127.0.0.1:8001";
  const o = window.location.origin || "";
  if (o && o.startsWith("http")) return o;
  return "http://127.0.0.1:8001";
})();

let adminWizardStep = 1;
let adminDraft = null;
let adminDiagnosticsOpen = false;
let adminDryRunResult = null;
let adminDryRunLoading = false;
let adminTestResults = {};
let adminProjectModalOpen = false;
let adminEditingProjectIndex = null; // null = new, "group:index" or "index" for edit
let adminEditingGroupIndex = null; // null = new, number = edit index
let adminGroupModalOpen = false;
let adminGuideModalOpen = false;
let adminGuideType = null; // "github", "datadog", "jira"
let adminAutoSaveTimer = null;

function scheduleAutoSave() {
  if (adminAutoSaveTimer) clearTimeout(adminAutoSaveTimer);
  adminAutoSaveTimer = setTimeout(() => {
    const draft = getAdminDraft();
    if (draft && window.AdminConfig) {
      try {
        localStorage.setItem("roc_admin_draft", JSON.stringify(draft));
        const indicator = document.getElementById("adminAutoSaveIndicator");
        if (indicator) {
          indicator.textContent = "Saved";
          indicator.style.opacity = "1";
          setTimeout(() => { indicator.style.opacity = "0"; }, 2000);
        }
      } catch (e) {
        console.warn("Auto-save failed:", e);
      }
    }
  }, 1000);
}

function validateField(input, fieldName, validator) {
  if (!input) return;
  const value = input.value.trim();
  const error = validator(value);
  const existingErr = input.parentElement.querySelector(".adminFieldError");
  if (existingErr) existingErr.remove();
  if (error) {
    input.classList.add("adminInputError");
    const errEl = document.createElement("div");
    errEl.className = "adminFieldError";
    errEl.textContent = error;
    input.parentElement.appendChild(errEl);
  } else {
    input.classList.remove("adminInputError");
  }
}

function renderGuideModal() {
  const title = el("adminGuideTitle");
  const body = el("adminGuideBody");
  if (!title || !body) return;
  
  const guides = {
    github: {
      title: "How to create a GitHub Personal Access Token",
      steps: [
        {
          num: 1,
          title: "Go to GitHub Settings",
          text: "Click your profile picture → Settings, or go directly to <a href='https://github.com/settings/tokens' target='_blank'>github.com/settings/tokens</a>",
          visual: "Profile → Settings"
        },
        {
          num: 2,
          title: "Navigate to Developer settings",
          text: "In the left sidebar, scroll down and click 'Developer settings'",
          visual: "Left sidebar → Developer settings"
        },
        {
          num: 3,
          title: "Go to Personal access tokens",
          text: "Click 'Personal access tokens' → 'Tokens (classic)'",
          visual: "Personal access tokens → Tokens (classic)"
        },
        {
          num: 4,
          title: "Generate new token",
          text: "Click 'Generate new token' → 'Generate new token (classic)'",
          visual: "Generate new token (classic)"
        },
        {
          num: 5,
          title: "Configure token",
          text: "Give it a name (e.g., 'WatchTurm Control Room'), set expiration, and check the <code>repo</code> scope (this gives read access to repositories)",
          visual: "Name: 'WatchTurm Control Room'<br/>Expiration: 90 days (or your choice)<br/>Check: <code>repo</code> scope"
        },
        {
          num: 6,
          title: "Copy the token",
          text: "Click 'Generate token' and immediately copy the token (it starts with <code>ghp_</code>). You won't be able to see it again!",
          visual: "Copy token starting with <code>ghp_</code>"
        }
      ]
    },
    datadog: {
      title: "Where to find Datadog API and Application Keys",
      steps: [
        {
          num: 1,
          title: "Go to Datadog API settings",
          text: "Log in to Datadog → Organization Settings → API Keys (or go to <a href='https://app.datadoghq.com/organization-settings/api-keys' target='_blank'>app.datadoghq.com/organization-settings/api-keys</a>)",
          visual: "Organization Settings → API Keys"
        },
        {
          num: 2,
          title: "Get API Key",
          text: "Copy your existing API key, or create a new one. The API key is visible in the list.",
          visual: "Copy API Key (starts with random characters)"
        },
        {
          num: 3,
          title: "Get Application Key",
          text: "Go to Application Keys section (same Organization Settings page). Copy your existing application key, or create a new one.",
          visual: "🔐 Copy Application Key (also starts with random characters)"
        },
        {
          num: 4,
          title: "Note your Datadog site",
          text: "Check the URL of your Datadog instance. If it's <code>app.datadoghq.com</code>, use <code>datadoghq.com</code>. If it's <code>app.datadoghq.eu</code>, use <code>datadoghq.eu</code>.",
          visual: "🌍 Site: <code>datadoghq.com</code> (US) or <code>datadoghq.eu</code> (EU)"
        }
      ]
    },
    jira: {
      title: "How to create a Jira API Token",
      steps: [
        {
          num: 1,
          title: "Go to Atlassian Account settings",
          text: "Go to <a href='https://id.atlassian.com/manage-profile/security/api-tokens' target='_blank'>id.atlassian.com/manage-profile/security/api-tokens</a>",
          visual: "Atlassian Account → Security → API tokens"
        },
        {
          num: 2,
          title: "Create API token",
          text: "Click 'Create API token', give it a label (e.g., 'WatchTurm Control Room'), and click 'Create'",
          visual: "Create API token → Label → Create"
        },
        {
          num: 3,
          title: "Copy the token",
          text: "Copy the token immediately (you won't see it again). Use your Jira email and this token for authentication.",
          visual: "Copy token (long random string)"
        },
        {
          num: 4,
          title: "Get your Jira base URL",
          text: "Your Jira base URL is usually <code>https://yourcompany.atlassian.net</code> or <code>https://jira.yourcompany.com</code>",
          visual: "Base URL: <code>https://yourcompany.atlassian.net</code>"
        }
      ]
    }
  };
  
  const guide = guides[adminGuideType];
  if (!guide) {
    title.textContent = "Guide not found";
    body.innerHTML = "<p class='muted'>Guide not available.</p>";
    return;
  }
  
  title.textContent = guide.title;
  body.innerHTML = `
    <div class="adminGuideSteps">
      ${guide.steps.map(step => `
        <div class="adminGuideStep">
          <div class="adminGuideStepNum">${step.num}</div>
          <div class="adminGuideStepContent">
            <h4>${step.title}</h4>
            <p>${step.text}</p>
            <div class="adminGuideVisual">${step.visual}</div>
          </div>
        </div>
      `).join("")}
    </div>
    <div class="adminGuideFooter">
      <p class="muted"><strong>Tip:</strong> Keep your tokens secure. Never share them or commit them to version control.</p>
    </div>
  `;
}

// ---------- Setup: Configuration guide (open-source instructions) ----------
function renderSetup() {
  const container = el("setupContent");
  if (!container) return;

  container.innerHTML = `
    <div class="panel">
      <p class="muted" style="margin-bottom:24px;">Follow these steps to deploy WatchTurm Control Room. Designed for DevOps.</p>

      <div class="setupStep">
        <span class="setupStepNum">1</span><h3>Requirements</h3>
        <div class="setupStepDesc">
          <ul style="margin:8px 0 0 20px;">
            <li>Python 3.10+ for the snapshot generator</li>
            <li>Integrations: GitHub (required), TeamCity (for builds), Jira (optional, for Ticket Tracker)</li>
          </ul>
        </div>
      </div>

      <div class="setupStep">
        <span class="setupStepNum">2</span><h3>Environment variables (secrets)</h3>
        <div class="setupStepDesc">
          Copy <code>.env.example</code> to <code>.env</code> in the project root. Add your tokens. <strong>Never commit .env to version control.</strong>
        </div>
        <div class="setupCode">GITHUB_TOKEN=ghp_xxxx
JIRA_URL=https://your-org.atlassian.net
JIRA_EMAIL=you@example.com
JIRA_API_TOKEN=xxxx
TEAMCITY_URL=https://teamcity.example.com
TEAMCITY_TOKEN=xxxx</div>
      </div>

      <div class="setupStep">
        <span class="setupStepNum">3</span><h3>Project config (admin-config.js)</h3>
        <div class="setupStepDesc">
          Edit <code>web/admin-config.js</code>. Define groups, projects, and environments. See the default export for structure. No secrets here—only project names, keys, and env keys.
        </div>
      </div>

      <div class="setupStep">
        <span class="setupStepNum">4</span><h3>Run snapshot</h3>
        <div class="setupStepDesc">
          From project root: <code>python MVP1/snapshot/snapshot.py</code>. This writes <code>data/latest.json</code> and <code>data/release_history/</code>. Schedule via cron (e.g. every 15–30 min).
        </div>
      </div>

      <div class="setupStep">
        <span class="setupStepNum">5</span><h3>Serve the app</h3>
        <div class="setupStepDesc">
          Serve <code>web/</code> and <code>data/</code> from the same origin (avoids CORS). Example nginx:
        </div>
        <div class="setupCode">server {
  listen 80;
  server_name control-room.example.com;
  root /path/to/watchturm-control-room;
  location / { try_files $uri $uri/ /web/index.html; }
  location /web/ { alias /path/to/watchturm-control-room/web/; }
  location /data/ { alias /path/to/watchturm-control-room/data/; }
}</div>
      </div>

      <div class="setupStep">
        <span class="setupStepNum">6</span><h3>Troubleshooting</h3>
        <div class="setupStepDesc">
          <ul style="margin:8px 0 0 20px;">
            <li><strong>Empty Overview / No projects:</strong> Run snapshot; check <code>data/latest.json</code> exists and has <code>projects</code> array.</li>
            <li><strong>Release History / Statistics empty:</strong> Snapshot produces <code>data/release_history/index.json</code> and <code>events.jsonl</code>. Run snapshot at least once.</li>
            <li><strong>CORS errors:</strong> Web and data must be same origin. Don't fetch data from a different domain.</li>
          </ul>
        </div>
      </div>
    </div>
  `;
}

function getAdminDraft() {
  if (adminDraft) return adminDraft;
  const loaded = window.AdminConfig && AdminConfig.load();
  adminDraft = loaded ? JSON.parse(JSON.stringify(loaded)) : (window.AdminConfig ? AdminConfig.defaultConfig() : null);
  return adminDraft;
}

function setAdminDraft(updates) {
  const d = getAdminDraft();
  if (!d) return;
  if (typeof updates === "function") updates(d);
  else Object.assign(d, updates);
}

function renderAdmin() {
  const container = el("adminContent");
  if (!container || !window.AdminConfig) return;

  const draft = getAdminDraft();
  if (!draft) return;

  const steps = [
    { n: 1, label: "Tenant basics" },
    { n: 2, label: "Projects & envs" },
    { n: 3, label: "GitHub (required)" },
    { n: 4, label: "Optional integrations" },
    { n: 5, label: "Validate & save" },
  ];

  const val = AdminConfig.validate(draft);
  const hasErrors = !val.valid && (val.errors || []).length > 0;

  const stepContent = () => {
    if (adminWizardStep === 1) {
      const t = draft.tenant || {};
      return `
        <div class="adminStep">
          <h3 class="adminStepTitle">Tenant basics</h3>
          <p class="muted adminHelper">Your organization identifier. Used for config storage and (later) multi-tenant isolation.</p>
          <div class="adminForm">
            <label class="adminLabel">Tenant name <span class="muted">(required)</span></label>
            <input type="text" id="adminTenantName" class="adminInput" placeholder="e.g. Acme Corp" value="${escapeHtml(String(t.name || ""))}" />
            <span class="adminHint">Example: Acme Corp, My Team</span>
          </div>
          <div class="adminForm">
            <label class="adminLabel">Slug <span class="muted">(optional)</span></label>
            <input type="text" id="adminTenantSlug" class="adminInput" placeholder="e.g. acme-corp" value="${escapeHtml(String(t.slug || ""))}" />
            <span class="adminHint">Lowercase, letters, numbers, hyphens. Used in URLs and config keys. If empty, we derive from name.</span>
          </div>
          <p class="adminSkip">What happens if I skip slug? You can leave it blank; we will use a default. Specify it if you need a stable URL-friendly id.</p>
        </div>`;
    }
    if (adminWizardStep === 3) {
      const gh = (draft.integrations || {}).github || {};
      return `
        <div class="adminStep">
          <h3 class="adminStepTitle">GitHub (required)</h3>
          <p class="muted adminHelper">
            <span class="adminTooltip" title="GitHub is our source of truth. We read infrastructure files (like kustomization.yaml) from your GitHub repos to determine what's deployed in each environment.">?</span>
            We read deployment state from your infrastructure repositories. Each service has a code repo (app code) and an infra repo (deployment config).
          </p>
          <div class="adminForm">
            <label class="adminLabel">GitHub organization or username <span class="muted">(required)</span> <span class="adminTooltip" title="This is the GitHub org or user that owns your repositories. Examples: 'mycompany', 'acme-corp', or a GitHub username.">?</span></label>
            <input type="text" id="adminGhOrg" class="adminInput" placeholder="e.g. my-org or myusername" value="${escapeHtml(String(gh.org || ""))}" />
            <span class="adminHint">Examples: <code>acme-corp</code>, <code>mycompany</code>, or your GitHub username if using personal repos.</span>
          </div>
          <div class="adminForm">
            <label class="adminLabel">Personal access token <span class="muted">(required)</span> <span class="adminTooltip" title="A GitHub token with 'repo' scope. This allows us to read your repositories (we never write anything). Create one at: github.com → Settings → Developer settings → Personal access tokens → Tokens (classic)">?</span></label>
            <input type="password" id="adminGhToken" class="adminInput" placeholder="ghp_xxxxxxxxxxxx" autocomplete="off" value="${escapeHtml(String(gh.token || ""))}" />
            <span class="adminHint">
              <strong>How to create:</strong> Go to <a class="cellLink" href="https://github.com/settings/tokens" target="_blank" rel="noopener">github.com/settings/tokens</a> → "Generate new token (classic)" → Check <code>repo</code> scope → Generate → Copy the token (starts with <code>ghp_</code>).
            </span>
          </div>
          <button type="button" class="btn ghost adminGuideBtn" id="adminGhGuideBtn" style="margin-bottom:12px; font-size:12px;">Guide: How to create GitHub token</button>
          <button type="button" class="btn adminTestBtn" id="adminTestGitHub">Test connection</button>
          <span id="adminTestGitHubResult" class="adminTestResult"></span>
          <p class="adminSkip">
            <strong>What happens if I skip?</strong> GitHub is required. Without it, we cannot read your infrastructure files and the snapshot will fail. You must configure GitHub to proceed.
          </p>
        </div>`;
    }
    if (adminWizardStep === 2) {
      const groups = draft.groups || [];
      const allProjects = groups.flatMap(g => g.type === "group" ? (g.projects || []) : [g]).filter(p => p.type === "project" || !p.type);
      const totalProjects = allProjects.length;
      
      const groupsHtml = groups.map((g, gi) => {
        if (g.type === "group") {
          const projs = g.projects || [];
          const projRows = projs.map((p, pi) => {
            const envs = (p.environments || []).map(e => `${e.key || e.name || "?"}`).join(", ") || "-";
            const svcCount = (p.services || []).length;
            return `<tr>
              <td style="padding-left:24px;"><code>${escapeHtml(p.key || "")}</code></td>
              <td>${escapeHtml(p.name || "")}</td>
              <td>${envs}</td>
              <td>${svcCount} service${svcCount !== 1 ? "s" : ""}</td>
              <td style="white-space:nowrap;">
                <button type="button" class="btn ghost adminRowBtn" data-edit-project="${gi}:${pi}" title="Edit project">Edit</button>
                <button type="button" class="btn ghost adminRowBtn" data-delete-project="${gi}:${pi}" title="Delete project" style="color:rgba(255,80,120,.9);">Delete</button>
              </td>
            </tr>`;
          }).join("");
          return `
            <div class="adminGroupSection">
              <div class="adminGroupHeader">
                <div>
                  <strong>${escapeHtml(g.name || g.key || "Group")}</strong>
                  <code style="margin-left:8px; font-size:11px; opacity:0.7;">${escapeHtml(g.key || "")}</code>
                </div>
                <div style="display:flex; gap:8px;">
                  <button type="button" class="btn ghost adminRowBtn" data-add-project-to-group="${gi}" title="Add project to group">+ Add project</button>
                  <button type="button" class="btn ghost adminRowBtn" data-edit-group="${gi}" title="Edit group">Edit</button>
                  <button type="button" class="btn ghost adminRowBtn" data-delete-group="${gi}" title="Delete group" style="color:rgba(255,80,120,.9);">Delete</button>
                </div>
              </div>
              ${projs.length ? `
                <table class="adminTable" style="margin-top:12px;">
                  <thead><tr><th>Key</th><th>Display name</th><th>Environments</th><th>Services</th><th></th></tr></thead>
                  <tbody>${projRows}</tbody>
                </table>
              ` : `<p class="muted" style="margin:12px 0 0 24px; font-size:12px;">No projects in this group yet. Click "Add project" to add one.</p>`}
            </div>`;
        } else {
          // Standalone project
          const p = g;
          const envs = (p.environments || []).map(e => `${e.key || e.name || "?"}`).join(", ") || "-";
          const svcCount = (p.services || []).length;
          return `<tr>
            <td><code>${escapeHtml(p.key || "")}</code></td>
            <td>${escapeHtml(p.name || "")}</td>
            <td>${envs}</td>
            <td>${svcCount} service${svcCount !== 1 ? "s" : ""}</td>
            <td style="white-space:nowrap;">
              <button type="button" class="btn ghost adminRowBtn" data-edit-project="${gi}" title="Edit project">Edit</button>
              <button type="button" class="btn ghost adminRowBtn" data-delete-project="${gi}" title="Delete project" style="color:rgba(255,80,120,.9);">Delete</button>
            </td>
          </tr>`;
        }
      }).join("");
      
      const standaloneProjects = groups.filter(g => g.type !== "group");
      const standaloneRows = standaloneProjects.map((p, gi) => {
        const envs = (p.environments || []).map(e => `${e.key || e.name || "?"}`).join(", ") || "-";
        const svcCount = (p.services || []).length;
        return `<tr>
          <td><code>${escapeHtml(p.key || "")}</code></td>
          <td>${escapeHtml(p.name || "")}</td>
          <td>${envs}</td>
          <td>${svcCount} service${svcCount !== 1 ? "s" : ""}</td>
          <td style="white-space:nowrap;">
            <button type="button" class="btn ghost adminRowBtn" data-edit-project="${gi}" title="Edit project">Edit</button>
            <button type="button" class="btn ghost adminRowBtn" data-delete-project="${gi}" title="Delete project" style="color:rgba(255,80,120,.9);">Delete</button>
          </td>
        </tr>`;
      }).join("");
      
      return `
        <div class="adminStep">
          <h3 class="adminStepTitle">Platforms, groups &amp; projects</h3>
          <p class="muted adminHelper">
            <span class="adminTooltip" title="You can organize projects into groups (platforms) like 'TCBP' containing 'TCBP MFEs' and 'TCBP Adapters', or create standalone projects.">?</span>
            Create groups (platforms) to organize related projects, or add standalone projects. Each project needs environments and services.
          </p>
          <div class="adminForm" style="display:flex; gap:10px; flex-wrap:wrap;">
            <button type="button" class="btn btnPrimary" id="adminAddGroup">+ Add group/platform</button>
            <button type="button" class="btn" id="adminAddProject">+ Add standalone project</button>
          </div>
          ${groups.length ? `
            <div class="adminGroupsContainer">
              ${groupsHtml}
            </div>
            ${standaloneProjects.length ? `
              <div style="margin-top:24px;">
                <h4 style="margin-bottom:12px; font-size:14px; color:var(--t1);">Standalone projects</h4>
                <table class="adminTable">
                  <thead><tr><th>Key</th><th>Display name</th><th>Environments</th><th>Services</th><th></th></tr></thead>
                  <tbody>${standaloneRows}</tbody>
                </table>
              </div>
            ` : ""}
          ` : `<div class="adminEmptyState">
            <p class="muted" style="margin:20px 0; padding:20px; border:2px dashed var(--panelStroke); border-radius:12px; text-align:center;">
              <strong>No groups or projects yet</strong><br/>
              Create a group/platform (like "PROJ") to organize related projects, or add a standalone project.
            </p>
          </div>`}
          <p class="adminSkip">
            <strong>What happens if I skip?</strong> At least 1 project with ≥1 environment and ≥1 service is required. Without projects, the snapshot has nothing to observe.
          </p>
        </div>`;
    }
    if (adminWizardStep === 4) {
      const tc = (draft.integrations || {}).teamcity || {};
      const jira = (draft.integrations || {}).jira || {};
      const dd = (draft.integrations || {}).datadog || {};
      const argo = (draft.integrations || {}).argocd || {};
      const argoHosts = argo.envHosts || {};
      const argoHostsList = Object.entries(argoHosts).map(([envKey, url], idx) => `
        <div class="adminListItem" data-argo-index="${idx}">
          <input type="text" class="adminInput adminInputSmall" placeholder="Environment key (e.g. dev)" value="${escapeHtml(String(envKey))}" data-argo-env="${idx}" />
          <input type="text" class="adminInput adminInputSmall" placeholder="ArgoCD URL" value="${escapeHtml(String(url))}" data-argo-url="${idx}" />
          <button type="button" class="btn ghost adminListItemBtn adminIconRemove" data-remove-argo="${idx}" title="Remove">×</button>
        </div>
      `).join("") || '<div class="muted" style="padding:8px 0;">No ArgoCD environments configured.</div>';
      return `
        <div class="adminStep">
          <h3 class="adminStepTitle">Optional integrations</h3>
          <p class="muted adminHelper">TeamCity, ArgoCD, Datadog, Jira are optional. Degraded mode allowed: we show &quot;unknown&quot; when skipped.</p>
          <div class="adminOptSection">
            <h4>TeamCity <span class="adminTooltip" title="TeamCity is a CI/CD server. If you use it, we'll show build metadata (branch, build number, build URL) in the dashboard.">?</span></h4>
            ${!tc.baseUrl || !tc.token ? `
              <div class="adminMissingSection">
                <div class="adminMissingHeader">
                  <span class="adminMissingIcon">CI/CD</span>
                  <strong>What you're missing without TeamCity:</strong>
                </div>
                <ul class="adminMissingList">
                  <li>Build status and build numbers</li>
                  <li>Links to build artifacts</li>
                  <li>Branch information for deployments</li>
                  <li>Build history and trends</li>
                </ul>
                <p class="adminMissingNote">The app works fine without it - you'll just see "unknown" for build info.</p>
              </div>
            ` : ""}
            <div class="adminForm"><label class="adminLabel">Base URL</label><input type="text" id="adminTcUrl" class="adminInput" placeholder="https://teamcity.example.com" value="${escapeHtml(String(tc.baseUrl || ""))}" /></div>
            <div class="adminForm"><label class="adminLabel">Token</label><input type="password" id="adminTcToken" class="adminInput" autocomplete="off" value="${escapeHtml(String(tc.token || ""))}" /></div>
            <button type="button" class="btn adminTestBtn" id="adminTestTc">Test connection</button>
            <span id="adminTestTcResult" class="adminTestResult"></span>
          </div>
          <div class="adminOptSection">
            <h4>Jira <span class="adminTooltip" title="Jira is a ticket tracker. If configured, we'll enrich ticket data with Jira metadata (status, assignee, etc.).">?</span></h4>
            ${!jira.baseUrl || !jira.token ? `
              <div class="adminMissingSection">
                <div class="adminMissingHeader">
                  <span class="adminMissingIcon">Tickets</span>
                  <strong>What you're missing without Jira:</strong>
                </div>
                <ul class="adminMissingList">
                  <li>Ticket status and assignee information</li>
                  <li>Ticket descriptions and details</li>
                  <li>Priority and labels</li>
                  <li>Ticket links and navigation</li>
                </ul>
                <p class="adminMissingNote">You'll still see ticket IDs from PR titles, but without Jira details.</p>
              </div>
            ` : ""}
            <div class="adminForm"><label class="adminLabel">Base URL</label><input type="text" id="adminJiraUrl" class="adminInput" placeholder="https://my.atlassian.net" value="${escapeHtml(String(jira.baseUrl || ""))}" /></div>
            <div class="adminForm"><label class="adminLabel">Email</label><input type="text" id="adminJiraEmail" class="adminInput" value="${escapeHtml(String(jira.email || ""))}" /></div>
            <div class="adminForm"><label class="adminLabel">API token</label><input type="password" id="adminJiraToken" class="adminInput" autocomplete="off" value="${escapeHtml(String(jira.token || ""))}" /></div>
            <button type="button" class="btn adminTestBtn" id="adminTestJira">Test connection</button>
            <span id="adminTestJiraResult" class="adminTestResult"></span>
            <button type="button" class="btn ghost adminGuideBtn" id="adminJiraGuideBtn" style="margin-top:8px; font-size:12px;">Guide: Where to find API token</button>
          </div>
          <div class="adminOptSection">
            <h4>Datadog <span class="adminTooltip" title="Datadog is an observability platform. If configured, we'll show health metrics (CPU, memory, error rate) per environment.">?</span></h4>
            ${!dd.apiKey || !dd.appKey ? `
              <div class="adminMissingSection">
                <div class="adminMissingHeader">
                  <span class="adminMissingIcon">Metrics</span>
                  <strong>What you're missing without Datadog:</strong>
                </div>
                <ul class="adminMissingList">
                  <li>Real-time health metrics (CPU, memory, error rates)</li>
                  <li>Environment health status indicators</li>
                  <li>Performance alerts and degradation warnings</li>
                  <li>Historical trend data</li>
                </ul>
                <p class="adminMissingNote">Don't worry - the app still works! You'll just see "unknown" for health metrics.</p>
              </div>
            ` : ""}
            <div class="adminForm"><label class="adminLabel">Site <span class="adminTooltip" title="Your Datadog site (usually datadoghq.com, or datadoghq.eu for EU)">?</span></label><input type="text" id="adminDdSite" class="adminInput" placeholder="datadoghq.com" value="${escapeHtml(String(dd.site || "datadoghq.com"))}" /></div>
            <div class="adminForm"><label class="adminLabel">API key</label><input type="password" id="adminDdApiKey" class="adminInput" autocomplete="off" value="${escapeHtml(String(dd.apiKey || ""))}" /></div>
            <div class="adminForm"><label class="adminLabel">Application key</label><input type="password" id="adminDdAppKey" class="adminInput" autocomplete="off" value="${escapeHtml(String(dd.appKey || ""))}" /></div>
            <button type="button" class="btn adminTestBtn" id="adminTestDd">Test connection</button>
            <span id="adminTestDdResult" class="adminTestResult"></span>
            <button type="button" class="btn ghost adminGuideBtn" id="adminDdGuideBtn" style="margin-top:8px; font-size:12px;">Guide: Where to find API keys</button>
          </div>
          <div class="adminOptSection">
            <h4>ArgoCD <span class="adminTooltip" title="ArgoCD is a GitOps tool. If you use it, configure environment URLs here. Each environment (dev, qa, prod) can have its own ArgoCD instance.">?</span></h4>
            <p class="muted" style="font-size:12px; margin-bottom:12px;">Map environment keys to ArgoCD base URLs. Example: dev → https://argocd.dev.example.com</p>
            <div class="adminListContainer" id="adminArgoHostsList">${argoHostsList}</div>
            <button type="button" class="btn ghost" id="adminAddArgoHost" style="margin-top:8px;">+ Add environment</button>
            <span class="adminHint" style="display:block; margin-top:6px;">Leave empty if you don't use ArgoCD. The app will show &quot;unknown&quot; for ArgoCD health.</span>
          </div>
          <div class="adminOptSection adminOptSectionInProgress">
            <h4>Rancher <span class="adminBadgeInProgress">In Progress</span></h4>
            <p class="muted" style="font-size:12px;">Rancher integration is coming soon. This will allow tracking deployments and cluster health from Rancher.</p>
            <div class="adminInProgressNote">
              <strong>Planned features:</strong> Cluster health, deployment status, workload information
            </div>
          </div>
          <div class="adminOptSection adminOptSectionInProgress">
            <h4>Octopus Deploy <span class="adminBadgeInProgress">In Progress</span></h4>
            <p class="muted" style="font-size:12px;">Octopus Deploy integration is coming soon. This will allow tracking releases and deployments from Octopus.</p>
            <div class="adminInProgressNote">
              <strong>Planned features:</strong> Release tracking, deployment status, environment information
            </div>
          </div>
          <p class="adminSkip">What happens if I skip? Build/ticket/health metadata will be partial or &quot;unknown&quot;. App still works.</p>
        </div>`;
    }
    if (adminWizardStep === 5) {
      const ticketing = draft.ticketing || {};
      return `
        <div class="adminStep">
          <h3 class="adminStepTitle">Validate &amp; save</h3>
          <p class="muted adminHelper">Ticket regex (optional) extracts ticket IDs from PR titles. Must be valid regex.</p>
          <div class="adminForm">
            <label class="adminLabel">Ticket regex</label>
            <input type="text" id="adminTicketRegex" class="adminInput" placeholder="e.g. PROJ-\\d+" value="${escapeHtml(String(ticketing.regex || ""))}" />
            <span class="adminHint">Example: PROJ-\\d+ or [A-Z]+-\\d+</span>
          </div>
          ${hasErrors ? `<div class="adminValidationErr"><strong>Fix errors before saving:</strong><ul>${(val.errors || []).map(e => {
            const field = e.field || "";
            const step = e.step || null;
            return `<li>
              ${escapeHtml(e.message || "")}
              ${step ? `<button type="button" class="btnTiny adminFixBtn" data-fix-step="${step}" data-fix-field="${field}" style="margin-left:8px;">Fix →</button>` : ""}
            </li>`;
          }).join("")}</ul></div>` : ""}
          ${(val.warnings || []).length ? `<div class="adminValidationWarn"><strong>Warnings:</strong><ul>${(val.warnings || []).map(w => `<li>${escapeHtml(w.message || "")}</li>`).join("")}</ul></div>` : ""}
          <div class="adminStep5Actions">
            <button type="button" class="btn" id="adminDiagnosticsBtn">Diagnostics</button>
            <button type="button" class="btn btnPrimary" id="adminDryRunBtn" ${adminDryRunLoading ? "disabled" : ""}>${adminDryRunLoading ? "Running…" : "Run snapshot (dry-run)"}</button>
            <button type="button" class="btn btnPrimary" id="adminSaveBtn" ${hasErrors ? "disabled" : ""}>Save</button>
            <button type="button" class="btn ghost" id="adminExportBtn">Export JSON</button>
            <button type="button" class="btn ghost" id="adminImportBtn">Import JSON</button>
          </div>
          ${adminDryRunResult ? `
            <div class="adminDryRunResult ${adminDryRunResult.ok ? "ok" : "warn"}">
              <strong>${adminDryRunResult.ok ? "Dry-run complete" : "Dry-run failed"}</strong>
              <p>${escapeHtml(adminDryRunResult.message || "")}</p>
              ${adminDryRunResult.summary ? `<pre class="adminDryRunSummary">${escapeHtml(JSON.stringify(adminDryRunResult.summary, null, 2))}</pre>` : ""}
            </div>
          ` : ""}
        </div>`;
    }
    return "";
  };

  const stepperHtml = steps.map(s => `
    <button type="button" class="adminStepperBtn ${adminWizardStep === s.n ? "active" : ""}" data-step="${s.n}">${s.n}. ${escapeHtml(s.label)}</button>
  `).join("");

  const previewHtml = (() => {
    const groups = draft.groups || [];
    const allProjects = groups.flatMap(g => g.type === "group" ? (g.projects || []) : [g]).filter(p => p.type !== "group");
    const int = draft.integrations || {};
    const gh = int.github || {};
    const ghOk = !!(String(gh.org || "").trim() && String(gh.token || "").trim());
    const tcOk = !!(String((int.teamcity || {}).baseUrl || "").trim() && String((int.teamcity || {}).token || "").trim());
    const jiraOk = !!(String((int.jira || {}).baseUrl || "").trim() && String((int.jira || {}).email || "").trim() && String((int.jira || {}).token || "").trim());
    const ddOk = !!(String((int.datadog || {}).apiKey || "").trim() && String((int.datadog || {}).appKey || "").trim());
    return `
      <div class="adminPreview">
        <div class="adminPreviewTitle">Preview</div>
        <p class="muted adminPreviewSub">What will appear in the app</p>
        <div class="adminPreviewSection">
          <strong>Groups & Projects</strong>
          <ul>${groups.length ? groups.map(g => {
            if (g.type === "group") {
              const projs = (g.projects || []).map(p => `<li style="margin-left:16px;">${escapeHtml(p.key || "?")} - ${escapeHtml(p.name || "?")}</li>`).join("");
              return `<li><strong>${escapeHtml(g.name || g.key || "?")}</strong>${projs ? `<ul>${projs}</ul>` : ""}</li>`;
            } else {
              return `<li>${escapeHtml(g.key || "?")} - ${escapeHtml(g.name || "?")}</li>`;
            }
          }).join("") : "<li class=\"muted\">None</li>"}</ul>
        </div>
        <div class="adminPreviewSection">
          <strong>Environments</strong>
          <ul>${allProjects.length ? (allProjects.flatMap(p => (p.environments || []).map(e => `<li>${escapeHtml(p.key || "?")} → ${escapeHtml(e.key || e.name || "?")}</li>`)).slice(0, 12).join("") || "<li class=\"muted\">None</li>") : "<li class=\"muted\">None</li>"}</ul>
        </div>
        <div class="adminPreviewSection">
          <strong>Integration states</strong>
          <ul>
            <li>GitHub: <span class="pill ${ghOk ? "healthy" : "unknown"}">${ghOk ? "configured" : "missing"}</span></li>
            <li>TeamCity: <span class="pill ${tcOk ? "healthy" : "unknown"}">${tcOk ? "configured" : "disabled"}</span></li>
            <li>Jira: <span class="pill ${jiraOk ? "healthy" : "unknown"}">${jiraOk ? "configured" : "disabled"}</span></li>
            <li>Datadog: <span class="pill ${ddOk ? "healthy" : "unknown"}">${ddOk ? "configured" : "disabled"}</span></li>
          </ul>
        </div>
        <div class="adminHowItWorks">
          <strong>How it works</strong> <span class="adminTooltip" title="Infra defines deployed state. GitHub required. Everything else optional. Explicit unknown when data is missing.">?</span>
          <p class="muted">Infra (e.g. kustomization) defines deployed state. GitHub is required. Everything else optional. We use explicit &quot;unknown&quot; when data is missing.</p>
        </div>
      </div>`;
  })();

  container.innerHTML = `
    <div class="adminLayout">
      <div class="adminMain">
        <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:16px;">
          <div class="adminStepper">${stepperHtml}</div>
          <div id="adminAutoSaveIndicator" class="adminAutoSaveIndicator" style="opacity:0; transition:opacity 0.3s;">Saved</div>
        </div>
        ${adminWizardStep === 1 && (!draft.tenant?.name || !draft.tenant?.slug) ? `
          <div class="adminQuickStart">
            <h3 style="margin:0 0 12px;">Quick Start</h3>
            <p class="muted" style="margin-bottom:16px;">New to this? Follow these steps to get started:</p>
            <ol class="adminQuickStartSteps">
              <li><strong>Step 1:</strong> Enter your company/tenant name (e.g., "Acme Corp")</li>
              <li><strong>Step 2:</strong> Configure GitHub (required) - you'll need a personal access token</li>
              <li><strong>Step 3:</strong> Add at least one project with environments and services</li>
              <li><strong>Step 4:</strong> Optionally configure TeamCity, Jira, Datadog, or ArgoCD</li>
              <li><strong>Step 5:</strong> Save and run a dry-run to test your configuration</li>
            </ol>
            <p class="muted" style="margin-top:12px; font-size:12px;"><strong>Tip:</strong> You can skip optional integrations - the app will show "unknown" for missing data, but it still works!</p>
          </div>
        ` : ""}
        ${stepContent()}
        <div class="adminStepNav">
          ${adminWizardStep > 1 ? `<button type="button" class="btn ghost" id="adminPrevStep">← Previous</button>` : ""}
          ${adminWizardStep < 5 ? `<button type="button" class="btn btnPrimary" id="adminNextStep">Next →</button>` : ""}
        </div>
      </div>
      <aside class="adminAside">${previewHtml}</aside>
    </div>
    <div class="adminModalOverlay ${adminDiagnosticsOpen ? "" : "hidden"}" id="adminDiagnosticsOverlay">
      <div class="adminModal">
        <div class="adminModalHeader">
          <h3>Diagnostics</h3>
          <button type="button" class="btn ghost" id="adminDiagnosticsClose">×</button>
        </div>
        <div class="adminModalBody" id="adminDiagnosticsBody">
          <p class="muted">Run validation and connection checks. Tokens are never shown.</p>
          <div id="adminDiagnosticsResults"></div>
        </div>
      </div>
    </div>
    <div class="adminModalOverlay ${adminProjectModalOpen ? "" : "hidden"}" id="adminProjectModalOverlay">
      <div class="adminModal adminModalLarge" id="adminProjectModal">
        <div class="adminModalHeader">
          <h3>${adminEditingProjectIndex === null ? "Add project" : "Edit project"}</h3>
          <button type="button" class="btn ghost" id="adminProjectModalClose">×</button>
        </div>
        <div class="adminModalBody" id="adminProjectModalBody"></div>
      </div>
    </div>
    <div class="adminModalOverlay ${adminGroupModalOpen ? "" : "hidden"}" id="adminGroupModalOverlay">
      <div class="adminModal adminModalLarge" id="adminGroupModal">
        <div class="adminModalHeader">
          <h3>${adminEditingGroupIndex === null ? "Add group/platform" : "Edit group/platform"}</h3>
          <button type="button" class="btn ghost" id="adminGroupModalClose">×</button>
        </div>
        <div class="adminModalBody" id="adminGroupModalBody"></div>
      </div>
    </div>
    <div class="adminModalOverlay ${adminGuideModalOpen ? "" : "hidden"}" id="adminGuideModalOverlay">
      <div class="adminModal adminModalLarge" id="adminGuideModal">
        <div class="adminModalHeader">
          <h3 id="adminGuideTitle">Visual Guide</h3>
          <button type="button" class="btn ghost" id="adminGuideModalClose">×</button>
        </div>
        <div class="adminModalBody" id="adminGuideBody"></div>
      </div>
    </div>
    <input type="file" accept=".json" id="adminImportFile" style="display:none" />
  `;

  const syncDraftFromStep = () => {
    if (adminWizardStep === 1) {
      const n = el("adminTenantName"); const s = el("adminTenantSlug");
      if (n) draft.tenant = draft.tenant || {}; if (draft.tenant) { draft.tenant.name = n ? n.value.trim() : ""; draft.tenant.slug = s ? s.value.trim() : ""; }
    }
    if (adminWizardStep === 3) {
      const o = el("adminGhOrg"); const t = el("adminGhToken");
      draft.integrations = draft.integrations || {}; draft.integrations.github = { org: o ? o.value.trim() : "", token: t ? t.value.trim() : "" };
    }
    if (adminWizardStep === 4) {
      draft.integrations = draft.integrations || {};
      const v = (id) => { const e = el(id); return (e && e.value ? String(e.value) : "").trim(); };
      const tc = { baseUrl: v("adminTcUrl"), token: v("adminTcToken") };
      const jira = { baseUrl: v("adminJiraUrl"), email: v("adminJiraEmail"), token: v("adminJiraToken") };
      const dd = { site: v("adminDdSite") || "datadoghq.com", apiKey: v("adminDdApiKey"), appKey: v("adminDdAppKey") };
      const argoHosts = {};
      const argoList = el("adminArgoHostsList");
      if (argoList) {
        argoList.querySelectorAll("[data-argo-index]").forEach(item => {
          const idx = item.getAttribute("data-argo-index");
          const envInput = item.querySelector(`[data-argo-env="${idx}"]`);
          const urlInput = item.querySelector(`[data-argo-url="${idx}"]`);
          const envKey = (envInput?.value || "").trim().toUpperCase();
          const url = (urlInput?.value || "").trim();
          if (envKey && url) argoHosts[envKey] = url;
        });
      }
      draft.integrations.teamcity = tc;
      draft.integrations.jira = jira;
      draft.integrations.datadog = dd;
      draft.integrations.argocd = { envHosts: argoHosts };
    }
    if (adminWizardStep === 5) {
      const r = el("adminTicketRegex");
      draft.ticketing = draft.ticketing || {}; draft.ticketing.regex = r ? r.value.trim() : "";
    }
  };

  // Guide modal handlers - attach outside step blocks so they work from any step
  const guideModalClose = el("adminGuideModalClose");
  const guideModalOverlay = el("adminGuideModalOverlay");
  if (guideModalClose) {
    guideModalClose.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      adminGuideModalOpen = false;
      renderAdmin();
    };
  }
  if (guideModalOverlay) {
    guideModalOverlay.onclick = (e) => {
      if (e.target === guideModalOverlay || e.target.id === "adminGuideModalOverlay") {
        e.preventDefault();
        e.stopPropagation();
        adminGuideModalOpen = false;
        renderAdmin();
      }
    };
  }

  container.querySelectorAll(".adminStepperBtn").forEach(btn => {
    btn.addEventListener("click", () => { syncDraftFromStep(); adminWizardStep = parseInt(btn.getAttribute("data-step"), 10); renderAdmin(); });
  });
  const prevBtn = el("adminPrevStep");
  if (prevBtn) prevBtn.onclick = () => { syncDraftFromStep(); adminWizardStep--; renderAdmin(); };
  const nextBtn = el("adminNextStep");
  if (nextBtn) nextBtn.onclick = () => { syncDraftFromStep(); adminWizardStep++; renderAdmin(); };

  if (adminWizardStep === 1) {
    const n = el("adminTenantName"); const s = el("adminTenantSlug");
    if (n) {
      n.addEventListener("input", () => { 
        draft.tenant = draft.tenant || {}; 
        draft.tenant.name = n.value.trim();
        scheduleAutoSave();
      });
      n.addEventListener("blur", () => validateField(n, "name", (v) => v.length > 0 ? null : "Tenant name is required"));
    }
    if (s) {
      s.addEventListener("input", () => { 
        draft.tenant = draft.tenant || {}; 
        draft.tenant.slug = s.value.trim();
        scheduleAutoSave();
      });
      s.addEventListener("blur", () => validateField(s, "slug", (v) => v.length > 0 && /^[a-z0-9-]+$/.test(v) ? null : "Slug must be lowercase letters, numbers, and hyphens only"));
    }
  }
  if (adminWizardStep === 3) {
    const o = el("adminGhOrg"); const t = el("adminGhToken");
    if (o) {
      o.addEventListener("input", () => { 
        draft.integrations = draft.integrations || {}; 
        draft.integrations.github = draft.integrations.github || {}; 
        draft.integrations.github.org = o.value.trim();
        scheduleAutoSave();
      });
      o.addEventListener("blur", () => validateField(o, "org", (v) => v.length > 0 ? null : "Organization is required"));
    }
    if (t) {
      t.addEventListener("input", () => { 
        draft.integrations = draft.integrations || {}; 
        draft.integrations.github = draft.integrations.github || {}; 
        draft.integrations.github.token = t.value.trim();
        scheduleAutoSave();
        if (t.value.trim() && !t.value.trim().startsWith("ghp_")) {
          validateField(t, "token", (v) => v.length > 0 && v.startsWith("ghp_") ? null : "Token must start with ghp_");
        } else {
          const existingErr = t.parentElement.querySelector(".adminFieldError");
          if (existingErr) existingErr.remove();
          t.classList.remove("adminInputError");
        }
      });
      t.addEventListener("blur", () => validateField(t, "token", (v) => v.length > 0 && v.startsWith("ghp_") ? null : "Token must start with ghp_"));
    }
    const ghGuideBtn = el("adminGhGuideBtn");
    if (ghGuideBtn) {
      ghGuideBtn.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        adminGuideType = "github";
        adminGuideModalOpen = true;
        renderAdmin();
        setTimeout(() => renderGuideModal(), 50);
      };
    }
    const testGh = el("adminTestGitHub");
    if (testGh) testGh.onclick = async () => {
      const resEl = el("adminTestGitHubResult");
      if (resEl) { resEl.textContent = "Checking…"; resEl.className = "adminTestResult pending"; }
      syncDraftFromStep();
      try {
        const r = await fetch(ADMIN_API_BASE + "/api/admin/test/github", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ org: (draft.integrations || {}).github?.org || "", token: (draft.integrations || {}).github?.token || "" }) });
        const j = await safeJsonResponse(r);
        if (resEl) {
          if (!j) {
            resEl.textContent = "Failed to parse response";
            resEl.className = "adminTestResult warn";
          } else {
            resEl.textContent = j.ok ? "OK" : "Failed: " + (j.message || "Connection error");
            resEl.className = "adminTestResult " + (j.ok ? "ok" : "warn");
          }
        }
      } catch (e) {
        if (resEl) { resEl.textContent = "Error: " + (e.message || "Request failed"); resEl.className = "adminTestResult warn"; }
      }
    };
  }
  if (adminWizardStep === 4) {
    const ddGuideBtn = el("adminDdGuideBtn");
    if (ddGuideBtn) {
      ddGuideBtn.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        adminGuideType = "datadog";
        adminGuideModalOpen = true;
        renderAdmin();
        setTimeout(() => renderGuideModal(), 50);
      };
    }
    const jiraGuideBtn = el("adminJiraGuideBtn");
    if (jiraGuideBtn) {
      jiraGuideBtn.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        adminGuideType = "jira";
        adminGuideModalOpen = true;
        renderAdmin();
        setTimeout(() => renderGuideModal(), 50);
      };
    }
    const testTc = el("adminTestTc"); const testJira = el("adminTestJira"); const testDd = el("adminTestDd");
    const runTest = async (kind, url, body, resultId) => {
      const resEl = el(resultId);
      if (resEl) { resEl.textContent = "Checking…"; resEl.className = "adminTestResult pending"; }
      syncDraftFromStep();
      try {
        const r = await fetch(ADMIN_API_BASE + url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        const j = await safeJsonResponse(r);
        if (resEl) {
          if (!j) {
            resEl.textContent = "Failed to parse response";
            resEl.className = "adminTestResult warn";
          } else {
            resEl.textContent = j.ok ? "OK" : "Failed: " + (j.message || "Connection error");
            resEl.className = "adminTestResult " + (j.ok ? "ok" : "warn");
          }
        }
      } catch (e) {
        if (resEl) { resEl.textContent = "Error: " + (e.message || "Request failed"); resEl.className = "adminTestResult warn"; }
      }
    };
    const v = (id) => { const e = el(id); return (e && e.value ? String(e.value) : "").trim(); };
    if (testTc) testTc.onclick = () => runTest("tc", "/api/admin/test/teamcity", { baseUrl: v("adminTcUrl"), token: v("adminTcToken") }, "adminTestTcResult");
    if (testJira) testJira.onclick = () => runTest("jira", "/api/admin/test/jira", { baseUrl: v("adminJiraUrl"), email: v("adminJiraEmail"), token: v("adminJiraToken") }, "adminTestJiraResult");
    if (testDd) testDd.onclick = () => runTest("dd", "/api/admin/test/datadog", { site: v("adminDdSite") || "datadoghq.com", apiKey: v("adminDdApiKey"), appKey: v("adminDdAppKey") }, "adminTestDdResult");
    
    const argoList = el("adminArgoHostsList");
    const addArgoBtn = el("adminAddArgoHost");
    if (addArgoBtn) addArgoBtn.onclick = () => {
      if (!argoList) return;
      const idx = Object.keys((draft.integrations || {}).argocd?.envHosts || {}).length;
      const div = document.createElement("div");
      div.className = "adminListItem";
      div.setAttribute("data-argo-index", idx);
      div.innerHTML = `
        <input type="text" class="adminInput adminInputSmall" placeholder="Environment key (e.g. dev)" value="" data-argo-env="${idx}" />
        <input type="text" class="adminInput adminInputSmall" placeholder="ArgoCD URL" value="" data-argo-url="${idx}" />
        <button type="button" class="btn ghost adminListItemBtn adminIconRemove" data-remove-argo="${idx}" title="Remove">×</button>
      `;
      argoList.appendChild(div);
      div.querySelector(`[data-remove-argo="${idx}"]`).onclick = () => { div.remove(); };
    };
    if (argoList) {
      argoList.querySelectorAll("[data-remove-argo]").forEach(btn => {
        btn.addEventListener("click", () => {
          const item = btn.closest("[data-argo-index]");
          if (item) item.remove();
        });
      });
    }
  }
  if (adminWizardStep === 5) {
    const r = el("adminTicketRegex");
    if (r) {
      r.addEventListener("input", () => { 
        draft.ticketing = draft.ticketing || {}; 
        draft.ticketing.regex = r.value.trim();
        scheduleAutoSave();
        if (r.value.trim()) {
          try {
            new RegExp(r.value.trim());
            const existingErr = r.parentElement.querySelector(".adminFieldError");
            if (existingErr) existingErr.remove();
            r.classList.remove("adminInputError");
          } catch (e) {
            validateField(r, "regex", () => "Invalid regex pattern");
          }
        }
      });
      r.addEventListener("blur", () => {
        if (r.value.trim()) {
          try {
            new RegExp(r.value.trim());
            const existingErr = r.parentElement.querySelector(".adminFieldError");
            if (existingErr) existingErr.remove();
            r.classList.remove("adminInputError");
          } catch (e) {
            validateField(r, "regex", () => "Invalid regex pattern");
          }
        }
      });
    }
    // Fix buttons
    container.querySelectorAll(".adminFixBtn").forEach(btn => {
      btn.addEventListener("click", () => {
        const step = btn.getAttribute("data-fix-step");
        const field = btn.getAttribute("data-fix-field");
        if (step) {
          adminWizardStep = parseInt(step, 10);
          renderAdmin();
          setTimeout(() => {
            const fieldEl = el(field);
            if (fieldEl) {
              fieldEl.focus();
              fieldEl.scrollIntoView({ behavior: "smooth", block: "center" });
            }
          }, 100);
        }
      });
    });
    const diagBtn = el("adminDiagnosticsBtn");
    if (diagBtn) diagBtn.onclick = () => { syncDraftFromStep(); adminDiagnosticsOpen = true; renderAdmin(); runAdminDiagnostics(); };
    const closeDiag = el("adminDiagnosticsClose");
    if (closeDiag) closeDiag.onclick = () => { adminDiagnosticsOpen = false; renderAdmin(); };
    const overlay = el("adminDiagnosticsOverlay");
    if (overlay) overlay.addEventListener("click", (e) => { if (e.target === overlay) { adminDiagnosticsOpen = false; renderAdmin(); } });
    const dryRunBtn = el("adminDryRunBtn");
    if (dryRunBtn) dryRunBtn.onclick = async () => {
      adminDryRunLoading = true; adminDryRunResult = null; renderAdmin();
      syncDraftFromStep();
      try {
        const res = await fetch(ADMIN_API_BASE + "/api/admin/snapshot/dry-run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ config: draft }) });
        const j = await safeJsonResponse(res);
        adminDryRunResult = j || { ok: false, message: "Failed to parse response" };
      } catch (e) {
        adminDryRunResult = { ok: false, message: e.message || "Request failed" };
      }
      adminDryRunLoading = false;
      renderAdmin();
    };
    const saveBtn = el("adminSaveBtn");
    if (saveBtn) saveBtn.onclick = () => {
      syncDraftFromStep();
      const v = AdminConfig.validate(draft);
      if (!v.valid) return;
      AdminConfig.save(draft);
      adminDraft = null;
      render();
    };
    const exportBtn = el("adminExportBtn");
    if (exportBtn) exportBtn.onclick = () => {
      syncDraftFromStep();
      const blob = new Blob([AdminConfig.exportJson(draft)], { type: "application/json" });
      const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "tenant-config.json"; a.click(); URL.revokeObjectURL(a.href);
    };
    const importBtn = el("adminImportBtn");
    const importFile = el("adminImportFile");
    if (importBtn && importFile) {
      importBtn.onclick = () => importFile.click();
      importFile.onchange = () => {
        const f = importFile.files && importFile.files[0];
        if (!f) return;
        const reader = new FileReader();
        reader.onload = () => {
          const data = AdminConfig.importJson(reader.result);
          if (data) { adminDraft = data; renderAdmin(); }
        };
        reader.readAsText(f);
        importFile.value = "";
      };
    }
  }

  if (adminWizardStep === 3) {
    const addGroupBtn = el("adminAddGroup");
    if (addGroupBtn) addGroupBtn.onclick = () => {
      adminEditingGroupIndex = null;
      adminGroupModalOpen = true;
      renderAdmin();
      renderGroupModal();
    };
    const addProjectBtn = el("adminAddProject");
    if (addProjectBtn) addProjectBtn.onclick = () => {
      adminEditingProjectIndex = null;
      adminProjectModalOpen = true;
      renderAdmin();
      renderProjectModal();
    };
    container.querySelectorAll("[data-edit-project]").forEach(btn => {
      btn.addEventListener("click", () => {
        const idx = btn.getAttribute("data-edit-project");
        adminEditingProjectIndex = idx;
        adminProjectModalOpen = true;
        renderAdmin();
        renderProjectModal();
      });
    });
    container.querySelectorAll("[data-delete-project]").forEach(btn => {
      btn.addEventListener("click", () => {
        const idx = btn.getAttribute("data-delete-project");
        const [gi, pi] = idx.includes(":") ? idx.split(":").map(Number) : [Number(idx), null];
        const groups = draft.groups || [];
        let proj = null;
        if (pi !== null) {
          const g = groups[gi];
          if (g && g.type === "group" && g.projects) proj = g.projects[pi];
        } else {
          proj = groups[gi];
        }
        if (!proj) return;
        if (confirm(`Delete project "${proj.name || proj.key}"? This cannot be undone.`)) {
          if (pi !== null) {
            groups[gi].projects.splice(pi, 1);
          } else {
            groups.splice(gi, 1);
          }
          renderAdmin();
        }
      });
    });
    container.querySelectorAll("[data-edit-group]").forEach(btn => {
      btn.addEventListener("click", () => {
        const gi = parseInt(btn.getAttribute("data-edit-group"), 10);
        adminEditingGroupIndex = gi;
        adminGroupModalOpen = true;
        renderAdmin();
        renderGroupModal();
      });
    });
    container.querySelectorAll("[data-delete-group]").forEach(btn => {
      btn.addEventListener("click", () => {
        const gi = parseInt(btn.getAttribute("data-delete-group"), 10);
        const groups = draft.groups || [];
        const g = groups[gi];
        if (!g || g.type !== "group") return;
        if (confirm(`Delete group "${g.name || g.key}" and all its projects? This cannot be undone.`)) {
          groups.splice(gi, 1);
          renderAdmin();
        }
      });
    });
    container.querySelectorAll("[data-add-project-to-group]").forEach(btn => {
      btn.addEventListener("click", () => {
        const gi = parseInt(btn.getAttribute("data-add-project-to-group"), 10);
        adminEditingProjectIndex = `group:${gi}`;
        adminProjectModalOpen = true;
        renderAdmin();
        renderProjectModal();
      });
    });
    const projectModalClose = el("adminProjectModalClose");
    const projectModalOverlay = el("adminProjectModalOverlay");
    if (projectModalClose) projectModalClose.onclick = () => { adminProjectModalOpen = false; renderAdmin(); };
    if (projectModalOverlay) projectModalOverlay.addEventListener("click", (e) => {
      if (e.target === projectModalOverlay) { adminProjectModalOpen = false; renderAdmin(); }
    });
    const groupModalClose = el("adminGroupModalClose");
    const groupModalOverlay = el("adminGroupModalOverlay");
    if (groupModalClose) groupModalClose.onclick = () => { adminGroupModalOpen = false; renderAdmin(); };
    if (groupModalOverlay) groupModalOverlay.addEventListener("click", (e) => {
      if (e.target === groupModalOverlay) { adminGroupModalOpen = false; renderAdmin(); }
    });
  }
}

function renderGroupModal() {
  const body = el("adminGroupModalBody");
  if (!body) return;
  const draft = getAdminDraft();
  const groups = draft.groups || [];
  const group = adminEditingGroupIndex === null ? null : groups[adminEditingGroupIndex];
  const isNew = adminEditingGroupIndex === null;
  
  body.innerHTML = `
    <div class="adminProjectForm">
      <div class="adminForm">
        <label class="adminLabel">Group/Platform key <span class="muted">(required)</span> <span class="adminTooltip" title="A short identifier for the group (e.g. PROJ, TEAM). Used in navigation.">?</span></label>
        <input type="text" id="adminGroupKey" class="adminInput" placeholder="e.g. PROJ or TEAM" value="${escapeHtml(String(group?.key || ""))}" ${isNew ? "" : "readonly"} style="text-transform:uppercase;" />
        <span class="adminHint">${isNew ? "Examples: <code>PROJ</code>, <code>TEAM</code>. Use uppercase, letters, numbers, underscores. Cannot be changed later." : "Key cannot be changed after creation."}</span>
      </div>
      <div class="adminForm">
        <label class="adminLabel">Display name <span class="muted">(required)</span></label>
        <input type="text" id="adminGroupName" class="adminInput" placeholder="e.g. PROJ or TEAM" value="${escapeHtml(String(group?.name || ""))}" />
        <span class="adminHint">How this group appears in the UI navigation.</span>
      </div>
      <div class="adminProjectFormActions">
        <button type="button" class="btn btnPrimary" id="adminSaveGroup">Save group</button>
        <button type="button" class="btn ghost" id="adminCancelGroup">Cancel</button>
      </div>
    </div>
  `;
  
  const saveBtn = el("adminSaveGroup");
  if (saveBtn) saveBtn.onclick = () => {
    const key = (el("adminGroupKey")?.value || "").trim().toUpperCase();
    const name = (el("adminGroupName")?.value || "").trim();
    if (!key) { alert("Group key is required."); return; }
    if (!name) { alert("Display name is required."); return; }
    
    draft.groups = draft.groups || [];
    if (isNew) {
      if (draft.groups.some(g => g.key === key)) {
        alert(`Group with key "${key}" already exists. Choose a different key.`);
        return;
      }
      draft.groups.push({ type: "group", key, name, projects: [] });
    } else {
      const existing = draft.groups[adminEditingGroupIndex];
      if (existing && existing.key !== key && draft.groups.some(g => g.key === key)) {
        alert(`Group with key "${key}" already exists.`);
        return;
      }
      Object.assign(existing, { name });
    }
    adminGroupModalOpen = false;
    renderAdmin();
  };
  
  const cancelBtn = el("adminCancelGroup");
  if (cancelBtn) cancelBtn.onclick = () => { adminGroupModalOpen = false; renderAdmin(); };
}

function renderProjectModal() {
  const body = el("adminProjectModalBody");
  if (!body) return;
  const draft = getAdminDraft();
  const groups = draft.groups || [];
  let proj = null;
  let isInGroup = false;
  let groupIndex = null;
  
  if (adminEditingProjectIndex === null) {
    // New project
  } else if (typeof adminEditingProjectIndex === "string" && adminEditingProjectIndex.startsWith("group:")) {
    // New project in group
    groupIndex = parseInt(adminEditingProjectIndex.split(":")[1], 10);
    isInGroup = true;
  } else if (typeof adminEditingProjectIndex === "string" && adminEditingProjectIndex.includes(":")) {
    // Edit project in group
    const [gi, pi] = adminEditingProjectIndex.split(":").map(Number);
    const g = groups[gi];
    if (g && g.type === "group" && g.projects) {
      proj = g.projects[pi];
      isInGroup = true;
      groupIndex = gi;
    }
  } else {
    // Edit standalone project
    const idx = typeof adminEditingProjectIndex === "number" ? adminEditingProjectIndex : parseInt(adminEditingProjectIndex, 10);
    proj = groups[idx];
  }
  
  const isNew = proj === null;
  
  const envs = (proj?.environments || []).map((e, i) => `
    <div class="adminListItem" data-env-index="${i}">
      <input type="text" class="adminInput adminInputSmall" placeholder="key" value="${escapeHtml(String(e.key || ""))}" data-env-key="${i}" title="Environment key (e.g. dev, qa, prod)" />
      <input type="text" class="adminInput adminInputSmall" placeholder="Display name" value="${escapeHtml(String(e.name || ""))}" data-env-name="${i}" title="Display name (e.g. Development, QA, Production)" />
      <input type="text" class="adminInput adminInputSmall" placeholder="URL (optional)" value="${escapeHtml(String(e.cmsUrl || e.url || ""))}" data-env-url="${i}" title="Optional: URL to CMS or other tool. Shows arrow next to env name." style="flex: 1.5;" />
      <input type="text" class="adminInput adminInputSmall" placeholder="Link label (optional)" value="${escapeHtml(String(e.cmsLabel || ""))}" data-env-label="${i}" title="Optional: Label for the link (default: CMS)" style="flex: 1;" />
      <button type="button" class="btn ghost adminListItemBtn adminIconRemove" data-remove-env="${i}" title="Remove">×</button>
    </div>
  `).join("") || '<div class="muted" style="padding:8px 0;">No environments yet. Add at least one.</div>';
  
  const services = (proj?.services || []).map((s, i) => `
    <div class="adminListItem" data-service-index="${i}">
      <input type="text" class="adminInput adminInputSmall" placeholder="Service key" value="${escapeHtml(String(s.key || ""))}" data-svc-key="${i}" title="Short identifier (e.g. api, frontend)" />
      <input type="text" class="adminInput adminInputSmall" placeholder="Code repo" value="${escapeHtml(String(s.codeRepo || ""))}" data-svc-code="${i}" title="GitHub repo name with app code" />
      <input type="text" class="adminInput adminInputSmall" placeholder="Infra repo" value="${escapeHtml(String(s.infraRepo || ""))}" data-svc-infra="${i}" title="GitHub repo name with deployment config (kustomization)" />
      <input type="text" class="adminInput adminInputSmall" placeholder="TeamCity build ID" value="${escapeHtml(String(s.teamcityBuildTypeId || ""))}" data-svc-tc="${i}" style="font-size:11px;" title="TeamCity build configuration ID - can differ from GitHub repo name" />
      <button type="button" class="btn ghost adminListItemBtn adminIconRemove" data-remove-service="${i}" title="Remove">×</button>
    </div>
  `).join("") || '<div class="muted" style="padding:8px 0;">No services yet. Add at least one service to track deployments.</div>';
  
  body.innerHTML = `
    <div class="adminProjectForm">
          <div class="adminForm">
            <label class="adminLabel">Project key <span class="muted">(required)</span> <span class="adminTooltip" title="A short identifier (e.g. P1, PLATFORM, API). Used in config files. Must be unique. Use uppercase letters, numbers, underscores only.">?</span></label>
            <input type="text" id="adminProjKey" class="adminInput" placeholder="e.g. PLATFORM or API_V1" value="${escapeHtml(String(proj?.key || ""))}" ${isNew ? "" : "readonly"} style="text-transform:uppercase;" />
            <span class="adminHint">${isNew ? "Examples: <code>PLATFORM</code>, <code>API_V1</code>, <code>FRONTEND</code>. Use uppercase, letters, numbers, underscores. Cannot be changed later." : "Key cannot be changed after creation."}</span>
          </div>
          <div class="adminForm">
            <label class="adminLabel">Display name <span class="muted">(required)</span></label>
            <input type="text" id="adminProjName" class="adminInput" placeholder="e.g. Platform or Product A" value="${escapeHtml(String(proj?.name || ""))}" />
            <span class="adminHint">How this project appears in the UI. Examples: "Platform", "Product A", "Main API".</span>
          </div>
          <div class="adminForm">
            <label class="adminLabel">Infra branch <span class="adminTooltip" title="The Git branch where your infrastructure code (kustomization files) lives. Usually 'main' or 'master'. We read from this branch to determine what's deployed.">?</span></label>
            <input type="text" id="adminProjInfraRef" class="adminInput" placeholder="main" value="${escapeHtml(String(proj?.infraRef || "main"))}" />
            <span class="adminHint">Default: <code>main</code>. Change if your infra repos use a different default branch (e.g. <code>master</code>).</span>
          </div>
          <div class="adminForm" style="margin-top:24px;">
            <div style="display:flex; align-items:center; gap:8px; margin-bottom:8px;">
              <label class="adminLabel" style="margin:0;">Ticket prefixes <span class="muted">(optional)</span> <span class="adminTooltip" title="Jira/ticket prefixes for this project (e.g. PROJ, TEAM). Used by runbooks and ticket tracker. Add one per field.">?</span></label>
              <button type="button" class="btn ghost" id="adminAddTicketPrefix" style="font-size:12px; padding:4px 10px;">+ Add</button>
            </div>
            <div class="adminListContainer" id="adminTicketPrefixesList">${(Array.isArray(proj?.ticketPrefixes) ? proj.ticketPrefixes : []).length ? (proj.ticketPrefixes || []).map((p, i) => `
              <div class="adminListItem" data-ticket-prefix-index="${i}">
                <input type="text" class="adminInput adminInputSmall" placeholder="e.g. PROJ" value="${escapeHtml(String(p || ""))}" data-prefix-val="${i}" />
                <button type="button" class="btn ghost adminListItemBtn adminIconRemove" data-remove-prefix="${i}" title="Remove">×</button>
              </div>
            `).join("") : `<div class="adminListItem" data-ticket-prefix-index="0">
                <input type="text" class="adminInput adminInputSmall" placeholder="e.g. PROJ" value="" data-prefix-val="0" />
                <button type="button" class="btn ghost adminListItemBtn adminIconRemove" data-remove-prefix="0" title="Remove" disabled>×</button>
              </div>`}</div>
            <span class="adminHint">Used by runbooks and ticket tracker. At least one field; leave empty to match all.</span>
          </div>
          <div class="adminForm" style="margin-top:24px;">
            <div style="display:flex; align-items:center; gap:8px; margin-bottom:8px;">
              <label class="adminLabel" style="margin:0;">Environments <span class="muted">(required, ≥1)</span> <span class="adminTooltip" title="Environments are where your services run (dev, qa, staging, prod, etc.). Each environment should have a kustomization file in your infra repo at envs/{env}/kustomization.yaml">?</span></label>
              <button type="button" class="btn ghost" id="adminAddEnv" style="font-size:12px; padding:4px 10px;">+ Add</button>
            </div>
            <div class="adminListContainer" id="adminEnvsList">${envs}</div>
            <span class="adminHint">
              <strong>Examples:</strong> Key: <code>dev</code> → Name: "Development", Key: <code>prod</code> → Name: "Production".<br/>
              The key must match the folder name in your infra repo: <code>envs/{key}/kustomization.yaml</code><br/>
              <strong>Optional:</strong> Add a URL (e.g., CMS, dashboard) and optional label. This will show a clickable arrow (▾) next to the environment name that expands to show the link.
            </span>
          </div>
        <div class="adminForm" style="margin-top:24px;">
          <div style="display:flex; align-items:center; gap:8px; margin-bottom:8px;">
            <label class="adminLabel" style="margin:0;">Services <span class="muted">(required, ≥1)</span> <span class="adminTooltip" title="A service is an application or component. Each service has a code repository (where the app code lives) and an infra repository (where deployment config like kustomization.yaml lives).">?</span></label>
            <button type="button" class="btn ghost" id="adminAddService" style="font-size:12px; padding:4px 10px;">+ Add</button>
          </div>
          <div class="adminListContainer" id="adminServicesList">${services}</div>
          <span class="adminHint">
            <strong>Example:</strong> Service key: <code>api</code>, Code repo: <code>my-api</code>, Infra repo: <code>my-api-infra</code>.<br/>
            The infra repo should contain <code>envs/{environment}/kustomization.yaml</code> files that define what's deployed.<br/>
            <strong>GitHub vs TeamCity:</strong> Repo names can differ from TeamCity build IDs (e.g. GitHub: <code>my-frontend</code>, TeamCity: <code>MyProject_Frontend_DockerBuildAndPush</code>). Specify both explicitly - no guessing.
          </span>
        </div>
      <div class="adminProjectFormActions">
        <button type="button" class="btn btnPrimary" id="adminSaveProject">Save project</button>
        <button type="button" class="btn ghost" id="adminCancelProject">Cancel</button>
      </div>
    </div>
  `;
  
  const saveBtn = el("adminSaveProject");
  if (saveBtn) saveBtn.onclick = () => {
    const key = (el("adminProjKey")?.value || "").trim();
    const name = (el("adminProjName")?.value || "").trim();
    const infraRef = (el("adminProjInfraRef")?.value || "").trim() || "main";
    if (!key) { alert("Project key is required."); return; }
    if (!name) { alert("Display name is required."); return; }
    
    const envs = [];
    body.querySelectorAll("[data-env-index]").forEach(item => {
      const idx = parseInt(item.getAttribute("data-env-index"), 10);
      const keyInput = item.querySelector(`[data-env-key="${idx}"]`);
      const nameInput = item.querySelector(`[data-env-name="${idx}"]`);
      const urlInput = item.querySelector(`[data-env-url="${idx}"]`);
      const labelInput = item.querySelector(`[data-env-label="${idx}"]`);
      const envKey = (keyInput?.value || "").trim().toLowerCase();
      const envName = (nameInput?.value || "").trim() || envKey;
      const envUrl = (urlInput?.value || "").trim();
      const envLabel = (labelInput?.value || "").trim();
      if (envKey) {
        const env = { key: envKey, name: envName };
        if (envUrl) {
          env.cmsUrl = envUrl;
          if (envLabel) env.cmsLabel = envLabel;
        }
        envs.push(env);
      }
    });
    if (envs.length === 0) { alert("At least one environment is required."); return; }

    const ticketPrefixes = [];
    body.querySelectorAll("[data-ticket-prefix-index]").forEach((item) => {
      const inp = item.querySelector("[data-prefix-val]");
      const v = (inp?.value || "").trim().replace(/[^\w]/g, "");
      if (v) ticketPrefixes.push(v);
    });

    const services = [];
    body.querySelectorAll("[data-service-index]").forEach(item => {
      const idx = parseInt(item.getAttribute("data-service-index"), 10);
      const keyInput = item.querySelector(`[data-svc-key="${idx}"]`);
      const codeInput = item.querySelector(`[data-svc-code="${idx}"]`);
      const infraInput = item.querySelector(`[data-svc-infra="${idx}"]`);
      const tcInput = item.querySelector(`[data-svc-tc="${idx}"]`);
      const svcKey = (keyInput?.value || "").trim();
      const codeRepo = (codeInput?.value || "").trim();
      const infraRepo = (infraInput?.value || "").trim();
      const tcBuildId = (tcInput?.value || "").trim();
      if (svcKey && codeRepo && infraRepo) {
        const svc = { key: svcKey, codeRepo, infraRepo };
        if (tcBuildId) svc.teamcityBuildTypeId = tcBuildId;
        // argoApp defaults to service key if not specified (handled by snapshot)
        services.push(svc);
      }
    });
    if (services.length === 0) { alert("At least one service is required."); return; }
    
    draft.groups = draft.groups || [];
    
    // Check for duplicate keys across all projects
    const allProjects = draft.groups.flatMap(g => g.type === "group" ? (g.projects || []) : [g]).filter(p => p.type !== "group");
    if (allProjects.some(p => p.key === key && (isNew || (adminEditingProjectIndex && !adminEditingProjectIndex.toString().includes(":"))))) {
      alert(`Project with key "${key}" already exists. Choose a different key.`);
      return;
    }
    
    if (isNew) {
      if (isInGroup && groupIndex !== null) {
        // Add to group
        const g = draft.groups[groupIndex];
        if (g && g.type === "group") {
          g.projects = g.projects || [];
          g.projects.push({ key, name, infraRef, ticketPrefixes: ticketPrefixes.length ? ticketPrefixes : undefined, environments: envs, services });
        }
      } else {
        // Add as standalone project
        draft.groups.push({ key, name, infraRef, ticketPrefixes: ticketPrefixes.length ? ticketPrefixes : undefined, environments: envs, services });
      }
    } else {
      if (isInGroup && groupIndex !== null) {
        // Edit project in group
        const g = draft.groups[groupIndex];
        if (g && g.type === "group" && g.projects) {
          const [gi, pi] = adminEditingProjectIndex.split(":").map(Number);
          const existing = g.projects[pi];
          if (existing && existing.key !== key && allProjects.some(p => p.key === key)) {
            alert(`Project with key "${key}" already exists.`);
            return;
          }
          Object.assign(existing, { name, infraRef, ticketPrefixes: ticketPrefixes.length ? ticketPrefixes : undefined, environments: envs, services });
        }
      } else {
        // Edit standalone project
        const idx = typeof adminEditingProjectIndex === "number" ? adminEditingProjectIndex : parseInt(adminEditingProjectIndex, 10);
        const existing = draft.groups[idx];
        if (existing && existing.key !== key && allProjects.some(p => p.key === key)) {
          alert(`Project with key "${key}" already exists.`);
          return;
        }
        Object.assign(existing, { name, infraRef, ticketPrefixes: ticketPrefixes.length ? ticketPrefixes : undefined, environments: envs, services });
      }
    }
    adminProjectModalOpen = false;
    renderAdmin();
  };
  
  const cancelBtn = el("adminCancelProject");
  if (cancelBtn) cancelBtn.onclick = () => { adminProjectModalOpen = false; renderAdmin(); };
  
  const addEnvBtn = el("adminAddEnv");
  if (addEnvBtn) addEnvBtn.onclick = () => {
    const list = el("adminEnvsList");
    if (!list) return;
    const idx = (proj?.environments || []).length;
    const div = document.createElement("div");
    div.className = "adminListItem";
    div.setAttribute("data-env-index", idx);
    div.innerHTML = `
      <input type="text" class="adminInput adminInputSmall" placeholder="key" value="" data-env-key="${idx}" title="Environment key (e.g. dev, qa, prod)" />
      <input type="text" class="adminInput adminInputSmall" placeholder="Display name" value="" data-env-name="${idx}" title="Display name (e.g. Development, QA, Production)" />
      <input type="text" class="adminInput adminInputSmall" placeholder="URL (optional)" value="" data-env-url="${idx}" title="Optional: URL to CMS or other tool. Shows arrow next to env name." style="flex: 1.5;" />
      <input type="text" class="adminInput adminInputSmall" placeholder="Link label (optional)" value="" data-env-label="${idx}" title="Optional: Label for the link (default: CMS)" style="flex: 1;" />
      <button type="button" class="btn ghost adminListItemBtn adminIconRemove" data-remove-env="${idx}" title="Remove">×</button>
    `;
    list.appendChild(div);
    div.querySelector(`[data-remove-env="${idx}"]`).onclick = () => { div.remove(); };
  };
  
  const addServiceBtn = el("adminAddService");
  if (addServiceBtn) addServiceBtn.onclick = () => {
    const list = el("adminServicesList");
    if (!list) return;
    const idx = (proj?.services || []).length;
    const div = document.createElement("div");
    div.className = "adminListItem";
    div.setAttribute("data-service-index", idx);
    div.innerHTML = `
      <input type="text" class="adminInput adminInputSmall" placeholder="Service key" value="" data-svc-key="${idx}" title="Short identifier (e.g. api, frontend)" />
      <input type="text" class="adminInput adminInputSmall" placeholder="Code repo" value="" data-svc-code="${idx}" title="GitHub repo name with app code" />
      <input type="text" class="adminInput adminInputSmall" placeholder="Infra repo" value="" data-svc-infra="${idx}" title="GitHub repo name with deployment config" />
      <input type="text" class="adminInput adminInputSmall" placeholder="TeamCity build ID (optional)" value="" data-svc-tc="${idx}" style="font-size:11px;" title="TeamCity build configuration ID" />
      <button type="button" class="btn ghost adminListItemBtn adminIconRemove" data-remove-service="${idx}" title="Remove">×</button>
    `;
    list.appendChild(div);
    div.querySelector(`[data-remove-service="${idx}"]`).onclick = () => { div.remove(); };
  };
  
  body.querySelectorAll("[data-remove-env]").forEach(btn => {
    btn.addEventListener("click", () => {
      const item = btn.closest("[data-env-index]");
      if (item) item.remove();
    });
  });
  
  body.querySelectorAll("[data-remove-service]").forEach(btn => {
    btn.addEventListener("click", () => {
      const item = btn.closest("[data-service-index]");
      if (item) item.remove();
    });
  });

  const addTicketPrefixBtn = body.querySelector("#adminAddTicketPrefix");
  if (addTicketPrefixBtn) {
    addTicketPrefixBtn.onclick = () => {
      const list = body.querySelector("#adminTicketPrefixesList");
      if (!list) return;
      const items = list.querySelectorAll("[data-ticket-prefix-index]");
      const idx = items.length;
      const div = document.createElement("div");
      div.className = "adminListItem";
      div.setAttribute("data-ticket-prefix-index", idx);
      div.innerHTML = `
        <input type="text" class="adminInput adminInputSmall" placeholder="e.g. PROJ" value="" data-prefix-val="${idx}" />
        <button type="button" class="btn ghost adminListItemBtn adminIconRemove" data-remove-prefix="${idx}" title="Remove">×</button>
      `;
      list.appendChild(div);
      div.querySelector(`[data-remove-prefix="${idx}"]`).onclick = () => {
        if (list.querySelectorAll("[data-ticket-prefix-index]").length > 1) div.remove();
      };
      list.querySelectorAll("[data-remove-prefix]").forEach(b => { if (list.querySelectorAll("[data-ticket-prefix-index]").length > 1) b.disabled = false; });
    };
  }
  body.querySelectorAll("[data-remove-prefix]").forEach(btn => {
    btn.addEventListener("click", () => {
      const list = body.querySelector("#adminTicketPrefixesList");
      const item = btn.closest("[data-ticket-prefix-index]");
      if (item && list && list.querySelectorAll("[data-ticket-prefix-index]").length > 1) {
        item.remove();
        list.querySelectorAll("[data-remove-prefix]").forEach(b => {
          b.disabled = list.querySelectorAll("[data-ticket-prefix-index]").length <= 1;
        });
      }
    });
  });
}

async function runAdminDiagnostics() {
  const elId = "adminDiagnosticsResults";
  const resEl = document.getElementById(elId);
  if (!resEl) return;
  resEl.innerHTML = "<p class=\"muted\">Running checks…</p>";
  const draft = getAdminDraft();
  const results = [];
  const add = (name, ok, msg) => results.push({ name, ok, msg });

  const v = AdminConfig.validate(draft);
  add("Config validation", v.valid, v.valid ? "OK" : (v.errors || []).map(e => e.message).join("; "));
  const gh = (draft.integrations || {}).github || {};
  if (gh.org && gh.token) {
    try {
      const r = await fetch(ADMIN_API_BASE + "/api/admin/test/github", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ org: gh.org, token: gh.token }) });
      const j = await safeJsonResponse(r);
      if (!j) {
        add("GitHub", false, "Failed to parse response");
      } else {
        add("GitHub", j.ok, j.message || (j.ok ? "OK" : "Failed"));
      }
    } catch (e) {
      add("GitHub", false, e.message || "Request failed");
    }
  } else {
    add("GitHub", false, "Missing org or token");
  }
  const tc = (draft.integrations || {}).teamcity || {};
  if (tc.baseUrl && tc.token) {
    try {
      const r = await fetch(ADMIN_API_BASE + "/api/admin/test/teamcity", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ baseUrl: tc.baseUrl, token: tc.token }) });
      const j = await safeJsonResponse(r);
      if (!j) {
        add("TeamCity", false, "Failed to parse response");
      } else {
        add("TeamCity", j.ok, j.message || (j.ok ? "OK" : "Failed"));
      }
    } catch (e) {
      add("TeamCity", false, e.message || "Request failed");
    }
  } else {
    add("TeamCity", null, "Skipped (optional)");
  }
  const jira = (draft.integrations || {}).jira || {};
  if (jira.baseUrl && jira.email && jira.token) {
    try {
      const r = await fetch(ADMIN_API_BASE + "/api/admin/test/jira", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ baseUrl: jira.baseUrl, email: jira.email, token: jira.token }) });
      const j = await safeJsonResponse(r);
      if (!j) {
        add("Jira", false, "Failed to parse response");
      } else {
        add("Jira", j.ok, j.message || (j.ok ? "OK" : "Failed"));
      }
    } catch (e) {
      add("Jira", false, e.message || "Request failed");
    }
  } else {
    add("Jira", null, "Skipped (optional)");
  }
  const dd = (draft.integrations || {}).datadog || {};
  if (dd.apiKey && dd.appKey) {
    try {
      const r = await fetch(ADMIN_API_BASE + "/api/admin/test/datadog", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ site: dd.site || "datadoghq.com", apiKey: dd.apiKey, appKey: dd.appKey }) });
      const j = await safeJsonResponse(r);
      if (!j) {
        add("Datadog", false, "Failed to parse response");
      } else {
        add("Datadog", j.ok, j.message || (j.ok ? "OK" : "Failed"));
      }
    } catch (e) {
      add("Datadog", false, e.message || "Request failed");
    }
  } else {
    add("Datadog", null, "Skipped (optional)");
  }

  resEl.innerHTML = results.map(({ name, ok, msg }) => {
    const cls = ok === true ? "ok" : ok === false ? "warn" : "muted";
    return `<div class="adminDiagRow ${cls}"><strong>${escapeHtml(name)}</strong> ${escapeHtml(msg)}</div>`;
  }).join("");
}

// ---------- Main render ----------
function render() {
  try {
    renderLeftNav();
    bindSidebarToggle();
    bindToolsNav();
    renderHeader();

  // MVP0.5 rule: Right sidebar exists only on project views
  applySidebarVisibility();
  if (isProjectSidebarAllowed()) {
    renderRightSidebar();
  }

  const envBlock = el("envBlock");
  const relBlock = el("releasesBlock");

  if (envBlock) envBlock.style.display = currentView === "env" ? "" : "none";
  if (relBlock) relBlock.style.display = currentView === "releases" ? "" : "none";

  const ovBlock = el("overviewBlock");
  if (ovBlock) ovBlock.style.display = currentView === "overview" ? "" : "none";

  const historyBlock = el("historyBlock");
  const ticketBlock  = el("ticketBlock");
  const statsBlock = el("statsBlock");
  const setupBlock = el("setupBlock");

  if (historyBlock) historyBlock.style.display = currentView === "history" ? "" : "none";
  if (ticketBlock)  ticketBlock.style.display  = currentView === "ticket"  ? "" : "none";
  if (statsBlock)   statsBlock.style.display   = currentView === "stats"   ? "" : "none";
  if (setupBlock)   setupBlock.style.display   = currentView === "setup"   ? "" : "none";


  if (currentView === "overview") {
    renderOverview();

    // hide compare/details areas on overview
    const compareBar = el("compareBar");
    const compare = el("compare");
    const details = el("details");
    if (compareBar) compareBar.innerHTML = "";
    if (compare) compare.innerHTML = "";
    if (details) details.innerHTML = "";
    return;
  }

  if (currentView === "env") {
    // Always show env cards so user can select 3rd/4th env for compare
    const envBlockEl = el("envBlock");
    if (envBlockEl) envBlockEl.style.display = "";
    renderEnvCards();

    if (compareSelected.size >= 2) {
      // Compare mode: show compare, hide details
      detailsOpenEnvKey = null;
      renderCompareBar();
      renderCompare();
      const details = el("details");
      if (details) details.innerHTML = "";
    } else {
      // Not comparing: hide compare panels, show details (if any)
      const compareBar = el("compareBar");
      const compare = el("compare");
      if (compareBar) compareBar.innerHTML = "";
      if (compare) compare.innerHTML = "";
      renderEnvDetails();
    }
  }

  if (currentView === "releases") {
    renderReleases();
    const compareBar = el("compareBar");
    const compare = el("compare");
    const details = el("details");
    if (compareBar) compareBar.innerHTML = "";
    if (compare) compare.innerHTML = "";
    if (details) details.innerHTML = "";
  }
  if (currentView === "history") {
  renderHistory();
  // wyczyść compare/details żeby nie zostały z env
  const compareBar = el("compareBar");
  const compare = el("compare");
  const details = el("details");
  if (compareBar) compareBar.innerHTML = "";
  if (compare) compare.innerHTML = "";
  if (details) details.innerHTML = "";
  return;
}

if (currentView === "ticket") {
  renderTicketTracker();
  const compareBar = el("compareBar");
  const compare = el("compare");
  const details = el("details");
  if (compareBar) compareBar.innerHTML = "";
  if (compare) compare.innerHTML = "";
  if (details) details.innerHTML = "";
  return;
}

  if (currentView === "stats") {
    renderStatistics();
    const compareBar = el("compareBar");
    const compare = el("compare");
    const details = el("details");
    if (compareBar) compareBar.innerHTML = "";
    if (compare) compare.innerHTML = "";
    if (details) details.innerHTML = "";
    return;
  }

  if (currentView === "setup") {
    const compareBar = el("compareBar");
    const compare = el("compare");
    const details = el("details");
    if (compareBar) compareBar.innerHTML = "";
    if (compare) compare.innerHTML = "";
    if (details) details.innerHTML = "";
    renderSetup();
  return;
}

  } catch (e) {
    renderFatal(e);
  }
}

// Load release history in append-only format (new enterprise format)
async function loadReleaseHistoryAppendOnly() {
  try {
    // Load index.json (lightweight metadata)
    const indexResponse = await fetch("../data/release_history/index.json", { cache: "no-store" });
    const index = await safeJsonResponse(indexResponse);
    if (!index) {
      throw new Error(`Failed to load or parse index.json (HTTP ${indexResponse.status})`);
    }
    
    // Load events.jsonl (stream and parse)
    const eventsResponse = await fetch("../data/release_history/events.jsonl", { cache: "no-store" });
    if (!eventsResponse.ok) throw new Error(`Failed to load events.jsonl (${eventsResponse.status})`);
    const eventsText = await eventsResponse.text();
    
    // Parse JSONL (one JSON object per line)
    const events = [];
    const lines = eventsText.split("\n").filter(line => line.trim());
    for (const line of lines) {
      try {
        const event = JSON.parse(line);
        events.push(event);
      } catch (e) {
        console.warn("[Release History] Failed to parse event line:", e);
      }
    }
    
    // Add project labels
    const labelByProjectKey = {
      TAP2: "TAP2.0",
      PO1V8: "PO1 (PO1v8)",
      B2C: "B2C (PO1v13)",
      TCBP_MFES: "TCBP → MFEs",
      TCBP_ADAPTERS: "TCBP → Adapters",
      LCW: "LCW",
      BS: "Booking Services",
    };
    
    const enrichedEvents = events.map(ev => ({
      ...ev,
      _projectKey: ev.projectKey || "",
      _projectLabel: labelByProjectKey[ev.projectKey] || ev.projectKey || "",
    }));
    
    return { format: "append-only", index, events: enrichedEvents };
  } catch (e) {
    throw e; // Re-throw to allow fallback to legacy format
  }
}

// Shared function to load release history data (used by both Overview and Release History)
async function ensureReleaseHistoryLoaded() {
  if (releaseHistoryData) {
    return Promise.resolve(releaseHistoryData);
  }
  
  if (releaseHistoryLoading) {
    // Wait for existing load to complete
    return new Promise((resolve) => {
      const checkInterval = setInterval(() => {
        if (!releaseHistoryLoading) {
          clearInterval(checkInterval);
          resolve(releaseHistoryData);
        }
      }, 50);
    });
  }
  
  releaseHistoryLoading = true;
  releaseHistoryLoadError = "";
  
  try {
    // Try new format first (append-only: index.json + events.jsonl)
    const data = await loadReleaseHistoryAppendOnly();
    releaseHistoryData = data;
    return data;
  } catch (e1) {
    // Fall back to legacy format
    console.log("[Release History] New format not available, trying legacy format:", e1);
    try {
      const response = await fetch(RELEASE_HISTORY_URL, { cache: "no-store" });
      const json = await safeJsonResponse(response);
      if (!json) {
        const errorMsg = response.status === 404
          ? "Release history file not found. History will be empty until the first snapshot runs."
          : `Failed to load release history (HTTP ${response.status}). The file may be corrupted.`;
        throw new Error(errorMsg);
      }
      releaseHistoryData = { format: "legacy", data: json };
      return releaseHistoryData;
    } catch (e2) {
      releaseHistoryLoadError = String(e2?.message || e2);
      // Don't throw - allow UI to continue with empty history
      console.error("[Release History] Failed to load both formats:", e2);
      releaseHistoryData = { format: "error", error: releaseHistoryLoadError, events: [] };
      return releaseHistoryData;
    }
  } finally {
    releaseHistoryLoading = false;
  }
}

// Render calendar view for Release History
// Uses ALL events for calendar visualization (not filtered), but can show filtered events for selected day
const CALENDAR_DAY_LIMIT = 20;

function renderHistoryCalendar(allEvents, displayedEvents, selectedDay = null, showAllForDay = false) {
  if (!allEvents || allEvents.length === 0) {
    return `<div class="panel"><div style="font-weight:800; margin-bottom:6px;">No data</div><div class="muted">Events appear when a tag changes between snapshots.</div></div>`;
  }

  // Helper to extract date key from ISO string
  const getDateKey = (iso) => {
    if (!iso) return "";
    const s = String(iso);
    return s.slice(0, 10); // YYYY-MM-DD
  };

  // Group ALL events by date (for calendar visualization - shows all deployment activity)
  const eventsByDate = new Map();
  for (const ev of allEvents) {
    const dateKey = getDateKey(ev.at || ev.time || "");
    if (!dateKey) continue;
    if (!eventsByDate.has(dateKey)) {
      eventsByDate.set(dateKey, []);
    }
    eventsByDate.get(dateKey).push(ev);
  }
  
  // Get events for selected day (use displayedEvents if filtered, otherwise allEvents)
  const selectedDayEvents = selectedDay ? (displayedEvents || allEvents).filter(ev => {
    const dateKey = getDateKey(ev.at || ev.time || "");
    return dateKey === selectedDay;
  }) : [];

  // Get date range
  const dates = Array.from(eventsByDate.keys()).sort();
  if (dates.length === 0) {
    return `<div class="panel"><div class="muted">No events to display</div></div>`;
  }

  const firstDate = new Date(dates[0] + "T00:00:00");
  const lastDate = new Date(dates[dates.length - 1] + "T00:00:00");
  
  // Determine month range to show (show at least current month, or range of events)
  const now = new Date();
  const startMonth = new Date(Math.min(firstDate.getTime(), now.getTime()));
  startMonth.setDate(1);
  const endMonth = new Date(Math.max(lastDate.getTime(), now.getTime()));
  endMonth.setMonth(endMonth.getMonth() + 1);
  endMonth.setDate(0); // Last day of month

  // Generate calendar months
  const months = [];
  let current = new Date(startMonth);
  while (current <= endMonth) {
    months.push(new Date(current));
    current.setMonth(current.getMonth() + 1);
  }

  const monthNames = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  const dayNames = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];

  const renderMonth = (monthDate) => {
    const year = monthDate.getFullYear();
    const month = monthDate.getMonth();
    const firstDay = new Date(year, month, 1);
    const lastDay = new Date(year, month + 1, 0);
    const daysInMonth = lastDay.getDate();
    const startDayOfWeek = (firstDay.getDay() + 6) % 7; // Monday = 0

    const days = [];
    // Empty cells for days before month starts
    for (let i = 0; i < startDayOfWeek; i++) {
      days.push({ empty: true });
    }
    // Days of the month
    for (let day = 1; day <= daysInMonth; day++) {
      const dateKey = `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
      const dayEvents = eventsByDate.get(dateKey) || [];
      days.push({ dateKey, day, events: dayEvents });
    }

    const dayCells = days.map((d, idx) => {
      if (d.empty) {
        return `<div class="calendarDay empty"></div>`;
      }
      
      const count = d.events.length;
      let intensity = "";
      let tooltip = `${d.dateKey}: ${count} deployment${count !== 1 ? "s" : ""}`;
      
      if (count === 0) {
        intensity = "none";
      } else if (count <= 2) {
        intensity = "low";
      } else if (count <= 5) {
        intensity = "medium";
      } else {
        intensity = "high";
      }

      const isToday = d.dateKey === `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
      const todayClass = isToday ? " today" : "";
      const isSelected = selectedDay && d.dateKey === selectedDay;
      const selectedClass = isSelected ? " selected" : "";

      return `
        <div class="calendarDay ${intensity}${todayClass}${selectedClass}" 
             data-calendar-date="${escapeAttr(d.dateKey)}" 
             title="${escapeAttr(tooltip)}"
             style="cursor:${count > 0 ? "pointer" : "default"};">
          <div class="calendarDayNumber">${d.day}</div>
          ${count > 0 ? `<div class="calendarDayCount">${count}</div>` : ""}
        </div>
      `;
    }).join("");

    return `
      <div class="calendarMonth">
        <div class="calendarMonthHeader">${monthNames[month]} ${year}</div>
        <div class="calendarWeekdays">
          ${dayNames.map(d => `<div class="calendarWeekday">${d}</div>`).join("")}
        </div>
        <div class="calendarDays">
          ${dayCells}
        </div>
      </div>
    `;
  };

  const monthsHtml = months.map(m => renderMonth(m)).join("");

  const selectedLabel = (() => {
    if (!selectedDay) return "";
    const todayKey = (() => {
      const d = new Date();
      const yyyy = d.getFullYear();
      const mm = String(d.getMonth() + 1).padStart(2, "0");
      const dd = String(d.getDate()).padStart(2, "0");
      return `${yyyy}-${mm}-${dd}`;
    })();
    const yesterdayKey = (() => {
      const d = new Date();
      d.setDate(d.getDate() - 1);
      const yyyy = d.getFullYear();
      const mm = String(d.getMonth() + 1).padStart(2, "0");
      const dd = String(d.getDate()).padStart(2, "0");
      return `${yyyy}-${mm}-${dd}`;
    })();
    if (selectedDay === todayKey) return "Today";
    if (selectedDay === yesterdayKey) return "Yesterday";
    return selectedDay;
  })();

  const selectedPanelHtml = selectedDay && selectedDayEvents.length > 0 ? `
        <div class="calendarSelectedDayPanel">
          <div class="panel" style="margin-top:0;">
            ${selectedDayEvents.slice(0, showAllForDay ? selectedDayEvents.length : CALENDAR_DAY_LIMIT).map((ev, idx) => {
              const envName = String(ev.envName || ev.envKey || ev.env || "-");
              const comp = String(ev.component || "-");
              const fromTagRaw = String(ev.fromTag || "-");
              const toTagRaw = String(ev.toTag || "-");
              const fromTag = preferVersionTag(fromTagRaw);
              const toTag = preferVersionTag(toTagRaw);
              const by = String(ev.by || "-");
              const at = String(ev.at || ev.time || "");
              const ago = fmtAgo(at);
              const abs = at ? fmtDate(at) : "-";
              const commitUrl = String(ev.commitUrl || ev.commitURL || "");
              const kustUrl = String(ev.kustomizationUrl || ev.kustomizationURL || "");
              return `
                <div class="historyRow historyRowCompact" data-hevent="${escapeAttr(JSON.stringify({
                  p: ev._projectKey,
                  e: String(ev.envKey || ev.env || ""),
                  c: String(ev.component || ""),
                  t: toTagRaw,
                  f: fromTagRaw,
                  by: by,
                  at: at,
                  commitUrl: commitUrl,
                  kustomizationUrl: kustUrl,
                  links: ev.links || [],
                }))}">
                  <div class="historyRowTop">
                    <div class="historyRowLeft">
                      <span class="pill softPill historyRowLeftEnv" title="${escapeAttr(envName)}">${escapeHtml(envName)}</span>
                      <span class="pill infoPill historyRowLeftPlatform" title="${escapeAttr(ev._projectLabel || ev._projectKey || "")}">${escapeHtml(ev._projectLabel || ev._projectKey || "")}</span>
                      <div class="historyRowLeftMain">
                        ${comp && comp !== "-" ? `<span class="historyComp" title="${escapeAttr(comp)}">${escapeHtml(comp)}</span>` : ""}
                      </div>
                    </div>
                    <div class="historyRowMid mono">
                      <span class="fromTag" title="${escapeAttr(fromTagRaw)}">${escapeHtml(fromTag)}</span>
                      <span class="arrow">→</span>
                      <span class="toTag" title="${escapeAttr(toTagRaw)}">${escapeHtml(toTag)}</span>
                    </div>
                    <div class="historyRowRight historyRowCompactRight">
                      <span class="muted" title="${escapeAttr(by)}">${escapeHtml(by)}</span>
                      <span class="muted" title="${escapeAttr(abs)}">· ${escapeHtml(ago)}</span>
                    </div>
                  </div>
                </div>
              `;
            }).join("")}
            ${selectedDayEvents.length > CALENDAR_DAY_LIMIT && !showAllForDay
              ? `<div class="calendarDayExpandRow">
                  <button type="button" class="pill softPill" id="calendarShowAllDay" style="cursor:pointer; border:1px solid rgba(255,255,255,.2); background:rgba(255,255,255,.06); padding:8px 14px; font-size:12px; font-weight:600;">Show all ${selectedDayEvents.length} events</button>
                </div>`
              : selectedDayEvents.length > CALENDAR_DAY_LIMIT && showAllForDay
              ? `<div class="calendarDayExpandRow">
                  <button type="button" class="pill softPill" id="calendarShowLessDay" style="cursor:pointer; border:1px solid rgba(255,255,255,.2); background:rgba(255,255,255,.06); padding:8px 14px; font-size:12px; font-weight:600;">Show less</button>
                </div>`
              : ""}
          </div>
        </div>
      ` : selectedDay ? `
            <div class="calendarDayExpandRow"></div>
          </div>
        </div>
      ` : `
        <div class="calendarSelectedDayPanel calendarSelectedDayEmpty">
          <div class="panel" style="margin-top:0;">
            <div class="muted" style="padding:12px; font-size:12px;">Click a day in the calendar to view deployment events for that date.</div>
          </div>
        </div>
      `;

  const emptySelectedPanelHtml = selectedDay ? `
        <div class="calendarSelectedDayPanel">
          <div class="panel" style="margin-top:0;">
            <div class="muted" style="padding:12px; text-align:center;">No deployment events match the current filters for this day.</div>
          </div>
        </div>
      ` : `
        <div class="calendarSelectedDayPanel calendarSelectedDayEmpty">
          <div class="panel" style="margin-top:0;">
            <div class="muted" style="padding:12px; font-size:12px;">Click a day in the calendar to view deployment events for that date.</div>
          </div>
        </div>
      `;

  return `
    <div class="panel calendarPanel">
      <div class="calendarHeaderRow">
        <div class="calendarHeaderLeft">
          <div style="font-weight:600; font-size:13px;">Deployment Calendar</div>
          <div class="muted" style="font-size:11px;">${displayedEvents.length} event${displayedEvents.length !== 1 ? "s" : ""}</div>
        </div>
        <div class="calendarHeaderRight">
          ${selectedDay
            ? `<span style="font-weight:600; font-size:12px; margin-right:8px;">${escapeHtml(selectedLabel)} - ${selectedDayEvents.length} event${selectedDayEvents.length !== 1 ? "s" : ""}</span>
               <button type="button" class="pill softPill" id="calendarCloseSelectedDay" style="cursor:pointer; border:none; background:transparent; padding:4px 8px; font-size:11px;">Close</button>`
            : `<span class="muted" style="font-size:11px;">Click a day to inspect deployments</span>`}
        </div>
      </div>
      <div class="calendarMain">
        <div class="calendarContainer">
          ${monthsHtml}
        </div>
        ${selectedDay
          ? (selectedDayEvents.length > 0 ? selectedPanelHtml : emptySelectedPanelHtml)
          : emptySelectedPanelHtml}
      </div>
    </div>
  `;
}

function renderHistory(opts = {}) {
  const wrap = el("historyContent");
  if (!wrap) return;

  // Scroll to top when rendering Release History
  if (currentView === "history") {
    window.scrollTo({ top: 0, behavior: "instant" });
  }

  // Load on demand using shared loader (same as Mini History)
  if (!releaseHistoryData && !releaseHistoryLoading) {
    wrap.innerHTML = `<div class="panel"><div class="muted">Loading release history…</div></div>`;
    
    ensureReleaseHistoryLoaded()
      .catch((e) => {
        console.error(e);
        releaseHistoryLoadError = String(e?.message || e);
      })
      .finally(() => {
        if (currentView === "history") renderHistory();
      });
    return;
  }

  if (releaseHistoryLoading) {
    wrap.innerHTML = `<div class="panel"><div class="muted">Loading release_history.json…</div></div>`;
    return;
  }

  if (releaseHistoryLoadError) {
    wrap.innerHTML = `
      <div class="panel">
        <div style="font-weight:800; margin-bottom:6px;">Release History unavailable</div>
        <div class="muted" style="margin-bottom:10px;">${escapeHtml(releaseHistoryLoadError)}</div>
        <div class="muted">Expected file: <span class="mono">data/release_history.json</span></div>
      </div>
    `;
    return;
  }

  const labelByProjectKey = {
    TAP2: "TAP2.0",
    PO1V8: "PO1 (PO1v8)",
    B2C: "B2C (PO1v13)",
    TCBP_MFES: "TCBP → MFEs",
    TCBP_ADAPTERS: "TCBP → Adapters",
    LCW: "LCW",
    BS: "Booking Services",
  };

  // Handle both new (append-only) and legacy formats
  let allEvents = [];
  let historyIndex = null;
  
  if (releaseHistoryData?.format === "append-only") {
    // New format: { format: "append-only", index: {...}, events: [...] }
    historyIndex = releaseHistoryData.index;
    allEvents = releaseHistoryData.events || [];
  } else if (releaseHistoryData?.format === "legacy") {
    // Legacy format wrapper
    const legacyData = releaseHistoryData.data;
    const projectsObj = legacyData?.projects && typeof legacyData.projects === "object" ? legacyData.projects : {};

  // Flatten events across projects (tolerant of both {key:{events:[]}} and {key:[]})
  Object.keys(projectsObj).forEach((pKey) => {
    const entry = projectsObj[pKey];
    const events = Array.isArray(entry) ? entry : (Array.isArray(entry?.events) ? entry.events : []);
    for (const ev of events) {
      allEvents.push({
        ...ev,
        _projectKey: pKey,
        _projectLabel: labelByProjectKey[pKey] || pKey,
      });
    }
  });
  } else {
    // Direct legacy format (backward compatibility)
    const projectsObj = releaseHistoryData?.projects && typeof releaseHistoryData.projects === "object" ? releaseHistoryData.projects : {};
    
    Object.keys(projectsObj).forEach((pKey) => {
      const entry = projectsObj[pKey];
      const events = Array.isArray(entry) ? entry : (Array.isArray(entry?.events) ? entry.events : []);
      for (const ev of events) {
        allEvents.push({
          ...ev,
          _projectKey: pKey,
          _projectLabel: labelByProjectKey[pKey] || pKey,
        });
      }
    });
  }

  // Ensure all events have _projectKey and _projectLabel
  allEvents = allEvents.map(ev => ({
    ...ev,
    _projectKey: ev._projectKey || ev.projectKey || "",
    _projectLabel: ev._projectLabel || labelByProjectKey[ev._projectKey || ev.projectKey] || (ev._projectKey || ev.projectKey || ""),
  }));

  // Sort newest first
  allEvents.sort((a, b) => String(b.at || b.time || "") .localeCompare(String(a.at || a.time || "")));

  // Build options from NAV order to keep the look consistent
  // Extract project keys from events (works for both formats)
  const projectKeysFromEvents = new Set(allEvents.map(ev => ev._projectKey || ev.projectKey).filter(Boolean));
  const orderedProjectKeys = ["TAP2", "PO1V8", "B2C", "TCBP_MFES", "TCBP_ADAPTERS", "LCW", "BS"]
    .filter((k) => projectKeysFromEvents.has(k));
  const extra = Array.from(projectKeysFromEvents).filter((k) => !orderedProjectKeys.includes(k));
  const projectKeysForFilter = ["ALL", ...orderedProjectKeys, ...extra];

  // Default filter: show ALL, envs empty = all
  const selectedProject = historyFilters.project || "ALL";
  const q = ""; // Search input removed - use Advanced Filters only
  const isAdvancedMode = historyFilters.advancedMode || false;
  const defaultLimit = historyFilters.defaultLimit || 10;
  const visibleLimit = historyFilters.visibleLimit || 10;

  // Apply filters
  let filtered = allEvents.filter((ev) => {
    if (selectedProject !== "ALL" && ev._projectKey !== selectedProject) return false;

    const kind = String(ev.kind || "TAG_CHANGE").toUpperCase();
    if (kind !== "TAG_CHANGE") return false;

    const envKey = String(ev.envKey || ev.env || "").toLowerCase();
    if (historyFilters.envs.size && !historyFilters.envs.has(envKey)) return false;

    // Advanced search filters (operate on full dataset)
    if (isAdvancedMode) {
      // Date range filter: compare date-only (YYYY-MM-DD)
      if (historyFilters.dateFrom) {
        const evDate = (ev.at || ev.time || "").slice(0, 10);
        if (evDate && evDate < historyFilters.dateFrom) return false;
      }
      if (historyFilters.dateTo) {
        const evDate = (ev.at || ev.time || "").slice(0, 10);
        if (evDate && evDate > historyFilters.dateTo) return false;
      }
      
      // Repository filter
      if (historyFilters.repo) {
        const repo = (ev.commitUrl || ev.repo || ev.component || "").toLowerCase();
        const repoFilter = historyFilters.repo.toLowerCase();
        if (!repo.includes(repoFilter)) return false;
      }
      
      // Tag filter
      if (historyFilters.tag) {
        const tag = (ev.toTag || ev.fromTag || "").toLowerCase();
        if (!tag.includes(historyFilters.tag.toLowerCase())) return false;
      }
      
      // Deployer filter
      if (historyFilters.deployer) {
        const deployer = (ev.by || "").toLowerCase();
        if (!deployer.includes(historyFilters.deployer.toLowerCase())) return false;
      }
    }

    // Text search removed - use Advanced Filters instead
    return true;
  });

  // Pagination: filters apply to full dataset first, then we show first N
  // displayed = first visibleLimit of filtered (never mutate filtered)
  const displayed = filtered.slice(0, visibleLimit);
  const hasMore = filtered.length > visibleLimit;

  // Env chips: only show when a project is selected, and only show envs for that project
  let envKeys = new Set();
  let envList = [];
  
  if (selectedProject !== "ALL") {
    // Get envs from events for the selected project
    const projectEvents = allEvents.filter((e) => e._projectKey === selectedProject);
    envKeys = new Set(projectEvents.map((e) => String(e.envKey || e.env || "").toLowerCase()).filter(Boolean));
    
    // Also get envs from appData for the selected project (fallback if release_history.json misses some)
    const projectKey = selectedProject; // selectedProject is already the project key from release_history.json
    const project = appData.projects?.find((p) => p.key === projectKey);
    if (project) {
      const projectEnvs = getEnvList(project);
      for (const env of projectEnvs) {
        const envKey = String(env.key || env.name || "").toLowerCase();
        if (envKey) envKeys.add(envKey);
      }
    }
    
    // Build ordered list
  const envOrder = ["dev", "alpha", "beta", "qa", "uat", "prod", "green", "blue", "orange", "pink", "grey", "gray", "purple"];
    envList = envOrder.filter((k) => envKeys.has(k));
    // Add any remaining envs not in the standard order
    for (const k of envKeys) {
      if (!envList.includes(k)) envList.push(k);
    }
  }

  const projectOptionsHtml = projectKeysForFilter.map((k) => {
    const label = k === "ALL" ? "All platforms" : (labelByProjectKey[k] || k);
    return `<option value="${escapeAttr(k)}" ${selectedProject === k ? "selected" : ""}>${escapeHtml(label)}</option>`;
  }).join("");

  const envChipsHtml = envList.map((k) => {
    const active = historyFilters.envs.has(k);
    return `<button type="button" class="chip ${active ? "active" : ""}" data-henv="${escapeAttr(k)}">${escapeHtml(k.toUpperCase())}</button>`;
  }).join("");

  const headerHtml = `
    <div class="panel" style="margin-bottom:12px;">
      <div class="historyFilters">
        <div class="historyFiltersLeft">
          <label class="historyLabel">
            <span class="muted">Platform</span>
            <select id="historyProjectSel" class="historySelect">${projectOptionsHtml}</select>
          </label>

        </div>

        <div class="historyFiltersRight">
          <div style="display:flex; align-items:center; gap:8px;">
            <button type="button" class="btn ${historyFilters.viewMode === "list" ? "btnPrimary" : "ghost"}" id="historyViewList" style="font-size:12px; padding:6px 12px;">
              List
            </button>
            <button type="button" class="btn ${historyFilters.viewMode === "calendar" ? "btnPrimary" : "ghost"}" id="historyViewCalendar" style="font-size:12px; padding:6px 12px;">
              Calendar
            </button>
          </div>
          <button type="button" class="pill softPill" id="historyAdvancedToggle" style="cursor:pointer; border:none; background:transparent; padding:6px 10px;">
            ${historyFilters.advancedMode ? "▼ Advanced" : "▶ Advanced"}
          </button>
          <div class="pill softPill">${filtered.length ? `${escapeHtml(String(displayed.length))} of ${filtered.length}` : "0"} events</div>
        </div>
      </div>
      
      ${historyFilters.advancedMode ? `
        <div class="panel" style="margin-top:12px; padding:12px; background:rgba(255,255,255,.02); border:1px solid rgba(255,255,255,.1);">
          <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:8px;">
            <div style="font-weight:600; font-size:12px;">Advanced Filters</div>
            <button type="button" class="pill softPill" id="historyAdvancedCollapse" style="cursor:pointer; border:none; background:transparent; padding:4px 8px; font-size:11px;">Collapse</button>
        </div>
          <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap:12px;">
            <label class="historyLabel">
              <span class="muted" style="font-size:11px;">Date From</span>
              <input type="date" id="historyDateFrom" class="historyInput historyDateInput" value="${escapeAttr(historyFilters.dateFrom)}" placeholder="Pick start date" style="height:32px; cursor:pointer;" aria-label="Start date" />
            </label>
            <label class="historyLabel">
              <span class="muted" style="font-size:11px;">Date To</span>
              <input type="date" id="historyDateTo" class="historyInput historyDateInput" value="${escapeAttr(historyFilters.dateTo)}" placeholder="Pick end date" style="height:32px; cursor:pointer;" aria-label="End date" />
            </label>
            <label class="historyLabel" style="position:relative;">
              <span class="muted" style="font-size:11px;">Repository</span>
              <input type="text" id="historyRepo" class="historyInput" value="${escapeAttr(historyFilters.repo)}" placeholder="repo name" style="height:32px;" autocomplete="off" />
              <div id="historyRepoAutocomplete" class="historyAutocomplete" style="display:none;"></div>
            </label>
            <label class="historyLabel">
              <span class="muted" style="font-size:11px;">Tag</span>
              <input type="text" id="historyTag" class="historyInput" value="${escapeAttr(historyFilters.tag)}" placeholder="v0.0.123" style="height:32px;" />
            </label>
            <label class="historyLabel">
              <span class="muted" style="font-size:11px;">Deployer</span>
              <input type="text" id="historyDeployer" class="historyInput" value="${escapeAttr(historyFilters.deployer)}" placeholder="username" style="height:32px;" />
            </label>
          </div>
        </div>
      ` : ""}
      </div>

      ${selectedProject !== "ALL" ? `<div class="historyEnvChips">${envChipsHtml}</div>` : ""}
    </div>
  `;

  const emptyState = `
    <div class="panel">
      <div style="font-weight:800; margin-bottom:6px;">No data</div>
      <div class="muted">Events appear when a tag changes between snapshots.</div>
      <div class="muted" style="margin-top:8px;">Tip: run snapshot once (baseline) → deploy (tag change) → run snapshot again.</div>
    </div>
  `;

  // --- Enterprise polish: group by date + burst grouping + details drawer
  const todayKey = (() => {
    const d = new Date();
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    return `${yyyy}-${mm}-${dd}`;
  })();

  const yesterdayKey = (() => {
    const d = new Date();
    d.setDate(d.getDate() - 1);
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    return `${yyyy}-${mm}-${dd}`;
  })();

  const dateKeyOf = (iso) => {
    if (!iso) return "";
    const s = String(iso);
    // ISO like 2026-01-19T...
    return s.slice(0, 10);
  };

  // Make dateKeyOf available globally for calendar view
  if (typeof window !== 'undefined') {
    window.dateKeyOf = dateKeyOf;
  }

  const sectionLabel = (key) => {
    if (!key) return "Unknown date";
    if (key === todayKey) return "Today";
    if (key === yesterdayKey) return "Yesterday";
    return key;
  };

  const limited = displayed.slice(0, 500);

  // Burst grouping: group events that share the same commit URL within the same project+env.
  // This prevents spam when a single infra change updates multiple components.
  const burstKey = (ev) => {
    const envKey = String(ev.envKey || ev.env || "").toLowerCase();
    const commitUrl = String(ev.commitUrl || ev.commitURL || "");
    return commitUrl ? `${ev._projectKey}::${envKey}::${commitUrl}` : "";
  };

  const burstCounts = new Map();
  for (const ev of limited) {
    const k = burstKey(ev);
    if (!k) continue;
    burstCounts.set(k, (burstCounts.get(k) || 0) + 1);
  }

  const groups = [];
  const used = new Set();
  for (let i = 0; i < limited.length; i++) {
    const ev = limited[i];
    if (used.has(i)) continue;

    const bk = burstKey(ev);
    if (bk && (burstCounts.get(bk) || 0) >= 2) {
      // Collect all events in this burst (stable order: as they appear in the feed)
      const items = [];
      for (let j = i; j < limited.length; j++) {
        const ev2 = limited[j];
        if (burstKey(ev2) === bk) {
          items.push({ ev: ev2, idx: j });
          used.add(j);
        }
      }
      groups.push({ type: "burst", head: ev, items: items.map((x) => x.ev), _id: `b_${i}` });
      continue;
    }

    groups.push({ type: "single", head: ev, items: [ev], _id: `s_${i}` });
    used.add(i);
  }

  // Group by date sections
  const sections = [];
  const secMap = new Map();
  for (const g of groups) {
    const at = String(g.head.at || g.head.time || "");
    const dk = dateKeyOf(at);
    if (!secMap.has(dk)) {
      const sec = { key: dk, label: sectionLabel(dk), groups: [] };
      secMap.set(dk, sec);
      sections.push(sec);
    }
    secMap.get(dk).groups.push(g);
  }

  const renderEventRow = (ev, idx, opts = {}) => {
    const envKey = String(ev.envKey || ev.env || "");
    const envName = String(ev.envName || envKey || "-");
    const comp = String(ev.component || "-");
    const fromTagRaw = String(ev.fromTag || "-");
    const toTagRaw = String(ev.toTag || "-");
    const fromTag = preferVersionTag(fromTagRaw);
    const toTag = preferVersionTag(toTagRaw);
    const by = String(ev.by || "-");
    const at = String(ev.at || ev.time || "");
    const ago = fmtAgo(at);
    const abs = at ? fmtDate(at) : "-";
    const kind = String(ev.kind || "TAG_CHANGE").toUpperCase();

    const links = Array.isArray(ev.links) ? ev.links : [];
    const commitUrl = ev.commitUrl || ev.commitURL || "";
    const kustUrl = ev.kustomizationUrl || ev.kustomizationURL || "";
    const baseLinks = [
      commitUrl ? { url: commitUrl, label: 'Commit' } : null,
      kustUrl ? { url: kustUrl, label: 'Kustomization' } : null,
      ...links.map((l) => l?.url ? ({ url: l.url, label: (l.label || l.type || 'Link') }) : null)
    ].filter(Boolean);

    const linkHtml = renderSourceLinks(baseLinks);

    const warnIcon = renderWarningsIcon(ev.warnings);

    const nested = opts.nested ? " nested" : "";

    return `
      <div class="historyRow${nested}" data-hevent="${escapeAttr(JSON.stringify({
        p: ev._projectKey,
        e: String(ev.envKey || ev.env || ""),
        c: String(ev.component || ""),
        f: String(ev.fromTag || ""),
        t: String(ev.toTag || ""),
        by: String(ev.by || ""),
        at: String(ev.at || ev.time || ""),
        commitUrl: String(ev.commitUrl || ev.commitURL || ""),
        kustomizationUrl: String(ev.kustomizationUrl || ev.kustomizationURL || ""),
        links: Array.isArray(ev.links) ? ev.links : [],
        warnings: ev.warnings || [],
      }))}">
        <div class="historyRowTop">
          <div class="historyRowLeft">
            <span class="pill softPill historyRowLeftEnv">${escapeHtml(envName)}</span>
            <span class="pill infoPill historyRowLeftPlatform">${escapeHtml(ev._projectLabel)}</span>
            <div class="historyRowLeftMain">
              <span class="historyComp">${escapeHtml(comp)}</span>
              ${kind !== "TAG_CHANGE" ? `<span class="pill">${escapeHtml(kind)}</span>` : ""}
              ${warnIcon}
            </div>
          </div>

          <div class="historyRowMid mono">
            <span class="fromTag" title="${escapeAttr(fromTagRaw)}">${escapeHtml(fromTag)}</span>
            <span class="arrow">→</span>
            <span class="toTag" title="${escapeAttr(toTagRaw)}">${escapeHtml(toTag)}</span>
          </div>

          <div class="historyRowRight">
            <span class="muted">by</span> <span class="historyBy">${escapeHtml(by)}</span>
            <span class="muted" title="${escapeAttr(abs)}">· ${escapeHtml(ago)}</span>
            <span class="historyLinks">${linkHtml || ""}</span>
          </div>
        </div>
      </div>
    `;

  };

  const rowsHtml = sections.map((sec) => {
    const groupsHtml = sec.groups.map((g) => {
      if (g.type === "burst") {
        const envKey = String(g.head.envKey || g.head.env || "");
        const envName = String(g.head.envName || envKey || "-");
        const by = String(g.head.by || "-");
        const at = String(g.head.at || g.head.time || "");
        const ago = fmtAgo(at);
        const abs = at ? fmtDate(at) : "-";
        const n = g.items.length;
        const commitUrl = String(g.head.commitUrl || g.head.commitURL || "");
        const kustUrl = String(g.head.kustomizationUrl || g.head.kustomizationURL || "");

        const baseLinks = [
          commitUrl ? { url: commitUrl, label: 'Commit' } : null,
          kustUrl ? { url: kustUrl, label: 'Kustomization' } : null,
        ].filter(Boolean);

        const links = renderSourceLinks(baseLinks);

        const children = g.items.map((ev, i2) => renderEventRow(ev, `${g._id}_${i2}`, { nested: true })).join("");
        return `
          <div class="historyGroup" data-hgroup="${escapeAttr(g._id)}">
            <div class="historyGroupHeader">
              <div class="historyRowLeft">
                <span class="pill softPill">${escapeHtml(envName)}</span>
                <span class="pill infoPill">${escapeHtml(g.head._projectLabel)}</span>
                <span class="historyComp">${escapeHtml(`${n} components updated`)}</span>
              </div>
              <div class="historyRowRight">
                <span class="muted">by</span> <span class="historyBy">${escapeHtml(by)}</span>
                <span class="muted" title="${escapeAttr(abs)}">· ${escapeHtml(ago)}</span>
                <span class="historyLinks">${links}</span>
                <button type="button" class="chip historyExpand" data-hexpand="${escapeAttr(g._id)}">Expand</button>
              </div>
            </div>
            <div class="historyGroupBody" data-hbody="${escapeAttr(g._id)}" style="display:none;">
              ${children}
            </div>
          </div>
        `;
      }

      // single
      return renderEventRow(g.head, g._id);
    }).join("");

    return `
      <div class="historySection">
        <div class="historySectionHeader">${escapeHtml(sec.label)}</div>
        <div class="historySectionBody">${groupsHtml}</div>
      </div>
    `;
  }).join("");

  // Load more: append next 10 over full filtered dataset
  const canLoadMore = hasMore;
  const showMoreHtml = canLoadMore ? `
    <div class="panel" style="margin-top:12px; text-align:center; padding:16px;">
      <button type="button" class="chip" id="historyShowMore" style="font-weight:800; padding:10px 20px;">
        Load more
      </button>
      <div class="muted" style="margin-top:8px; font-size:12px;">Showing ${displayed.length} of ${filtered.length} events</div>
    </div>
  ` : "";

  // Render content based on view mode
  const contentHtml = historyFilters.viewMode === "calendar" 
    ? renderHistoryCalendar(allEvents, filtered, historyFilters.selectedCalendarDay, historyFilters.calendarDayShowAll)
    : (displayed.length ? `<div class="panel">${rowsHtml}</div>${showMoreHtml}` : emptyState);

  wrap.innerHTML = `
    ${headerHtml}
    ${contentHtml}

    <div id="historyDrawer" class="historyDrawer hidden">
      <div class="historyDrawerBackdrop" data-hdrawer-close="1"></div>
      <div class="historyDrawerPanel">
        <div class="historyDrawerHeader">
          <div class="historyDrawerTitle">Event details</div>
          <button type="button" class="chip" data-hdrawer-close="1">Close</button>
        </div>
        <div id="historyDrawerBody" class="historyDrawerBody"></div>
      </div>
    </div>
  `;

  // Focus restoration removed (search input no longer exists)

  // Reset pagination when filters change
  const resetPagination = () => {
    historyFilters.visibleLimit = 10;
  };

  // Bind filters
  const sel = el("historyProjectSel");
  if (sel) {
    sel.onchange = () => {
      const newProject = sel.value || "ALL";
      const oldProject = historyFilters.project;
      historyFilters.project = newProject;
      if (newProject === "ALL" || newProject !== oldProject) {
        historyFilters.envs.clear();
      }
      resetPagination();
      renderHistory();
    };
  }

  // Search input removed - using Advanced Filters only

  // View mode toggle (List/Calendar) - use event delegation for reliability
  const historyFiltersRight = document.querySelector(".historyFiltersRight");
  if (historyFiltersRight) {
    historyFiltersRight.addEventListener('click', (e) => {
      if (e.target.id === "historyViewList" || e.target.closest("#historyViewList")) {
        e.preventDefault();
        historyFilters.viewMode = "list";
        renderHistory();
      } else if (e.target.id === "historyViewCalendar" || e.target.closest("#historyViewCalendar")) {
        e.preventDefault();
        historyFilters.viewMode = "calendar";
        renderHistory();
      }
    });
  }

  // Advanced search toggle
  const advancedToggle = el("historyAdvancedToggle");
  if (advancedToggle) {
    advancedToggle.onclick = () => {
      historyFilters.advancedMode = !historyFilters.advancedMode;
      renderHistory();
    };
  }

  // Load more button: append next 10
  const showMoreBtn = el("historyShowMore");
  if (showMoreBtn) {
    showMoreBtn.onclick = () => {
      historyFilters.visibleLimit = (historyFilters.visibleLimit || 10) + 10;
      renderHistory();
    };
  }

  // Advanced search collapse toggle
  const advancedCollapse = el("historyAdvancedCollapse");
  if (advancedCollapse) {
    advancedCollapse.onclick = () => {
      historyFilters.advancedMode = false;
      renderHistory();
    };
  }

  // Advanced search inputs
  const dateFromInput = el("historyDateFrom");
  if (dateFromInput) {
    // Make date input clickable (opens calendar) - don't use readonly (breaks showPicker)
    dateFromInput.onclick = (e) => {
      e.preventDefault();
      try {
        if (dateFromInput.showPicker) {
          dateFromInput.showPicker();
        }
      } catch (err) {
        // Fallback: just focus the input (browser will show native picker)
        dateFromInput.focus();
      }
    };
    dateFromInput.onfocus = (e) => {
      // Prevent default focus behavior that might interfere
      try {
        if (dateFromInput.showPicker && document.activeElement === dateFromInput) {
          dateFromInput.showPicker();
        }
      } catch (err) {
        // Ignore - browser will handle it
      }
    };
    // Prevent typing in date input
    dateFromInput.onkeydown = (e) => {
      // Allow only navigation keys and backspace/delete
      if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'Backspace', 'Delete', 'Tab'].includes(e.key)) {
        e.preventDefault();
      }
    };
    dateFromInput.onchange = () => {
      historyFilters.dateFrom = dateFromInput.value;
      resetPagination();
      renderHistory();
    };
  }

  const dateToInput = el("historyDateTo");
  if (dateToInput) {
    // Make date input clickable (opens calendar) - don't use readonly (breaks showPicker)
    dateToInput.onclick = (e) => {
      e.preventDefault();
      try {
        if (dateToInput.showPicker) {
          dateToInput.showPicker();
        }
      } catch (err) {
        // Fallback: just focus the input (browser will show native picker)
        dateToInput.focus();
      }
    };
    dateToInput.onfocus = (e) => {
      // Prevent default focus behavior that might interfere
      try {
        if (dateToInput.showPicker && document.activeElement === dateToInput) {
          dateToInput.showPicker();
        }
      } catch (err) {
        // Ignore - browser will handle it
      }
    };
    // Prevent typing in date input
    dateToInput.onkeydown = (e) => {
      // Allow only navigation keys and backspace/delete
      if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'Backspace', 'Delete', 'Tab'].includes(e.key)) {
        e.preventDefault();
      }
    };
    dateToInput.onchange = () => {
      historyFilters.dateTo = dateToInput.value;
      resetPagination();
      renderHistory();
    };
  }

  const repoInput = el("historyRepo");
  const repoAutocomplete = el("historyRepoAutocomplete");
  if (repoInput) {
    // Build repo list from existing events for typeahead
    const allRepos = new Set();
    allEvents.forEach(ev => {
      const commitUrl = String(ev.commitUrl || ev.commitURL || "");
      if (commitUrl) {
        // Extract repo name from commit URL (e.g., github.com/owner/repo -> repo)
        const match = commitUrl.match(/\/([^\/]+?)(?:\.git)?(?:\/|$)/g);
        if (match && match.length > 0) {
          const repoName = match[match.length - 1].replace(/[\/\.git]/g, "");
          if (repoName) allRepos.add(repoName);
        }
      }
      // Also check component name as fallback
      const comp = String(ev.component || "").trim();
      if (comp) allRepos.add(comp);
    });
    const repoList = Array.from(allRepos).sort();

    let selectedIndex = -1;
    let isOpen = false;

    const updateAutocomplete = () => {
      if (!repoAutocomplete) return;
      const query = repoInput.value.toLowerCase().trim();
      if (!query || query.length < 1) {
        repoAutocomplete.style.display = "none";
        isOpen = false;
        selectedIndex = -1;
        return;
      }

      const matches = repoList.filter(r => r.toLowerCase().includes(query)).slice(0, 8);
      if (matches.length === 0) {
        repoAutocomplete.innerHTML = `<div class="historyAutocompleteItem" style="padding:8px; color:var(--muted); font-size:12px;">No matches</div>`;
        repoAutocomplete.style.display = "block";
        isOpen = true;
        return;
      }

      repoAutocomplete.innerHTML = matches.map((repo, idx) => 
        `<div class="historyAutocompleteItem ${idx === selectedIndex ? 'active' : ''}" data-repo="${escapeAttr(repo)}" data-index="${idx}">${escapeHtml(repo)}</div>`
      ).join("");
      repoAutocomplete.style.display = "block";
      isOpen = true;
    };

    repoInput.oninput = () => {
      selectedIndex = -1;
      updateAutocomplete();
      debounce(() => {
        historyFilters.repo = repoInput.value;
        resetPagination();
        renderHistory();
      }, 300)();
    };

    repoInput.onfocus = () => {
      if (repoInput.value.trim()) updateAutocomplete();
    };

    repoInput.onkeydown = (e) => {
      if (!isOpen || !repoAutocomplete) return;
      const items = repoAutocomplete.querySelectorAll(".historyAutocompleteItem");
      if (items.length === 0) return;

      if (e.key === "ArrowDown") {
        e.preventDefault();
        selectedIndex = Math.min(selectedIndex + 1, items.length - 1);
        items.forEach((item, idx) => {
          item.classList.toggle("active", idx === selectedIndex);
        });
        items[selectedIndex]?.scrollIntoView({ block: "nearest" });
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        selectedIndex = Math.max(selectedIndex - 1, -1);
        items.forEach((item, idx) => {
          item.classList.toggle("active", idx === selectedIndex);
        });
        if (selectedIndex >= 0) items[selectedIndex]?.scrollIntoView({ block: "nearest" });
      } else if (e.key === "Enter" && selectedIndex >= 0) {
        e.preventDefault();
        const selected = items[selectedIndex];
        if (selected) {
          repoInput.value = selected.getAttribute("data-repo") || "";
          historyFilters.repo = repoInput.value;
          repoAutocomplete.style.display = "none";
          isOpen = false;
          resetPagination();
          renderHistory();
        }
      } else if (e.key === "Escape") {
        repoAutocomplete.style.display = "none";
        isOpen = false;
        selectedIndex = -1;
        repoInput.blur();
      }
    };

    // Click handler for autocomplete items
    if (repoAutocomplete) {
      repoAutocomplete.onclick = (e) => {
        const item = e.target.closest(".historyAutocompleteItem");
        if (item && item.getAttribute("data-repo")) {
          repoInput.value = item.getAttribute("data-repo");
          historyFilters.repo = repoInput.value;
          repoAutocomplete.style.display = "none";
          isOpen = false;
          resetPagination();
          renderHistory();
        }
      };
    }

    // Close on outside click
    document.addEventListener("click", (e) => {
      if (repoInput && repoAutocomplete && !repoInput.contains(e.target) && !repoAutocomplete.contains(e.target)) {
        repoAutocomplete.style.display = "none";
        isOpen = false;
      }
    });
  }

  const tagInput = el("historyTag");
  if (tagInput) {
    tagInput.oninput = debounce(() => {
      historyFilters.tag = tagInput.value;
      resetPagination();
      renderHistory();
    }, 300);
  }

  const deployerInput = el("historyDeployer");
  if (deployerInput) {
    deployerInput.oninput = debounce(() => {
      historyFilters.deployer = deployerInput.value;
      resetPagination();
      renderHistory();
    }, 300);
  }


  // Expand burst groups
  document.querySelectorAll("button[data-hexpand]").forEach((btn) => {
    btn.onclick = () => {
      const id = String(btn.getAttribute("data-hexpand") || "");
      const body = document.querySelector(`[data-hbody='${CSS.escape(id)}']`);
      if (!body) return;
      const isOpen = body.style.display !== "none";
      body.style.display = isOpen ? "none" : "block";
      btn.textContent = isOpen ? "Expand" : "Collapse";
    };
  });

  // Details drawer
  const drawer = el("historyDrawer");
  const drawerBody = el("historyDrawerBody");
  const closeDrawer = () => {
    if (!drawer) return;
    drawer.classList.add("hidden");
    if (drawerBody) drawerBody.innerHTML = "";
  };
  document.querySelectorAll("[data-hdrawer-close]").forEach((x) => {
    x.onclick = closeDrawer;
  });
  // Use same click handler function for both Release History and Mini History
  const handleHistoryRowClick = (e) => {
    // Don't open drawer if clicking on a link or button
    if (e.target.closest('a') || e.target.closest('button')) return;
    
    const row = e.currentTarget || e.target.closest('.historyRow');
    if (!row) return;
    
      const raw = row.getAttribute("data-hevent");
      if (!raw || !drawer || !drawerBody) return;
      let ev;
      try { ev = JSON.parse(raw); } catch { return; }

      const parts = [];
    const kv = (k, v) => `<div class='historyKV'><div class='muted'>${escapeHtml(k)}</div><div class='mono'>${escapeHtml(String(v || "-"))}</div></div>`;

    parts.push(kv("Platform", labelByProjectKey[ev.p] || ev.p));
      parts.push(kv("Environment", ev.e));
      parts.push(kv("Component", ev.c));
      parts.push(kv("From", ev.f));
      parts.push(kv("To", ev.t));
      parts.push(kv("By", ev.by));
      parts.push(kv("When", ev.at ? fmtDate(ev.at) : "-"));

      const baseLinks = [];
      if (ev.commitUrl) baseLinks.push({ url: ev.commitUrl, label: 'Commit' });
      if (ev.kustomizationUrl) baseLinks.push({ url: ev.kustomizationUrl, label: 'Kustomization' });
      if (Array.isArray(ev.links)) {
        for (const l of ev.links) {
          if (l?.url) baseLinks.push({ url: l.url, label: (l.label || l.type || 'Link') });
        }
      }
      const linksHtml = renderSourceLinks(baseLinks);
      if (linksHtml) {
        parts.push(`<div class='historyKV'><div class='muted'>Links</div><div class='historyDrawerLinks'>${linksHtml}</div></div>`);
      }

      if (ev.warnings && Array.isArray(ev.warnings) && ev.warnings.length) {
        const w = ev.warnings.map((w) => {
          const code = w?.code ? String(w.code) : "";
          const msg = w?.message ? String(w.message) : "Warning";
          const text = code ? `${code}: ${msg}` : msg;
          return `<div class='warnRow'><span class='warnDot'></span><span>${escapeHtml(text)}</span></div>`;
        }).join("");
        parts.push(`<div class='historyKV'><div class='muted'>Warnings</div><div class='warnList'>${w}</div></div>`);
      }

      drawerBody.innerHTML = parts.join("");
      drawer.classList.remove("hidden");
    };
  
  document.querySelectorAll(".historyRow").forEach((row) => {
    // Remove any existing handler and add new one
    row.onclick = null;
    row.addEventListener('click', handleHistoryRowClick);
  });

  document.querySelectorAll("button[data-henv]").forEach((btn) => {
    btn.onclick = () => {
      const k = String(btn.getAttribute("data-henv") || "").toLowerCase();
      if (!k) return;
      if (historyFilters.envs.has(k)) historyFilters.envs.delete(k);
      else historyFilters.envs.add(k);
      resetPagination();
      renderHistory();
    };
  });

  // Calendar day click handlers (only if calendar view is active)
  // Use event delegation to handle clicks on calendar days
  const calendarContainer = document.querySelector(".calendarContainer");
  if (calendarContainer && historyFilters.viewMode === "calendar") {
    // Remove any existing listeners by cloning and replacing
    const newContainer = calendarContainer.cloneNode(true);
    calendarContainer.parentNode.replaceChild(newContainer, calendarContainer);
    
    newContainer.addEventListener('click', (e) => {
      const dayEl = e.target.closest('.calendarDay[data-calendar-date]');
      if (!dayEl) return;
      
      const dateKey = dayEl.getAttribute("data-calendar-date");
      if (!dateKey) return;
      
      const count = parseInt(dayEl.querySelector('.calendarDayCount')?.textContent || "0");
      if (count === 0) return; // Don't select days with no events
      
      // Toggle selection: if same day clicked, deselect; otherwise select new day
      if (historyFilters.selectedCalendarDay === dateKey) {
        historyFilters.selectedCalendarDay = null;
        historyFilters.calendarDayShowAll = false;
      } else {
        historyFilters.selectedCalendarDay = dateKey;
        historyFilters.calendarDayShowAll = false;
      }
      // Stay in calendar view - don't switch to list
      renderHistory();
    });
  }
  
  // Close selected day panel button
  const closeSelectedDayBtn = document.getElementById("calendarCloseSelectedDay");
  if (closeSelectedDayBtn) {
    closeSelectedDayBtn.addEventListener('click', () => {
      historyFilters.selectedCalendarDay = null;
      historyFilters.calendarDayShowAll = false;
      renderHistory();
    });
  }

  // Show all / Show less for selected day
  const showAllDayBtn = document.getElementById("calendarShowAllDay");
  if (showAllDayBtn) {
    showAllDayBtn.addEventListener('click', () => {
      historyFilters.calendarDayShowAll = true;
      renderHistory();
    });
  }
  const showLessDayBtn = document.getElementById("calendarShowLessDay");
  if (showLessDayBtn) {
    showLessDayBtn.addEventListener('click', () => {
      historyFilters.calendarDayShowAll = false;
      renderHistory();
    });
  }
}
// ---------- Ticket Tracker ----------
// IMPORTANT: Mock data is shaped to be realistically fetchable in MVP1 from:
// - GitHub: PRs, merge dates, target branches
// - TeamCity: build numbers / promoted build tags
// - ArgoCD: when a version/tag is observed on an environment
// - Jira: ticket status + links between ticket <-> PRs (later)
// Real data comes from latest.json -> ticketIndex. No mock fallback (empty list when not configured).
// Allowed Jira project prefixes for now (until Admin panel exists).
// Later (hosted mode): this becomes configurable in Admin.
// Ticket key format: PROJ-1234. Any prefix accepted when fetching via API.
const TICKET_KEY_REGEX = /^[A-Z][A-Z0-9]*-\d+$/i;

function getTicketListFromAppData() {
  const idx = appData && appData.ticketIndex ? appData.ticketIndex : null;
  if (!idx || typeof idx !== "object") return [];

  return Object.keys(idx).filter((k) => TICKET_KEY_REGEX.test(String(k || "").trim())).map((k) => {
    const t = idx[k] || {};
    const jira = t.jira || {};
    const prs = t.pullRequests || t.prs || [];
    const evidence = t.evidence || [];  // Component-based evidence (fallback when no PRs)
    const timeline = t.timeline || [];
    const perPrTimelines = t.perPrTimelines || null;

    // Normalize PR fields so the UI stays stable.
    const normPrs = Array.isArray(prs)
      ? prs.map((p) => ({
          repo: p.repo || p.repository || "",
          pr: p.pr || p.number || p.id || "",
          title: p.title || "",
          mergedAt: p.mergedAt || p.merged_at || "",
          mergedTo: p.base || p.baseBranch || p.targetBranch || p.mergedTo || "",
          url: p.url || p.html_url || "",
        }))
      : [];

    // Normalize evidence entries (component-based, when PRs are not available)
    const normEvidence = Array.isArray(evidence)
      ? evidence.map((e) => ({
          repo: e.repo || e.repository || "",
          component: e.component || "",
          tag: e.tag || "",
          branch: e.branch || "",
          build: e.build || "",
          deployedAt: e.deployedAt || "",
          buildUrl: e.buildUrl || "",
          source: e.source || "component_metadata",
        }))
      : [];

    return {
      key: k,
      summary: jira.summary || t.summary || "",
      status: jira.status || t.status || "",
      jiraUrl: t.jiraUrl || (jira.key ? `${(appData.jiraBase || "").replace(/\/$/, "")}/browse/${jira.key}` : ""),
      prs: normPrs,
      evidence: normEvidence,  // Include evidence for component-based tickets
      timeline,
      perPrTimelines,
      envPresence: t.envPresence || null,
      envPresenceMeta: t.envPresenceMeta || null,
    };
  });
}

let ticketQuery = "";
let ticketFilterHasPRs = false;
let ticketFilterStage = ""; // "DEV" | "QA" | "UAT" | "PROD" | ""
let ticketLoading = false;
let ticketError = "";

function fmtDash(x) {
  return (x === undefined || x === null || String(x).trim() === "") ? "-" : String(x);
}

function renderTimelineTable(items) {
  if (!Array.isArray(items) || !items.length) return `<div class="muted">No timeline.</div>`;
  
  // Helper to get event icon/indicator
  const getEventIcon = (type, stage) => {
    if (type === "branch") return "Branch";
    if (type === "tag") return "Tag";
    if (type === "deployment") return "Deploy";
    if (stage && stage.toLowerCase().includes("pr merged")) return "Merge";
    return "Note";
  };
  
  return `
    <table class="table">
      <thead>
        <tr>
          <th style="width: 200px;">Event</th>
          <th style="width: 120px;">When</th>
          <th>Version / branch / build</th>
          <th style="width: 180px;">Source</th>
        </tr>
      </thead>
      <tbody>
        ${items.map((t) => {
          const eventType = t.type || "";
          const stage = t.stage || "";
          const icon = getEventIcon(eventType, stage);
          const at = t.at ? fmtDate(t.at) : "-";
          const ref = t.ref || "-";
          const source = t.source || "-";
          const url = t.url || "";
          
          return `
          <tr>
            <td><b>${icon} ${escapeHtml(fmtDash(stage))}</b></td>
            <td>${escapeHtml(at)}</td>
            <td class="muted">${escapeHtml(fmtDash(ref))}</td>
            <td class="muted">${url ? `<a class="cellLink" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source)}</a>` : escapeHtml(fmtDash(source))}</td>
          </tr>
        `;
        }).join("")}
      </tbody>
    </table>
  `;
}

function renderTicketTracker() {
  const wrap = el("ticketContent");
  if (!wrap) return;
  
  // Scroll to top when rendering Ticket Tracker
  if (currentView === "ticket") {
    window.scrollTo({ top: 0, behavior: "instant" });
  }

  const query = (ticketQuery || "").trim().toUpperCase();

  const allTickets = getTicketListFromAppData();
  let list = query
    ? allTickets.filter((t) => String(t.key).toUpperCase() === query)
    : allTickets;

  // Filters (MVP1)
  if (ticketFilterHasPRs) {
    list = list.filter((t) => Array.isArray(t.prs) && t.prs.length > 0);
  }
  if (ticketFilterStage) {
    const stage = ticketFilterStage;
    list = list.filter((t) => {
      const ep = t && t.envPresence ? t.envPresence : null;
      if (ep && typeof ep === "object") return ep[stage] === true;
      // fallback: look into timeline
      const tl = Array.isArray(t?.timeline) ? t.timeline : [];
      return tl.some((x) => String(x.stage || "").toUpperCase().includes(stage));
    });
  }

  const isValidTicketKey = (k) => TICKET_KEY_REGEX.test(String(k || "").trim());
  const showPrefixWarning = !!query && !isValidTicketKey(query);

  wrap.innerHTML = `
    <div class="panel">
      <div class="panelHead">
        <div>
          <div class="panelTitle">Ticket Tracker</div>
          <div class="muted" style="max-width:920px;">
            Tip: results depend on consistent naming. We look for PRs that contain the ticket key (e.g. <b>PROJ-1234</b>) in the PR title/branch.
            If a PR was named like “fix-to-ingester”, we may not find it.
          </div>
        </div>
      </div>
      <div class="ttBar">
        <div class="ttBarLeft">
          <div class="ttSearch">
            <input class="ticketInput" id="ticketInput" placeholder="e.g. PROJ-1234" value="${escapeHtml(query)}" />
            <button class="btn ghost ttClearInline ${query ? "" : "isHidden"}" type="button" id="ticketClearBtn" ${query ? "" : "disabled"}>${escapeHtml(tt('clear'))}</button>
          </div>
          <button class="btn primary ttSearchBtn" type="button" id="ticketSearchBtn" ${ticketLoading ? "disabled" : ""}>
            ${ticketLoading ? "Searching…" : escapeHtml(tt('search'))}
          </button>
        </div>

        <div class="ttBarRight">
          <div class="ttFilters">
            <button class="btn ghost ttFilterBtn ${ticketFilterHasPRs ? "active" : ""}" type="button" id="ticketFilterHasPRs">Has PRs</button>
            <div class="ttDivider"></div>
            <button class="btn ghost ttFilterBtn ${ticketFilterStage === "DEV" ? "active" : ""}" type="button" data-stage="DEV">DEV</button>
            <button class="btn ghost ttFilterBtn ${ticketFilterStage === "QA" ? "active" : ""}" type="button" data-stage="QA">QA</button>
            <button class="btn ghost ttFilterBtn ${ticketFilterStage === "UAT" ? "active" : ""}" type="button" data-stage="UAT">UAT</button>
            <button class="btn ghost ttFilterBtn ${ticketFilterStage === "PROD" ? "active" : ""}" type="button" data-stage="PROD">PROD</button>
          </div>
          <button class="btn ghost ttFiltersClear ${((ticketFilterHasPRs || ticketFilterStage) ? '' : 'isHidden')}" type="button" id="ticketFilterClear" ${((ticketFilterHasPRs || ticketFilterStage) ? '' : 'disabled')}>${escapeHtml(tt('clearFilters'))}</button>
        </div>
      </div>

      ${ticketError ? `
        <div class="muted" style="margin-top:10px; color: var(--accent);">
          ${escapeHtml(ticketError)}
        </div>
      ` : showPrefixWarning ? `
        <div class="muted" style="margin-top:10px;">
          Use a valid ticket key format, e.g. <b>PROJ-1234</b> (prefix + hyphen + number).
        </div>
      ` : ``}

      ${list.length ? `
        <div class="ticketGrid">
          ${list.map(renderTicketCard).join("")}
        </div>
      ` : query ? `
        <div class="muted" style="margin-top:10px;">No ticket found for <b>${escapeHtml(query)}</b>.
        It may not exist, or the API server is not configured. Ensure snapshot API is running and Jira/GitHub/TeamCity are configured.</div>
      ` : `
        <div class="muted" style="margin-top:16px; font-size:14px;">Enter a ticket key (e.g. <b>PROJ-1234</b>) and click Search to fetch details from Jira, GitHub and TeamCity.</div>
      `}
    </div>
  `;

  const btn = el("ticketSearchBtn");
  const input = el("ticketInput");
  const clearBtn = el("ticketClearBtn");

  if (btn && input) {
    btn.onclick = async () => {
      ticketQuery = input.value || "";
      input.blur();
      await fetchTicketOnDemand();
    };
    input.onkeydown = async (e) => {
      if (e.key === "Enter") {
        ticketQuery = input.value || "";
        input.blur();
        await fetchTicketOnDemand();
      }
    };
  }
  if (clearBtn) {
    clearBtn.onclick = () => {
      ticketQuery = "";
      ticketError = "";
      renderTicketTracker();
    };
  }

  const hasPrBtn = el("ticketFilterHasPRs");
  if (hasPrBtn) {
    hasPrBtn.onclick = () => {
      ticketFilterHasPRs = !ticketFilterHasPRs;
      renderTicketTracker();
    };
  }

  // Stage buttons (data-stage)
  wrap.querySelectorAll("button[data-stage]").forEach((b) => {
    b.onclick = () => {
      const s = (b.getAttribute("data-stage") || "").toUpperCase();
      ticketFilterStage = ticketFilterStage === s ? "" : s;
      renderTicketTracker();
    };
  });

  const clearFilters = el("ticketFilterClear");
  if (clearFilters) {
    clearFilters.onclick = () => {
      ticketFilterHasPRs = false;
      ticketFilterStage = "";
      renderTicketTracker();
    };
  }
}

// ---------- Statistics (deployment activity analytics) ----------
function renderStatistics() {
  const wrap = el("statsContent");
  if (!wrap) return;

  // Ensure release history is loaded; reuse shared loader
  if (!releaseHistoryData && !releaseHistoryLoading) {
    wrap.innerHTML = `<div class="panel"><div class="muted">Loading deployment history…</div></div>`;
    ensureReleaseHistoryLoaded()
      .catch((e) => {
        console.error(e);
        releaseHistoryLoadError = String(e?.message || e);
      })
      .finally(() => {
        if (currentView === "stats") renderStatistics();
      });
    return;
  }

  if (releaseHistoryLoading) {
    wrap.innerHTML = `<div class="panel"><div class="muted">Loading deployment history…</div></div>`;
    return;
  }

  if (releaseHistoryLoadError) {
    wrap.innerHTML = `
      <div class="panel">
        <div style="font-weight:800; margin-bottom:6px;">Statistics unavailable</div>
        <div class="muted" style="margin-bottom:10px;">${escapeHtml(releaseHistoryLoadError)}</div>
        <div class="muted" style="font-size:12px;">Check that snapshot generator produced <code>release_history/index.json</code> and <code>events.jsonl</code>.</div>
      </div>
    `;
    return;
  }

  // Build flat list of events from releaseHistoryData (same as Release History)
  const labelByProjectKey = {
    TAP2: "TAP2.0",
    PO1V8: "PO1 (PO1v8)",
    B2C: "B2C (PO1v13)",
    TCBP_MFES: "TCBP MFEs",
    TCBP_ADAPTERS: "TCBP Adapters",
    LCW: "LCW",
    BS: "Booking Services",
  };

  let allEvents = [];
  if (releaseHistoryData?.format === "append-only") {
    allEvents = releaseHistoryData.events || [];
  } else if (releaseHistoryData?.format === "legacy") {
    const legacyData = releaseHistoryData.data;
    const projectsObj = legacyData?.projects && typeof legacyData.projects === "object" ? legacyData.projects : {};
    Object.keys(projectsObj).forEach((pKey) => {
      const entry = projectsObj[pKey];
      const events = Array.isArray(entry) ? entry : (Array.isArray(entry?.events) ? entry.events : []);
      for (const ev of events) {
        allEvents.push({
          ...ev,
          _projectKey: pKey,
          _projectLabel: labelByProjectKey[pKey] || pKey,
        });
      }
    });
  } else {
    const projectsObj = releaseHistoryData?.projects && typeof releaseHistoryData.projects === "object" ? releaseHistoryData.projects : {};
    Object.keys(projectsObj).forEach((pKey) => {
      const entry = projectsObj[pKey];
      const events = Array.isArray(entry) ? entry : (Array.isArray(entry?.events) ? entry.events : []);
      for (const ev of events) {
        allEvents.push({
          ...ev,
          _projectKey: pKey,
          _projectLabel: labelByProjectKey[pKey] || pKey,
        });
      }
    });
  }

  const nowMs = Date.now();
  const daysToMs = (d) => d * 24 * 60 * 60 * 1000;

  const buckets = {
    "7": { label: "Last 7 days", windowMs: daysToMs(7) },
    "30": { label: "Last 30 days", windowMs: daysToMs(30) },
  };

  const perBucket = {};
  for (const [key, meta] of Object.entries(buckets)) {
    const cutoff = nowMs - meta.windowMs;
    const slice = allEvents.filter((ev) => {
      const ts = new Date(ev.at || ev.time || "").getTime();
      if (!Number.isFinite(ts)) return false;
      return ts >= cutoff;
    });

    const perProject = Object.create(null); // { [projectKey]: { label, byCounts, total, envCounts, componentCounts } }
    const byCountsGlobal = Object.create(null);
    const envCountsGlobal = Object.create(null); // { [envKey]: count }
    const componentCountsGlobal = Object.create(null); // { [component]: count }
    const projectActivity = []; // For ranking

    for (const ev of slice) {
      const kind = String(ev.kind || "TAG_CHANGE").toUpperCase();
      if (kind !== "TAG_CHANGE") continue;
      const projKey = ev._projectKey || ev.projectKey || "UNKNOWN";
      const projLabel = labelByProjectKey[projKey] || projKey;
      const by = (ev.by && String(ev.by).trim()) || "unknown";
      const envKey = (ev.envKey && String(ev.envKey).trim().toUpperCase()) || "unknown";
      const component = (ev.component && String(ev.component).trim()) || "unknown";

      if (!perProject[projKey]) {
        perProject[projKey] = { 
          label: projLabel, 
          byCounts: Object.create(null), 
          envCounts: Object.create(null),
          componentCounts: Object.create(null),
          total: 0 
        };
      }
      perProject[projKey].total += 1;
      perProject[projKey].byCounts[by] = (perProject[projKey].byCounts[by] || 0) + 1;
      perProject[projKey].envCounts[envKey] = (perProject[projKey].envCounts[envKey] || 0) + 1;
      perProject[projKey].componentCounts[component] = (perProject[projKey].componentCounts[component] || 0) + 1;

      byCountsGlobal[by] = (byCountsGlobal[by] || 0) + 1;
      envCountsGlobal[envKey] = (envCountsGlobal[envKey] || 0) + 1;
      componentCountsGlobal[component] = (componentCountsGlobal[component] || 0) + 1;
    }

    // Build project activity ranking
    Object.values(perProject).forEach(p => {
      projectActivity.push({ label: p.label, total: p.total });
    });
    projectActivity.sort((a, b) => b.total - a.total);

    // Calculate deployment velocity (deployments per day)
    const days = Math.round(meta.windowMs / daysToMs(1));
    const deploymentVelocity = slice.length > 0 ? (slice.length / days).toFixed(1) : "0";

    perBucket[key] = { 
      meta, 
      perProject, 
      byCountsGlobal, 
      envCountsGlobal,
      componentCountsGlobal,
      projectActivity,
      deploymentVelocity,
      totalEvents: slice.length 
    };
  }

  const renderByTable = (byCounts, labelKey = "Item", labelValue = "Count") => {
    const entries = Object.entries(byCounts).sort((a, b) => b[1] - a[1]);
    if (!entries.length) {
      return `<div class="muted" style="font-size:12px;">No data in this window.</div>`;
    }
    return `
      <table class="table compact">
        <thead>
          <tr><th style="width:60%;">${escapeHtml(labelKey)}</th><th style="width:40%;">${escapeHtml(labelValue)}</th></tr>
        </thead>
        <tbody>
          ${entries.map(([item, count]) => `
            <tr>
              <td>${escapeHtml(item)}</td>
              <td>${escapeHtml(String(count))}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
  };

  // Get data for selected window
  const selectedData = perBucket[statsSelectedWindow] || perBucket["30"];
  if (!selectedData) {
    wrap.innerHTML = `<div class="panel"><div class="muted">No data available.</div></div>`;
    return;
  }

  const { label, windowMs } = selectedData.meta;
  const days = Math.round(windowMs / daysToMs(1));
  const data = selectedData;

  // Render selected view content
  let mainContent = "";
  
  if (statsSelectedView === "overview") {
    // Summary metrics card
    mainContent = `
      <div class="panel">
        <div class="panelTop">
          <div>
            <div class="panelTitle">${escapeHtml(label)} - Overview</div>
            <div class="panelMeta muted">Window: last ${escapeHtml(String(days))} day(s). Source: Release History events (<code>TAG_CHANGE</code> with <code>by</code> field).</div>
          </div>
        </div>
        <div class="panelBody">
          <div class="panel" style="margin-top:8px; background:var(--bg2);">
            <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(120px, 1fr)); gap:12px; padding:8px;">
              <div>
                <div class="muted" style="font-size:11px;">Total deployments</div>
                <div style="font-size:18px; font-weight:600;">${escapeHtml(String(data.totalEvents))}</div>
              </div>
              <div>
                <div class="muted" style="font-size:11px;">Avg per day</div>
                <div style="font-size:18px; font-weight:600;">${escapeHtml(data.deploymentVelocity)}</div>
              </div>
              <div>
                <div class="muted" style="font-size:11px;">Active projects</div>
                <div style="font-size:18px; font-weight:600;">${escapeHtml(String(data.projectActivity.length))}</div>
              </div>
              <div>
                <div class="muted" style="font-size:11px;">Environments</div>
                <div style="font-size:18px; font-weight:600;">${escapeHtml(String(Object.keys(data.envCountsGlobal).length))}</div>
              </div>
            </div>
          </div>
          <div class="muted" style="margin-top:12px; font-size:12px; line-height:1.4;">
            Select a statistic type from the right menu to view detailed breakdowns.
          </div>
        </div>
      </div>
    `;
  } else if (statsSelectedView === "deployers") {
    mainContent = `
      <div class="panel">
        <div class="panelTop">
          <div>
            <div class="panelTitle">${escapeHtml(label)} - Top Deployers</div>
            <div class="panelMeta muted">Window: last ${escapeHtml(String(days))} day(s)</div>
          </div>
        </div>
        <div class="panelBody">
          <div class="panel" style="margin-top:8px;">
            <div class="ov2H">Top deployers (all projects)</div>
            ${renderByTable(data.byCountsGlobal, "User", "Deploy count")}
          </div>
        </div>
      </div>
    `;
  } else if (statsSelectedView === "projects") {
    mainContent = `
      <div class="panel">
        <div class="panelTop">
          <div>
            <div class="panelTitle">${escapeHtml(label)} - Most Active Projects</div>
            <div class="panelMeta muted">Window: last ${escapeHtml(String(days))} day(s)</div>
          </div>
        </div>
        <div class="panelBody">
          <div class="panel" style="margin-top:8px;">
            <div class="ov2H">Most active projects</div>
            ${renderByTable(
              Object.fromEntries(data.projectActivity.map(p => [p.label, p.total])),
              "Project",
              "Deployments"
            )}
          </div>
        </div>
      </div>
    `;
  } else if (statsSelectedView === "environments") {
    mainContent = `
      <div class="panel">
        <div class="panelTop">
          <div>
            <div class="panelTitle">${escapeHtml(label)} - Environment Activity</div>
            <div class="panelMeta muted">Window: last ${escapeHtml(String(days))} day(s)</div>
          </div>
        </div>
        <div class="panelBody">
          <div class="panel" style="margin-top:8px;">
            <div class="ov2H">Environment activity</div>
            ${renderByTable(data.envCountsGlobal, "Environment", "Deployments")}
          </div>
        </div>
      </div>
    `;
  } else if (statsSelectedView === "components") {
    const topComponents = Object.entries(data.componentCountsGlobal)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 20);
    mainContent = `
      <div class="panel">
        <div class="panelTop">
          <div>
            <div class="panelTitle">${escapeHtml(label)} - Most Deployed Components</div>
            <div class="panelMeta muted">Window: last ${escapeHtml(String(days))} day(s)</div>
          </div>
        </div>
        <div class="panelBody">
          <div class="panel" style="margin-top:8px;">
            <div class="ov2H">Most deployed components</div>
            ${renderByTable(
              Object.fromEntries(topComponents),
              "Component",
              "Deployments"
            )}
          </div>
        </div>
      </div>
    `;
  } else if (statsSelectedView === "perproject") {
    const projectHtml = Object.values(data.perProject).length
      ? Object.values(data.perProject).map((p) => `
          <div class="panel" style="margin-top:10px;">
            <div class="panelTop">
              <div>
                <div class="panelTitle">${escapeHtml(p.label)}</div>
                <div class="panelMeta muted">${escapeHtml(String(p.total))} deployment event(s)</div>
              </div>
            </div>
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:8px;">
              <div>
                <div class="muted" style="font-size:11px; margin-bottom:4px;">Top deployers</div>
                ${renderByTable(p.byCounts, "User", "Count")}
              </div>
              <div>
                <div class="muted" style="font-size:11px; margin-bottom:4px;">Environments</div>
                ${renderByTable(p.envCounts, "Environment", "Count")}
              </div>
            </div>
          </div>
        `).join("")
      : `<div class="muted" style="margin-top:8px; font-size:12px;">No deployment events for any project in this window.</div>`;
    
    mainContent = `
      <div class="panel">
        <div class="panelTop">
          <div>
            <div class="panelTitle">${escapeHtml(label)} - Per-Project Details</div>
            <div class="panelMeta muted">Window: last ${escapeHtml(String(days))} day(s)</div>
          </div>
        </div>
        <div class="panelBody">
          <div style="margin-top:8px;">
            ${projectHtml}
          </div>
        </div>
      </div>
    `;
  }

  // Navigation menu
  const navItems = [
    { id: "overview", label: "Overview", icon: "" },
    { id: "deployers", label: "Top Deployers", icon: "" },
    { id: "projects", label: "Most Active Projects", icon: "" },
    { id: "environments", label: "Environment Activity", icon: "" },
    { id: "components", label: "Most Deployed Components", icon: "" },
    { id: "perproject", label: "Per-Project Details", icon: "" },
  ];

  const navHtml = navItems.map(item => `
    <button 
      class="btn ${statsSelectedView === item.id ? "btnPrimary" : "ghost"}" 
      type="button"
      id="statsNav_${item.id}"
      style="width:100%; text-align:left; margin-bottom:6px; padding:10px 12px; display:flex; align-items:center; gap:8px;"
    >
      ${item.icon ? `<span style="font-size:16px;">${item.icon}</span>` : ""}
      <span>${escapeHtml(item.label)}</span>
    </button>
  `).join("");

  // Window selector
  const windowSelector = `
    <div class="panel" style="margin-bottom:12px;">
      <div class="ov2H" style="margin-bottom:8px;">Time Window</div>
      <div style="display:flex; gap:8px;">
        <button 
          class="btn ${statsSelectedWindow === "7" ? "btnPrimary" : "ghost"}" 
          type="button"
          id="statsWindow_7"
          style="flex:1;"
        >
          7 days
        </button>
        <button 
          class="btn ${statsSelectedWindow === "30" ? "btnPrimary" : "ghost"}" 
          type="button"
          id="statsWindow_30"
          style="flex:1;"
        >
          30 days
        </button>
      </div>
    </div>
  `;

  wrap.innerHTML = `
    <div class="ov2Grid">
      <div class="ov2Left">
        ${mainContent}
      </div>
      <div class="ov2Right">
        ${windowSelector}
        <div class="panel">
          <div class="ov2H">Statistics</div>
          <div style="margin-top:8px;">
            ${navHtml}
          </div>
        </div>
        <div class="panel" style="margin-top:12px;">
          <div class="ov2H">Notes</div>
          <div class="muted" style="font-size:12px; line-height:1.4;">
            Statistics are read-only and based solely on recorded deployment events in <code>release_history</code>.
            If some integrations do not provide deployer / author fields, entries will appear as <b>unknown</b>.
          </div>
        </div>
      </div>
    </div>
  `;

  // Bind navigation buttons
  navItems.forEach(item => {
    const btn = el(`statsNav_${item.id}`);
    if (btn) {
      btn.onclick = () => {
        statsSelectedView = item.id;
        renderStatistics();
      };
    }
  });

  // Bind window selector buttons
  const btn7 = el("statsWindow_7");
  if (btn7) {
    btn7.onclick = () => {
      statsSelectedWindow = "7";
      renderStatistics();
    };
  }
  const btn30 = el("statsWindow_30");
  if (btn30) {
    btn30.onclick = () => {
      statsSelectedWindow = "30";
      renderStatistics();
    };
  }
}

async function fetchTicketOnDemand() {
  const key = (ticketQuery || "").trim().toUpperCase();
  if (!key) {
    ticketError = "Please enter a ticket key, e.g. PROJ-1845.";
    ticketLoading = false;
    renderTicketTracker();
    return;
  }

  ticketLoading = true;
  ticketError = "";
  renderTicketTracker();

  try {
    const data = await safeApiFetch(`${SNAPSHOT_API_BASE}/api/ticket/${encodeURIComponent(key)}`, { cache: "no-store" });
    ticketLoading = false;

    if (!data || data._error) {
      ticketError = data?._error ? (data.message || "Cannot connect to API server") : "Failed to load ticket details from backend.";
      renderTicketTracker();
      return;
    }

    if (data.status !== "ok") {
      ticketError = data.message || `Ticket ${key} not found.`;
      renderTicketTracker();
      return;
    }

    // Normalize for existing renderTicketCard
    const jira = data.jira || {};
    const jiraBaseRaw = (appData && appData.jiraBase) ? String(appData.jiraBase) : "";
    const jiraBase = jiraBaseRaw.endsWith("/") ? jiraBaseRaw.slice(0, -1) : jiraBaseRaw;
    const jiraUrlFromBase = (jira && jira.key && jiraBase) ? `${jiraBase}/browse/${jira.key}` : "";

    const ticket = {
      key: data.key,
      summary: jira.summary || "",
      status: jira.status || "",
      jiraUrl: jiraUrlFromBase || jira.url || "",
      prs: (data.prs || []).map((p) => ({
        repo: p.repo || "",
        pr: p.number || "",
        title: p.title || "",
        mergedAt: p.mergedAt || "",
        mergedTo: p.baseRef || "",
        url: p.url || "",
      })),
      evidence: (data.evidence || []).map((e) => ({
        repo: e.repo || "",
        component: e.component || "",
        tag: e.tag || "",
        branch: e.branch || "",
        build: e.build || "",
        deployedAt: e.deployedAt || "",
        buildUrl: e.buildUrl || "",
        source: e.source || "component_metadata",
      })),
      timeline: data.timeline || [],
      perPrTimelines: null,
      envPresence: null,
      envPresenceMeta: null,
    };

    // Render just this ticket as a focused view
    const wrap = el("ticketContent");
    if (!wrap) return;

    wrap.innerHTML = `
      <div class="panel">
        <div class="panelHead">
          <div>
            <div class="panelTitle">Ticket Tracker</div>
            <div class="muted" style="max-width:920px;">
              Live view for ticket <b>${escapeHtml(key)}</b>. Data fetched directly from Jira/GitHub/TeamCity using current config.
            </div>
          </div>
        </div>
        <div class="ticketGrid">
          ${renderTicketCard(ticket)}
        </div>
      </div>
    `;
  } catch (e) {
    ticketLoading = false;
    ticketError = e?.message || "Error while loading ticket details.";
    renderTicketTracker();
  }
}

function buildEnvEvidenceTooltip(stage, meta, timeline) {
  if (!meta || typeof meta !== "object") return null;
  
  const parts = [];
  if (meta.repo) parts.push(`Repository: ${meta.repo}`);
  if (meta.tag) parts.push(`Tag: ${meta.tag}`);
  if (meta.branch) parts.push(`Branch: ${meta.branch}`);
  if (meta.when) {
    const when = fmtDate(meta.when);
    parts.push(`Deployed: ${when}`);
  }
  
  // Add PR info from timeline if available
  if (Array.isArray(timeline)) {
    const deployEvents = timeline.filter((ev) => {
      const stageStr = String(ev.stage || "").toUpperCase();
      return stageStr === `DEPLOYED TO ${stage.toUpperCase()}`;
    });
    if (deployEvents.length > 0) {
      const ev = deployEvents[0];
      if (ev.source) parts.push(`Source: ${ev.source}`);
      if (ev.url) parts.push(`PR: ${ev.url}`);
    }
  }
  
  return parts.length > 0 ? parts.join("\n") : null;
}

// Feature flag: AI reasoning layer (non-authoritative, UI-only)
// Can be enabled via: window.TICKET_AI_ENABLED = true
const TICKET_AI_ENABLED = typeof window !== "undefined" && (window.TICKET_AI_ENABLED === true || window.TICKET_AI_ENABLED === "true");

function buildTicketNarrative(ticket) {
  /**Build a human-readable narrative of ticket lifecycle.
  
  This is a rule-based summarization that interprets the deterministic timeline.
  It never overwrites factual data - only provides interpretation.
  
  Returns: { text: string, confidence: "high"|"medium"|"low", reasons: string[] } | null
  */
  if (!TICKET_AI_ENABLED) return null;
  
  const prs = Array.isArray(ticket.prs) ? ticket.prs : [];
  const timeline = Array.isArray(ticket.timeline) ? ticket.timeline : [];
  const envPresence = ticket.envPresence || {};
  const meta = ticket.envPresenceMeta || {};
  
  if (!prs.length && !timeline.length) {
    return null; // No data to summarize
  }
  
  const parts = [];
  const reasons = [];
  let overallConfidence = "high";
  
  // PR merge events
  const prMerges = timeline.filter(e => e.stage && e.stage.toLowerCase().includes("pr merged"));
  if (prMerges.length > 0) {
    const firstMerge = prMerges[prMerges.length - 1]; // Oldest (timeline is reverse sorted)
    const repo = firstMerge.source || "";
    const date = firstMerge.at ? fmtDate(firstMerge.at) : "";
    if (date) {
      parts.push(`${ticket.key} was merged into ${repo} on ${date}`);
      reasons.push(`PR merge event at ${firstMerge.at}`);
    }
  } else if (prs.length > 0) {
    const firstPr = prs[prs.length - 1]; // Oldest
    const repo = firstPr.repo || "";
    const date = firstPr.mergedAt ? fmtDate(firstPr.mergedAt) : "";
    if (date) {
      parts.push(`${ticket.key} was merged into ${repo} on ${date}`);
      reasons.push(`PR #${firstPr.number || firstPr.pr} merged at ${firstPr.mergedAt}`);
    }
  }
  
  // Branch/release progression
  const branchEvents = timeline.filter(e => e.type === "branch" || (e.stage && e.stage.toLowerCase().includes("included in")));
  if (branchEvents.length > 0) {
    const branches = branchEvents.map(e => e.ref).filter(Boolean).filter((v, i, a) => a.indexOf(v) === i);
    if (branches.length > 0) {
      parts.push(`promoted via ${branches.join(" → ")}`);
      reasons.push(`Branch inclusion events: ${branches.join(", ")}`);
    }
  }
  
  // Tag/release events
  const tagEvents = timeline.filter(e => e.type === "tag" || (e.stage && e.stage.toLowerCase().includes("tagged")));
  if (tagEvents.length > 0) {
    const tags = tagEvents.map(e => e.ref).filter(Boolean).filter((v, i, a) => a.indexOf(v) === i);
    if (tags.length > 0) {
      parts.push(`tagged as ${tags.join(", ")}`);
      reasons.push(`Tag events: ${tags.join(", ")}`);
    }
  }
  
  // Deployment events (validate against raw data)
  const deployments = timeline.filter(e => e.type === "deployment" || (e.stage && e.stage.toLowerCase().includes("deployed")));
  const deployedEnvs = [];
  const deploymentReasons = [];
  
  for (const stage of ["DEV", "QA", "UAT", "PROD"]) {
    const deployEvent = deployments.find(e => 
      e.stage && e.stage.toUpperCase().includes(`DEPLOYED TO ${stage}`)
    );
    const envMeta = meta[stage];
    const isPresent = envPresence[stage] === true;
    
    if (deployEvent && isPresent) {
      // High confidence: both timeline event and envPresence agree
      const date = deployEvent.at ? fmtDate(deployEvent.at) : "";
      const ref = deployEvent.ref || "";
      deployedEnvs.push(`${stage}${date ? ` (${date})` : ""}${ref ? ` with ${ref}` : ""}`);
      deploymentReasons.push(`${stage}: deployment event at ${deployEvent.at}, envPresence=true`);
    } else if (isPresent && envMeta) {
      // Medium confidence: envPresence true but no explicit deployment event
      const date = envMeta.when ? fmtDate(envMeta.when) : "";
      deployedEnvs.push(`${stage}${date ? ` (${date}, inferred)` : " (inferred)"}`);
      deploymentReasons.push(`${stage}: envPresence=true, meta.when=${envMeta.when || "unknown"}`);
      if (overallConfidence === "high") overallConfidence = "medium";
    } else if (deployEvent && !isPresent) {
      // Low confidence: deployment event exists but envPresence disagrees
      deployedEnvs.push(`${stage} (uncertain)`);
      deploymentReasons.push(`${stage}: deployment event exists but envPresence=false`);
      overallConfidence = "low";
    }
  }
  
  if (deployedEnvs.length > 0) {
    parts.push(`deployed to ${deployedEnvs.join(", ")}`);
    reasons.push(...deploymentReasons);
  }
  
  // Validation: check if narrative claims match raw data
  const validationIssues = [];
  if (deployedEnvs.length > 0 && deployments.length === 0) {
    validationIssues.push("Narrative claims deployments but no deployment events in timeline");
    overallConfidence = "low";
  }
  
  if (validationIssues.length > 0) {
    reasons.push(`Validation warnings: ${validationIssues.join("; ")}`);
  }
  
  if (parts.length === 0) {
    return null;
  }
  
  const text = parts.join(", ") + ".";
  
  return {
    text: text,
    confidence: overallConfidence,
    reasons: reasons,
    validationIssues: validationIssues.length > 0 ? validationIssues : null,
  };
}

function buildTicketNarrativeSection(ticket) {
  const narrative = buildTicketNarrative(ticket);
  if (!narrative) return "";
  
  const confidenceClass = narrative.confidence === "high" ? "ok" : narrative.confidence === "medium" ? "warn" : "bad";
  const confidenceLabel = narrative.confidence === "high" ? "High confidence" : narrative.confidence === "medium" ? "Medium confidence" : "Low confidence";
  
  return `
    <div class="ticketSectionTitle" style="margin-top:12px;">AI interpretation <span class="muted" style="font-size:11px;">(beta, non-authoritative)</span></div>
    <div class="ticketNarrative">
      <div class="ticketNarrativeText">${escapeHtml(narrative.text)}</div>
      <div class="ticketNarrativeMeta" style="margin-top:6px;">
        <span class="pill ${confidenceClass}" style="font-size:11px;">${escapeHtml(confidenceLabel)}</span>
        ${narrative.validationIssues ? `
          <div class="muted" style="margin-top:4px; font-size:11px;">
            ⚠️ Validation: ${escapeHtml(narrative.validationIssues.join("; "))}
          </div>
        ` : ""}
        <details class="ticketNarrativeDetails" style="margin-top:6px;">
          <summary class="muted" style="cursor:pointer; font-size:11px;">Show evidence</summary>
          <div class="muted" style="margin-top:4px; font-size:11px; line-height:1.4;">
            ${narrative.reasons.map(r => `• ${escapeHtml(r)}`).join("<br>")}
          </div>
        </details>
      </div>
    </div>
  `;
}

function renderTicketCard(t) {
  const prs = Array.isArray(t.prs) ? t.prs : [];
  const evidence = Array.isArray(t.evidence) ? t.evidence : [];
  const envBadges = deriveEnvBadges(t);
  const meta = t.envPresenceMeta || {};

  // Build evidence tooltips for each stage
  const timeline = Array.isArray(t.timeline) ? t.timeline : [];
  const devTooltip = buildEnvEvidenceTooltip("DEV", meta.DEV, timeline);
  const qaTooltip = buildEnvEvidenceTooltip("QA", meta.QA, timeline);
  const uatTooltip = buildEnvEvidenceTooltip("UAT", meta.UAT, timeline);
  const prodTooltip = buildEnvEvidenceTooltip("PROD", meta.PROD, timeline);

  return `
    <div class="ticketCard">
      <div class="ticketTop">
        <div>
          <div class="ticketId">${escapeHtml(t.key)}</div>
          <div class="ticketSummary muted">${escapeHtml(t.summary || "")}</div>
        </div>
        <div class="ticketMeta muted">Status: ${escapeHtml(t.status || "-")}</div>
      </div>

      ${t.jiraUrl ? `<div class="muted" style="margin-top:6px;"><a class="cellLink" href="${escapeHtml(t.jiraUrl)}" target="_blank" rel="noopener noreferrer">Open in Jira</a></div>` : ``}

      <div class="envStatusRow">
        <div class="envPill ${envBadges.dev}" ${devTooltip ? `title="${escapeAttr(devTooltip)}"` : ""}>DEV</div>
        <div class="envPill ${envBadges.qa}" ${qaTooltip ? `title="${escapeAttr(qaTooltip)}"` : ""}>QA</div>
        <div class="envPill ${envBadges.uat}" ${uatTooltip ? `title="${escapeAttr(uatTooltip)}"` : ""}>UAT</div>
        <div class="envPill ${envBadges.prod}" ${prodTooltip ? `title="${escapeAttr(prodTooltip)}"` : ""}>PROD</div>
      </div>

      ${prs.length > 0 ? `
      <div class="ticketSectionTitle">Pull requests</div>
      <div class="ticketPrList">
          ${prs.map((p) => {
            const prNumber = p.number || p.pr || "";
            const prUrl = p.url || p.htmlUrl || "";
            const mergedAt = p.mergedAt || "";
            const baseRef = p.baseRef || p.mergedTo || "";
            const branches = Array.isArray(p.branches) ? p.branches : [];
            const tags = Array.isArray(p.tags) ? p.tags : [];
            
            return `
          <div class="ticketPrRow">
              <div class="ticketPrRepo"><b>${escapeHtml(p.repo || "")}</b></div>
            <div class="ticketPrMeta muted">
                ${prUrl ? `<a class="cellLink" href="${escapeHtml(prUrl)}" target="_blank" rel="noopener noreferrer">PR #${escapeHtml(prNumber)}</a>` : `PR #${escapeHtml(prNumber)}`}
                ${mergedAt ? ` • merged ${escapeHtml(fmtDate(mergedAt))}` : ""}
                ${baseRef ? ` → ${escapeHtml(baseRef)}` : ""}
              </div>
              ${branches.length > 0 ? `
                <div class="ticketPrMeta muted" style="margin-top:4px; margin-left:0;">
                  <span class="muted">Branches:</span> ${branches.map(b => `<span class="pill" style="margin-left:4px;">${escapeHtml(b)}</span>`).join("")}
                </div>
              ` : ""}
              ${tags.length > 0 ? `
                <div class="ticketPrMeta muted" style="margin-top:4px; margin-left:0;">
                  <span class="muted">Tags:</span> ${tags.map(t => {
                    const tagName = (typeof t === "string" ? t : t.name) || "";
                    return `<span class="pill" style="margin-left:4px;">${escapeHtml(tagName)}</span>`;
                  }).join("")}
                </div>
              ` : ""}
            </div>
          `;
          }).join("")}
        </div>
      ` : evidence.length > 0 ? `
        <div class="ticketSectionTitle">Component evidence</div>
        <div class="ticketPrList">
          ${evidence.map((e) => `
            <div class="ticketPrRow">
              <div class="ticketPrRepo"><b>${escapeHtml(e.repo || "-")}</b> ${e.component ? `<span class="muted">(${escapeHtml(e.component)})</span>` : ""}</div>
              <div class="ticketPrMeta muted">
                ${e.tag ? `Tag: ${escapeHtml(e.tag)}` : ""}
                ${e.branch ? ` • Branch: ${escapeHtml(e.branch)}` : ""}
                ${e.build ? ` • Build: ${escapeHtml(e.build)}` : ""}
                ${e.deployedAt ? ` • Deployed: ${escapeHtml(fmtDate(e.deployedAt))}` : ""}
                ${e.buildUrl ? ` • <a class="cellLink" href="${escapeHtml(e.buildUrl)}" target="_blank" rel="noopener noreferrer">Build</a>` : ""}
            </div>
          </div>
        `).join("")}
      </div>
      ` : `
        <div class="ticketSectionTitle">Pull requests</div>
        <div class="muted" style="margin-top:6px;">No pull requests or component evidence found.</div>
      `}

      <div class="ticketSectionTitle" style="margin-top:10px;">Ticket lifecycle timeline</div>
      ${renderTimelineTable(t.timeline)}

      ${buildTicketNarrativeSection(t)}

      ${t.perPrTimelines ? `
        <div class="ticketSectionTitle" style="margin-top:12px;">Per-PR timelines</div>
        ${Object.keys(t.perPrTimelines).map((k) => `
          <div class="docCard" style="margin-top:10px;">
            <div class="docTitle">${escapeHtml(k)}</div>
            ${renderTimelineTable(t.perPrTimelines[k])}
          </div>
        `).join("")}
      ` : ``}
    </div>
  `;
}

function deriveEnvBadges(ticket) {
  const ep = ticket && ticket.envPresence ? ticket.envPresence : null;
  const tl = Array.isArray(ticket?.timeline) ? ticket.timeline : [];
  
  // Check timeline for "Deployed to {STAGE}" events (more reliable than envPresence alone)
  const hasDeployEvent = (stage) => {
    const stageUpper = stage.toUpperCase();
    return tl.some((x) => {
      const stageStr = String(x.stage || "").toUpperCase();
      return stageStr === `DEPLOYED TO ${stageUpper}` || stageStr.includes(`DEPLOYED TO ${stageUpper}`);
    });
  };
  
  // Primary: use envPresence if available
  if (ep && typeof ep === "object") {
    const on = (v) => v === true;
    // Also check timeline as fallback for cases where envPresence might be incomplete
    return {
      dev: on(ep.DEV) || hasDeployEvent("DEV") ? "ok" : "off",
      qa: on(ep.QA) || hasDeployEvent("QA") ? "ok" : "off",
      uat: on(ep.UAT) || hasDeployEvent("UAT") ? "ok" : "off",
      prod: on(ep.PROD) || hasDeployEvent("PROD") ? "ok" : "off",
    };
  }

  // Fallback: infer from timeline text (older snapshots or when envPresence missing)
  const has = (needle) => tl.some((x) => String(x.stage || "").toLowerCase().includes(needle));
  return {
    dev: has("deployed to dev") || hasDeployEvent("DEV") ? "ok" : "off",
    qa: has("deployed to qa") || hasDeployEvent("QA") ? "ok" : "off",
    uat: has("uat") || hasDeployEvent("UAT") ? "ok" : "off",
    prod: has("prod") || hasDeployEvent("PROD") ? "ok" : "off",
  };
}



function renderInitialLoading() {
  try {
    // Apply theme immediately so loading state matches final theme
    try { applyTheme(); } catch {}
    
    // Ensure overview block is visible during initial load
    const ovBlock = el("overviewBlock");
    if (ovBlock) ovBlock.style.display = "";
    
    // Fill key containers with a skeleton loading state so the UI never looks empty.
    const topBar = el("topBar");
    if (topBar && !topBar.innerHTML.trim()) {
      topBar.innerHTML = `
        <div class="topLeft">
          <div class="topTitle">Overview</div>
          <div class="topSub">Loading data…</div>
        </div>
        <div class="topRight">
          <span class="statusBadge muted">Loading</span>
        </div>
      `;
    }

    const overview = el("overviewContent");
    if (overview && !overview.innerHTML.trim()) {
      overview.innerHTML = `
        <div class="ov2Grid">
          <div class="ov2Left">
        <div class="panel">
              <div class="ov2H">Pipeline radar</div>
              <div style="padding: 40px 20px; text-align: center; color: var(--muted);">
                <div style="font-size: 14px; margin-bottom: 8px;">Loading pipeline data…</div>
                <div style="font-size: 11px; opacity: 0.7;">Fetching latest.json</div>
              </div>
            </div>
            <div class="panel">
              <div class="ov2H">Mini history (last 3 versions)</div>
              <div style="padding: 20px; text-align: center; color: var(--muted); font-size: 12px;">Loading history…</div>
            </div>
          </div>
          <div class="ov2Right">
            <div class="panel">
              <div class="ov2H">Parameters & logs</div>
              <div style="padding: 20px; text-align: center; color: var(--muted); font-size: 12px;">Loading metrics…</div>
            </div>
          </div>
        </div>
      `;
    }

    const releases = el("releasesContent");
    if (releases && !releases.innerHTML.trim()) {
      releases.innerHTML = `
        <div class="panel">
          <div style="padding: 40px 20px; text-align: center; color: var(--muted);">
            <div style="font-size: 14px; margin-bottom: 8px;">Loading release data…</div>
            <div style="font-size: 11px; opacity: 0.7;">Fetching latest.json</div>
          </div>
        </div>
      `;
    }

    const envRow = el("envRow");
    if (envRow && !envRow.innerHTML.trim()) {
      envRow.innerHTML = `
        <div style="padding: 20px; text-align: center; color: var(--muted); font-size: 12px;">Loading environments…</div>
      `;
    }
  } catch (_) {
    // ignore
  }
}

// ---------- Start ----------
renderInitialLoading();
boot();
