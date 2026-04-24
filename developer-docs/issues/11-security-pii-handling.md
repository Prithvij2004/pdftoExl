# Issue 11 — Security and PII handling

## Summary

Production assessments contain Protected Health Information (PHI) and PII: client names, SSNs, dates of birth, addresses, Medicaid IDs, narrative descriptions of medical conditions. The seed fixtures in `docs/support_docs/` are blank state-published forms — no real client data — but the same pipeline runs on real assessments in production. This issue defines the data-handling rules.

This is a baseline. A formal HIPAA / state-specific compliance review must precede any production rollout; this doc is engineering guidance, not legal sign-off.

## Data-handling rules

| Data | At rest | In transit | In logs |
|---|---|---|---|
| Source PDFs | S3 with SSE-KMS, bucket policy denies public access | TLS to/from S3 | **never logged** beyond `source_sha256` |
| Canonical / enriched JSON (intermediate) | Local cache encrypted at rest; or S3 with SSE-KMS; 7-day lifecycle | TLS | `block_id` only; text only at `trace` level |
| LLM prompts | Bedrock data-handling per AWS commitments (no training, region-pinned) | TLS | redacted before logging |
| LLM responses | Cached locally for eval reruns; encrypted at rest; 30-day lifecycle | TLS | `rationale` strings redacted |
| Output workbooks | S3 with SSE-KMS; 90-day retention | TLS | path only |

## Bedrock-specific

- Pin to a region where the model honors no-training data-handling: `us-east-1` for v1 (verify at deploy time against current AWS Bedrock data protection docs).
- Use the Converse API, not InvokeModel-with-streaming for production (cleaner auditability).
- Never include client identifiers in the system prompt or few-shot examples — few-shot examples are drawn from the example bank, which is itself sanitized.

## What never goes in logs

- Raw block text at `info` level (full text only at `trace`).
- LLM `rationale` strings without redaction.
- Filenames or S3 paths that contain client identifiers (use program + assessment slug).
- Bedrock token-level traces.

A lint check on the structured-logging schema (issue 09) enforces these rules statically where possible.

## S3 bucket policy

Two buckets:

- `eab-source-pdfs-<env>` — receives uploads from the intake workflow. Bucket policy denies anonymous access; access via IAM role only. Object lock for compliance retention.
- `eab-workbooks-<env>` — receives pipeline outputs. Same controls.

KMS key per environment (`prod`, `staging`, `dev`); cross-account access denied at the key policy level.

## Notification path

Reviewer notifications via SES (preferred, since SNS payloads are not encrypted at rest in transit unless using HTTPS-only subscriptions). Notification bodies link to the workbook in S3; they do NOT include client identifiers in the email body.

## Local development

- Dev machines use sanitized fixtures only. The seed fixtures are sanitized by construction.
- A real assessment never lands on a dev laptop. If reproducing a production bug, sanitize first.
- The local `.cache/` for canonical JSON and LLM responses is `.gitignore`'d.

## Known failure modes

1. **PII in stack traces.** An exception that includes `RawBlock(text=...)` will dump real text. Custom exception classes redact `text` to length + hash.
2. **LLM cache leak.** If the cache ever ships to a shared environment, it carries past prompts. Per-env cache, never crossed.
3. **CloudWatch retention.** Default retention is "never delete." Set explicit 90-day retention per log group; longer retention requires compliance review.
4. **Email subject lines.** Reviewer notifications must not include assessment titles that could leak context. Use a generic subject; details only behind authenticated S3 link.

## Acceptance criteria

- All S3 buckets pass the standard public-access audit.
- Log lint asserts no `info`-level events carry full block text or LLM rationale.
- Bedrock region/model pinning verified against current AWS data-handling docs at every quarterly review.
- Sanitized-fixture-only rule documented in onboarding.

## Cross-references

- Logging discipline: `09-observability-and-logging.md`
- LLM stage: `04-llm-refinement.md`
- Output destination: `07-excel-writer-output.md`
