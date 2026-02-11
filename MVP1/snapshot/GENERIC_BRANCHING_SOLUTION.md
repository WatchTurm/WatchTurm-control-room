# Generic Branching Solution for Runbooks

## Problem

Different companies use different branch naming conventions and Git workflows:
- **TAP2.0**: `release/BE.1.31`, `release/FE.1.31` (FE/BE split)
- **TCBP**: `release/0.19.0` (semantic versioning)
- **Gitflow**: `release/1.2.3`, `hotfix/1.2.4`
- **GitHub Flow**: `main` branch only, tags like `v1.2.3`
- **Custom**: `release-v1.2.3`, `v1.2.3`, etc.

The original implementation had hardcoded logic for TAP2.0 and TCBP, which doesn't scale.

## Solution

A **pattern-based, configurable system** that works with minimal configuration:

### Key Features

1. **Regex Pattern Matching**: Support for both regex and glob-like patterns
2. **Auto-Detection**: Intelligent defaults if no config provided
3. **Per-Repo Overrides**: Different conventions per repository
4. **Flexible Version Extraction**: Custom regex or auto-detection
5. **Multiple Strategies**: `semver` (recommended) or `recent`

### Configuration

Add to your project YAML:

```yaml
runbooks:
  branching:
    defaultBranch: main
    releaseBranchPatterns:
      - "release/.*"                    # Generic
      - "release/\\d+\\.\\d+\\.\\d+"     # Semantic versioning
      - "release/BE\\.\\d+\\.\\d+"      # TAP2.0 backend
    releaseBranchPickStrategy: semver
    versionExtractionRegex: "(\\d+)\\.(\\d+)(?:\\.(\\d+))?"  # Optional
```

### Auto-Detection

If no configuration is provided, the system:
1. Tries common default branches: `main`, `master`, `develop`
2. Uses generic patterns: `release/*`, `release/\\d+\\.\\d+(\\.\\d+)?`
3. Auto-detects version formats:
   - `x.y.z` (semantic version)
   - `x.y` (major.minor)
   - `BE.x.y` / `FE.x.y` (TAP2.0)
   - `vx.y.z` (with v prefix)

### Migration

**Before** (hardcoded):
- Code had special cases for TAP2.0 and TCBP
- Required code changes for new formats

**After** (configurable):
- Add patterns to YAML config
- No code changes needed
- Works with any naming convention

### Examples

See `RUNBOOK_BRANCHING_CONFIG.md` for detailed examples covering:
- TAP2.0 (FE/BE split)
- TCBP (semantic versioning)
- Gitflow (release/* and hotfix/*)
- GitHub Flow (tags)
- Custom formats

### Benefits

1. **Minimal Configuration**: Works out-of-the-box with auto-detection
2. **Flexible**: Supports any branch naming convention via regex
3. **Maintainable**: No code changes needed for new formats
4. **Backward Compatible**: Existing configs continue to work
5. **Per-Repo Support**: Different conventions per repository

### Implementation Details

- **Pattern Matching**: Supports both regex (`release/\\d+\\.\\d+`) and glob-like (`release/*`)
- **FE/BE Detection**: Automatically filters by repo name (frontend vs backend)
- **Version Extraction**: Custom regex or auto-detection of common formats
- **Strategy Selection**: `semver` (version-based) or `recent` (alphabetical)

### Testing

To test with your branch naming convention:

1. Add `runbooks.branching` section to your project YAML
2. Define patterns matching your branches
3. Run a scope/drift/readiness check
4. Check the "View Detailed Output" modal to see detected branches

If branches aren't detected:
- Verify your regex patterns (test at https://regex101.com)
- Check the warnings in the runbook output
- Try more generic patterns first, then narrow down
