"""第十一轮 E2E 实测（真实 gRPC server + coordination/stub_llm.py 桩，端口 18080）

toolcalling sprint PlanTools 全链路五场景 + tool_results 渲染实测：
1. 正常规划：get_stats+search_records 两步计划 → PlannedCall 校验通过（桩按场景词返回造好 JSON）
2. 空计划：闲聊类问题 → {"calls":[]}，PlanToolsReply 空（B 侧据此跳过工具）
3. 工具名幻觉：注册表外 delete_all_records → 剔除，只留注册表内的 get_stats
4. LLM 超时：慢桩 25s > 20s 预算 → DEADLINE_EXCEEDED（B 侧 3s deadline 内自行放弃走现有链路）
5. 坏 JSON：桩返回非 JSON → INVALID_ARGUMENT，且失败隔离（后续调用正常）
6. 步数超限（3 步 → 截断 2）+ args 坏 JSON（仅剔除该步）附加场景
7. Chat tool_results 渲染：带工具结果流式正常；空列表零影响；B1/B2/B4 场景接线（coverage/persona 计划）

前置：coordination/stub_llm.py 已在 127.0.0.1:18080 运行，server.py 已在 50051 运行。
用法：.venv/Scripts/python.exe tests/e2e_round11.py
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import grpc  # noqa: E402

from generated import common_pb2 as common  # noqa: E402
from generated import mirror_chat_pb2 as chat_pb2  # noqa: E402
from generated import mirror_chat_pb2_grpc as chat_grpc  # noqa: E402

PASS, FAIL = [], []

STUB_URL = "http://127.0.0.1:18080/v1"
SLOW_PORT = 18106     # 故意 25s 才响应的桩（> 20s 服务端预算）
BADJSON_PORT = 18105  # 返回非 JSON 的桩

# E2E 用的注册表快照（模拟 Java ToolRegistry 传入，含 get_coverage/get_profile 变体）
REGISTRY = [
    common.ToolSpec(name="search_records", description="四路检索日记记录，返回 chunk 列表",
                    args_schema='{"query?":"string","days?":"int","moods?":"string[]","limit?":"int<=20"}'),
    common.ToolSpec(name="get_stats", description="统计汇总：记录数/情绪分布/待办剩余",
                    args_schema='{"days?":"int"}'),
    common.ToolSpec(name="get_profile", description="画像快照（支持快照人格变体）",
                    args_schema='{"month?":"string"}'),
    common.ToolSpec(name="get_coverage", description="主题覆盖度：最早/最近提及、记录数、空白期",
                    args_schema='{"query":"string"}'),
]


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
        body = json.dumps({"choices": [{"message": {"content": "抱歉，我无法以 JSON 格式输出工具计划。"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def llm_config(base_url=STUB_URL):
    return common.LlmConfig(provider="stub", protocol=common.AiProtocol.OPENAI,
                            api_key="stub-key", base_url=base_url, model="stub-model")


def plan_request(question, base_url=STUB_URL):
    return chat_pb2.PlanToolsRequest(question=question, tools=REGISTRY, llm_config=llm_config(base_url))


def main():
    print("=" * 60)
    print("第十一轮 E2E：PlanTools 五场景 + tool_results 渲染（真实 server + 桩 LLM）")
    print("=" * 60)

    slow = ThreadingHTTPServer(("127.0.0.1", SLOW_PORT), SlowHandler)
    threading.Thread(target=slow.serve_forever, daemon=True).start()
    badjson = ThreadingHTTPServer(("127.0.0.1", BADJSON_PORT), BadJsonHandler)
    threading.Thread(target=badjson.serve_forever, daemon=True).start()

    with grpc.insecure_channel("localhost:50051") as channel:
        grpc.channel_ready_future(channel).result(timeout=5)
        chat = chat_grpc.MirrorChatStub(channel)

        # --- 1. 正常规划（统计汇总类，2 步）---
        print("\n[1] PlanTools 正常规划（get_stats + search_records 两步）")
        r = chat.PlanTools(plan_request("我这个月记了多少条日记？"))
        check("计划非空", len(r.calls) == 2, f"n={len(r.calls)}")
        check("步数 ≤2", len(r.calls) <= 2)
        check("工具名在注册表内", all(c.tool in {t.name for t in REGISTRY} for c in r.calls),
              ",".join(c.tool for c in r.calls))
        args0 = json.loads(r.calls[0].args_json)
        check("args_json 可解析且为对象", isinstance(args0, dict), r.calls[0].args_json)
        check("首步 get_stats", r.calls[0].tool == "get_stats", r.calls[0].tool)

        # --- 1b. B1 事实核查场景（可核查自我评价 → search_records 本周窗口）---
        print("\n[1b] B1 场景接线：可核查自我评价 → search_records")
        r_b1 = chat.PlanTools(plan_request("汇总一下我这周啥也没干的核查线索"))
        check("B1 计划含 search_records", any(c.tool == "search_records" for c in r_b1.calls),
              ",".join(c.tool for c in r_b1.calls))

        # --- 1c. B2 覆盖度场景 ---
        print("\n[1c] B2 场景接线：我什么时候开始… → get_coverage")
        r_b2 = chat.PlanTools(plan_request("我什么时候开始健身的？我了解你什么——覆盖度"))
        check("B2 计划含 get_coverage", any(c.tool == "get_coverage" for c in r_b2.calls),
              ",".join(c.tool for c in r_b2.calls))

        # --- 1d. B4 快照人格场景 ---
        print("\n[1d] B4 场景接线：用 8 月的我回答 → get_profile 变体")
        r_b4 = chat.PlanTools(plan_request("请用 8 月的我回答：那段时间的我状态如何？（快照人格）"))
        check("B4 计划含 get_profile", any(c.tool == "get_profile" for c in r_b4.calls),
              ",".join(c.tool for c in r_b4.calls))

        # --- 2. 空计划（闲聊，无需工具）---
        print("\n[2] PlanTools 空计划（闲聊类问题）")
        r2 = chat.PlanTools(plan_request("你好呀，谢谢你的陪伴，闲聊一下"))
        check("空 calls", len(r2.calls) == 0, f"n={len(r2.calls)}")

        # --- 3. 工具名幻觉（注册表外 → 剔除）---
        print("\n[3] PlanTools 工具名幻觉（delete_all_records 不在注册表 → 剔除）")
        r3 = chat.PlanTools(plan_request("幻觉注册表外场景：帮我看看数据"))
        check("幻觉工具被剔除", all(c.tool != "delete_all_records" for c in r3.calls),
              ",".join(c.tool for c in r3.calls))
        check("合法项保留", any(c.tool == "get_stats" for c in r3.calls),
              ",".join(c.tool for c in r3.calls))

        # --- 4. LLM 超时 → DEADLINE_EXCEEDED ---
        print("\n[4] PlanTools LLM 超时（慢桩 25s，服务端 20s 预算）")
        t0 = time.time()
        try:
            chat.PlanTools(plan_request("我这个月记了多少条日记？",
                                        base_url=f"http://127.0.0.1:{SLOW_PORT}/v1"))
            check("超时应抛 RpcError", False)
        except grpc.RpcError as e:
            elapsed = time.time() - t0
            check("超时 → DEADLINE_EXCEEDED", e.code() == grpc.StatusCode.DEADLINE_EXCEEDED, f"{e.code()}")
            check("约 20s 返回（20s 级，B 侧 3s deadline 前自行放弃）", 18 < elapsed < 30, f"{elapsed:.1f}s")

        # --- 5. 坏 JSON → INVALID_ARGUMENT + 失败隔离 ---
        print("\n[5] PlanTools 坏 JSON（桩返回非 JSON）")
        try:
            chat.PlanTools(plan_request("我这个月记了多少条日记？",
                                        base_url=f"http://127.0.0.1:{BADJSON_PORT}/v1"))
            check("坏 JSON 应抛 RpcError", False)
        except grpc.RpcError as e:
            check("坏 JSON → INVALID_ARGUMENT", e.code() == grpc.StatusCode.INVALID_ARGUMENT, f"{e.code()}")
            check("details 安全摘要", "ContentInvalidError" in (e.details() or ""), e.details())

        print("\n[5b] 失败隔离：坏 JSON 后 PlanTools/Chat 仍正常")
        r5 = chat.PlanTools(plan_request("我这个月记了多少条日记？"))
        check("失败后再调 PlanTools 正常", len(r5.calls) == 2)

        # --- 6. 附加：步数超限截断 + args 坏 JSON 剔除 ---
        print("\n[6] 附加场景：步数超限（3 步 → 2）+ args 坏 JSON（仅剔除该步）")
        r6a = chat.PlanTools(plan_request("步数超限场景：多查几路数据"))
        check("步数截断 ≤2", len(r6a.calls) <= 2, f"n={len(r6a.calls)}")
        r6b = chat.PlanTools(plan_request("参数坏场景：查一下"))
        check("args 坏的步被剔除", all(json.loads(c.args_json) is not None for c in r6b.calls),
              ",".join(f"{c.tool}" for c in r6b.calls))
        check("合法步保留", len(r6b.calls) >= 1, f"n={len(r6b.calls)}")

        # --- 7. Chat tool_results 渲染 ---
        print("\n[7] Chat tool_results 渲染（带工具结果 / 空列表零影响）")
        req = chat_pb2.ChatRequest(
            question="我论文进展如何？", llm_config=llm_config(),
            tool_results=[common.ToolResult(tool="search_records",
                                            summary="12 条：9-01 论文开题 RAG 方向获导师认可；9-05 检索模块跑通",
                                            payload_json="[]", success=True)],
            chunks=[chat_pb2.RetrievedChunk(record_id=1, content="论文开题通过",
                                            title="论文", created_at="2026-09-01")])
        got, done = "", False
        for chunk in chat.Chat(req):
            got += chunk.content
            done = done or chunk.done
        check("带 tool_results 流式正常", bool(got) and done, f"len={len(got)}")

        req_empty = chat_pb2.ChatRequest(question="我论文进展如何？", llm_config=llm_config())
        got2, done2 = "", False
        for chunk in chat.Chat(req_empty):
            got2 += chunk.content
            done2 = done2 or chunk.done
        check("空 tool_results 零影响（Chat 正常）", bool(got2) and done2, f"len={len(got2)}")

        # --- 8. 空注册表 → 空计划（不烧 LLM）---
        print("\n[8] 空注册表快照 → 空计划")
        r8 = chat.PlanTools(chat_pb2.PlanToolsRequest(question="随便聊聊",
                                                      llm_config=llm_config(
                                                          "http://127.0.0.1:1/v1")))
        check("空注册表返回空计划", len(r8.calls) == 0)

    print("\n" + "=" * 60)
    print(f"结果: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        print("失败项: " + ", ".join(FAIL))
        sys.exit(1)


if __name__ == "__main__":
    main()
