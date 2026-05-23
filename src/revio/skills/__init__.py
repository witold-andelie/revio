"""Skills — Anthropic Agent Skills spec.

A "skill" is a procedural-knowledge unit (vs RAG's factual knowledge): a
markdown file with YAML frontmatter that describes *when* it applies and
*how* to handle that scenario. The agent loads the lightweight description
during the plan phase, decides which skills are relevant for the current
session, then loads the full body on demand.

Directory convention (dual layer, project shadows user):
    .revio/skills/<name>/SKILL.md           # project — committable
    ~/.config/revio/skills/<name>/SKILL.md  # user-global — personal

YAML frontmatter (revio extends the Anthropic spec):
    ---
    name: review-react-server-components
    description: How to review React Server Components for correctness + perf
    when_to_use: Reviewing TSX files under app/ or pages/ that mix server/client code
    matches:                                          # optional auto-activation
      extensions: [".tsx", ".jsx"]
      imports: ["react", "next"]
      filename_patterns: ["**/app/**/*", "**/pages/**/*"]
    ---

    # Body markdown — full instructions, examples, anti-patterns…
"""

from .loader import (
    Skill,
    SkillActivation,
    SkillsRegistry,
    discover_skills,
    project_skills_dir,
    user_skills_dir,
)

__all__ = [
    "Skill",
    "SkillActivation",
    "SkillsRegistry",
    "discover_skills",
    "project_skills_dir",
    "user_skills_dir",
]
