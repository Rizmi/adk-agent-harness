"""Agent Definition Schema.

Defines the structure for standalone, pluggable agent specifications.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AgentDefinition:
    """Standalone specification for a pluggable agent."""

    name: str
    description: str
    instructions: str
    model: Optional[str] = None
    skills: List[str] = field(default_factory=list)
    permissions: Dict[str, Any] = field(
        default_factory=lambda: {"allow": [], "ask": []}
    )

    def validate(self) -> None:
        """Validate agent definition fields."""
        if not self.name or not self.name.strip():
            raise ValueError("Agent definition requires a non-empty 'name'.")
        if not self.instructions or not self.instructions.strip():
            raise ValueError(f"Agent '{self.name}' requires non-empty 'instructions'.")
        if not isinstance(self.permissions, dict):
            raise ValueError(f"Agent '{self.name}' permissions must be a dictionary.")
        if "allow" not in self.permissions and "ask" not in self.permissions:
            raise ValueError(f"Agent '{self.name}' permissions must contain 'allow' or 'ask' list.")
