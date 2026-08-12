"""RecordProcessor 服务实现"""

import json
import grpc
from pathlib import Path

from generated import common_pb2 as common
from generated import record_processor_pb2 as pb2
from generated import record_processor_pb2_grpc as pb2_grpc

from llm.factory import create_llm

# Prompt 模板
PROMPT_DIR = Path(__file__).parent.parent / "prompts"
CLASSIFY_PROMPT = (PROMPT_DIR / "classify.txt").read_text(encoding="utf-8")

# 枚举映射表
CONTENT_TYPE_MAP = {
    "TODO": common.ContentType.TODO,
    "THOUGHT": common.ContentType.THOUGHT,
    "LEARNING": common.ContentType.LEARNING,
    "PLAN": common.ContentType.PLAN,
    "NOTE": common.ContentType.NOTE,
    "WORK": common.ContentType.WORK,
    "SOCIAL": common.ContentType.SOCIAL,
    "HEALTH": common.ContentType.HEALTH,
}

MOOD_MAP = {
    "HAPPY": common.MoodType.HAPPY,
    "EXCITED": common.MoodType.EXCITED,
    "SATISFIED": common.MoodType.SATISFIED,
    "GRATEFUL": common.MoodType.GRATEFUL,
    "EXPECTING": common.MoodType.EXPECTING,
    "CALM": common.MoodType.CALM,
    "BORED": common.MoodType.BORED,
    "CONFUSED": common.MoodType.CONFUSED,
    "ANXIOUS": common.MoodType.ANXIOUS,
    "SAD": common.MoodType.SAD,
    "ANGRY": common.MoodType.ANGRY,
    "EXHAUSTED": common.MoodType.EXHAUSTED,
    "STRESSED": common.MoodType.STRESSED,
}

STATUS_MAP = {
    "NOT_STARTED": common.TaskStatus.NOT_STARTED,
    "IN_PROGRESS": common.TaskStatus.IN_PROGRESS,
    "COMPLETED": common.TaskStatus.COMPLETED,
}


class RecordProcessorServicer(pb2_grpc.RecordProcessorServicer):

    def Classify(self, request, context):
        content = request.content
        llm_config = request.llm_config

        print(f"[Classify] 内容: {content}")
        print(f"[Classify] LLM: provider={llm_config.provider}, model={llm_config.model}, "
              f"api_key={'***' + llm_config.api_key[-4:] if llm_config.api_key else 'EMPTY'}, "
              f"base_url={llm_config.base_url}, protocol={llm_config.protocol}")

        try:
            # 创建 LLM 实例
            llm = create_llm(
                provider=llm_config.provider,
                api_key=llm_config.api_key,
                base_url=llm_config.base_url,
                model=llm_config.model,
                protocol=llm_config.protocol,
            )

            # 构建 prompt
            prompt = CLASSIFY_PROMPT.replace("{content}", content)
            messages = [{"role": "user", "content": prompt}]

            # 调用 LLM
            response_text = llm.chat(messages)
            print(f"[Classify] LLM 响应: {response_text}")

            # 解析 JSON
            result = _parse_json(response_text)

            # 映射到 proto
            return pb2.ClassifyResponse(
                skip=result.get("skip", False),
                skip_reason=result.get("skip_reason", ""),
                title=result.get("title", ""),
                summary=result.get("summary", ""),
                content_type=CONTENT_TYPE_MAP.get(result.get("content_type", ""), common.ContentType.CONTENT_UNKNOWN),
                moods=[MOOD_MAP[m] for m in result.get("moods", []) if m in MOOD_MAP],
                status=STATUS_MAP.get(result.get("status", ""), common.TaskStatus.STATUS_UNKNOWN),
                keywords=result.get("keywords", []),
            )

        except Exception as e:
            print(f"[Classify] 错误: {e}")
            context.abort(grpc.StatusCode.INTERNAL, f"分类失败: {str(e)}")

    def Split(self, request, context):
        content = request.content
        llm_config = request.llm_config

        print(f"[Split] 内容: {content}")

        # 暂不实现拆分逻辑，直接返回不需要拆分
        return pb2.SplitResponse(
            need_split=False,
            segments=[]
        )


def _parse_json(text: str) -> dict:
    """从 LLM 响应中提取 JSON"""
    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 尝试提取 ```json ... ``` 块
    if "```json" in text:
        start = text.index("```json") + 7
        end = text.index("```", start)
        return json.loads(text[start:end].strip())

    # 尝试提取 { ... } 块
    start = text.find("{")
    end = text.rfind("}") + 1
    if start != -1 and end > start:
        return json.loads(text[start:end])

    raise ValueError(f"无法解析 JSON: {text}")