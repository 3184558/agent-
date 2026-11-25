"""有限的中文提醒规则。来源和已知局限见 docs/safety.md。"""

import re

from .schemas import Analysis


# 有限的中文表述匹配规则，不是完整分诊系统；未匹配不能解释为没有风险。
RULES = (
    r"(?:剧烈|严重|压榨性)(?:的)?胸(?:痛|疼)|胸(?:口)?(?:剧烈疼痛|像被压住)",
    r"(?:严重|剧烈)(?:的)?呼吸困难|喘不过气|无法呼吸|嘴唇发紫|叫不醒|失去意识",
    r"(?:突然|突发).{0,12}(?:一侧|半边|左侧|右侧).{0,6}(?:无力|麻木)|口角歪斜|突然说不清话",
)
NOTICE = "如果这些症状正在发生或刚刚出现，请立即联系当地急救服务或前往急诊，不要等待本工具分析，也不要自行驾车。此提示不能确认具体疾病。"


def check_safety(text: str) -> Analysis | None:
    """对实际输入检查既有即时提醒规则；命中返回统一分析对象，否则返回 None。

    仅识别规则覆盖的文字与紧邻否定，不调用模型，也不保证理解所有语境。
    """
    for clause in re.split(r"[，,。！？!?；;\n]|但是|(?<!喘)不过|可是|但", text):
        for pattern in RULES:
            for match in re.finditer(pattern, clause):
                # 只处理紧邻症状的常见否定，不把整句里的任意“没有”当否定。
                if re.search(
                    r"(?:没有|并无|无|否认|未出现|不是|不伴)(?:出现|发生|明显|任何|了|过|的|\s)*$",
                    clause[: match.start()],
                ):
                    continue
                return Analysis(
                    status="urgent_attention",
                    summary="描述中出现了需要优先关注的症状表述。",
                    facts=[],
                    directions=[],
                    missing_information=[],
                    safety_notice=NOTICE,
                )
    return None
