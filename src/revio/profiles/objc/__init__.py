"""Objective-C profile — LLM-only review."""

from ..base import ProfileBase, register


@register("objc")
class ObjectiveCProfile(ProfileBase):
    description = "Objective-C (LLM-only review)"
    extensions = (".m", ".mm")  # .m overlaps with MATLAB — see comment below
    languages = ("objective_c",)

    @classmethod
    def make_reasoning_hints(cls) -> str:
        # Note on .m collision with MATLAB: detect heuristic should look at
        # leading characters (#import, @interface → ObjC; function foo →
        # MATLAB). For now we let the user disambiguate via --profile.
        return (
            "Target language: Objective-C / Objective-C++.\n"
            "No Tree-sitter grammar bundled; reviewing via read_file + LLM judgment.\n"
            "Note: .m files also belong to MATLAB; if this is a MATLAB project,\n"
            "use --profile matlab to override.\n"
            "\n"
            "Common issue patterns to watch for:\n"
            "- Retain cycles in blocks: `self` captured strongly inside a block\n"
            "  property (use __weak typeof(self) weakSelf = self;)\n"
            "- Missing nil checks before [obj method] (nil messaging is OK but\n"
            "  return value 0/nil may be misinterpreted)\n"
            "- Manual reference counting (-retain/-release) in modern ARC code\n"
            "- `dispatch_async(dispatch_get_main_queue(), ...)` from main queue\n"
            "  (deadlock risk with dispatch_sync)\n"
            "- KVO observer registered without matching removeObserver (crash on dealloc)\n"
            "- NSUserDefaults storing secrets (use Keychain)\n"
            "- NSURLConnection / NSURLSession without certificate pinning for\n"
            "  privileged endpoints\n"
            "- Format-string vulnerability: NSLog(userInput) — should be NSLog(@\"%@\", userInput)\n"
            "- objc_msgSend usage in performance-critical hot paths bypassing inlining\n"
            "- @synchronized(self) — coarse lock; better use NSLock / GCD queue\n"
            "- C array bounds: NSArray indices not checked vs count\n"
            "- Mixing ARC and MRC code in the same target without exception flags\n"
            "\n"
            "Tools available: read_file, list_files, search_guidelines, report_finding.\n"
        )
