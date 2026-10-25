import unittest
from generated import common_pb2
from generated import mirror_chat_pb2 as pb2
from services.chat_memory_service import ChatMemoryService
from services.plan_service import MirrorChatServicer


class _LLM:
    def __init__(self, response):
        self.response = response
        self.messages = None

    def json_task(self, messages):
        self.messages = messages
        return self.response


class ChatMemoryServiceTest(unittest.TestCase):
    def test_summarize_day_is_exposed_by_mirror_chat_rpc(self):
        self.assertIn("SummarizeDay", pb2.DESCRIPTOR.services_by_name["MirrorChat"].methods_by_name)
        self.assertTrue(hasattr(MirrorChatServicer, "SummarizeDay"))

    def _request(self, messages):
        return pb2.SummarizeDayRequest(
            messages=messages,
            source_hash="hash-1",
            source_date="2026-10-18",
            timezone="Asia/Shanghai",
            llm_config=common_pb2.LlmConfig(provider="test", model="fixture"),
        )

    def test_candidates_only_reference_user_input_and_are_capped_at_three(self):
        llm = _LLM('''{"summary":"用户聊到工作和计划。","memory_candidates":[
          {"content":"猜测用户焦虑","kind":"fact","source_message_ids":[999]},
          {"content":"喜欢早晨跑步","kind":"preference","source_message_ids":[11]},
          {"content":"准备报名课程","kind":"plan","source_message_ids":[12]},
          {"content":"正在照顾猫","kind":"fact","source_message_ids":[11,12]},
          {"content":"第五条","kind":"fact","source_message_ids":[12]}],"source_hash":"spoof-hash"}''')
        service = ChatMemoryService(llm_factory=lambda *args, **kwargs: llm)
        request = self._request([
            pb2.ChatDayMessage(message_id=11, content="我喜欢早晨跑步", created_at="2026-10-18T08:00:00+08:00"),
            pb2.ChatDayMessage(message_id=12, content="我准备报名课程", created_at="2026-10-18T09:00:00+08:00"),
        ])

        result = service.summarize_day(request)

        self.assertEqual(result.source_hash, "hash-1")
        self.assertEqual(len(result.memory_candidates), 3)
        self.assertEqual([candidate.source_message_ids[0] for candidate in result.memory_candidates], [11, 12, 11])
        self.assertTrue(all(set(candidate.source_message_ids) <= {11, 12}
                            for candidate in result.memory_candidates))
        self.assertIn("source_hash：hash-1", llm.messages[0]["content"])
        self.assertNotIn("999", llm.messages[0]["content"])

    def test_empty_summary_result_has_no_candidates(self):
        llm = _LLM('{"summary":"","memory_candidates":[],"source_hash":"hash-2"}')
        service = ChatMemoryService(llm_factory=lambda *args, **kwargs: llm)

        result = service.summarize_day(self._request([
            pb2.ChatDayMessage(message_id=7, content="你好", created_at="2026-10-18T12:00:00+08:00"),
        ]))

        self.assertEqual(result.summary, "")
        self.assertEqual(len(result.memory_candidates), 0)

    def test_incoming_ids_are_the_only_source_ids_exposed_to_prompt(self):
        llm = _LLM('{"summary":"","memory_candidates":[],"source_hash":"hash-1"}')
        service = ChatMemoryService(llm_factory=lambda *args, **kwargs: llm)
        request = self._request([
            pb2.ChatDayMessage(message_id=21, content="用户说的事实", created_at="2026-10-18T12:00:00+08:00"),
        ])
        service.summarize_day(request)
        self.assertIn('"message_id": 21', llm.messages[0]["content"])
        self.assertNotIn("assistant", llm.messages[0]["content"].lower())

    def test_empty_input_returns_no_summary_without_calling_model(self):
        llm = _LLM("should not be used")
        service = ChatMemoryService(llm_factory=lambda *args, **kwargs: llm)

        result = service.summarize_day(self._request([]))

        self.assertEqual(result.source_hash, "hash-1")
        self.assertEqual(result.summary, "")
        self.assertIsNone(llm.messages)


if __name__ == "__main__":
    unittest.main()


