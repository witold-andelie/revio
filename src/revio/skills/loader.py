"""Discover and load skills following the Anthropic Agent Skills spec.

Each skill lives in its own directory containing at minimum a `SKILL.md`
file with YAML frontmatter. We also let skill authors drop supplementary
files (templates / examples / scripts) in the same directory — those are
made available to the agent if it loads the skill.

The loader is intentionally simple: it does discovery and parsing only.
Activation logic + plan/react integration lives in `activation.py`.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


logger = logging.getLogger(__name__)


# --- Filesystem locations -----------------------------------------------------


def project_skills_dir(cwd: Path | None = None) -> Path:
    """Project-level skills dir (intended to be committed)."""
    return (cwd or Path.cwd()) / ".revio" / "skills"


def user_skills_dir() -> Path:
    """User-global skills dir (XDG / APPDATA conformant)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "revio" / "skills"


# --- Models -------------------------------------------------------------------


@dataclass
class SkillMatchRules:
    """Optional auto-activation rules embedded in a skill's frontmatter."""

    extensions: list[str] = field(default_factory=list)         # e.g. [".tsx", ".jsx"]
    imports: list[str] = field(default_factory=list)            # e.g. ["react", "next"]
    filename_patterns: list[str] = field(default_factory=list)  # e.g. ["**/app/**/*"]
    frameworks: list[str] = field(default_factory=list)         # e.g. ["react", "vue"]
    languages: list[str] = field(default_factory=list)          # e.g. ["javascript", "python"]


@dataclass
class Skill:
    """A loaded skill — frontmatter parsed, body lazily readable."""

    name: str
    description: str
    when_to_use: str
    matches: SkillMatchRules
    source: str                  # "project" | "user"
    skill_dir: Path
    body_path: Path

    # Filled in lazily by `load_body()` to support progressive disclosure
    _body: str | None = None

    def load_body(self) -> str:
        """Read the full markdown body (skipping frontmatter)."""
        if self._body is not None:
            return self._body
        try:
            raw = self.body_path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("skill body read failed %s: %s", self.body_path, e)
            self._body = ""
            return self._body
        self._body = _strip_frontmatter(raw)
        return self._body

    def list_files(self) -> list[Path]:
        """Supplementary files in the skill directory (templates, scripts, examples)."""
        if not self.skill_dir.is_dir():
            return []
        return [
            p for p in sorted(self.skill_dir.iterdir())
            if p.is_file() and p.name != "SKILL.md"
        ]


@dataclass
class SkillActivation:
    """Why a skill is being activated for the current session."""

    skill: Skill
    matched_rules: list[str] = field(default_factory=list)  # human-readable reasons
    auto_activated: bool = True                              # vs explicitly invoked


# --- Discovery + parsing ------------------------------------------------------


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _strip_frontmatter(content: str) -> str:
    """Return the body (everything after the YAML frontmatter)."""
    m = _FRONTMATTER_RE.match(content)
    if m:
        return content[m.end():]
    return content


def _parse_frontmatter(content: str) -> dict[str, Any]:
    """Extract the YAML frontmatter dict from a SKILL.md file."""
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        logger.warning("YAML frontmatter parse error: %s", e)
        return {}
    return data if isinstance(data, dict) else {}


def _load_one(skill_dir: Path, source: str) -> Skill | None:
    """Load a single skill from its directory. Returns None on failure."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        logger.debug("skipping %s: no SKILL.md", skill_dir)
        return None

    try:
        content = skill_md.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("skill read failed %s: %s", skill_md, e)
        return None

    fm = _parse_frontmatter(content)
    name = fm.get("name") or skill_dir.name
    description = (fm.get("description") or "").strip()
    when_to_use = (fm.get("when_to_use") or "").strip()

    if not description:
        logger.warning("skill %s missing 'description' — skipping", skill_md)
        return None

    matches_dict = fm.get("matches") or {}
    if not isinstance(matches_dict, dict):
        matches_dict = {}

    rules = SkillMatchRules(
        extensions=list(matches_dict.get("extensions") or []),
        imports=list(matches_dict.get("imports") or []),
        filename_patterns=list(matches_dict.get("filename_patterns") or []),
        frameworks=list(matches_dict.get("frameworks") or []),
        languages=list(matches_dict.get("languages") or []),
    )

    return Skill(
        name=name,
        description=description,
        when_to_use=when_to_use,
        matches=rules,
        source=source,
        skill_dir=skill_dir,
        body_path=skill_md,
    )


def discover_skills(
    *,
    project_root: Path | None = None,
    include_user: bool = True,
) -> dict[str, Skill]:
    """Discover all skills from user + project dirs.

    Returns: {skill_name: Skill}. Project skills shadow user skills by name.
    """
    skills: dict[str, Skill] = {}

    # 1. User-level (loaded first so project can shadow)
    if include_user:
        user_root = user_skills_dir()
        if user_root.is_dir():
            for sub in sorted(user_root.iterdir()):
                if not sub.is_dir():
                    continue
                s = _load_one(sub, source="user")
                if s is not None:
                    skills[s.name] = s

    # 2. Project-level (overrides user on name collision)
    proj_root = project_skills_dir(project_root)
    if proj_root.is_dir():
        for sub in sorted(proj_root.iterdir()):
            if not sub.is_dir():
                continue
            s = _load_one(sub, source="project")
            if s is not None:
                skills[s.name] = s  # project wins on collision

    return skills


# --- Registry -----------------------------------------------------------------


class SkillsRegistry:
    """Holds discovered skills + provides activation queries.

    Activation is "what skills should we surface to the LLM for this session?"
    The default heuristic: a skill activates if ANY of its match rules fire
    against the current fingerprint (extensions, imports, languages, etc.)
    OR if it's explicitly requested.
    """

    def __init__(self, skills: dict[str, Skill]):
        self.skills = dict(skills)

    @classmethod
    def discover(cls, project_root: Path | None = None) -> "SkillsRegistry":
        return cls(discover_skills(project_root=project_root))

    def all(self) -> list[Skill]:
        # Stable sort by name for deterministic prompt ordering
        return [self.skills[k] for k in sorted(self.skills)]

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name)

    def activate_for(
        self,
        *,
        extensions: set[str] | None = None,
        imports: set[str] | None = None,
        languages: set[str] | None = None,
        frameworks: set[str] | None = None,
        filenames: list[str] | None = None,
    ) -> list[SkillActivation]:
        """Decide which skills auto-activate for a given fingerprint."""
        activations: list[SkillActivation] = []
        extensions = extensions or set()
        imports = imports or set()
        languages = languages or set()
        frameworks = frameworks or set()
        filenames = filenames or []

        for skill in self.all():
            reasons: list[str] = []
            rules = skill.matches

            # No explicit rules → never auto-activates (still loadable on demand)
            if not any([rules.extensions, rules.imports, rules.filename_patterns,
                        rules.frameworks, rules.languages]):
                continue

            for ext in rules.extensions:
                if ext.lower() in extensions:
                    reasons.append(f"extension {ext}")
                    break

            for imp in rules.imports:
                if imp in imports:
                    reasons.append(f"imports {imp!r}")
                    break

            for fw in rules.frameworks:
                if fw in frameworks:
                    reasons.append(f"framework {fw}")
                    break

            for lang in rules.languages:
                if lang in languages:
                    reasons.append(f"language {lang}")
                    break

            for pattern in rules.filename_patterns:
                if any(fnmatch.fnmatch(fn, pattern) for fn in filenames):
                    reasons.append(f"filename matches {pattern!r}")
                    break

            if reasons:
                activations.append(SkillActivation(
                    skill=skill,
                    matched_rules=reasons,
                    auto_activated=True,
                ))

        return activations
