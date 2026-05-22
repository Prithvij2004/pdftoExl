# LangGraph Extraction Pipeline

## Summary

SHOWLAY's core extraction orchestration now runs through a compiled LangGraph
`StateGraph` in `showlay.agentic.document_extraction_graph`.

The public app contract did not change:

- `run.py` still calls `extract_document_agentic(...)`.
- `run_new_pdfs.py` still calls `extract_document_agentic(...)`.
- `webapp.py` still calls `extract_document_agentic(...)`.

That function now invokes the graph internally.

## Graph Nodes

The graph is intentionally small and maps to the existing pipeline stages:

1. `profile_document` identifies the form title, sections, complexity, and extraction strategy.
2. `plan_sections` converts the profile into the actual extraction chunks.
3. `extract_section` loops until all planned sections are extracted.
4. `normalize_rows` converts raw canonical rows into workbook rows.

This follows the LangGraph model where state is shared across nodes and edges decide
which node runs next. Here, the loop edge returns to `extract_section` until
`section_index` reaches the planned section count, then routes to `normalize_rows`.

Runtime-only dependencies, including the Bedrock client, `AgentConfig`, and progress
callback, are passed through LangGraph runtime context. They are not stored as graph
state.

## Why This Shape

LangChain's LangGraph docs recommend wrapping existing application logic inside
graph nodes when the useful business logic already exists. That fits this codebase:
the Bedrock tool-use prompts, Pydantic schemas, normalizer, confidence scorer, and
writer were already separated well enough to keep.

So the migration changes orchestration, not the PDF extraction semantics.

## Next Steps

The section prompt lives in
`showlay/prompts/section_extractor_prompt.txt`. It asks Bedrock to populate
`branching_source`, `external_id`, and `source_ids` when the PDF evidence
supports them. These are internal traceability fields that help review,
confidence scoring, and item-code branch resolution.

Keep the current Excel output template and the existing `Branching Logic`
Q-reference convention unchanged while validating this prompt change. The next
evaluation pass should compare row quality, metadata coverage, and branch
resolution on CHOICES, H1700-3, and MNLOC before changing writer behavior.
