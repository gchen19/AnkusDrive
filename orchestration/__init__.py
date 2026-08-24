"""
AnkusDrive orchestration — the HOST-SIDE reference layer for multi-agent design.

This package is deliberately NOT part of the `ankusdrive` package. AnkusDrive ships
thin, tool-agnostic primitives (merge_assembly, publish_interface, the gates,
the lockfile); *orchestration* — decomposing a design, fanning out builder
agents, merging, and renegotiating — lives here, on the host side, exactly as
docs/MULTI_AGENT.md §8 and Appendix A describe. Any host (Claude, another AI
coding tool, a human) can use this, replace it, or ignore it; the contract that
matters is the manifest + the component files, not this code.

- agentkit  : the host-side agent toolkit (tool surface, a cached tool-use loop,
              cost accounting, a scripted stub client for free dry runs).
- coordinator: the reference orchestrator — spec -> manifest -> build -> merge ->
              gate -> renegotiate.
"""
