---
name: workspace-basics
description: Safely read, create, edit, and organize files in workspace/, artifacts/, and tmp/, and maintain persistent notes in MEMORY.md.
---

# Workspace & Memory Workflow

Use this skill when working with files in the sandboxed directories or updating persistent agent memory.

## Directory Conventions

1. **`workspace/`** — Primary working directory for user documents, notes, inputs, and code snippets.
2. **`artifacts/`** — Final deliverables, exports, and generated reports meant to be kept.
3. **`tmp/`** — Ephemeral scratchpad files and intermediate outputs.

## File Operations Procedure

1. **Inspect First**: Use `basic.file_list` or `basic.file_glob` to locate files, then `basic.file_read` before modifying existing files.
2. **Create / Overwrite**: Use `basic.file_write` with `overwrite=False` for new files, or `overwrite=True` when intentionally replacing a file.
3. **Targeted Edits**: Prefer `basic.file_edit` with an exact `old_string` and `new_string` over rewriting large files.
4. **Deletions**: `basic.file_delete` is gated behind human confirmation (`ask` tier). Only call it when the user explicitly asks to remove a file.

## Memory Procedure

1. Call `basic.memory_read` before making changes to `MEMORY.md`.
2. Use `basic.memory_edit` for small additions/updates, or `basic.memory_write` when compacting and reorganizing sections.
3. Never store secrets, tokens, or `.env` values in `MEMORY.md`.
