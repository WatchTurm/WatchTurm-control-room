/* Admin / Configuration - tenant config schema, storage, validation.
   Loaded before app.js. Exposes window.AdminConfig. */

(function () {
  "use strict";

  const STORAGE_KEY = "roc_tenant_config";
  const VERSION = 1;

  function defaultConfig() {
    return {
      version: VERSION,
      tenant: { id: "local", name: "", slug: "" },
      ticketing: { regex: "" },
      groups: [], // Array of groups/platforms, each can contain projects
      integrations: {
        github: { org: "", token: "" },
        teamcity: { baseUrl: "", token: "" },
        argocd: { envHosts: {} },
        datadog: { site: "datadoghq.com", apiKey: "", appKey: "" },
        jira: { baseUrl: "", email: "", token: "" },
      },
    };
  }

  function load() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      const data = JSON.parse(raw);
      if (data && typeof data === "object" && (data.version || data.tenant)) {
        return data;
      }
    } catch (_) {}
    return null;
  }

  function save(data) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
      return true;
    } catch (_) {
      return false;
    }
  }

  function exportJson(data) {
    return JSON.stringify(data || load() || defaultConfig(), null, 2);
  }

  function importJson(str) {
    try {
      const data = JSON.parse(str);
      if (data && typeof data === "object") return data;
    } catch (_) {}
    return null;
  }

  function validateRegex(s) {
    if (typeof s !== "string" || !s.trim()) return { valid: true, error: null };
    try {
      new RegExp(s.trim());
      return { valid: true, error: null };
    } catch (e) {
      return { valid: false, error: e.message || "Invalid regex" };
    }
  }

  function validate(config) {
    const errors = [];
    const warnings = [];

    const c = config || load() || defaultConfig();
    const t = c.tenant || {};
    const tk = c.ticketing || {};
    const groups = Array.isArray(c.groups) ? c.groups : [];
    const allProjects = groups.flatMap(g => g.type === "group" ? (g.projects || []) : [g]).filter(p => p.type !== "group");
    const int = c.integrations || {};
    const gh = int.github || {};

    if (!String(t.name || "").trim()) {
      errors.push({ field: "tenant.name", message: "Tenant name is required." });
    }
    const slug = String(t.slug || "").trim();
    if (slug && !/^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$/.test(slug)) {
      errors.push({ field: "tenant.slug", message: "Slug must be lowercase letters, numbers, hyphens (e.g. my-org)." });
    }

    const regexVal = validateRegex(tk.regex);
    if (!regexVal.valid) {
      errors.push({ field: "ticketing.regex", message: "Ticket regex is invalid: " + (regexVal.error || "invalid") });
    }

    if (!String(gh.org || "").trim()) {
      errors.push({ field: "integrations.github.org", message: "GitHub org/owner is required." });
    }
    if (!String(gh.token || "").trim()) {
      errors.push({ field: "integrations.github.token", message: "GitHub token is required." });
    }

    if (allProjects.length === 0) {
      errors.push({ field: "groups", message: "At least one project is required (standalone or in a group)." });
    }
    for (let gi = 0; gi < groups.length; gi++) {
      const g = groups[gi];
      if (g.type === "group") {
        if (!String((g || {}).key || "").trim()) {
          errors.push({ field: `groups[${gi}].key`, message: "Group key is required." });
        }
        if (!String((g || {}).name || "").trim()) {
          warnings.push({ field: `groups[${gi}].name`, message: "Group display name is recommended." });
        }
        const projs = Array.isArray(g.projects) ? g.projects : [];
        projs.forEach((p, pi) => {
          const key = String((p || {}).key || "").trim();
          const name = String((p || {}).name || "").trim();
          const envs = Array.isArray((p || {}).environments) ? (p || {}).environments : [];
          const svcs = Array.isArray((p || {}).services) ? (p || {}).services : [];
          if (!key) errors.push({ field: `groups[${gi}].projects[${pi}].key`, message: "Project key is required." });
          if (!name) warnings.push({ field: `groups[${gi}].projects[${pi}].name`, message: "Project display name is recommended." });
          if (envs.length === 0) errors.push({ field: `groups[${gi}].projects[${pi}].environments`, message: "Each project must have at least one environment." });
          if (svcs.length === 0) warnings.push({ field: `groups[${gi}].projects[${pi}].services`, message: "Projects without services have no infra mapping; deployment data will be missing." });
        });
      } else {
        // Standalone project
        const key = String((g || {}).key || "").trim();
        const name = String((g || {}).name || "").trim();
        const envs = Array.isArray((g || {}).environments) ? (g || {}).environments : [];
        const svcs = Array.isArray((g || {}).services) ? (g || {}).services : [];
        if (!key) errors.push({ field: `groups[${gi}].key`, message: "Project key is required." });
        if (!name) warnings.push({ field: `groups[${gi}].name`, message: "Project display name is recommended." });
        if (envs.length === 0) errors.push({ field: `groups[${gi}].environments`, message: "Each project must have at least one environment." });
        if (svcs.length === 0) warnings.push({ field: `groups[${gi}].services`, message: "Projects without services have no infra mapping; deployment data will be missing." });
      }
    }

    return {
      valid: errors.length === 0,
      errors,
      warnings,
    };
  }

  function isAdminMode() {
    try {
      if (typeof window === "undefined") return false;
      const u = window.location || {};
      const host = (u.hostname || "").toLowerCase();
      if (host === "localhost" || host === "127.0.0.1") return true;
      const params = new URLSearchParams(u.search || "");
      if (params.get("admin") === "1" || params.get("admin") === "true") return true;
      return !!localStorage.getItem("roc_admin_mode");
    } catch (_) {
      return false;
    }
  }

  function setAdminMode(enabled) {
    try {
      if (enabled) localStorage.setItem("roc_admin_mode", "1");
      else localStorage.removeItem("roc_admin_mode");
    } catch (_) {}
  }

  window.AdminConfig = {
    VERSION,
    defaultConfig,
    load,
    save,
    exportJson,
    importJson,
    validate,
    validateRegex,
    isAdminMode,
    setAdminMode,
    STORAGE_KEY,
  };
})();
