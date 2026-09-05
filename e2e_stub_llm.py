"""E2E 联调桩 LLM — OpenAI 兼容 HTTP 服务（端口 18080）

供 Java 后端配置 ai_protocol=openai / base_url=http://127.0.0.1:18080/v1 时使用。
按 prompt 关键词返回不同 JSON / SSE 流式，与 Mu-mirror-AI/test_client.py 的桩一致。
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 18080


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        print("[stub-llm]", self.command, self.path)

    def do_GET(self):
        # /v1/models 供连通性探测
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        body = json.dumps({"object": "list", "data": [{"id": "stub-model", "object": "model"}]}).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        prompt = " ".join(m.get("content", "") for m in body.get("messages", []))
        stream = body.get("stream", False)

        if self.path.endswith("/embeddings"):
            payload = {"data": [{"embedding": [0.01] * 1024, "index": 0}]}
            out = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        elif "确认过边界" in prompt:  # classify-single
            payload = {"skip": False, "title": "学习gRPC", "summary": "学习了 gRPC 流式调用",
                       "content_type": "LEARNING", "moods": [], "status": "STATUS_UNKNOWN",
                       "keywords": ["gRPC"]}
        elif "拆分" in prompt and "记录分类助手" in prompt:  # classify split
            payload = {"skip": False, "split_content": "中午吃饭|||下午干活",
                       "items": [
                           {"title": "吃饭", "summary": "中午吃饭", "content_type": "NOTE",
                            "moods": [], "status": "STATUS_UNKNOWN", "keywords": ["吃饭"]},
                           {"title": "干活", "summary": "下午干活", "content_type": "TODO",
                            "moods": [], "status": "in_progress", "keywords": ["工作"]},
                       ]}
        elif "query_type" in prompt:  # intent
            payload = {"query_type": "hybrid", "content_type": "", "moods": [],
                       "time_range": "", "rewritten_query": "未完成待办 焦虑"}
        elif "画像分析" in prompt:  # profile
            payload = {"todo_analysis": "标记了 3 次未完成待办", "learning_analysis": "学习了 gRPC 与 pgvector",
                       "mood_analysis": "标记了 2 次焦虑", "user_tags": ["夜猫子", "学习者"],
                       "rhythm_analysis": "晚间 22 点后记录最多", "overall_summary": "近期记录集中在学习与工作。"}
        else:  # chat
            text = "你最近记录了 3 条待办[1]，其中 2 条已完成。"
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for piece in [text[:10], text[10:]]:
                    data = json.dumps({"choices": [{"delta": {"content": piece}}]}, ensure_ascii=False)
                    chunk = f"data: {data}\n\n".encode("utf-8")
                    self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
                end = b"data: [DONE]\n\n"
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                return
            payload = text

        out = json.dumps({"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]},
                         ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


if __name__ == "__main__":
    print(f"stub LLM on http://127.0.0.1:{PORT}/v1")
    ThreadingHTTPServer(("127.0.0.1", PORT), StubHandler).serve_forever()
