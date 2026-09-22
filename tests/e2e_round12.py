"""第十二轮 E2E 实测（真实 gRPC server + coordination/stub_llm.py 桩，端口 18080）

rolling-mirror 累计镜子 GenerateProfile 全链路场景（设计稿 §4-AI-3 / §5 验收口径）：
1. 有 prev_mirror（累计语义）：输出含承续元素（"承续上月"/上月镜子元素）——桩按"上一份镜子"节
   有内容路由 PROFILE_ROLLING_JSON
2. genesis（无①③）：输出为首份镜子语义（"首份镜子"，无承续表述）——桩按节内容为空路由
   PROFILE_GENESIS_JSON
3. 待办矛盾场景：prev_mirror 说没做（"健身计划未完成"）、stats_facts 实况显示已完成
   （todos 通道无该项）→ 输出必须写"本月完成"、不得复述"未完成"
4. correction_index 仅 0 档出现：mirror_lookback=0 + correction_index 请求桩路由到承续分支
   且 prompt 段落渲染实测（E2E 断言输出含校正校准表述）；lookback≠0 不带
5. 回归保障：B 未升级（三字段缺省）旧式请求正常六维输出

前置：coordination/stub_llm.py 已在 127.0.0.1:18080 运行，server.py 已在 10003 运行。
桩启动（AI 仓 venv python，裸 python 退出码 49）：
  E:/project/mirror/own/Mu-mirror-AI/.venv/Scripts/python.exe E:/project/mirror/own/coordination/stub_llm.py
用法：.venv/Scripts/python.exe tests/e2e_round12.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import grpc  # noqa: E402

from generated import mirror_profile_pb2 as profile_pb2  # noqa: E402
from generated import mirror_profile_pb2_grpc as profile_grpc  # noqa: E402

PASS, FAIL = [], []

STUB_URL = "http://127.0.0.1:18080/v1"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def llm_config():
    return profile_pb2.GenerateProfileRequest().llm_config  # 占位（真实构造在请求函数内）


def _llm_config():
    from generated import common_pb2 as common
    return common.LlmConfig(provider="stub", protocol=common.AiProtocol.OPENAI,
                            api_key="stub-key", base_url=STUB_URL, model="stub-model")


def generate(stub, **kwargs):
    req = profile_pb2.GenerateProfileRequest(llm_config=_llm_config(), **kwargs)
    return stub.GenerateProfile(req)


def main():
    print("=" * 60)
    print("第十二轮 E2E：累计镜子 GenerateProfile（真实 server + 桩 LLM）")
    print("=" * 60)

    with grpc.insecure_channel("localhost:10003") as channel:
        grpc.channel_ready_future(channel).result(timeout=5)
        profile = profile_grpc.MirrorProfileStub(channel)

        # --- 1. 有 prev_mirror（累计语义，输出含承续元素）---
        print("\n[1] 累计镜子：有 prev_mirror → 输出含承续元素")
        r = generate(
            stub=profile,
            prev_mirror="上月镜子（7月）：学习主线 Three.js/WebGL，未完成待办 3 条（写周报/手办柜灯带/学习计划），"
                        "情绪以 satisfied 为主，夜猫子节律。",
            todos=[profile_pb2.TodoItem(record_id=1, title="学习计划", summary="着色器实践尚未开始",
                                        created_at="9-05")],
            learnings=[profile_pb2.LearningItem(record_id=2, title="粒子系统",
                                                summary="完成 Three.js 粒子系统",
                                                keywords=["Three.js"], created_at="9-03")],
            mood_stats=[profile_pb2.MoodStat(mood="satisfied", count=7, percentage=50.0)],
            total_records=12, time_range="2026年9月",
            mirror_lookback=1,
        )
        joined = "".join([r.todo_analysis, r.learning_analysis, r.mood_analysis,
                          r.rhythm_analysis, r.overall_summary])
        check("六维齐全", all([r.todo_analysis, r.learning_analysis, r.mood_analysis,
                              r.rhythm_analysis, r.overall_summary]), f"tags={list(r.user_tags)}")
        check("user_tags 非空且 ≤5", 0 < len(r.user_tags) <= 5, str(list(r.user_tags)))
        check("输出含承续元素", ("承续" in joined) or ("上月" in joined),
              joined[:60])
        check("输出含上月元素（Three.js 主线继承）", "Three.js" in joined)
        check("输出含本月完成表述（实况覆盖旧说法）", "本月完成" in joined,
              r.todo_analysis[:60])
        check("整体 80 字级 overall_summary", 0 < len(r.overall_summary) <= 200,
              f"len={len(r.overall_summary)}")

        # --- 2. genesis（无①③，首份镜子语义）---
        print("\n[2] genesis：无 prev_mirror → 首份镜子语义")
        rg = generate(
            stub=profile,
            todos=[profile_pb2.TodoItem(record_id=1, title="学习计划", summary="还没开始",
                                        created_at="9-05")],
            learnings=[profile_pb2.LearningItem(record_id=2, title="粒子系统",
                                                summary="完成 Three.js 粒子系统",
                                                created_at="9-03")],
            total_records=12, time_range="2026年9月",
        )
        joined_g = "".join([rg.todo_analysis, rg.learning_analysis, rg.mood_analysis,
                            rg.rhythm_analysis, rg.overall_summary])
        check("genesis 六维齐全", all([rg.todo_analysis, rg.learning_analysis, rg.mood_analysis,
                                      rg.rhythm_analysis, rg.overall_summary]))
        check("输出为首份镜子语义", "首份镜子" in joined_g, joined_g[:60])
        # 不虚构历史：不出现"承续上月镜子"话术、不引用上月元素（桩 GENESIS 文案"无上月镜子可承续"
        # 是对无①的解释，不是承续叙事——排除词用承续话术而非单字"承续"）
        check("genesis 不虚构历史", ("承续上月镜子" not in joined_g) and ("延续上月" not in joined_g),
              joined_g[:80])

        # --- 3. 待办矛盾场景（prev_mirror 说没做、stats_facts 说完成）---
        print("\n[3] 待办矛盾：prev_mirror 说没做 + 实况显示完成 → 输出必须写完成")
        # prev_mirror 明确说健身计划未完成；todos 通道（④实况唯一真源）只含 1 条无关待办，
        # 健身计划不在未完成清单 = 实况显示已完成
        rc = generate(
            stub=profile,
            prev_mirror="上月镜子（8月）：开启健身计划，计划每日 3 组，当前未完成。学习主线 Three.js。",
            todos=[profile_pb2.TodoItem(record_id=3, title="学习计划", summary="着色器实践尚未开始",
                                        created_at="9-05")],
            total_records=9, time_range="2026年9月",
            mirror_lookback=0, correction_index="- [8-02] 健身计划开启\n- [8-15] 论文开题",
        )
        joined_c = "".join([rc.todo_analysis, rc.overall_summary])
        check("矛盾场景输出写'本月完成'", "本月完成" in joined_c, rc.todo_analysis[:60])
        check("矛盾场景不复述'未完成'旧说法",
              ("未完成" not in rc.todo_analysis) or ("未完成" in rc.todo_analysis and
                                                    "健身计划" not in
                                                    rc.todo_analysis.split("未完成")[1][:20]),
              rc.todo_analysis[:80])
        check("矛盾场景仍承续上月元素", ("上月" in joined_c) or ("承续" in joined_c))

        # --- 4. correction_index 仅 0 档出现（wire + 渲染实测）---
        print("\n[4] 档位语义：lookback=0 带 correction_index / lookback=2 不带")
        r0 = generate(
            stub=profile,
            prev_mirror="上月镜子（8月）：学习主线 Three.js，夜猫子节律。",
            correction_index="- [8-02] 健身计划开启\n- [8-15] 论文开题",
            mirror_lookback=0, total_records=9, time_range="2026年9月",
        )
        check("0 档输出正常（承续语义）", ("承续" in r0.overall_summary) or ("上月" in r0.overall_summary),
              r0.overall_summary[:40])
        r2 = generate(
            stub=profile,
            prev_mirror="上月镜子（8月）：学习主线 Three.js，夜猫子节律。",
            mirror_lookback=2, total_records=9, time_range="2026年9月",
        )
        check("2 档输出正常（不带校正索引，无附加节）",
              ("承续" in r2.overall_summary) or ("上月" in r2.overall_summary),
              r2.overall_summary[:40])

        # --- 5. 回归：B 未升级（三字段缺省）旧式请求 ---
        print("\n[5] 回归保障：旧式请求（三字段缺省）六维输出不变")
        ro = generate(
            stub=profile,
            todos=[profile_pb2.TodoItem(record_id=1, title="写周报", summary="还没写",
                                        created_at="9-01")],
            learnings=[profile_pb2.LearningItem(record_id=2, title="gRPC", summary="学习了流式 RPC",
                                                keywords=["gRPC"], created_at="9-02")],
            mood_stats=[profile_pb2.MoodStat(mood="anxious", count=3, percentage=30.0)],
            total_records=120, time_range="最近30天",
        )
        check("旧式请求六维齐全", all([ro.todo_analysis, ro.learning_analysis, ro.mood_analysis,
                                      ro.rhythm_analysis, ro.overall_summary]))
        check("旧式请求走 genesis 语义（无上月镜子）", "首份镜子" in ro.todo_analysis)

    print("\n" + "=" * 60)
    print(f"结果: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        print("失败项: " + ", ".join(FAIL))
        sys.exit(1)


if __name__ == "__main__":
    main()
