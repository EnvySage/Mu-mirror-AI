"""MirrorProfile 服务实现 — 画像生成"""

from generated import mirror_profile_pb2 as pb2
from generated import mirror_profile_pb2_grpc as pb2_grpc


class MirrorProfileServicer(pb2_grpc.MirrorProfileServicer):
    """画像服务（测试阶段，返回硬编码数据）"""

    def GenerateProfile(self, request, context):
        llm_config = request.llm_config
        total_records = request.total_records
        time_range = request.time_range

        print(f"[GenerateProfile] records: {total_records}, range: {time_range}")
        print(f"[GenerateProfile] LLM: {llm_config.provider}/{llm_config.model}")

        # 测试数据
        return pb2.GenerateProfileResponse(
            todo_analysis="你有若干待办事项等待完成，建议优先处理紧急的。",
            learning_analysis="最近在学习技术相关知识，保持了良好的学习节奏。",
            mood_analysis="整体情绪平稳，偶尔有焦虑感，建议适当放松。",
            user_tags=["学习型", "技术向", "有规划"],
            rhythm_analysis="你习惯在晚间记录，是个夜猫子。",
            overall_summary="近期状态不错，学习节奏稳定，待办事项需要适当清理。",
        )