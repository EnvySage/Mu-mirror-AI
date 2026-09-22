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

固定词**不依赖 LLM 是否列出**（2026-09-22 真机实测补充）：prompt 要求模型在每条记录里输出
time_substitutions，但 mimo-v2.5 实测只有约 1/3 的请求照做——同一句话连跑 6 次，2 次替换成功、
4 次原样落库；抓完整响应发现模型有时把该字段放到 items 外层（与 items 平级），有时甚至不放。
固定偏移词是纯算术（参照日期 ± N 天），本就该由代码算，不该赌模型每次记得说。故 resolve_
substitutions 在遍历 LLM 结果之外，**独立扫描原文补齐固定词**（见 _scan_fixed_terms）。
LLM 只管它真正擅长的推理类词（下周三/三天后/月底）。

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


def format_time_rule(ref: date | None, single: bool = False) -> str:
    """prompt 的{time_rule}段落；无参照日期返回空串（整节消失，模板与旧版一致）

    single=True 用于单段模式：那份 JSON 是平铺的（没有 items 数组），字段直接放顶层——
    文案必须跟着变，否则模型会照抄"每个 item 对象内部"，反而误导。

    渲染位置紧贴「输出格式」之后（2026-09-22）：原先放在 prompt 中部，距 JSON 骨架 70 行，
    真机实测模型频繁漏字段或把它提到 items 外层。格式要求与字段骨架必须挨着。
    """
    if ref is None:
        return ""
    if single:
        where = "在这个 JSON 对象里加 `time_substitutions` 字段"
        example = (f"""```json
{{"title": "补综述", "summary": "2026-09-13 起补文献综述", "...": "其余字段照常",
 "time_substitutions": [{{"original": "明天", "resolved": "2026-09-13"}}]}}
```""")
    else:
        where = "**每个 item 对象内部**加 `time_substitutions` 字段"
        example = (f"""```json
"items": [
  {{"title": "补综述", "summary": "2026-09-13 起补文献综述", "...": "其余字段照常",
   "time_substitutions": [{{"original": "明天", "resolved": "2026-09-13"}}, {{"original": "下周三", "resolved": "2026-09-16"}}]}}
]
```""")
    return f"""## 输出格式补充：相对时间词消解

这段记录写于 **{ref.isoformat()}（星期{_WEEKDAYS[ref.weekday()]}）**。原文里的"明天""下周三"这类相对时间词，脱离这一天就读不懂了。请{where}：

- 只列能**确定到具体某一天**的词；指一段时间的（"下周""这个月""最近""这几天"）不列
- `original` 必须是原文中**逐字出现**的词本身（"明天上午去开会"写 `"明天"`，不写"明天上午"）
- `resolved` 用 `yyyy-MM-dd`，以 {ref.isoformat()} 为"今天"；一周从周一起算
- 没有相对时间词时写 `[]`

同时输出的 `summary`/`keywords` 里的相对时间词也一并换成绝对日期。

示例：写于 2026-09-12，原文"明天开始补文献综述，下周三交初稿"
{example}

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


def _scan_fixed_terms(text: str) -> list[str]:
    """独立扫描原文，按长词优先取出所有固定偏移词（"大后天"不额外产出"后天"）

    贪心 + 消费已匹配跨度：扫到"大后天"就跳过 3 个字符，不会再从中间匹配出"后天"。
    这是"不依赖 LLM 是否列出固定词"的兜底——纯算术，代码自己算。
    """
    found: list[str] = []
    i, n = 0, len(text)
    while i < n:
        for w in _FIXED_BY_LEN:
            if text.startswith(w, i):
                found.append(w)
                i += len(w)
                break
        else:
            i += 1
    return found


def resolve_substitutions(raw, text: str, ref: date | None) -> list[tuple[str, str]]:
    """LLM 输出的 time_substitutions → 校验规整后的 [(original, resolved)]（脏值丢弃，不阻断分类）

    raw 允许是 None / 非列表 / 空列表——固定词兜底扫描独立于它，照样产出。
    """
    if ref is None or not text:
        return []

    picked: list[tuple[str, str]] = []
    seen: set[str] = set()

    # ① 固定词兜底：代码自己扫原文，不看 LLM 脸色（真机实测 LLM 只有约 1/3 的请求会列出替换表）
    for word in _scan_fixed_terms(text):
        if word in seen:
            continue
        offset, suffix = _FIXED[word]
        seen.add(word)
        picked.append((word, _cn_date(ref + timedelta(days=offset), ref) + suffix))

    # ② LLM 结果：固定词用确定性日期覆盖它的 resolved；其余走防线 2 校验
    for sub in (raw if isinstance(raw, list) else []):
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
            if original in seen:      # ①已扫到，跳过（值相同）
                continue
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
