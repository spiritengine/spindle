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
- Test suite (18 tests)
- GitHub Actions CI

**Remaining:**
- Uncomment badges in README after CI is running publicly
- Publish to PyPI (name `spindle-mcp`) and tag the release
