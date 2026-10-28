import json
import pytest
from generated import tarot_pb2 as pb


def request(materials=()):
    return pb.TarotAnalyzeRequest(reading_id="reading", analysis_id="analysis", question="我想学画画，但怕自己学不会。",
        card=pb.TarotCard(id="00-fool", name="愚者", meaning="勇敢开始，边走边学", action="先尝试十分钟"),
        materials=materials, reference_date="2026-10-26", timezone="Asia/Shanghai")


def test_reading_uses_selected_sources_and_returns_traceable_advice():
    from mirror_core.tarot_reading import analyze_reading
    source=pb.TarotMaterial(ref="D1",kind="diary",source_id="7",excerpt="昨天开始练习画画",date="2026-10-25",version="v1")
    class Model:
        def json_task(self,messages,**kwargs):
            assert "昨天开始练习画画" in messages[-1]["content"]
            assert "不执行" in messages[0]["content"]
            return json.dumps({"text":"你已经开始练习画画[D1]。愚者可以提醒你允许自己从不会开始。今天先练十分钟，再看看哪里进步了。","used_refs":["D1"]},ensure_ascii=False)
    result=analyze_reading(request([source]),Model(),timeout=10)
    assert list(result.used_refs)==["D1"]
    assert "十分钟" in result.text


def test_unknown_citations_fail_instead_of_showing_invented_diary():
    from mirror_core.tarot_reading import analyze_reading
    class Model:
        def json_task(self,*args,**kwargs):
            return '{"text":"你昨天成功了[D9]。","used_refs":["D9"]}'
    with pytest.raises(ValueError,match="引用"):
        analyze_reading(request(),Model(),timeout=10)


def test_no_sources_still_allows_question_and_card_only():
    from mirror_core.tarot_reading import analyze_reading
    class Model:
        def json_task(self,messages,**kwargs):
            data=json.loads(messages[-1]["content"])
            assert data["materials"]==[]
            return '{"text":"可以先从一张简单的速写开始。愚者的启发是允许自己边走边学。","used_refs":[]}'
    result=analyze_reading(request(),Model(),timeout=10)
    assert not result.used_refs
    assert "速写" in result.text
