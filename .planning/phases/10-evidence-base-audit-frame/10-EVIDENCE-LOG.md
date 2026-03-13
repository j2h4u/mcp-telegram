# Phase 10 Evidence Log

## Methodology

This evidence log keeps only sources that materially shape the later audit or redesign conclusions
for `mcp-telegram`; it is an audit input, not a general MCP literature review.

### Source tiers

- `Primary external`: normative external guidance from official MCP and Anthropic tool-use docs.
- `Brownfield authority`: live reflection, source code, and tests that define current
  `mcp-telegram` behavior.
- `Supporting official`: official but non-normative clarifications such as SDK docs or maintainer
  commentary.
- `Context only`: weaker community or secondary material used only when it adds explanatory context.

### Retention rule

- Official MCP and Anthropic documents are normative for external guidance.
- Live reflection, source code, and tests are authoritative for current `mcp-telegram` behavior.
- Retain a source only when later Phases 11-13 would cite it directly in an audit finding,
  redesign comparison, or sequencing decision.
- If no source from a weaker tier is retained, say so explicitly instead of silently collapsing the
  tier.
