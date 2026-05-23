"""Function fingerprinting and similarity index — foundation for dedup mode.

For every function we collect from SymbolGraph, compute:
- `structural_hash`  : SHA-256 of the **normalized** body
                       (identifiers renamed to placeholders p1/p2/...,
                        comments/whitespace stripped). Exact match means the
                        two functions have the same syntactic structure.
- `token_shingles`   : set of overlapping k-grams over the normalized token
                       stream. Used for near-duplicate detection via Jaccard.

The agent's `find_similar_functions` tool queries this index. The LLM (Layer 3)
then makes the final semantic judgement — this layer only provides candidates.

This is intentionally simple. No ML embeddings (M4 future work). For "AI-
generated redundancy" the simple normalize+hash pipeline catches most cases
because AI tends to produce structurally identical wrappers.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .symbol_graph import SymbolGraph
from .treesitter_js import FunctionInfo


logger = logging.getLogger(__name__)


# --- Models -------------------------------------------------------------------


@dataclass
class FunctionFingerprint:
    """Stable identity + similarity hashes for one function."""

    file: Path
    function: FunctionInfo

    # The function body, normalized: comments stripped, identifiers renamed
    # to stable placeholders (preserves intra-function uses).
    normalized_body: str = ""

    # SHA-256 of normalized_body — exact-match key.
    structural_hash: str = ""

    # Token-level k-gram set for fuzzy similarity (Jaccard).
    token_shingles: set[str] = field(default_factory=set)

    # Raw token count after normalization (small functions are uninteresting)
    token_count: int = 0

    @property
    def fqn(self) -> str:
        cls = f"{self.function.enclosing_class}." if self.function.enclosing_class else ""
        return f"{self.file.name}::{cls}{self.function.name or '<anon>'}@L{self.function.line_start}"


@dataclass
class DuplicateGroup:
    """A set of functions sharing the same structural_hash."""

    structural_hash: str
    members: list[FunctionFingerprint]

    @property
    def count(self) -> int:
        return len(self.members)

    @property
    def representative(self) -> FunctionFingerprint:
        return self.members[0]


# --- Index --------------------------------------------------------------------


class FunctionIndex:
    """Fingerprint + similarity index over all functions in a SymbolGraph."""

    # Functions shorter than this many normalized tokens are excluded —
    # tiny utility one-liners create noise.
    MIN_TOKENS = 8

    # k-gram size for shingles
    SHINGLE_K = 5

    def __init__(self, symbol_graph: SymbolGraph):
        self.symbol_graph = symbol_graph
        self.fingerprints: list[FunctionFingerprint] = []
        # structural_hash → list of FunctionFingerprint
        self._by_hash: dict[str, list[FunctionFingerprint]] = defaultdict(list)

    # ---- Building ----

    @classmethod
    def build(cls, symbol_graph: SymbolGraph) -> "FunctionIndex":
        idx = cls(symbol_graph)
        for fs in symbol_graph.files.values():
            for fn in fs.functions:
                fp = idx._fingerprint(fs.path, fn)
                if fp is None:
                    continue
                idx.fingerprints.append(fp)
                idx._by_hash[fp.structural_hash].append(fp)
        return idx

    def _fingerprint(self, file: Path, fn: FunctionInfo) -> FunctionFingerprint | None:
        if not fn.body:
            return None

        normalized, tokens = _normalize_body(fn.body)
        if len(tokens) < self.MIN_TOKENS:
            return None

        h = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        shingles = _make_shingles(tokens, self.SHINGLE_K)

        return FunctionFingerprint(
            file=file,
            function=fn,
            normalized_body=normalized,
            structural_hash=h,
            token_shingles=shingles,
            token_count=len(tokens),
        )

    # ---- Query API ----

    def find_duplicate_groups(self, *, min_size: int = 2) -> list[DuplicateGroup]:
        """Functions with identical structural_hash."""
        groups: list[DuplicateGroup] = []
        for h, fps in self._by_hash.items():
            if len(fps) >= min_size:
                groups.append(DuplicateGroup(structural_hash=h, members=fps))
        # Sort: largest groups first, then by member fqn for stability
        groups.sort(key=lambda g: (-g.count, g.representative.fqn))
        return groups

    def find_near_duplicates(
        self,
        target: FunctionFingerprint,
        *,
        threshold: float = 0.85,
        max_results: int = 10,
    ) -> list[tuple[FunctionFingerprint, float]]:
        """Functions whose token-shingle Jaccard ≥ threshold.

        Excludes the target itself and exact-hash duplicates (those are
        already in find_duplicate_groups).
        """
        if not target.token_shingles:
            return []

        results: list[tuple[FunctionFingerprint, float]] = []
        for fp in self.fingerprints:
            if fp is target:
                continue
            if fp.structural_hash == target.structural_hash:
                continue  # exact match handled elsewhere
            sim = _jaccard(target.token_shingles, fp.token_shingles)
            if sim >= threshold:
                results.append((fp, sim))

        results.sort(key=lambda t: -t[1])
        return results[:max_results]

    def find_all_near_duplicate_pairs(
        self,
        *,
        threshold: float = 0.85,
    ) -> list[tuple[FunctionFingerprint, FunctionFingerprint, float]]:
        """All unordered pairs with similarity ≥ threshold.

        O(n²) — fine for repos up to ~1000 functions. Larger repos
        will need LSH / minhash (M4).
        """
        pairs: list[tuple[FunctionFingerprint, FunctionFingerprint, float]] = []
        fps = self.fingerprints
        for i in range(len(fps)):
            for j in range(i + 1, len(fps)):
                a, b = fps[i], fps[j]
                if a.structural_hash == b.structural_hash:
                    continue
                sim = _jaccard(a.token_shingles, b.token_shingles)
                if sim >= threshold:
                    pairs.append((a, b, sim))
        pairs.sort(key=lambda t: -t[2])
        return pairs

    def get_by_location(self, file: Path | str, line: int) -> FunctionFingerprint | None:
        target = Path(file).resolve()
        for fp in self.fingerprints:
            if fp.file == target and fp.function.line_start <= line <= fp.function.line_end:
                return fp
        return None

    def stats(self) -> dict[str, int | float]:
        groups = self.find_duplicate_groups()
        return {
            "functions_indexed": len(self.fingerprints),
            "unique_structures": len(self._by_hash),
            "duplicate_groups": len(groups),
            "functions_in_duplicate_groups": sum(g.count for g in groups),
        }


# --- Normalization + tokenization ---------------------------------------------


# JS/TS reserved words — kept as-is in normalization (they encode structure)
_KEYWORDS = frozenset({
    "abstract", "any", "as", "async", "await", "boolean", "break", "case",
    "catch", "class", "const", "constructor", "continue", "debugger",
    "declare", "default", "delete", "do", "else", "enum", "export",
    "extends", "false", "finally", "for", "from", "function", "get",
    "if", "implements", "import", "in", "instanceof", "interface", "is",
    "keyof", "let", "module", "namespace", "never", "new", "null",
    "number", "of", "package", "private", "protected", "public", "readonly",
    "require", "return", "set", "static", "string", "super", "switch",
    "symbol", "this", "throw", "true", "try", "type", "typeof", "undefined",
    "unique", "unknown", "var", "void", "while", "with", "yield",
    # Common builtins we don't want to mask
    "console", "Math", "Object", "Array", "String", "Number", "Boolean",
    "Promise", "Map", "Set", "JSON", "Error", "Date", "Symbol",
})


_TOKEN_PATTERN = re.compile(
    r"""
    //[^\n]*               # line comment
    | /\*[\s\S]*?\*/        # block comment
    | `(?:\\.|[^`\\])*`     # template literal (kept as TPL)
    | "(?:\\.|[^"\\])*"     # double-quoted string
    | '(?:\\.|[^'\\])*'     # single-quoted string
    | [A-Za-z_$][\w$]*       # identifier
    | \d+(?:\.\d+)?          # number
    | =>|\.\.\.|\*\*=?|\+\+|--|\|\||&&|==|===|!=|!==|<=|>=|\+=|-=|\*=|/=|<<=?|>>=?  # multi-char ops
    | [{}\[\]().,;:?]
    | [+\-*/%=<>!&|^~]
    """,
    re.VERBOSE,
)


def _normalize_body(body: str) -> tuple[str, list[str]]:
    """Strip comments, rename identifiers, collapse whitespace.

    Returns (normalized_text, token_list).

    Identifiers are mapped to stable placeholders p1, p2, ... in first-seen
    order so that two functions differing only in variable names produce the
    same normalized text.
    """
    tokens: list[str] = []
    ident_map: dict[str, str] = {}
    next_id = 1

    for match in _TOKEN_PATTERN.finditer(body):
        tok = match.group(0)
        # Drop comments
        if tok.startswith("//") or tok.startswith("/*"):
            continue
        # Strings: collapse all to STR_ (preserves "there is a string here")
        if tok and tok[0] in ('"', "'", "`"):
            tokens.append("STR_")
            continue
        # Numbers: collapse to NUM_
        if tok and tok[0].isdigit():
            tokens.append("NUM_")
            continue
        # Identifier: keep keywords + builtins, rename others
        if re.match(r"^[A-Za-z_$][\w$]*$", tok):
            if tok in _KEYWORDS:
                tokens.append(tok)
            else:
                if tok not in ident_map:
                    ident_map[tok] = f"p{next_id}"
                    next_id += 1
                tokens.append(ident_map[tok])
            continue
        # Operators and punctuation: keep as-is
        tokens.append(tok)

    normalized = " ".join(tokens)
    return normalized, tokens


def _make_shingles(tokens: list[str], k: int) -> set[str]:
    if len(tokens) < k:
        return {" ".join(tokens)}
    return {" ".join(tokens[i : i + k]) for i in range(len(tokens) - k + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    intersect = len(a & b)
    union = len(a | b)
    return intersect / union if union > 0 else 0.0
