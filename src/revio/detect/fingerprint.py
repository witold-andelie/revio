"""Project fingerprinting.

Given a directory, identify:
- Primary language(s) (by file extension distribution + marker files)
- Frameworks in use (from manifests like package.json)
- PLC vendor (if PLC content present)
- Which profile to activate

Strategy: marker files give strong signals; extension counts disambiguate.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, Field


# Directories to ignore when counting extensions
_IGNORE_DIRS = {
    "node_modules", "__pycache__", ".git", "venv", ".venv", "env",
    "dist", "build", "target", ".next", ".nuxt", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", "coverage", "htmlcov",
    ".idea", ".vscode", ".gradle", ".terraform",
}

# Extension → language label
_EXT_LANG: dict[str, str] = {
    # JS family
    ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".mjs": "javascript", ".cjs": "javascript",
    # Python
    ".py": "python",
    # PLC (text)
    ".st": "structured_text", ".iecst": "structured_text",
    # PLC (vendor XML — needs content inspection to confirm)
    ".l5x": "plc_rockwell",
    ".smc2": "plc_omron",
    # Others (for future profiles, currently unmapped to a profile)
    ".java": "java", ".kt": "kotlin",
    ".go": "go", ".rs": "rust",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp",
    ".rb": "ruby", ".php": "php",
}

# Language → which profile should handle it
_LANG_PROFILE: dict[str, str] = {
    "javascript": "js", "typescript": "js",
    "python": "python",
    "structured_text": "plc",
    "plc_rockwell": "plc", "plc_omron": "plc", "plc_siemens": "plc",
    "plc_beckhoff": "plc", "plc_codesys": "plc",
}


class ProjectFingerprint(BaseModel):
    """A summary of what a project directory contains."""

    root: str

    # Distribution
    extension_counts: dict[str, int] = Field(default_factory=dict)
    total_files: int = 0

    # Languages, ordered by file-count desc
    languages: list[str] = Field(default_factory=list)
    primary_language: str | None = None

    # Frameworks (from manifest inspection)
    frameworks: list[str] = Field(default_factory=list)

    # PLC-specific
    plc_vendor: str | None = None  # siemens | beckhoff | codesys | rockwell | abb | ge | omron

    # Recommended profile
    suggested_profile: str = "auto"  # auto | js | plc | python — what we'd activate

    # Marker files we found
    markers: list[str] = Field(default_factory=list)


# --- Detection -----------------------------------------------------------------


def detect_project(root: Path | str) -> ProjectFingerprint:
    """Walk root, build a fingerprint."""
    root = Path(root).expanduser().resolve()
    fp = ProjectFingerprint(root=str(root))

    if not root.is_dir():
        return fp

    # Pass 1: walk files, count extensions, note markers
    ext_counter: Counter[str] = Counter()
    total = 0
    has_package_json = False
    has_pyproject = False
    plc_xmls: list[Path] = []

    for p in _walk(root):
        total += 1
        ext = p.suffix.lower()
        if ext:
            ext_counter[ext] += 1

        name = p.name
        if name == "package.json":
            has_package_json = True
            fp.markers.append(str(p.relative_to(root)))
        elif name == "pyproject.toml":
            has_pyproject = True
            fp.markers.append(str(p.relative_to(root)))
        elif name == "tsconfig.json":
            fp.markers.append(str(p.relative_to(root)))
        elif name == "go.mod":
            fp.markers.append(str(p.relative_to(root)))
        elif name == "Cargo.toml":
            fp.markers.append(str(p.relative_to(root)))
        elif ext == ".l5x":
            plc_xmls.append(p)
            fp.markers.append(str(p.relative_to(root)))
        elif ext == ".smc2":
            plc_xmls.append(p)
            fp.markers.append(str(p.relative_to(root)))
        elif ext == ".xml" and total <= 5000:  # inspect a sample to avoid huge scans
            plc_xmls.append(p)

    fp.total_files = total
    fp.extension_counts = dict(ext_counter)

    # Pass 2: compute language distribution
    lang_counts: Counter[str] = Counter()
    for ext, count in ext_counter.items():
        lang = _EXT_LANG.get(ext)
        if lang:
            lang_counts[lang] += count

    fp.languages = [lang for lang, _ in lang_counts.most_common()]
    fp.primary_language = fp.languages[0] if fp.languages else None

    # Pass 3: framework hints from manifests
    if has_package_json:
        fp.frameworks.extend(_detect_js_frameworks(root / "package.json"))

    # Pass 4: PLC vendor detection (only if XML found, peek at content)
    if plc_xmls:
        fp.plc_vendor = _detect_plc_vendor(plc_xmls[:20])  # check up to 20 to be safe
        if fp.plc_vendor:
            # Promote PLC if vendor confirmed
            if not fp.primary_language or fp.primary_language not in {"javascript", "typescript", "python"}:
                fp.primary_language = f"plc_{fp.plc_vendor}"

    # Pass 5: suggested profile
    fp.suggested_profile = _suggest_profile(fp)

    return fp


def _walk(root: Path):
    """Yield files, skipping noise dirs."""
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        # Skip if any parent dir is in ignore set
        if any(part in _IGNORE_DIRS for part in p.relative_to(root).parts[:-1]):
            continue
        yield p


def _detect_js_frameworks(package_json_path: Path) -> list[str]:
    """Inspect package.json deps for known frameworks."""
    try:
        data = json.loads(package_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    deps = {}
    deps.update(data.get("dependencies") or {})
    deps.update(data.get("devDependencies") or {})

    hits: list[str] = []
    framework_markers = {
        "react": "react",
        "next": "nextjs",
        "vue": "vue",
        "nuxt": "nuxt",
        "@angular/core": "angular",
        "svelte": "svelte",
        "express": "express",
        "@nestjs/core": "nestjs",
        "fastify": "fastify",
        "@prisma/client": "prisma",
        "typeorm": "typeorm",
        "mongoose": "mongoose",
        "sequelize": "sequelize",
    }
    for dep, label in framework_markers.items():
        if dep in deps:
            hits.append(label)
    return hits


def _detect_plc_vendor(xml_paths: list[Path]) -> str | None:
    """Peek at XML headers to guess vendor."""
    # Signatures (lowercase substring matched in first 4KB)
    sigs = {
        "siemens": ("simaticml", "siemens.com/automation"),
        "beckhoff": ("tcpou", "twincat"),
        "codesys": ("project xmlns", "codesys"),
        "rockwell": ("rslogix", "rsl5kfile"),
        "abb": ("abb.com/automation",),
        "ge": ("ge-ip", "ge.com/automation"),
        "omron": ("omron",),
    }
    for path in xml_paths:
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                head = f.read(4096).lower()
        except OSError:
            continue
        for vendor, markers in sigs.items():
            if any(m in head for m in markers):
                return vendor
    return None


def _suggest_profile(fp: ProjectFingerprint) -> str:
    """Pick the best profile name for the agent to activate."""
    if fp.plc_vendor or "structured_text" in fp.languages:
        return "plc"
    if not fp.primary_language:
        return "auto"
    profile = _LANG_PROFILE.get(fp.primary_language)
    return profile or "auto"


# --- Display -------------------------------------------------------------------


def summarize_fingerprint(fp: ProjectFingerprint) -> str:
    """Build a human-readable summary of a fingerprint (for stream output)."""
    lines: list[str] = []
    if fp.primary_language:
        # Compute percentage if extensions known
        lang_files = sum(
            n for ext, n in fp.extension_counts.items()
            if _EXT_LANG.get(ext) == fp.primary_language
        )
        pct = (lang_files / fp.total_files * 100) if fp.total_files else 0
        lines.append(f"Primary language : {fp.primary_language} ({pct:.0f}% of files)")
    else:
        lines.append("Primary language : (unknown)")

    if fp.frameworks:
        lines.append(f"Frameworks       : {', '.join(fp.frameworks)}")
    if fp.plc_vendor:
        lines.append(f"PLC vendor       : {fp.plc_vendor}")
    if fp.markers:
        shown = fp.markers[:3]
        more = f" (+{len(fp.markers) - 3} more)" if len(fp.markers) > 3 else ""
        lines.append(f"Marker files     : {', '.join(shown)}{more}")
    lines.append(f"Suggested profile: {fp.suggested_profile}")
    lines.append(f"Total files      : {fp.total_files}")
    return "\n".join(lines)
