# Agent Skills Frontmatter Migration Design

## Goal

Migrate this repo's agent skill system from `metadata.yaml + instructions.md` to Anthropic-compatible `SKILL.md` files with YAML frontmatter, while preserving progressive disclosure and keeping the current `list_skills` / `read_skill` tools robust.

## Background

Anthropic Agent Skills use one required file per skill directory:

```markdown
---
name: your-skill-name
description: Brief description of what this Skill does and when to use it
---

# Your Skill Name

## Instructions
[Clear guidance]
```

Only frontmatter metadata is used for discovery. The full `SKILL.md` body is loaded only after the agent selects a skill.

## Requirements

- Each non-hidden direct child of `backend/skills/` can be a skill directory.
- Directories whose names start with `.` are ignored.
- A valid skill directory contains `SKILL.md`.
- `list_skills()` reads only the YAML frontmatter from each `SKILL.md` and returns `name`, `description`, and `path` metadata.
- `list_skills()` must not load or return the full skill body.
- `read_skill(skill_name)` returns the full `SKILL.md` content for the chosen skill.
- `read_skill()` continues to replace `{{SKILL_DIR}}` and `{{SESSION_ID}}` in the returned content.
- Skill names follow Anthropic constraints: lowercase letters, numbers, hyphens, max 64 characters.
- Existing underscore directory names remain usable as aliases during migration.
- `metadata.yaml` is no longer required.
- The implementation should fail gracefully for malformed skill files and include useful error messages.

## Current Plan A

Keep the current two exposed agent tools:

- `list_skills`: returns all available skills' frontmatter metadata.
- `read_skill`: loads the selected skill's full guidance.

This is acceptable for the current repo-scale skill set.

## Future Plan B Constraint

For a future corpus with 80k+ skills, `list_skills` should not be exposed in the prompt workflow because returning all metadata would exceed context limits. The future tool should be `search_skills`, backed by vector search over skill descriptions.

To avoid blocking Plan B, the Plan A implementation should keep parsing and indexing logic small, deterministic, and reusable. It should not bake UI wording or current small-scale assumptions into low-level parsing helpers.

## Architecture

`backend/tools/skill_ops.py` owns skill discovery and loading:

- `_extract_frontmatter(text)` parses only an initial YAML frontmatter block.
- `_load_skill_record(skill_dir)` validates one skill directory and returns metadata plus path.
- `_iter_skill_records()` scans non-hidden child directories of `backend/skills/`.
- `list_skills()` serializes discovery metadata to JSON.
- `read_skill(skill_name)` resolves by frontmatter name, directory name, or underscore/hyphen aliases, then returns the full `SKILL.md` with template substitutions.

## Skill File Migration

Current skills become:

- `backend/skills/load_local_images/SKILL.md` with `name: load-local-images`
- `backend/skills/git_workflow/SKILL.md` with `name: git-workflow`
- `backend/skills/playwright-cli/SKILL.md` with `name: playwright-browser-skill`
- `backend/skills/collect_texts_to_csv/SKILL.md` with `name: collect-texts-to-csv`

Existing script files remain in place and are referenced from `SKILL.md`.

## Testing

Update `tests/test_skill_session_id.py` to cover:

- `list_skills()` reads frontmatter names and descriptions from `SKILL.md`.
- `list_skills()` ignores hidden directories.
- `list_skills()` does not expose body text.
- `read_skill()` resolves by directory name, frontmatter name, and underscore/hyphen aliases.
- `read_skill()` replaces `{{SKILL_DIR}}` and `{{SESSION_ID}}`.
- malformed or missing frontmatter is handled without crashing.

## Non-Goals

- Do not implement vector search or `search_skills` in this change.
- Do not scan nested skill registries under hidden directories.
- Do not support arbitrary legacy `metadata.yaml` as the primary runtime format.
