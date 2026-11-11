# AI Agent v2 M0 baseline

Collected on 2026-10-20 before tracked code changes. No API keys, DSNs, or tokens are recorded here.

## Repository snapshots

| Repository | Branch | HEAD at baseline | Initial tracked worktree |
|---|---|---|---|
| Mu-mirror-AI | `codex/record-ai-authorization` | `227d3e0` (`feat: reject unauthorized classify requests`) | clean; pre-existing untracked design documents listed below |
| Mu-mirror-B | `codex/record-ai-authorization` | `c03d5a7` (`feat: gate record AI memory by explicit consent`) | clean |
| Mu-mirror-F | `codex/record-ai-authorization` | `2626d9d` (`feat: add mock-backed public preview mode`) | clean |

Pre-existing untracked AI content, left intact: `docs/2026-10-20-ai-agent-architecture-v2.md`, `docs/2026-10-20-chat-agent-langgraph-migration.md`, and `docs/superpowers/`.

The tracked preimage archive is stored outside the repository at `%TEMP%\mirror-ai-m0-preimage-20261020\tracked-preimage.tar` and extracted under the same directory. The original historical migration document SHA-256 is `01CC48B88BD143DAB77282B384FDB801AB89F023170090768039CB433AA7FB17`.

Prompt Git blob IDs at baseline:

| Prompt | Git blob |
|---|---|
| `prompts/intent.txt` | `25f01393db427f92ee36e5b315e158704de128f5` |
| `prompts/chat.txt` | `16865d4aac6de831bf06406f4ba1f5ecc0f59ce9` |
| `prompts/plan_tools.txt` | `383725fce024c08c2d75853d97dc579fadd219d4` |
| `prompts/plan_tools_single.txt` | `23ee95f3ced9fa13b1c72f024aef5eef774c1479` |

## Legacy runtime and deployment baseline

- Existing Python gRPC server listens on port `10003` with `workers=4` and registers the existing services together.
- `config.py` reads service configuration from `config.yml`; model configuration arrives per gRPC request. The inspected service config has no user model key or database credential.
- The original Dockerfile used Python `3.12-slim` but installed the broad `requirements.txt`, then copied the entire repository into the image. It did not define a Docker ignore file.
- `upload/` is the runtime mirror. The pre-sync source and mirror versions of `services/lexicon_service.py` and `prompts/extract_terms.txt` differed: the mirror lacked source-chunk membership validation and used the older numbered-chunk prompt. The M0 sync brought these runtime files to the current source versions. `upload/config.yml` remains an excluded deployment override.

## Python test baseline

Command:

```text
venv/Scripts/python.exe -B -m pytest tests/ -q -p no:cacheprovider
```

Result before changes: **358 passed, 7 failed, 365 total, 283.88s**. All seven failures were in `tests/test_round9.py` and stopped while constructing OpenAI SDK exceptions because the active environment did not provide the imported `httpx2` module. The exception mapping assertions were not reached for those cases. No test was skipped or removed. No live-model or true end-to-end test ran.

## Dependency baseline

| Package/runtime | Existing `venv` | `requirements-minimal.txt` before M0 |
|---|---:|---:|
| Python | `3.14.6` | Docker base `3.12-slim` |
| `openai` | `2.53.0` | `3.1.0` |
| `anthropic` | `0.121.0` | `0.122.0` |
| `httpx` | `0.28.1` | Transitive dependency |

The installed OpenAI 2.53 client imports `httpx` from `openai._base_client`; the locked OpenAI 3.1 client imports `httpx2` there and declares `httpx2<3,>=2.7.0`. The test fixture now constructs request/response objects using the installed SDK's own imported transport, without changing or upgrading the existing venv SDK.

## M0 implementation verification

- The original working venv was left on its existing SDK versions. After extracting the shared intent, answer, and planner functions, the full suite was rerun with the baseline command: **373 passed in 5.78s**. The previous seven OpenAI exception tests now use the installed SDK transport dynamically; no SDK was changed in that venv and no tests were skipped or removed.
- An isolated Python `3.12.13` environment was created under `%TEMP%`. Installing the original minimal requirements there resolved OpenAI `3.1.0` with its declared `httpx2` transport. The three M0 candidate dependencies were pinned as `langgraph==1.2.16`, `psycopg[binary]==3.3.6`, and `psycopg-pool==3.3.3`; a Python 3.12 universal lock was generated. Both `uv pip check` and `python -m pip check` reported no broken requirements.
- In that isolated environment, dependency smoke checks passed for generated protobuf imports, LangGraph state graph construction/invoke/custom stream events, and psycopg binary plus pool imports. The focused SDK exception, service extraction, and upload sync test subset passed: **21 passed in 5.12s**. This is dependency and unit-level evidence, not a service or user-flow end-to-end run.
- `scripts/sync_upload.py --check` reports that runtime files are in sync. `git diff --check` reports no whitespace errors. The four legacy prompt blobs listed above remain unchanged, and the historical migration document SHA-256 remains unchanged.
- Docker image construction was not completed. `docker pull python:3.12-slim` failed while downloading a layer because Docker's internal proxy hostname could not resolve (`http.docker.internal` DNS timeout). No container was stopped or changed, and no image was deployed.
- No database was created or accessed. Java and Vue tests were not run because M0 changes only the Python baseline, shared functions, dependency lock, and image boundary. No live model call or true cross-service end-to-end test was performed.

- The Task 1 file inventory names `server.py` as a modification, but its interfaces and steps do not specify an M0 entry-point change; the architecture assigns the separate Agent listener to Task 5. The existing legacy entry point was inspected and left unchanged so M0 does not register a partial/new service prematurely.
