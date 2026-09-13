"""RecordProcessor / ExtractTerms — 个人词典候选抽取（lexicon-design.md 第 3 节）

并入 record_processor 同一 gRPC 服务（RecordProcessor{Classify, ExtractTerms}），
复用 B 侧既有 channel，不新增服务连接。Classify 实现沿用 services/record_processor.py。

输入：近 14 天（月度审计 30 天）confirmed chunks（扁平 ChunkDTO）+ 现有词条 + LlmConfig
输出：候选词条 TermCandidate{term, aliases, description, kind, evidence, source_chunk_id}

设计要点：
- 语料按时间排序渲染，user_edited=true 的 chunk 前置标注"用户手动修改过"（权重更高）
- description 要求"用户语境解释 + 来源依据"，用户 10 秒能判断对错
- 已有词条传入 prompt 用于去重 + evidence/update 判定
- LLM 失败绝不影响其他服务：解析失败/超时走 errors.py 统一映射（abort_with_mapped），
  本 servicer 不缓存任何用户数据（无状态铁律 #3）

超量防护（config extract_terms 段）：chunks 超过 max_chunks 按时间窗截断——
语料按时间升序传入，截断时保留最近的 max_chunks 条（旧语料丢弃，日志说明）。

继承 RecordProcessorServicer 复用 Classify 两个模式（single/split），本文件只新增
ExtractTerms。
"""

from generated import record_processor_pb2 as pb2
from generated import record_processor_pb2_grpc as pb2_grpc

from services.record_processor import RecordProcessorServicer as _BaseServicer
from config import CONFIG
from errors import abort_with_mapped
from llm.factory import create_llm
from llm_json import parse_json
from prompts_loader import loader

# kind 三选一（英文小写，设计稿第 3 节分级）
VALID_KINDS = {"new", "evidence", "update"}
DEFAULT_KIND = "new"

# 每条语料渲染长度截断（prompt 膨胀防线；config extract_terms.max_chunk_chars）
_MAX_CHUNK_CHARS = int(CONFIG["extract_terms"]["max_chunk_chars"])
_MAX_CHUNKS = int(CONFIG["extract_terms"]["max_chunks"])
_MAX_CANDIDATES = int(CONFIG["extract_terms"]["max_candidates"])


def sort_chunks_by_time(chunks) -> list:
    """语料按 created_at 升序（时间早 → 晚），prompt 里"按时间排序"承诺的前提。

    created_at 为 ISO 字符串（YYYY-MM-DDTHH:mm:ss），字符串排序即时间排序；
    缺失/空值排最后。稳定排序，同时间保持请求顺序。
    """
    return sorted(chunks, key=lambda c: ((c.created_at or "").strip() == "", c.created_at or ""))


def truncate_chunks(chunks) -> tuple[list, int]:
    """输入 token 上限防护：超出 max_chunks 时按时间窗截断，保留最近的。

    返回 (保留的 chunks, 截掉的条数)。调用方先 sort_chunks_by_time 再传入，
    截断语义 = 丢最旧的（设计稿参数表"14 天窗口"，超量即缩窗）。
    """
    if len(chunks) <= _MAX_CHUNKS:
        return list(chunks), 0
    kept = list(chunks)[-_MAX_CHUNKS:]
    return kept, len(chunks) - len(kept)


def _fmt_chunk(idx: int, chunk) -> str:
    """单条语料 → prompt 行。user_edited 前置标注（权重更高的说明）。"""
    seg = (chunk.segment or chunk.content or chunk.summary or "").strip()
    if len(seg) > _MAX_CHUNK_CHARS:
        seg = seg[:_MAX_CHUNK_CHARS] + "…"
    edited = " [用户手动修改过]" if chunk.user_edited else ""
    date = (chunk.created_at or "").strip()[:10] or "日期未知"
    return f"[{idx}]{edited} {date} chunk_id={chunk.chunk_id}：{seg}"


def _fmt_chunks(chunks) -> str:
    if not chunks:
        return "（无语料）"
    return "\n".join(_fmt_chunk(i, c) for i, c in enumerate(chunks, start=1))


def _fmt_existing_terms(terms) -> str:
    if not terms:
        return "（用户词汇表还是空的，所有候选都算 new）"
    lines = []
    for t in terms:
        term = (t.term or "").strip()
        if not term:
            continue
        aliases = "/".join(a for a in t.aliases if a)
        alias_part = f"（又称：{aliases}）" if aliases else ""
        desc = (t.description or "").strip()
        lines.append(f"- {term}{alias_part}：{desc}" if desc else f"- {term}{alias_part}")
    return "\n".join(lines) if lines else "（用户词汇表还是空的，所有候选都算 new）"


def _sanitize_candidates(data: dict) -> list[pb2.ExtractTermsReply.TermCandidate]:
    """LLM JSON → 合法 TermCandidate 列表：kind 白名单、term 非空、上限截断。"""
    raw = data.get("candidates")
    if not isinstance(raw, list):
        return []
    out: list[pb2.ExtractTermsReply.TermCandidate] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term", "")).strip()
        if not term or term in seen:
            continue
        seen.add(term)
        kind = str(item.get("kind", "")).strip().lower()
        if kind not in VALID_KINDS:
            kind = DEFAULT_KIND
        aliases = [str(a).strip() for a in (item.get("aliases") or [])
                   if a is not None and str(a).strip()]
        try:
            chunk_id = int(item.get("source_chunk_id") or 0)
        except (TypeError, ValueError):
            chunk_id = 0
        out.append(pb2.ExtractTermsReply.TermCandidate(
            term=term,
            aliases=aliases,
            description=str(item.get("description", "")).strip(),
            kind=kind,
            evidence=str(item.get("evidence", "")).strip(),
            source_chunk_id=chunk_id,
        ))
        if len(out) >= _MAX_CANDIDATES:
            break
    return out


class RecordProcessorServicer(_BaseServicer):
    """ExtractTerms servicer——继承 Classify 实现（record_processor.py），新增 ExtractTerms。"""

    def ExtractTerms(self, request, context):
        llm_config = request.llm_config
        print(f"[ExtractTerms] chunks={len(request.chunks)}, existing_terms={len(request.existing_terms)}")
        print(f"[ExtractTerms] LLM: provider={llm_config.provider}, model={llm_config.model}, "
              f"protocol={llm_config.protocol}")

        try:
            # 时间排序 + 超量截断（保留最近，日志说明缩窗）
            ordered = sort_chunks_by_time(request.chunks)
            kept, dropped = truncate_chunks(ordered)
            if dropped:
                print(f"[ExtractTerms] 语料超量截断：{len(ordered)} 条 → 保留最近 {len(kept)} 条，"
                      f"丢弃最早 {dropped} 条")

            llm = create_llm(
                provider=llm_config.provider,
                api_key=llm_config.api_key,
                base_url=llm_config.base_url,
                model=llm_config.model,
                protocol=llm_config.protocol,
            )

            prompt = loader.render(
                "extract_terms",
                chunks=_fmt_chunks(kept),
                existing_terms=_fmt_existing_terms(request.existing_terms),
            )
            response_text = llm.chat([{"role": "user", "content": prompt}])
            print(f"[ExtractTerms] LLM 响应: {response_text[:200]}")

            result = parse_json(response_text, ctx="词条抽取")
            candidates = _sanitize_candidates(result)
            print(f"[ExtractTerms] 候选 {len(candidates)} 条: "
                  f"{[(c.term, c.kind) for c in candidates]}")
            return pb2.ExtractTermsReply(candidates=candidates)

        except Exception as e:
            print(f"[ExtractTerms] 错误: {e}")
            abort_with_mapped(context, e)
