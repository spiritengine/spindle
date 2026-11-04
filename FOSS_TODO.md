# Spindle FOSS Release Checklist

Preparing Spindle for open-source release.

## High Priority

### Licensing
- [x] **Add LICENSE file** - MIT (see LICENSE, `license = "MIT"` in pyproject.toml)

### Package Metadata
- [x] **Enhance pyproject.toml**
  - Added keywords and classifiers
  - Added optional dev dependencies (pytest, ruff)
  - Added ruff and pytest configuration
  - License field set (`license = "MIT"`)
  - URL fields point at github.com/spiritengine/spindle

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
- [x] **Create test suite** (about 2,500 tests as of 2026-11-03)
  - Permission profile tests
  - Spool storage tests
  - Process utility tests
  - Data structure tests

- [x] **Add GitHub Actions CI**
  - Tests on Python 3.11, 3.12, 3.13, 3.14
  - Ruff linting and format checking

### Cleanup
- [x] **Removed spindle-wrapper.sh** - Not needed with pip install

## Low Priority

### Nice to Have
- [ ] Add type hints throughout (partially present)
- [ ] Add docstrings to all public functions
- [x] Add a CHANGELOG.md - added for 1.2.0
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
- Test suite
- GitHub Actions CI
- Published to PyPI as `spindle-mcp` (1.0.0 through 1.2.0)

**Remaining:**
- Uncomment badges in README once CI on master is green
- Tag releases in git: no published version has a tag yet
