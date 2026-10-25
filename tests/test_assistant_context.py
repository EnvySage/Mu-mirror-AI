import unittest
from generated import common_pb2
from generated import mirror_chat_pb2
from generated import mirror_profile_pb2
from services.chat_service import assistant_context_text
from services.profile_service import _fmt_assistant_memories

class AssistantContextTest(unittest.TestCase):
    def test_context_protocol_field_numbers(self):
        self.assertEqual({k:v.number for k,v in common_pb2.AssistantContext.DESCRIPTOR.fields_by_name.items()}, {
            'role_tone':1,'current_date':2,'timezone':3,'action_status_summary':4,'memories':5,'divination':6})
        memory = common_pb2.ChatMemoryContext.DESCRIPTOR.fields_by_name
        self.assertEqual({k:v.number for k,v in memory.items()}, {'memory_id':1,'content':2,'kind':3,'source_date':4,'source_message_ids':5,'event_date':6,'expires_at':7})
        self.assertEqual(memory['memory_id'].type, 9)  # TYPE_STRING: B memory IDs are UUIDs.
        self.assertEqual(memory['source_message_ids'].type, 3)  # TYPE_INT64
        self.assertEqual(mirror_profile_pb2.GenerateProfileRequest.DESCRIPTOR.fields_by_name['stats_facts'].number, 14)
        self.assertEqual(mirror_profile_pb2.GenerateProfileRequest.DESCRIPTOR.fields_by_name['assistant_context'].number, 15)

    def test_chat_context_renders_memory_and_isolates_divination(self):
        context = common_pb2.AssistantContext(current_date='2026-10-19', timezone='Asia/Shanghai')
        context.memories.add(memory_id='1fe343fd-900d-475c-9366-bd6a2f2b6c79', content='喜欢晨跑', kind='preference', source_date='2026-10-18', source_message_ids=[3])
        context.divination.draw_id='d1'; context.divination.drawn_at='2026-10-19T10:00:00+08:00'
        context.divination.question='今天适合做什么？'; context.divination.entertainment_only=True
        context.divination.cards.add(card_name='星星', orientation='upright', interpretation='保持希望')
        rendered = assistant_context_text(context)
        self.assertIn('喜欢晨跑', rendered)
        self.assertIn('2026-10-18', rendered)
        self.assertIn('仅供娱乐', rendered)
        self.assertIn('星星', rendered)

    def test_profile_uses_only_authorized_memory_context_not_raw_chats(self):
        request = mirror_profile_pb2.GenerateProfileRequest(recent_chats=[
            mirror_profile_pb2.ChatRecord(role='user', content='RAW_PRIVATE_CHAT')])
        request.assistant_context.memories.add(memory_id='1fe343fd-900d-475c-9366-bd6a2f2b6c79', content='授权事实', kind='fact', source_date='2026-10-10')
        rendered = _fmt_assistant_memories(request.assistant_context.memories)
        self.assertIn('授权事实', rendered)
        self.assertNotIn('RAW_PRIVATE_CHAT', rendered)
