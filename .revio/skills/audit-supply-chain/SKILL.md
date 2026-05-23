---
name: audit-supply-chain
description: Audit a JS/TS project for supply-chain attack indicators (typosquatting, post-install hooks, sketchy deps)
when_to_use: Running an audit on projects with package.json — check for compromised dependencies
matches:
  extensions: [".json"]
  filename_patterns: ["**/package.json", "**/package-lock.json"]
  languages: ["javascript", "typescript"]
---

# JS Supply-Chain Audit Playbook

Look for these indicators of supply-chain compromise:

1. **Typo-squatted package names**: e.g. `lodash-es-utils`, `react-types-fix`
   (compare against well-known packages by edit distance ≤ 2).
2. **Post-install scripts** in dependencies — these can run arbitrary code.
   Look at the `scripts.postinstall` field in any nested package.json.
3. **Recently published packages** with low download counts being depended on
   by critical code paths (auth, payment, crypto).
4. **Suspicious maintainer changes** in lock files (different `resolved` URLs).
