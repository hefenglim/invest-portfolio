"""llm_insight — LiteLLM orchestration: assemble computed portfolio numbers into prompts.

This layer is a *narrator*, not a calculator. It imports the calculation core
(``portfolio``) and ``shared`` only (one-way dependency, ``rules/architecture.md``); it
never recomputes a number of record. ``variables`` holds the data-variable registry, the
reusable token-validation core, and per-token value assembly (spec 06a).
"""
