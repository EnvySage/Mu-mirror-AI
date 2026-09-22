# Mu-mirror-AI（Python gRPC）交接文档

> 2026-09-06 多 Agent 开发阶段收尾交接。接手人：项目所有者。
> 配套阅读：`docs/2026-09-03-system-design-v2.md`（主设计 v2.3）。

## 1. 技术栈与运行

| 项 | 值 |
|---|---|
| Python 3 + gRPC 同步 server（**非 aio**） | `server.py` |
| 端口 | **10003**，workers 4 |
| 虚拟环境 | `.venv/`（**必须用 `.venv/Scripts/python.exe`，裸 python 启动桩会退出码 49**） |
| 启动 | `.venv/Scripts/python.exe server.py` |
| 桩 LLM | `coordination/stub_llm.py`，**OpenAI 兼容 18080 端口**，同样必须用 AI 仓 venv python 启动 |
| proto 生成 | 改 `proto/*.proto` 后跑 `generate_proto.py` 重生成 `generated/` |

## 2. 架构铁律（改代码前必读）

1. **无状态**：不连数据库、不存任何会话状态。用户 LLM/embedding 配置随**每次 gRPC 请求**到达（LLMConfig/EmbeddingConfig 消息），用完即弃。
2. **不做业务闸门**：镜子回看档位的条数/字符闸在 Java 侧（四道防洪闸），本仓只做渲染层截断防线（config.yml mirror 段三个 max_chars）。
3. **失败隔离**：LLM 超时/解析失败走 errors.py 统一映射 gRPC 状态码（abort_with_mapped），Java 侧降级不崩。
4. **不改存量向量语义**：1024 维硬约束（裁决 #18）。

## 3. 目录导航

```
server.py               gRPC server 装配（4 个 service 注册）
services/
  record_processor.py   Classify（single 模式）+ ExtractTerms（词典抽取）
  chat_service.py       Chat 流式 + ExtractIntent + CONTENT_TYPES 常量
  profile_service.py    GenerateProfile——递归累计镜子四块渲染（_fmt_prev_mirror/_fmt_correction_index/_fmt_stats_facts）
  plan_service.py       PlanTools——LLM 输出工具计划 JSON（≤2 步）
  embedding_service.py  Embed（api/local 双模式，factory 选择）
llm/                    openai_llm / anthropic_llm + factory（用户配置驱动）
embedding/              api_embedder（qwen3.7-text-embedding 阿里云 MaaS）/ local_embedder（BGE-m3，本机无 torch 不可用）
glossary_render.py      词典 prompt 渲染（固定软约束话术：词表仅供参考，与近期记录矛盾以记录为准）
prompts/                8 套 prompt：classify / classify-single / intent / chat / profile / inspiration / extract_terms / plan_tools
prompts_loader.py       占位符渲染 + _ALIASES 别名通道（todos→stats_facts、learnings→records，旧调用名不破坏）
errors.py / llm_json.py / config.py（config.yml）
tests/                  按轮次：test_round9~12 + e2e_round9~12
```

## 4. 近两轮大改动

1. **PlanTools RPC**（dac15e8，第十一轮）：第 8 套 prompt `plan_tools.txt`；ChatRequest.tool_results 渲染进 chat prompt；工具结果里标注 [F编号] 的文件引用规则写进 chat.txt。
2. **递归累计镜子**（a3396f6，第十二轮）：
   - `mirror_profile.proto` GenerateProfileRequest 加 `prev_mirror=11 / correction_index=12 / optional int32 mirror_lookback=13`——**lookback 必须 proto3 optional**：显式 0（纯继承档）与未传（旧客户端缺省 1）语义不同，裸 int32 表达不了
   - `prompts/profile.txt` 重写：累计画像 7 规则，核心是**待办状态以 stats_facts 实况为唯一真源，不从上月镜子继承旧说法**
   - ②本月记录/④统计实况复用既有 learnings/todos 通道传（不加 proto，靠 _ALIASES 映射）——**Java 组装时须按此约定塞字段**

## 5. 测试

```
单测：test_round9~12 共 143 项        → .venv/Scripts/python.exe -m pytest tests/test_round12.py（逐文件跑）
冒烟：test_client.py --stub 18 项     → 需桩 LLM 18080 在线
E2E：e2e_round9~12                    → 需 server 10003 + 桩 18080 都在线
环境自检：test_environment.py
```

⚠️ 改桩 LLM 后**必须重启 18080 进程**才生效（曾因旧桩残留导致 E2E genesis 场景假失败）。

## 6. 已知问题 / 待办

| 项 | 说明 |
|---|---|
| PlanTools 稳定性 | 曾 DEADLINE_EXCEEDED（3s 超时），Java 侧降级走 RAG 零回归，但"问论文→文件卡"端到端还没真跑通。若联调卡这，先看 plan_service 超时配置与桩 LLM 响应速度 |
| chat_service.py CONTENT_TYPES | ExtractIntent 不校验该常量（早期记录的疑点，后来未见复现，接手后可顺手核查） |
| local embedding 不可用 | 本机无 torch，factory 走 api 模式；真实 BGE-m3 链路未验（设计上等价） |
| 用户真实 API 配置 | 在数据库 user_settings 表（Java 仓管理），本仓 yml 无任何密钥 |

## 7. 联调提醒

- 服务启动顺序无强依赖；但**改了 services/ 或 prompts/ 必须重启 10003**
- Java 侧 gRPC 超时 15s，本端 LLM timeout 20s——Java 会先放弃，本端不应白跑
- 词典轮 dev 要点：glossary_render 空词条不留孤儿话术（B 未传 glossary 时 prompt 与旧版完全一致，零回归）
