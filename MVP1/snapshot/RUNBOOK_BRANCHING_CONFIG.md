# Runbook Branching Configuration Guide

## Overview

The runbook system uses a flexible, pattern-based configuration to support different branch naming conventions and Git workflows (Gitflow, GitHub Flow, etc.).

## Configuration Schema

Add a `runbooks.branching` section to your project YAML config:

```yaml
runbooks:
  branching:
    # Default branch name (main, master, develop, etc.)
    defaultBranch: main
    
    # Branch matching patterns (regex or glob-like)
    releaseBranchPatterns:
      - "release/.*"           # Generic: any release/* branch
      - "release/v?\\d+\\.\\d+(\\.\\d+)?"  # Regex: release/1.2.3 or release/v1.2.3
      - "release/BE\\.\\d+\\.\\d+"        # TAP2.0 backend format
      - "release/FE\\.\\d+\\.\\d+"        # TAP2.0 frontend format
    
    # Strategy for picking the "latest" release branch
    releaseBranchPickStrategy: semver  # Options: semver, recent, date
    
    # Version extraction regex (optional, auto-detected if not provided)
    versionExtractionRegex: "(\\d+)\\.(\\d+)(?:\\.(\\d+))?"
    
    # Per-repo overrides (optional)
    repoOverrides:
      "repo-name":
        releaseBranchPatterns:
          - "custom-pattern"
        versionExtractionRegex: "v(\\d+)\\.(\\d+)"
```

## Pattern Types

### 1. Glob-like Patterns (Simple)
- `release/*` - Matches any branch starting with `release/`
- `release/v*` - Matches branches like `release/v1.2.3`
- `hotfix/*` - Matches hotfix branches

### 2. Regex Patterns (Advanced)
- `release/\\d+\\.\\d+\\.\\d+` - Matches semantic versions: `release/1.2.3`
- `release/v?\\d+\\.\\d+` - Matches with optional `v` prefix: `release/v1.2` or `release/1.2`
- `release/(BE|FE)\\.\\d+\\.\\d+` - TAP2.0 format: `release/BE.1.31` or `release/FE.1.31`

## Strategies

### `semver` (Recommended)
Picks the branch with the highest semantic version number.
- Works with: `1.2.3`, `v1.2.3`, `BE.1.31`, `0.19.0`
- Extracts version using `versionExtractionRegex` or auto-detection

### `recent`
Picks the lexicographically last branch (alphabetical sorting).
- Simple but may not work correctly for versions like `release/10.0.0` vs `release/2.0.0`

### `date` (Future)
Picks the branch with the most recent commit date.
- Requires additional API calls (not yet implemented)

## Version Extraction

The system automatically extracts version numbers from branch names. You can override with a custom regex:

```yaml
versionExtractionRegex: "(\\d+)\\.(\\d+)(?:\\.(\\d+))?"
```

This regex should have capture groups for:
- Group 1: Major version
- Group 2: Minor version  
- Group 3: Patch version (optional)

## Examples

### Example 1: TAP2.0 (FE/BE Split)
```yaml
runbooks:
  branching:
    defaultBranch: main
    releaseBranchPatterns:
      - "release/BE\\.\\d+\\.\\d+"  # Backend: release/BE.1.31
      - "release/FE\\.\\d+\\.\\d+"  # Frontend: release/FE.1.31
    releaseBranchPickStrategy: semver
    versionExtractionRegex: "(?:BE|FE)\\.(\\d+)\\.(\\d+)"
```

### Example 2: TCBP (Semantic Versioning)
```yaml
runbooks:
  branching:
    defaultBranch: main
    releaseBranchPatterns:
      - "release/\\d+\\.\\d+\\.\\d+"  # release/0.19.0
    releaseBranchPickStrategy: semver
    versionExtractionRegex: "(\\d+)\\.(\\d+)\\.(\\d+)"
```

### Example 3: Gitflow (release/* and hotfix/*)
```yaml
runbooks:
  branching:
    defaultBranch: develop
    releaseBranchPatterns:
      - "release/.*"
      - "hotfix/.*"
    releaseBranchPickStrategy: semver
    versionExtractionRegex: "(\\d+)\\.(\\d+)(?:\\.(\\d+))?"
```

### Example 4: GitHub Flow (Simple tags)
```yaml
runbooks:
  branching:
    defaultBranch: main
    releaseBranchPatterns:
      - "v\\d+\\.\\d+\\.\\d+"  # Tags like v1.2.3
    releaseBranchPickStrategy: semver
```

### Example 5: Custom Format (release-v1.2.3)
```yaml
runbooks:
  branching:
    defaultBranch: main
    releaseBranchPatterns:
      - "release-v\\d+\\.\\d+\\.\\d+"
    releaseBranchPickStrategy: semver
    versionExtractionRegex: "release-v(\\d+)\\.(\\d+)\\.(\\d+)"
```

## Auto-Detection

If no configuration is provided, the system uses intelligent defaults:

1. **Default branch**: Tries `main`, then `master`, then `develop`
2. **Patterns**: Falls back to `release/*` and common patterns
3. **Version extraction**: Auto-detects common formats:
   - `x.y.z` (semantic version)
   - `x.y` (major.minor)
   - `BE.x.y` / `FE.x.y` (TAP2.0 format)
   - `vx.y.z` (with v prefix)

## Per-Repo Overrides

For projects with mixed conventions, you can override per repository:

```yaml
runbooks:
  branching:
    defaultBranch: main
    releaseBranchPatterns:
      - "release/.*"
    repoOverrides:
      "frontend-app":
        releaseBranchPatterns:
          - "release/FE\\.\\d+\\.\\d+"
      "backend-api":
        releaseBranchPatterns:
          - "release/BE\\.\\d+\\.\\d+"
```

## Frontend/Backend Detection

The system automatically detects frontend repos by checking if the repo name contains:
- `frontend`
- `-fe`
- Ends with `-fe`

For FE repos, it filters to only match FE patterns (if both BE and FE patterns are provided).

## Migration from Hardcoded Logic

The old hardcoded TAP2.0/TCBP logic is replaced with this generic system. Existing configs without `runbooks.branching` will use auto-detection, which should work for most cases.
