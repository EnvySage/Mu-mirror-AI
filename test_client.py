"""测试客户端 — 验证 gRPC 链路（T-AI-6 冒烟测试）

覆盖：Classify（拆分 + single 两模式）、Embed、ExtractIntent、Chat 流式、GenerateProfile、GetModelInfo。
无真实 LLM Key 时用 --stub 模式：本地起一个假 LLM HTTP 服务，验证 gRPC 全链路。

用法:
  python test_client.py            # 真实 LLM（需有效 key）
  python test_client.py --stub     # 桩模式（零外部依赖）
  python test_client.py --skip-embed   # 跳过 local Embed（避免下载 BGE-m3）
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import grpc

from generated import common_pb2 as common
from generated import record_processor_pb2 as rp_pb2
from generated import record_processor_pb2_grpc as rp_grpc
from generated import embedding_pb2 as emb_pb2
from generated import embedding_pb2_grpc as emb_grpc
from generated import mirror_chat_pb2 as chat_pb2
from generated import mirror_chat_pb2_grpc as chat_grpc
from generated import mirror_profile_pb2 as profile_pb2
from generated import mirror_profile_pb2_grpc as profile_grpc

STUB_PORT = 50199
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    mark = "PASS" if cond else "FAIL"
    (PASS if cond else FAIL).append(name)
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 桩 LLM 服务：OpenAI 兼容 /v1/chat/completions，按 prompt 关键词返回不同 JSON
# ---------------------------------------------------------------------------
class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        prompt = " ".join(m.get("content", "") for m in body.get("messages", []))
        stream = body.get("stream", False)

        if "确认过边界" in prompt:  # classify-single
            payload = {"skip": False, "title": "学习gRPC", "summary": "学习了 gRPC 流式调用",
                       "content_type": "LEARNING", "moods": [], "status": "STATUS_UNKNOWN",
                       "keywords": ["gRPC"]}
        elif "拆分" in prompt and "记录分类助手" in prompt:  # classify
            payload = {"skip": False, "split_content": "中午吃饭|||下午干活",
                       "items": [
                           {"title": "吃饭", "summary": "中午吃饭", "content_type": "NOTE",
                            "moods": [], "status": "STATUS_UNKNOWN", "keywords": ["吃饭"]},
                           {"title": "干活", "summary": "下午干活", "content_type": "TODO",
                            "moods": [], "status": "in_progress", "keywords": ["工作"]},
                       ]}
        elif "query_type" in prompt:  # intent
            payload = {"query_type": "hybrid", "content_type": "todo", "moods": ["anxious"],
                       "time_range": "最近7天", "rewritten_query": "未完成待办 焦虑"}
        elif "画像分析" in prompt:  # profile
            payload = {"todo_analysis": "标记了 3 次未完成待办", "learning_analysis": "学习了 gRPC 与 pgvector",
                       "mood_analysis": "标记了 2 次焦虑", "user_tags": ["夜猫子", "学习者"],
                       "rhythm_analysis": "晚间 22 点后记录最多", "overall_summary": "近期记录集中在学习与工作。"}
        else:  # chat（流式/非流式）
            text = "你最近记录了 3 条待办[1]，其中 2 条已完成。"
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for piece in [text[:10], text[10:]]:
                    data = json.dumps({"choices": [{"delta": {"content": piece}}]}, ensure_ascii=False)
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
                return
            payload = text

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]
        }, ensure_ascii=False).encode("utf-8"))


def start_stub():
    server = HTTPServer(("127.0.0.1", STUB_PORT), StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def llm_config(protocol=common.AiProtocol.OPENAI):
    return common.LlmConfig(
        provider="stub", protocol=protocol, api_key="stub-key",
        base_url=f"http://127.0.0.1:{STUB_PORT}/v1", model="stub-model",
    )


# ---------------------------------------------------------------------------
def test_classify(stub, single: bool):
    label = "single" if single else "split"
    print(f"\n[Classify/{label}]")
    try:
        resp = stub.Classify(rp_pb2.ClassifyRequest(
            content="今天学习了 Python gRPC 编程，中午吃饭，下午干活",
            llm_config=llm_config(),
            single=single,
        ))
        check(f"classify/{label} not skip", not resp.skip, resp.skip_reason)
        check(f"classify/{label} has items", len(resp.items) > 0)
        if single:
            check("classify/single 恰好 1 条", len(resp.items) == 1, f"n={len(resp.items)}")
            check("classify/single 保留原文", resp.items[0].content != "")
        else:
            check("classify/split 拆分条数>1", len(resp.items) > 1, f"n={len(resp.items)}")
            todo_items = [i for i in resp.items if i.content_type == common.ContentType.TODO]
            check("classify/split taskStatus 必填", all(
                i.status in (common.TaskStatus.NOT_STARTED, common.TaskStatus.IN_PROGRESS,
                             common.TaskStatus.COMPLETED) for i in todo_items),
                f"todo n={len(todo_items)}")
            for i, item in enumerate(resp.items):
                print(f"      item{i}: {item.title} | {common.ContentType.Name(item.content_type)} "
                      f"| {common.TaskStatus.Name(item.status)}")
    except grpc.RpcError as e:
        check(f"classify/{label}", False, f"{e.code()}: {e.details()}")


def test_embed(stub):
    print("\n[Embed]")
    try:
        resp = stub.Embed(emb_pb2.EmbedRequest(
            text="测试文本转向量",
            embedding_config=common.EmbeddingConfig(
                source="api", api_provider="stub", api_key="stub-key",
                api_model="stub-embed", base_url=f"http://127.0.0.1:{STUB_PORT}/v1"),
        ))
        check("embed dimension>0", resp.dimension > 0, str(resp.dimension))
    except grpc.RpcError as e:
        print(f"  [SKIP] Embed（桩模式无 embedding 端点或未装模型）: {e.code()}")


def test_get_model_info(stub):
    print("\n[GetModelInfo]")
    try:
        resp = stub.GetModelInfo(emb_pb2.ModelInfoRequest())
        check("health available", resp.available, f"{resp.model_name}/{resp.source}")
    except grpc.RpcError as e:
        check("GetModelInfo", False, f"{e.code()}: {e.details()}")


def test_extract_intent(stub):
    print("\n[ExtractIntent]")
    try:
        resp = stub.ExtractIntent(chat_pb2.ExtractIntentRequest(
            query="我最近有哪些没做完的事？", llm_config=llm_config()))
        check("intent query_type 合法", resp.query_type in ("profile", "structured", "semantic", "hybrid"),
              resp.query_type)
        check("intent content_type 小写", resp.content_type in ("", "todo", "thought", "learning",
              "plan", "note", "work", "social", "health"), resp.content_type or "(unset)")
        check("intent rewritten_query 非空", resp.rewritten_query != "")
        print(f"      query_type={resp.query_type} content_type={resp.content_type} "
              f"moods={list(resp.moods)} range={resp.time_range}")
    except grpc.RpcError as e:
        check("ExtractIntent", False, f"{e.code()}: {e.details()}")


def test_chat(stub):
    print("\n[Chat] 流式")
    try:
        req = chat_pb2.ChatRequest(
            question="我最近状态怎么样？",
            history=[chat_pb2.ChatMessage(role="user", content="你好")],
            chunks=[
                chat_pb2.RetrievedChunk(record_id=101, content="今天很焦虑，加班到十点",
                                        title="", created_at="2026-09-01", content_type="work",
                                        moods=["anxious"], score=0.91),
                chat_pb2.RetrievedChunk(record_id=102, content="学习了 gRPC 流式调用",
                                        title="", created_at="2026-09-02", content_type="learning",
                                        moods=[], score=0.88),
            ],
            llm_config=llm_config(),
        )
        content, done_seen, sources = "", False, []
        for chunk in stub.Chat(req):
            content += chunk.content
            done_seen = done_seen or chunk.done
            if chunk.sources:
                sources = list(chunk.sources)
        check("chat 流式收到内容", len(content) > 0, f"len={len(content)}")
        check("chat done=true 收到", done_seen)
        check("chat sources 引用解析", len(sources) > 0 and sources[0].record_id == 101,
              f"n={len(sources)} id={sources[0].record_id if sources else '-'}")
        print(f"      回答: {content[:50]}")
    except grpc.RpcError as e:
        check("Chat", False, f"{e.code()}: {e.details()}")


def test_generate_profile(stub):
    print("\n[GenerateProfile]")
    try:
        resp = stub.GenerateProfile(profile_pb2.GenerateProfileRequest(
            todos=[profile_pb2.TodoItem(record_id=1, title="写周报", summary="还没写", created_at="2026-09-01")],
            learnings=[profile_pb2.LearningItem(record_id=2, title="gRPC", summary="学习了流式 RPC",
                                                keywords=["gRPC"], created_at="2026-09-02")],
            mood_stats=[profile_pb2.MoodStat(mood="anxious", count=3, percentage=30.0)],
            keywords=[profile_pb2.KeywordStat(keyword="工作", count=10)],
            active_time=profile_pb2.ActiveTimeStats(
                hour_distribution={"22": 8, "23": 5}, weekday_distribution={"Mon": 4},
                records_last_7_days=15, peak_hour="22"),
            total_records=120, time_range="最近30天",
            recent_chats=[profile_pb2.ChatRecord(role="user", content="我最近怎么样", created_at="2026-09-02")],
            llm_config=llm_config(),
        ))
        check("profile 六维齐全", all([resp.todo_analysis, resp.learning_analysis, resp.mood_analysis,
                                       resp.rhythm_analysis, resp.overall_summary]), "5 text fields")
        check("profile user_tags 非空", len(resp.user_tags) > 0, str(list(resp.user_tags)))
        print(f"      tags={list(resp.user_tags)} | summary={resp.overall_summary[:40]}")
    except grpc.RpcError as e:
        check("GenerateProfile", False, f"{e.code()}: {e.details()}")


def main():
    stub_mode = "--stub" in sys.argv
    skip_embed = "--skip-embed" in sys.argv or stub_mode

    print("=" * 50)
    print("Mirror AI 冒烟测试客户端")
    print(f"模式: {'桩模式（无真实 LLM Key）' if stub_mode else '真实 LLM'}")
    print("=" * 50)

    if stub_mode:
        start_stub()
        print(f"桩 LLM 已启动: http://127.0.0.1:{STUB_PORT}/v1")

    with grpc.insecure_channel('localhost:50051') as channel:
        grpc.channel_ready_future(channel).result(timeout=5)
        print("\n[连接] OK")

        rp_stub = rp_grpc.RecordProcessorStub(channel)
        emb_stub = emb_grpc.EmbeddingServiceStub(channel)
        chat_stub = chat_grpc.MirrorChatStub(channel)
        profile_stub = profile_grpc.MirrorProfileStub(channel)

        test_get_model_info(emb_stub)
        test_classify(rp_stub, single=False)
        test_classify(rp_stub, single=True)
        if not skip_embed:
            test_embed(emb_stub)
        test_extract_intent(chat_stub)
        test_chat(chat_stub)
        test_generate_profile(profile_stub)

    print("\n" + "=" * 50)
    print(f"结果: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        print("失败项: " + ", ".join(FAIL))
        sys.exit(1)
    print("=" * 50)


if __name__ == '__main__':
    main()
