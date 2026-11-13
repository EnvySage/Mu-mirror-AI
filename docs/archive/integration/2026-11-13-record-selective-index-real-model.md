# Selective indexing real-model verification

Date: 2026-11-13

Archived acceptance snapshot. Current cross-repository status and subsequent full-suite results are maintained in `Mu-mirror-B/docs/PROGRESS.md`.

## Scope and result

Ran the fixed 12-case classification set through the user-provided local OpenAI-compatible proxy at `127.0.0.1:7863/v1` using `cn:deepseek-v4.1-flash` and synthetic content only: four low-information examples expected to remain unindexed and eight specific examples expected to be indexed. All 12 cases matched the expected decision (12/12). One specific input was split into two segments; both received the expected indexed decision.

Evidence:

- `Mu-mirror-B/.verify/record-selective-index/real-model-12-case-deepseek.log`
- `Mu-mirror-B/.verify/record-selective-index/real-model-12-case-deepseek-results.json`
- Runner: `Mu-mirror-B/.verify/record-selective-index/real-model-12-case-openai.py`

The result validates selective-indexing classification on this synthetic set. It does not validate real chat answers, the live app's provider settings, or either engine's end-to-end dialogue quality.

## Compatibility defect found and fixed

The first Anthropic-compatibility call returned a content sequence with a `thinking` block before the text block. `AnthropicLlm.chat()` previously read `.text` from the first block unconditionally and failed. It now concatenates only `type == "text"` blocks and raises the existing LLM-unavailable error if no text block exists. Added targeted tests for text extraction, JSON responses, missing text blocks, and the proxy's incomplete streaming snapshot; the runtime-client fake now identifies its text block explicitly. `upload/llm/anthropic_llm.py` was synchronized from the source.

Verification:

- Anthropic, JSON, and thinking tests: 53 passed, zero skipped (`Mu-mirror-B/.verify/record-selective-index/ai-anthropic-chat-tests-final.log`).
- Full AI suite with the isolated PostgreSQL reader DSN after the final compatibility changes: 805 passed, zero skipped (`Mu-mirror-B/.verify/record-selective-index/ai-full-with-deepseek-and-postgres-final.log`).
- `scripts/sync_upload.py --check`: rerun in the final cross-repository check.

The model alias in this acceptance run was `cn:deepseek-v4.1-flash`. No API key, database credential, or personal diary text is included in the evidence report.
