"""标的代码规范化：与 Tickflow/ts_code 后缀约定一致（.SH /.SZ /.BJ）。"""


def normalize_tickflow_symbol(code: str) -> str:
    """
    将用户输入转为 ``代码.后缀`` 格式（Tushare ``ts_code`` 与 Tickflow 标的串一致）。

    - 已含 ``.``：规范大小写后缀后返回（如 ``600000.sh`` -> ``600000.SH``）。
    - 6 位数字：按常见 A 股 / ETF 规则推断 ``.SH`` / ``.SZ`` / ``.BJ``。
    - 无法推断时抛出 ``ValueError``，请传入完整 ``代码.后缀``。
    """
    s = str(code).strip().upper()
    if not s:
        raise ValueError("标的代码为空")

    if "." in s:
        sym, suffix = s.split(".", 1)
        sym = sym.strip()
        suffix = suffix.strip().upper()
        if not sym or not suffix:
            raise ValueError(f"无效的标的代码格式: {code!r}，请使用 代码.后缀")
        return f"{sym}.{suffix}"

    if not s.isdigit():
        raise ValueError(
            f"无法解析标的代码: {code!r}，请使用 6 位数字或完整 代码.后缀 形式"
        )

    if len(s) != 6:
        raise ValueError(
            f"非 6 位数字代码请使用完整形式 代码.后缀: {code!r}"
        )

    if s.startswith(("83", "87", "43")) or s.startswith("920"):
        return f"{s}.BJ"

    if s.startswith(("5", "6")):
        return f"{s}.SH"

    if s.startswith(("0", "1", "2", "3")):
        return f"{s}.SZ"

    raise ValueError(
        f"无法为代码 {code!r} 推断市场后缀，请使用完整形式，例如 689009.SH"
    )
