# Agent Skills Frontmatter Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace repo skills discovery with Anthropic-style `SKILL.md` frontmatter while preserving `list_skills` and `read_skill` behavior.

**Architecture:** `skill_ops.py` will parse `SKILL.md` frontmatter for discovery and read the full file only on demand. Existing skill directories keep their scripts, while old `metadata.yaml` and `instructions.md` files are removed after equivalent `SKILL.md` files are created.

**Tech Stack:** Python standard library, PyYAML, pytest, smolagents tool wrappers.

---

### Task 1: Tests For Frontmatter Discovery

**Files:**
- Modify: `tests/test_skill_session_id.py`

- [x] **Step 1: Replace legacy fixture with SKILL.md fixture**

Create temporary skill directories with `SKILL.md` frontmatter and body content instead of `metadata.yaml` and `instructions.md`.

- [x] **Step 2: Add discovery tests**

Cover frontmatter parsing, hidden directory ignoring, body non-disclosure, and malformed frontmatter graceful handling.

- [x] **Step 3: Add read tests**

Cover resolution by directory name, frontmatter name, hyphen/underscore alias, `{{SKILL_DIR}}`, and `{{SESSION_ID}}` substitution.

- [x] **Step 4: Run focused tests and confirm failure before implementation**

Run: `./.sglang/bin/python -m pytest tests/test_skill_session_id.py -q`

Expected: failing assertions because `skill_ops.py` still requires `metadata.yaml` and `instructions.md`.

### Task 2: Implement SKILL.md Parsing

**Files:**
- Modify: `backend/tools/skill_ops.py`

- [x] **Step 1: Add frontmatter helper**

Implement `_extract_frontmatter(text: str) -> tuple[dict, str]` that only accepts YAML frontmatter at the very start of a file.

- [x] **Step 2: Add skill record helper**

Implement `_load_skill_record(skill_dir: Path) -> dict` that validates `SKILL.md`, validates `name` and `description`, and returns `name`, `description`, `path`, `directory`, and optional `error`.

- [x] **Step 3: Update list_skills**

Scan only direct non-hidden directories and serialize frontmatter metadata without body text.

- [x] **Step 4: Update read_skill**

Resolve by frontmatter name, directory name, and underscore/hyphen aliases; read full `SKILL.md`; replace `{{SKILL_DIR}}` and `{{SESSION_ID}}`.

- [x] **Step 5: Run focused tests**

Run: `./.sglang/bin/python -m pytest tests/test_skill_session_id.py -q`

Expected: all tests pass.

### Task 3: Migrate Repo Skills

**Files:**
- Create: `backend/skills/load_local_images/SKILL.md`
- Create: `backend/skills/git_workflow/SKILL.md`
- Create: `backend/skills/playwright-cli/SKILL.md`
- Create: `backend/skills/collect_texts_to_csv/SKILL.md`
- Delete: old `metadata.yaml` and `instructions.md` files in those directories

- [x] **Step 1: Create SKILL.md files**

Move each old description into frontmatter and each old instruction body into the markdown body.

- [x] **Step 2: Use Anthropic-compatible names**

Use hyphenated frontmatter names: `load-local-images`, `git-workflow`, `playwright-browser-skill`, `collect-texts-to-csv`.

- [x] **Step 3: Preserve script references**

Keep `{{SKILL_DIR}}/load_local_images.py` and `{{SKILL_DIR}}/collect_texts_to_csv.py` references unchanged.

### Task 4: Documentation And Verification

**Files:**
- Modify: `backend/tools/README.md`

- [x] **Step 1: Update tool README**

Document `SKILL.md` frontmatter discovery and on-demand full read.

- [x] **Step 2: Run focused tests**

Run: `./.sglang/bin/python -m pytest tests/test_skill_session_id.py -q`

Expected: all tests pass.

- [x] **Step 3: Run a lightweight import check**

Run: `./.sglang/bin/python -m pytest tests/test_skill_session_id.py tests/test_write_memory_tool_calls.py -q`

Expected: all tests pass.

## Self-Review

- The plan covers Anthropic frontmatter, Plan A tools, hidden directory ignoring, alias compatibility, and Plan B-friendly parser boundaries.
- No vector search or `search_skills` implementation is included.
- No git commit is planned because repo instructions require explicit user permission before git commands.
