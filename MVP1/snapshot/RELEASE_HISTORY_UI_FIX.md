# Release History UI Fix Guide

## Problem

The backend has been updated to use append-only storage (`events.jsonl` + `index.json`), but the UI still tries to load the old `release_history.json` format. This causes the Release History view to fail.

## Solution

Update `web/app.js` to support both formats with automatic fallback.

## Required Changes

### 1. Add Helper Function (Before `renderHistory`)

Add this function around line 3476 (before `renderHistory`):

```javascript
// Load release history in append-only format (new enterprise format)
async function loadReleaseHistoryAppendOnly() {
  try {
    // Load index.json (lightweight metadata)
    const indexResponse = await fetch("../data/release_history/index.json", { cache: "no-store" });
    if (!indexResponse.ok) throw new Error(`Failed to load index.json (${indexResponse.status})`);
    const index = await indexResponse.json();
    
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
```

### 2. Update Data Loading (In `renderHistory`, around line 3483)

Replace the existing data loading code:

**OLD:**
```javascript
if (!releaseHistoryData && !releaseHistoryLoading) {
  releaseHistoryLoading = true;
  releaseHistoryLoadError = "";
  wrap.innerHTML = `<div class="panel"><div class="muted">Loading release_history.json…</div></div>`;
  fetch(RELEASE_HISTORY_URL, { cache: "no-store" })
    .then((r) => {
      if (!r.ok) throw new Error(`Failed to load release_history.json (${r.status})`);
      return r.json();
    })
    .then((j) => {
      releaseHistoryData = j || null;
    })
    .catch((e) => {
      console.error(e);
      releaseHistoryLoadError = String(e?.message || e);
    })
    .finally(() => {
      releaseHistoryLoading = false;
      if (currentView === "history") renderHistory();
    });
  return;
}
```

**NEW:**
```javascript
if (!releaseHistoryData && !releaseHistoryLoading) {
  releaseHistoryLoading = true;
  releaseHistoryLoadError = "";
  wrap.innerHTML = `<div class="panel"><div class="muted">Loading release history…</div></div>`;
  
  // Try new format first (append-only: index.json + events.jsonl)
  loadReleaseHistoryAppendOnly()
    .then((data) => {
      releaseHistoryData = data;
    })
    .catch((e1) => {
      // Fall back to legacy format
      console.log("[Release History] New format not available, trying legacy format:", e1);
      return fetch(RELEASE_HISTORY_URL, { cache: "no-store" })
        .then((r) => {
          if (!r.ok) throw new Error(`Failed to load release_history.json (${r.status})`);
          return r.json();
        })
        .then((j) => {
          // Convert legacy format to expected structure
          releaseHistoryData = { format: "legacy", data: j };
        });
    })
    .catch((e) => {
      console.error(e);
      releaseHistoryLoadError = String(e?.message || e);
    })
    .finally(() => {
      releaseHistoryLoading = false;
      if (currentView === "history") renderHistory();
    });
  return;
}
```

### 3. Update Event Processing (In `renderHistory`, around line 3542)

Replace the event flattening code:

**OLD:**
```javascript
const projectsObj = releaseHistoryData?.projects && typeof releaseHistoryData.projects === "object" ? releaseHistoryData.projects : {};

// Flatten events across projects (tolerant of both {key:{events:[]}} and {key:[]})
const allEvents = [];
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
```

**NEW:**
```javascript
// Handle both new (append-only) and legacy formats
let allEvents = [];
let historyIndex = null;
let historyFormat = "legacy";

if (releaseHistoryData?.format === "append-only") {
  // New format: { format: "append-only", index: {...}, events: [...] }
  historyFormat = "append-only";
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
```

### 4. Update Filtering Logic (In `renderHistory`, around line 3567)

Replace the filtering code:

**OLD:**
```javascript
const filtered = allEvents.filter((ev) => {
  if (selectedProject !== "ALL" && ev._projectKey !== selectedProject) return false;

  const kind = String(ev.kind || "TAG_CHANGE").toUpperCase();
  if (kind !== "TAG_CHANGE") return false;

  const envKey = String(ev.envKey || ev.env || "").toLowerCase();
  if (historyFilters.envs.size && !historyFilters.envs.has(envKey)) return false;

  if (!q) return true;
  const hay = [
    ev._projectKey,
    ev.envName,
    ev.envKey,
    ev.component,
    ev.fromTag,
    ev.toTag,
    ev.by,
  ].filter(Boolean).join(" | ").toLowerCase();
  return hay.includes(q);
});
```

**NEW:**
```javascript
const isAdvancedMode = historyFilters.advancedMode || false;
const defaultLimit = historyFilters.defaultLimit || 20;

// Apply filters
let filtered = allEvents.filter((ev) => {
  if (selectedProject !== "ALL" && ev._projectKey !== selectedProject) return false;

  const kind = String(ev.kind || "TAG_CHANGE").toUpperCase();
  if (kind !== "TAG_CHANGE") return false;

  const envKey = String(ev.envKey || ev.env || "").toLowerCase();
  if (historyFilters.envs.size && !historyFilters.envs.has(envKey)) return false;

  // Advanced search filters
  if (isAdvancedMode) {
    // Date range filter
    if (historyFilters.dateFrom) {
      const evDate = ev.at || ev.time || "";
      if (evDate < historyFilters.dateFrom) return false;
    }
    if (historyFilters.dateTo) {
      const evDate = ev.at || ev.time || "";
      if (evDate > historyFilters.dateTo) return false;
    }
    
    // Repository filter
    if (historyFilters.repo) {
      const repo = (ev.commitUrl || "").toLowerCase();
      if (!repo.includes(historyFilters.repo.toLowerCase())) return false;
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

  // Text search
  if (!q) return true;
  const hay = [
    ev._projectKey,
    ev.envName,
    ev.envKey,
    ev.component,
    ev.fromTag,
    ev.toTag,
    ev.by,
  ].filter(Boolean).join(" | ").toLowerCase();
  return hay.includes(q);
});

// Default view: show last N events (unless in advanced mode or search active)
const showLimited = !isAdvancedMode && !q && historyFilters.envs.size === 0 && selectedProject === "ALL";
if (showLimited && filtered.length > defaultLimit) {
  filtered = filtered.slice(0, defaultLimit);
}
```

### 5. Update Filter UI (In `renderHistory`, around line 3647)

Replace the filter header:

**OLD:**
```javascript
<div class="historyFiltersRight">
  <div class="pill softPill">${escapeHtml(String(filtered.length))} events</div>
</div>
```

**NEW:**
```javascript
<div class="historyFiltersRight">
  <button type="button" class="pill softPill" id="historyAdvancedToggle" style="cursor:pointer; border:none; background:transparent;">
    ${historyFilters.advancedMode ? "▼ Advanced" : "▶ Advanced"}
  </button>
  <div class="pill softPill">${escapeHtml(String(filtered.length))}${showLimited && allEvents.length > defaultLimit ? ` of ${allEvents.length}` : ""} events</div>
</div>
</div>

${historyFilters.advancedMode ? `
  <div class="panel" style="margin-top:12px; padding:12px; background:rgba(255,255,255,.02); border:1px solid rgba(255,255,255,.1);">
    <div style="font-weight:600; margin-bottom:8px; font-size:12px;">Advanced Filters</div>
    <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap:12px;">
      <label class="historyLabel">
        <span class="muted" style="font-size:11px;">Date From</span>
        <input type="date" id="historyDateFrom" class="historyInput" value="${escapeAttr(historyFilters.dateFrom)}" style="height:32px;" />
      </label>
      <label class="historyLabel">
        <span class="muted" style="font-size:11px;">Date To</span>
        <input type="date" id="historyDateTo" class="historyInput" value="${escapeAttr(historyFilters.dateTo)}" style="height:32px;" />
      </label>
      <label class="historyLabel">
        <span class="muted" style="font-size:11px;">Repository</span>
        <input type="text" id="historyRepo" class="historyInput" value="${escapeAttr(historyFilters.repo)}" placeholder="repo name" style="height:32px;" />
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
```

### 6. Add Event Handlers (In `renderHistory`, after existing event handlers, around line 3914)

Add after the existing search input handler:

```javascript
// Advanced search toggle
const advancedToggle = el("historyAdvancedToggle");
if (advancedToggle) {
  advancedToggle.onclick = () => {
    historyFilters.advancedMode = !historyFilters.advancedMode;
    renderHistory();
  };
}

// Advanced search inputs
const dateFromInput = el("historyDateFrom");
if (dateFromInput) {
  dateFromInput.onchange = () => {
    historyFilters.dateFrom = dateFromInput.value;
    renderHistory();
  };
}

const dateToInput = el("historyDateTo");
if (dateToInput) {
  dateToInput.onchange = () => {
    historyFilters.dateTo = dateToInput.value;
    renderHistory();
  };
}

const repoInput = el("historyRepo");
if (repoInput) {
  debounce(() => {
    historyFilters.repo = repoInput.value;
    renderHistory();
  }, 300)(repoInput?.value || "");
}

const tagInput = el("historyTag");
if (tagInput) {
  debounce(() => {
    historyFilters.tag = tagInput.value;
    renderHistory();
  }, 300)(tagInput?.value || "");
}

const deployerInput = el("historyDeployer");
if (deployerInput) {
  debounce(() => {
    historyFilters.deployer = deployerInput.value;
    renderHistory();
  }, 300)(deployerInput?.value || "");
}
```

### 7. Update historyFilters Initialization (Around line 121)

**OLD:**
```javascript
const historyFilters = {
  project: "ALL",
  envs: new Set(),
  q: "",
  includeBootstrap: true,
};
```

**NEW:**
```javascript
const historyFilters = {
  project: "ALL",
  envs: new Set(),
  q: "",
  includeBootstrap: true,
  // Advanced search mode
  advancedMode: false,
  dateFrom: "",
  dateTo: "",
  repo: "",
  tag: "",
  deployer: "",
  // Default view limit
  defaultLimit: 20,
};
```

## Testing

After making these changes:

1. **Test with new format**: Ensure `data/release_history/index.json` and `events.jsonl` exist
2. **Test with legacy format**: Ensure fallback works if new format doesn't exist
3. **Test default view**: Should show last 20 events by default
4. **Test advanced search**: Toggle advanced mode and verify filters work
5. **Test event details**: Click on events to ensure details drawer still works

## Summary

These changes:
- ✅ Support new append-only format (index.json + events.jsonl)
- ✅ Fall back to legacy format automatically
- ✅ Show last 20 events by default
- ✅ Add advanced search mode with date range and filters
- ✅ Preserve all existing functionality (event details, filtering, etc.)
