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
3. `build_extraction_policy` reads the full PDF evidence and writes PDF-specific extraction guidance.
4. `extract_section` loops until all planned sections are extracted, using that policy in each prompt.
5. `normalize_rows` converts raw canonical rows into workbook rows.

This follows the LangGraph model where state is shared across nodes and edges decide
which node runs next. Here, the loop edge returns to `extract_section` until
`section_index` reaches the planned section count, then routes to `normalize_rows`.

Runtime-only dependencies, including the Bedrock client, `AgentConfig`, and progress
callback, are passed through LangGraph runtime context. They are not stored as graph
state.

## Why This Shape

LangChain's LangGraph docs recommend wrapping existing application logic inside
graph nodes when the useful business logic already exists. That fits this codebase:
the profiler, policy, and section extractor nodes now use `ChatBedrockConverse`
with Pydantic-validated JSON. The Pydantic schemas, normalizer, confidence
scorer, and writer were already separated well enough to keep.

So the migration changes orchestration, not the PDF extraction semantics.

## Next Steps

The section prompt lives in
`showlay/prompts/section_extractor_prompt.txt`. It now includes only the nested
`policy` object from the extraction policy agent before the section evidence.
That policy provides the allowed question types, target Excel columns, and
PDF-specific field rules. The section extractor prompt stays generic and follows
visible evidence first.

Keep the current Excel output template and the existing `Branching Logic`
Q-reference convention unchanged while validating this prompt change. The next
evaluation pass should compare row quality, metadata coverage, and branch
resolution on CHOICES, H1700-3, and MNLOC before changing writer behavior.
