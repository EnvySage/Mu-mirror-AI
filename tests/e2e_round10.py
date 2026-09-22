"""第十轮 E2E 实测（真实 gRPC server + coordination/stub_llm.py 桩，端口 18080）

词典 sprint ExtractTerms 全链路三场景 + glossary 注入实测：
1. ExtractTerms 正常返回（桩返回造好的 candidates JSON，断言 kind/aliases/evidence/source_chunk_id）
2. ExtractTerms LLM 超时（慢桩 25s > 20s 预算）→ DEADLINE_EXCEEDED
3. ExtractTerms JSON 坏（桩返回非 JSON）→ INVALID_ARGUMENT（解析失败不影响其他服务）
4. glossary 注入 E2E：Classify/ExtractIntent/Chat/GenerateProfile 四注入点带词条实测
5. 语料超量截断 E2E（mock 超量 chunks，验证服务不炸且仍返回）

前置：coordination/stub_llm.py 已在 127.0.0.1:18080 运行，server.py 已在 10003 运行。
用法：.venv/Scripts/python.exe tests/e2e_round10.py
"""

import json
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import grpc  # noqa: E402

from generated import common_pb2 as common  # noqa: E402
from generated import mirror_chat_pb2 as chat_pb2  # noqa: E402
from generated import mirror_chat_pb2_grpc as chat_grpc  # noqa: E402
from generated import mirror_profile_pb2 as profile_pb2  # noqa: E402
from generated import mirror_profile_pb2_grpc as profile_grpc  # noqa: E402
from generated import record_processor_pb2 as rp_pb2  # noqa: E402
from generated import record_processor_pb2_grpc as rp_grpc  # noqa: E402

PASS, FAIL = [], []

STUB_URL = "http://127.0.0.1:18080/v1"
SLOW_PORT = 18096     # 故意 25s 才响应的桩
BADJSON_PORT = 18095  # 返回非 JSON 的桩


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


class SlowHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        time.sleep(25)
        body = b'{"choices": [{"message": {"content": "{}"}}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BadJsonHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.dumps({"choices": [{"message": {"content": "抱歉，我无法以 JSON 格式输出该内容。"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def llm_config(base_url=STUB_URL):
    return common.LlmConfig(provider="stub", protocol=common.AiProtocol.OPENAI,
                            api_key="stub-key", base_url=base_url, model="stub-model")


def make_chunks(n=2):
    chunks = []
    for i in range(n):
        chunks.append(common.ChunkDTO(
            chunk_id=100 + i, record_id=i + 1,
            segment=f"论文进展 day{i}" if i % 2 == 0 else f"游戏 FGO 活动 day{i}",
            content="content", title="t", summary="s",
            content_type="learning", moods=["calm"], keywords=["论文"],
            task_status="", created_at=f"2026-09-0{i + 1}T10:00:00",
            user_edited=(i % 2 == 0)))
    return chunks


def main():
    print("=" * 60)
    print("第十轮 E2E：ExtractTerms 全链路 + glossary 注入（真实 server + 桩 LLM）")
    print("=" * 60)

    slow = ThreadingHTTPServer(("127.0.0.1", SLOW_PORT), SlowHandler)
    threading.Thread(target=slow.serve_forever, daemon=True).start()
    badjson = ThreadingHTTPServer(("127.0.0.1", BADJSON_PORT), BadJsonHandler)
    threading.Thread(target=badjson.serve_forever, daemon=True).start()

    with grpc.insecure_channel("localhost:10003") as channel:
        grpc.channel_ready_future(channel).result(timeout=5)
        rp = rp_grpc.RecordProcessorStub(channel)
        chat = chat_grpc.MirrorChatStub(channel)
        profile = profile_grpc.MirrorProfileStub(channel)

        # --- 1. ExtractTerms 正常路径 ---
        print("\n[1] ExtractTerms 正常返回")
        existing = [common.GlossaryTerm(term="游戏", description="明日方舟", confirmed_at="2026-03-15")]
        r = rp.ExtractTerms(rp_pb2.ExtractTermsRequest(
            chunks=make_chunks(2), existing_terms=existing, llm_config=llm_config()))
        check("候选非空", len(r.candidates) == 2, f"n={len(r.candidates)}")
        c0 = r.candidates[0]
        check("term/aliases 解析", c0.term == "论文" and list(c0.aliases) == ["毕设", "那个设计"],
              f"{c0.term}/{list(c0.aliases)}")
        check("kind 白名单值", all(c.kind in ("new", "evidence", "update") for c in r.candidates),
              ",".join(c.kind for c in r.candidates))
        check("evidence 非空", all(c.evidence for c in r.candidates))
        check("source_chunk_id 解析", r.candidates[0].source_chunk_id == 101, str(r.candidates[0].source_chunk_id))
        check("description 含依据", "依据" in c0.description, c0.description[:40])

        # --- 2. LLM 超时 → DEADLINE_EXCEEDED ---
        print("\n[2] ExtractTerms LLM 超时（慢桩 25s，服务端 20s 超时预算）")
        t0 = time.time()
        try:
            rp.ExtractTerms(rp_pb2.ExtractTermsRequest(
                chunks=make_chunks(1), llm_config=llm_config(f"http://127.0.0.1:{SLOW_PORT}/v1")))
            check("超时应抛 RpcError", False)
        except grpc.RpcError as e:
            elapsed = time.time() - t0
            check("超时 → DEADLINE_EXCEEDED", e.code() == grpc.StatusCode.DEADLINE_EXCEEDED, f"{e.code()}")
            check("约 20s 返回", 18 < elapsed < 30, f"{elapsed:.1f}s")

        # --- 3. JSON 坏 → INVALID_ARGUMENT ---
        print("\n[3] ExtractTerms JSON 坏（桩返回非 JSON 文本）")
        try:
            rp.ExtractTerms(rp_pb2.ExtractTermsRequest(
                chunks=make_chunks(1), llm_config=llm_config(f"http://127.0.0.1:{BADJSON_PORT}/v1")))
            check("坏 JSON 应抛 RpcError", False)
        except grpc.RpcError as e:
            check("坏 JSON → INVALID_ARGUMENT", e.code() == grpc.StatusCode.INVALID_ARGUMENT, f"{e.code()}")
            check("details 安全摘要", "ContentInvalidError" in (e.details() or ""), e.details())

        # 超时/坏 JSON 后其他服务不受影响（隔离性）
        print("\n[3b] 失败隔离：后续 Classify/ExtractTerms 正常仍可用")
        r3 = rp.ExtractTerms(rp_pb2.ExtractTermsRequest(
            chunks=make_chunks(1), llm_config=llm_config()))
        check("失败后再调 ExtractTerms 正常", len(r3.candidates) == 2)

        # --- 4. glossary 注入四注入点 ---
        print("\n[4] glossary 注入（Classify / ExtractIntent / Chat / GenerateProfile）")
        glossary = [common.GlossaryTerm(term="论文", description="毕设《AI日记镜子系统》",
                                        aliases=["毕设"], confirmed_at="2026-06-01")]
        rc = rp.Classify(rp_pb2.ClassifyRequest(content="今天推进了论文的检索模块", llm_config=llm_config(),
                                                single=True, glossary=glossary))
        check("Classify 带 glossary 正常", not rc.skip and len(rc.items) == 1, f"n={len(rc.items)}")

        ri = chat.ExtractIntent(chat_pb2.ExtractIntentRequest(
            query="我论文咋样了", llm_config=llm_config(), glossary=glossary))
        check("ExtractIntent 带 glossary 正常", ri.query_type in ("profile", "structured", "semantic", "hybrid"),
              ri.query_type)

        req = chat_pb2.ChatRequest(question="我论文进展如何？", glossary=glossary, llm_config=llm_config(),
                                   chunks=[chat_pb2.RetrievedChunk(record_id=1, content="论文开题",
                                                                   title="", created_at="2026-09-01")])
        got, done = "", False
        for chunk in chat.Chat(req):
            got += chunk.content
            done = done or chunk.done
        check("Chat 带 glossary 流式正常", bool(got) and done, f"len={len(got)}")

        rpf = profile.GenerateProfile(profile_pb2.GenerateProfileRequest(
            total_records=3, time_range="最近14天", glossary=glossary, llm_config=llm_config()))
        check("GenerateProfile 带 glossary 正常", bool(rpf.overall_summary), rpf.overall_summary[:30])

        # --- 5. 超量语料截断 ---
        print("\n[5] 语料超量截断（mock 250 条 > max_chunks 200）")
        big = make_chunks(2) * 125  # 250 条
        r5 = rp.ExtractTerms(rp_pb2.ExtractTermsRequest(chunks=big, llm_config=llm_config()))
        check("超量语料不炸且正常返回", len(r5.candidates) == 2, f"chunks={len(big)}")

    print("\n" + "=" * 60)
    print(f"结果: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        print("失败项: " + ", ".join(FAIL))
        sys.exit(1)


if __name__ == "__main__":
    main()
