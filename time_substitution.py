"""相对时间消解（ClassifyRequest.reference_date → ClassifyItem.time_substitutions）

分工（与 B 仓 TimeSubstitutionApplier 约定）：本端负责「识别 + 计算」，B 侧负责「执行替换」。
不让 LLM 直接吐改写后的全文——它会顺手润色掉非时间内容，而 segment 既给用户看又进 embedding。

B 侧 2026-09-20（bc54a8c）已上线替换执行器并开始传 reference_date，本端此前漏做：proto 没有
对应字段、prompt 没有要求、代码不产出，B 拿到的替换表恒为空，按其零回归设计原样返回——
功能上线但一个字都没替换过，且不报任何错。

两道防线（LLM 做日期算术不可靠）：
1. 固定偏移词（今天/明天/后天/昨天/前天/大后天/今晚……）由本端按参照日期**确定性计算**，
   覆盖 LLM 给的 resolved；"明天上午"这类带上下文的 original 收窄成"明天"，时段信息留在原文
2. 其余（下周三/这周末/三天后/月底……）采用 LLM 的结果，但必须解析成真实日期、距参照日期
   不超过一年、original 逐字出现在片段里且像个时间词，否则丢弃

无参照日期时：prompt 不渲染本节（空串，模板与旧版一致）、不产出任何替换——零回归。
"""

import re
from datetime import date, timedelta

# B 侧 TimeSubstitutionApplier 的同口径上限（双保险：两端都校验）
MAX_SUBSTITUTIONS = 5
MAX_ORIGINAL_LEN = 20

_WEEKDAYS = "一二三四五六日"

# 固定偏移的相对日词 → (相对参照日期的偏移天数, 保留的时段后缀)
# 带时段的（今晚/明早）替换成"9月12日晚上"，不丢"晚上"这层意思
_FIXED = {
    "大前天": (-3, ""),
    "前天": (-2, ""),
    "昨天": (-1, ""), "昨日": (-1, ""), "昨晚": (-1, "晚上"), "昨夜": (-1, "夜里"),
    "今天": (0, ""), "今日": (0, ""), "今早": (0, "早上"), "今晚": (0, "晚上"), "今夜": (0, "夜里"),
    "明天": (1, ""), "明日": (1, ""), "明早": (1, "早上"), "明晚": (1, "晚上"),
    "后天": (2, ""),
    "大后天": (3, ""),
}
# 长词优先匹配（"大后天"先于"后天"）
_FIXED_BY_LEN = sorted(_FIXED, key=len, reverse=True)
# 被更长固定词包含的短词（"后天" ⊂ "大后天"）：B 侧 String.replace 会替换所有出现，
# 文本里有"大后天"而替换表只有"后天"时，会把"大后天"改成"大9月14日"
_CONTAINED_IN = {w: [lw for lw in _FIXED if lw != w and w in lw] for w in _FIXED}

# original 必须像个"某一天"的时间词：至少含一个日期单位字，挡住 LLM 把"开题报告"之类
# 标成时间词；"上午""下午"只是时段不是某一天，不含单位字，同样挡住（今早/今晚已归固定词）
_TIME_CHARS = set("天日周期拜月年号末底初")
# 已经是绝对日期的不再替换（"9月13日" → "9月13日" 没意义，还可能改乱）
_ABSOLUTE_DATE = re.compile(r"\d{1,4}\s*[-/年]\s*\d{1,2}|\d{1,2}\s*月\s*\d{1,2}\s*[日号]")

_ISO = re.compile(r"^\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_CN_FULL = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_CN_MONTH_DAY = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")

# resolved 距参照日期的最大跨度（超过一年基本是算错了）
_MAX_SPAN_DAYS = 366


def parse_reference_date(value: str) -> date | None:
    """ClassifyRequest.reference_date（yyyy-MM-dd）→ date；空/非法 → None（不消解）"""
    m = _ISO.match(value or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def format_time_rule(ref: date | None) -> str:
    """prompt 的{time_rule}段落；无参照日期返回空串（整节消失，模板与旧版一致）"""
    if ref is None:
        return ""
    day = f"{ref.isoformat()}（星期{_WEEKDAYS[ref.weekday()]}）"
    return f"""## 相对时间消解（这条记录写于 {day}）

原文里的"今天""明天""下周三""这周末""三天后"这类相对时间词，脱离写日记的这一天就读不懂了。
请在**每条记录**的 JSON 里额外输出 `time_substitutions` 字段（单条模式就是那一个 JSON 对象），
把**能确定到具体某一天**的相对时间词列出来：

- `original`：原文里**逐字出现**的时间词本身，不带上下文（"明天上午去开会"只写"明天"）
- `resolved`：算出来的日期，格式 yyyy-MM-dd，以 {ref.isoformat()} 为"今天"
- 一周从周一算起："这周五""本周五"= {ref.isoformat()} 所在这一周的周五；"下周三"= 下一周的周三
- 只写**具体某一天**；指一段时间的（"下周""这个月""最近""这几天""以后"）不要写
- 不是时间意思的不要写（如"明天会更好"这类套话）
- 没有相对时间词时写空列表：`"time_substitutions": []`
- **其他字段照常输出、不要改写原文**，替换由系统按这个列表自动完成

示例（记录写于 2026-09-12 星期六）：原文"明天开始补文献综述，下周三交初稿"
→ `"time_substitutions": [{{"original": "明天", "resolved": "2026-09-13"}}, {{"original": "下周三", "resolved": "2026-09-16"}}]`

"""


def _parse_resolved(value, ref: date) -> date | None:
    """LLM 给的 resolved → date。认 yyyy-MM-dd / yyyy年M月D日 / M月D日（年份取离参照日期最近的一年）"""
    s = str(value or "").strip()
    if not s:
        return None
    for pattern in (_ISO, _CN_FULL):
        m = pattern.search(s)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
    m = _CN_MONTH_DAY.search(s)
    if m:
        month, day_ = int(m.group(1)), int(m.group(2))
        candidates = []
        for year in (ref.year - 1, ref.year, ref.year + 1):
            try:
                candidates.append(date(year, month, day_))
            except ValueError:
                continue
        return min(candidates, key=lambda d: abs((d - ref).days)) if candidates else None
    return None


def _cn_date(d: date, ref: date) -> str:
    """回写进 segment 的日期文本：同年"9月13日"，跨年带年份（B 侧校验：含数字 + "日"）"""
    return f"{d.month}月{d.day}日" if d.year == ref.year else f"{d.year}年{d.month}月{d.day}日"


def resolve_substitutions(raw, text: str, ref: date | None) -> list[tuple[str, str]]:
    """LLM 输出的 time_substitutions → 校验规整后的 [(original, resolved)]（脏值丢弃，不阻断分类）"""
    if ref is None or not isinstance(raw, list) or not text:
        return []
    picked: list[tuple[str, str]] = []
    seen: set[str] = set()
    for sub in raw:
        if not isinstance(sub, dict):
            continue
        original = str(sub.get("original") or "").strip()
        if not original or len(original) > MAX_ORIGINAL_LEN:
            continue

        fixed = next((w for w in _FIXED_BY_LEN if original.startswith(w)), None)
        if fixed:
            # 防线 1：固定偏移词确定性计算，并把"明天上午"收窄成"明天"
            offset, suffix = _FIXED[fixed]
            original = fixed
            resolved = _cn_date(ref + timedelta(days=offset), ref) + suffix
        else:
            # 防线 2：LLM 算的日期必须站得住
            if not (_TIME_CHARS & set(original)) or _ABSOLUTE_DATE.search(original):
                continue
            d = _parse_resolved(sub.get("resolved"), ref)
            if d is None or abs((d - ref).days) > _MAX_SPAN_DAYS:
                continue
            resolved = _cn_date(d, ref)

        if original not in text or original in seen:
            continue
        seen.add(original)
        picked.append((original, resolved))

    # 先截断再查包含关系：反过来的话"大前天"可能恰好被截掉而"前天"留下，照样改坏
    picked = picked[:MAX_SUBSTITUTIONS]
    # "后天"被"大后天"包含：文本里有"大后天"但替换表里没有它时，替换"后天"会把"大后天"改坏
    chosen = {o for o, _ in picked}
    return [(o, r) for o, r in picked
            if not any(lw in text and lw not in chosen for lw in _CONTAINED_IN.get(o, ()))]
