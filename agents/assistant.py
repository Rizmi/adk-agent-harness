"""Default General-Purpose Assistant Agent.

Demonstrates declarative agent scoping with:
- System instructions loaded from AGENTS.md
- Workflow skills from skills/
- Zero-trust default-deny permissions (explicit allow + human-approval ask tier)
"""

from pathlib import Path
from harness.schema import AgentDefinition

INSTRUCTIONS = (Path(__file__).resolve().parent.parent / "AGENTS.md").read_text(encoding="utf-8").strip()

agent = AgentDefinition(
    name="assistant",
    description="General-purpose workspace assistant with sandboxed file, skill, and memory tools",
    instructions=INSTRUCTIONS,
    model=None,  # None = use active ADK_MODEL from .env
    skills=[
        "workspace-basics",
    ],
    permissions={
        "allow": [
            "filesystem.file_read",
            "filesystem.file_glob",
            "filesystem.file_list",
            "filesystem.file_write",
            "filesystem.file_edit",
            "skills.skill_list",
            "skills.skill_load",
            "memory.memory_read",
            "memory.memory_write",
            "memory.memory_edit",
        ],
        # Destructive operations require human confirmation via /api/session/{id}/approval
        "ask": [
            "filesystem.file_delete",
        ],
    },
)
