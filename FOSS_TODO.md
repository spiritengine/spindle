# Spindle FOSS Release Checklist

Preparing Spindle for open-source release.

## High Priority

### Licensing
- [ ] **Add LICENSE file** - Pending decision on MIT vs Apache 2.0
  - MIT: Simple, permissive, widely used
  - Apache 2.0: Includes patent grant, more corporate-friendly
  - **ACTION NEEDED**: Patrick to decide on license

### Package Metadata
- [x] **Enhance pyproject.toml**
  - Added keywords and classifiers
  - Added optional dev dependencies (pytest, ruff)
  - Added ruff and pytest configuration
  - License field ready (commented, uncomment after adding LICENSE)
  - URL fields ready (commented, fill in after repo created)

### Code Cleanup
- [x] No hardcoded paths found (clean!)
- [x] No personal references (clean!)
- [x] **Remove debug logging** in `spin()` function - DONE

### Basic Hygiene
- [x] **Add .gitignore** - Added standard Python ignores + project-specific

## Medium Priority

### Documentation
- [x] **Review README.md**
  - Added Features section
  - Added Requirements section
  - Added Configuration/env vars section
  - Added How it Works section
  - Added Contributing link
  - Badge placeholders ready

- [x] **Add CONTRIBUTING.md**
  - Dev setup instructions
  - Testing instructions
  - Architecture overview

### Testing
- [x] **Create basic test suite** (18 tests passing)
  - Permission profile tests
  - Spool storage tests
  - Process utility tests
  - Data structure tests

- [x] **Add GitHub Actions CI**
  - Tests on Python 3.10, 3.11, 3.12
  - Ruff linting and format checking

### Cleanup
- [x] **Removed spindle-wrapper.sh** - Not needed with pip install

## Low Priority

### Nice to Have
- [ ] Add type hints throughout (partially present)
- [ ] Add docstrings to all public functions
- [ ] Consider adding a CHANGELOG.md
- [x] Add example systemd service file to repo + `spindle install-service` command

### Configuration
- [x] SPINDLE_DIR is `~/.spindle/spools` (sensible default)
- [x] MAX_CONCURRENT configurable via `SPINDLE_MAX_CONCURRENT` env var
- [x] Document all environment variables in README

## Summary

**Completed:**
- .gitignore
- pyproject.toml enhancements
- Debug logging removed
- README polished
- CONTRIBUTING.md
- Test suite (18 tests)
- GitHub Actions CI

**Remaining:**
- LICENSE file (needs Patrick's decision on MIT vs Apache 2.0)
- Uncomment license/URL fields in pyproject.toml after repo is set up
- Uncomment badges in README after CI is running
