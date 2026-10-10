"""模型输入仅做局部脱敏，不分类意图、不重写整句话。

用户原文单独保存用于展示；模型仍能理解问候、追问和自然语言查询。
脱敏是防止明显敏感内容外发的辅助措施，权限始终由凭证与工具执行层控制。
真实资产结果仍不进入模型，局部匹配也不宣称能识别所有隐晦的敏感表达。
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SafeInput:
    text: str
    clarification: str | None = None


# 只匹配明确的地址和密钥形态，不能把代币名中的数字或网络 ID 全部抹掉。
ADDRESS = re.compile(r"0x[a-fA-F0-9]{40}(?![a-fA-F0-9])")
PRIVATE_KEY = re.compile(r"(?<![a-fA-F0-9])(?:0x)?[a-fA-F0-9]{64}(?![a-fA-F0-9])")
NUMBER = r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
CHINESE_AMOUNT = r"[零一二三四五六七八九十百千万亿两点]+"
# “我有两个问题”等普通说法不当作持仓金额；只有明确余额标签或资产单位才遮蔽。
HOLDING_AMOUNT = re.compile(
    rf"(?P<label>持仓数量(?:是|为)?|余额(?:是|为)?)"
    rf"(?P<space>\s*)(?P<amount>{NUMBER})(?![\d.])"
)
OWNED_AMOUNT = re.compile(
    rf"(?P<label>我有|持有|页面显示)(?P<space>\s*)(?P<amount>{NUMBER}|{CHINESE_AMOUNT})"
    rf"(?=\s*(?:个|枚)?\s*(?:[A-Z][A-Z0-9._-]{{1,31}}|代币|美元|人民币|元)(?![A-Za-z0-9]))"
)
CURRENCY_AMOUNT = re.compile(rf"(?:[$¥￥]\s*{NUMBER}|(?:{NUMBER}|{CHINESE_AMOUNT})\s*(?:美元|人民币|元))")


def normalize_query(raw: str, public_contracts: tuple[str, ...] = ()) -> SafeInput:
    """保留原始语义，只替换明确的地址、密钥与持仓金额。

    Args:
        raw: 用户原文，不改变展示层保存的内容。
        public_contracts: 已经基础服务验证的公开代币合约；仅它们可以保留。

    Returns:
        SafeInput: 保留措辞和语序的模型输入，不生成固定意图模板或问候拦截。
    """
    allowed = {address.lower() for address in public_contracts}
    text = PRIVATE_KEY.sub("[密钥已隐藏]", raw)
    text = ADDRESS.sub(lambda m: m.group(0) if m.group(0).lower() in allowed else "[地址已隐藏]", text)
    text = HOLDING_AMOUNT.sub(lambda m: m["label"] + m["space"] + "[金额已隐藏]", text)
    text = OWNED_AMOUNT.sub(lambda m: m["label"] + m["space"] + "[金额已隐藏]", text)
    text = CURRENCY_AMOUNT.sub("[金额已隐藏]", text)
    return SafeInput(text)
