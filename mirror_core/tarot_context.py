"""Tarot is an optional final-answer aside, never planning evidence."""

def render_tarot_context(context) -> str:
    if not context.HasField("divination"):
        return ""
    draw = context.divination
    if (not draw.entertainment_only or not draw.draw_id or not draw.cards
            or not context.current_date or draw.draw_date != context.current_date
            or draw.mention_mode not in ("casual", "explicit")):
        return ""
    card = draw.cards[0]
    limit = ("用户没有主动问牌。仅当贴合当前闲聊时可顺带提一句，也可以完全不提；最多一句，不另起塔罗章节。"
             if draw.mention_mode == "casual" else "用户主动询问当天牌，可以简短回应牌义。")
    return (f"当天日牌（仅供娱乐）：{card.card_name}；牌义：{card.interpretation}\n"
            + limit + "真实问题与感受始终优先。牌不是事实、预测或行动依据；不保证好结果，不给人贴人格标签。"
            "只提供建设性的视角，不能用‘愚者’等名称贬低用户。用户未抽牌时不可编造抽牌结果。")
