"""Dart / Flutter profile — LLM-only review."""

from ..base import ProfileBase, register


@register("dart")
class DartProfile(ProfileBase):
    description = "Dart / Flutter (LLM-only review)"
    extensions = (".dart",)
    languages = ("dart",)

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: Dart (typically Flutter mobile / web apps).\n"
            "No Tree-sitter grammar bundled; reviewing via read_file + LLM judgment.\n"
            "\n"
            "Common issue patterns to watch for:\n"
            "- Null safety bypass: `!` operator used without null check (runtime crash)\n"
            "- async without await: dangling Futures, race conditions\n"
            "- BuildContext used after async gap (mounted check missing in StatefulWidget)\n"
            "- setState called after dispose (\"setState() called after dispose()\" crash)\n"
            "- Heavy build() methods: expensive ops on every rebuild (use const widgets)\n"
            "- ListView without itemBuilder for large lists (entire list built)\n"
            "- Hardcoded API keys / secrets in source (visible in compiled output)\n"
            "- HTTPS not enforced — http:// URLs hardcoded (App Transport Security)\n"
            "- Insecure JSON deserialization without schema validation\n"
            "- WebView with javascriptMode unrestricted on untrusted URLs\n"
            "- shared_preferences for sensitive data (use flutter_secure_storage)\n"
            "- Provider / Riverpod state not properly disposed (memory leak)\n"
            "- print() statements left in production (logs leaking PII)\n"
            "- TextEditingController not disposed in State.dispose\n"
            "- Iterables: map/where without toList() when caller expects list\n"
            "- Locale-sensitive operations using default locale (BCP-47 vs system)\n"
            "\n"
            "Tools available: read_file, list_files, search_guidelines, report_finding.\n"
        )
