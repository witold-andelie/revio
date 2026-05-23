"""Python profile — secondary target.

M1: declarative stub. M2 wires up Tree-sitter Python + bandit subprocess.
"""

from ..base import ProfileBase, register


@register("python")
class PythonProfile(ProfileBase):
    description = "Python (Tree-sitter + bandit-backed)"
    extensions = (".py", ".pyi")
    languages = ("python",)
    optional_dep_group = "python"

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: Python.\n"
            "Common issue patterns to watch for in this profile:\n"
            "- SQL injection via f-string / .format() in cursor.execute\n"
            "- Command injection via subprocess(shell=True)\n"
            "- Insecure deserialization (pickle.load, yaml.load without safe loader)\n"
            "- eval / exec on untrusted input\n"
            "- Weak crypto (hashlib.md5/sha1 for passwords, random for secrets)\n"
            "- Hardcoded secrets, API keys, database URLs with passwords\n"
            "- Path traversal via os.path.join with user input\n"
            "- Mutable default arguments\n"
            "- Bare except / except Exception silently swallowing errors\n"
            "- Missing context managers (open files, db connections)\n"
        )
