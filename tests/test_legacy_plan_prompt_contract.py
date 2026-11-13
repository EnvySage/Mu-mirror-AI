from mirror_core.prompts_loader import loader


def _render(name):
    return loader.render(name)


def test_legacy_loop_planner_preserves_search_modes_and_reads_authorized_body_pages():
    prompt = _render("plan_tools")

    assert "保留 legacy `mode`" in prompt
    assert "日期问题优先结构化日期查询，不用语义向量检索" in prompt
    assert "不能为扩大召回而放宽或省略这些条件" in prompt
    assert "`quote` 最多 120 字，只是定位摘录，绝不是完整记录正文" in prompt
    assert "先 `search_records` 找到记录 ID，再用返回的 ID 调 `read_record`" in prompt
    assert "snippets:[{chunk_id, record_id, date, text}]" in prompt
    assert "`content_scope=\"partial\"` 或 `complete=false` 时按 `next_offset` 续读" in prompt
    assert "{step}" not in prompt


def test_legacy_single_planner_does_not_claim_search_excerpt_is_full_text():
    prompt = _render("plan_tools_single")

    assert "legacy `mode=semantic`" in prompt
    assert "日期问题优先 `mode=structured` + 日期参数，不带语义 query" in prompt
    assert "主题问题同时带有日期、情绪或类型边界，则用 `mode=hybrid`" in prompt
    assert "只有纯粹询问情绪次数、频率或分布时才走统计意图" in prompt
    assert '"mode": "hybrid", "query": "哪些事情让我焦虑", "moods": ["anxious"], "days": 30' in prompt
    assert "问焦虑主题或原因，应使用 `mode=hybrid`" in prompt
    assert "`quote` 最多 120 字，是定位摘录而非全文" in prompt
    assert "这个单次规划看不到新搜索执行结果" in prompt
    assert "只有已有明确授权的 `record_id` 时才规划 `read_record`" in prompt
    assert "snippets:[{chunk_id, record_id, date, text}]" in prompt
    assert "{tools}" not in prompt
