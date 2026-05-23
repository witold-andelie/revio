"""Solidity profile — LLM-only review (smart contract security)."""

from ..base import ProfileBase, register


@register("solidity")
class SolidityProfile(ProfileBase):
    description = "Solidity (LLM-only review — smart-contract security)"
    extensions = (".sol",)
    languages = ("solidity",)

    @classmethod
    def make_reasoning_hints(cls) -> str:
        return (
            "Target language: Solidity (Ethereum / EVM smart contracts).\n"
            "No Tree-sitter grammar bundled; reviewing via read_file + LLM judgment.\n"
            "\n"
            "Common issue patterns to watch for (smart-contract specific):\n"
            "- Reentrancy: external call before state update (Checks-Effects-Interactions)\n"
            "- Integer overflow/underflow (pre-0.8.0 needs SafeMath; 0.8+ has built-in checks)\n"
            "- tx.origin used for auth (phishable; use msg.sender)\n"
            "- delegatecall to untrusted contract / proxy storage layout drift\n"
            "- block.timestamp / block.number used as randomness source (miner-manipulable)\n"
            "- Unchecked low-level call (.call.value() returning false ignored)\n"
            "- Missing access control on critical state-changing functions\n"
            "- selfdestruct accessible by unprivileged caller\n"
            "- ERC20 approve race (the well-known approve→approve attack)\n"
            "- Front-running: visible mempool transactions enabling sandwich / replay\n"
            "- DoS via gas exhaustion: unbounded loops over storage arrays\n"
            "- Floating-pragma: `pragma solidity ^0.8.0` — pin exact version for prod\n"
            "- Use of `now` (deprecated alias for block.timestamp)\n"
            "- Centralized ownership without timelock or multi-sig\n"
            "- Upgradable proxies: storage slot collisions, init-once not enforced\n"
            "- Oracle dependency: single price feed → manipulation risk\n"
            "- Function visibility default-public (Solidity < 0.5.0) leaking internals\n"
            "- ERC721 transfer to contract without onERC721Received check\n"
            "\n"
            "Tools available: read_file, list_files, search_guidelines, report_finding.\n"
            "Highly recommend the user run external tools too: slither, mythril, echidna.\n"
        )
