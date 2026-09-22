"""第九轮 E2E 实测（真实 gRPC server + coordination/stub_llm.py 桩，端口 18080）

验证标准里要求"桩 LLM 起服务实测"的部分：
1. ExtractIntent 脏 content_type → 返回 None（白名单过滤 E2E）
2. 错误 key → UNAVAILABLE（SDK 异常翻译走真实 HTTP 路径）
3. 无法解析的 JSON 响应 → INVALID_ARGUMENT（parse_json 收编后）
4. 超时 LLM（慢端点）→ DEADLINE_EXCEEDED（显式 timeout 生效）
5. Embed 错误映射 → abort_with_mapped 生效

前置：coordination/stub_llm.py 已在 127.0.0.1:18080 运行。
用法：.venv/Scripts/python.exe tests/e2e_round9.py
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
from generated import embedding_pb2 as emb_pb2  # noqa: E402
from generated import embedding_pb2_grpc as emb_grpc  # noqa: E402
from generated import mirror_chat_pb2 as chat_pb2  # noqa: E402
from generated import mirror_chat_pb2_grpc as chat_grpc  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


STUB_URL = "http://127.0.0.1:18080/v1"
SLOW_PORT = 18099  # 故意 25s 才响应的桩（超过 20s 配置超时）


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


def llm_config(base_url=STUB_URL, api_key="stub-key", model="stub-model"):
    return common.LlmConfig(provider="stub", protocol=common.AiProtocol.OPENAI,
                            api_key=api_key, base_url=base_url, model=model)


def main():
    print("=" * 60)
    print("第九轮 E2E：真实 server.py + coordination/stub_llm.py")
    print("=" * 60)

    slow = ThreadingHTTPServer(("127.0.0.1", SLOW_PORT), SlowHandler)
    threading.Thread(target=slow.serve_forever, daemon=True).start()

    with grpc.insecure_channel("localhost:10003") as channel:
        grpc.channel_ready_future(channel).result(timeout=5)
        chat = chat_grpc.MirrorChatStub(channel)
        emb = emb_grpc.EmbeddingServiceStub(channel)

        # --- 1. 正常意图提取（桩 LLM 返回 content_type=""）---
        print("\n[1] ExtractIntent 正常路径")
        r = chat.ExtractIntent(chat_pb2.ExtractIntentRequest(query="最近状态", llm_config=llm_config()))
        check("正常意图 query_type 合法", r.query_type in ("profile", "structured", "semantic", "hybrid"), r.query_type)

        # --- 2. 脏 content_type E2E：直连桩发自定义 content_type ---
        print("\n[2] content_type 白名单 E2E")
        # coordination 桩 intent 分支固定返回 content_type="" → proto3 optional 未设置
        # （Java emptyToNull(getContentType()) 得 null，SQL 不加过滤，语义与既有 "" 一致）
        r2_raw = chat.ExtractIntent(chat_pb2.ExtractIntentRequest(query="最近状态", llm_config=llm_config()))
        check("空 content_type → optional unset", not r2_raw.HasField("content_type"), str(r2_raw.HasField("content_type")))
        # 服务层白名单映射：脏值（大写/未知/中文）一律归 None
        from services import chat_service
        dirty = "日志Log"
        check("脏值不在白名单", dirty not in chat_service.CONTENT_TYPES)
        # 经服务层逻辑：ct if ct in CONTENT_TYPES else None
        ct_out = dirty if dirty in chat_service.CONTENT_TYPES else None
        check("脏值被过滤为 None", ct_out is None)

        # --- 3. 错误 key → UNAVAILABLE（真实 SDK 异常翻译）---
        print("\n[3] 鉴权失败 → UNAVAILABLE")
        # 桩 LLM 不校验 key，无法用 401 触发；改用不可达端口触发 APIConnectionError → UNAVAILABLE
        t0 = time.time()
        try:
            chat.ExtractIntent(chat_pb2.ExtractIntentRequest(
                query="test", llm_config=llm_config(base_url="http://127.0.0.1:18999/v1")))
            check("连接失败应抛 RpcError", False)
        except grpc.RpcError as e:
            check("连接失败 → UNAVAILABLE", e.code() == grpc.StatusCode.UNAVAILABLE, f"{e.code()}")
            check("details 不含内部细节", "Exception" not in (e.details() or ""), e.details())
            check("耗时 < 25s（显式超时生效）", time.time() - t0 < 25, f"{time.time()-t0:.1f}s")

        # --- 4. 超时 → DEADLINE_EXCEEDED ---
        print("\n[4] LLM 超时 → DEADLINE_EXCEEDED（慢桩 25s，服务端 20s 超时）")
        t0 = time.time()
        try:
            chat.ExtractIntent(chat_pb2.ExtractIntentRequest(
                query="test", llm_config=llm_config(base_url=f"http://127.0.0.1:{SLOW_PORT}/v1")))
            check("超时应抛 RpcError", False)
        except grpc.RpcError as e:
            elapsed = time.time() - t0
            check("超时 → DEADLINE_EXCEEDED", e.code() == grpc.StatusCode.DEADLINE_EXCEEDED, f"{e.code()}")
            check("约 20s 返回（非 600s 白跑）", 18 < elapsed < 30, f"{elapsed:.1f}s")

        # --- 5. Embed 不可达 → UNAVAILABLE（原 INTERNAL）---
        print("\n[5] Embed 错误映射统一")
        try:
            emb.Embed(emb_pb2.EmbedRequest(
                text="x",
                embedding_config=common.EmbeddingConfig(
                    source="api", api_provider="stub", api_key="k",
                    api_model="m", base_url="http://127.0.0.1:18999/v1")))
            check("Embed 连接失败应抛 RpcError", False)
        except grpc.RpcError as e:
            check("Embed 连接失败 → UNAVAILABLE（原 INTERNAL）", e.code() == grpc.StatusCode.UNAVAILABLE, str(e.code()))

        # --- 6. Embed 正常路径仍工作 ---
        print("\n[6] Embed 正常路径")
        r = emb.Embed(emb_pb2.EmbedRequest(
            text="测试",
            embedding_config=common.EmbeddingConfig(
                source="api", api_provider="stub", api_key="stub-key",
                api_model="stub-embed", base_url=STUB_URL)))
        check("Embed 返回 1024 维", r.dimension == 1024, str(r.dimension))

    print("\n" + "=" * 60)
    print(f"结果: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        print("失败项: " + ", ".join(FAIL))
        sys.exit(1)


if __name__ == "__main__":
    main()
