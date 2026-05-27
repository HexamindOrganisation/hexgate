"""`fortify policy` CLI — author + inspect + dry-run policy.yaml documents.

Four sub-subcommands today (M2 phase 2):

    fortify policy build <source>        compile to Rego (+ wasm later)
    fortify policy validate <source>     parse + check constraint grammar
    fortify policy show-rego <source>    print compiled Rego to stdout
    fortify policy test <source> --...   dry-run a tool-call decision

All four are thin wrappers around :mod:`fortify.security` helpers. The
same library powers the platform's save-time pipeline and a developer's
local iteration — no code duplication, no drift.
"""

from fortify.cli.policy.main import add_parser, main

__all__ = ["add_parser", "main"]
