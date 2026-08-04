"""所有玩家可見／可聽的訊息文字（規格 §21.2）。

把訊息集中在此模組的理由：

1. 可被自動測試驗證（規格 §23.1 最後一項）。
2. 保證任何狀態變化都有對應的文字，不會只存在於視覺介面（§2.1、§25.5）。
3. 日後要調整用詞或加入其他語言時只需修改一處。

數字一律轉為中文數字，與規格 §21.2 的範例一致
（例如「目前速度八十二公里」、「距離一點二公里」）。
"""

from __future__ import annotations

__all__ = [
    "DIRECTION_NAMES",
    "SIGNAL_ASPECT_NAMES",
    "STOP_KIND_NAMES",
    "approaching_speed_limit",
    "approaching_stop_point",
    "brake_notch",
    "distance_phrase",
    "emergency_brake_applied",
    "emergency_brake_released",
    "horn",
    "missed_stop",
    "next_station",
    "num_to_zh",
    "overspeed",
    "position_report",
    "power_notch",
    "signal_report",
    "speed_report",
    "station_arrival",
    "station_passed",
    "train_status",
]

_DIGITS = "零一二三四五六七八九"
_SMALL_UNITS = ("", "十", "百", "千")
_BIG_UNITS = ((100_000_000, "億"), (10_000, "萬"))


def _int_below_10000(value: int) -> str:
    if value == 0:
        return _DIGITS[0]
    digits = [int(c) for c in str(value)]
    length = len(digits)
    out: list[str] = []
    zero_pending = False
    for index, digit in enumerate(digits):
        unit = _SMALL_UNITS[length - 1 - index]
        if digit == 0:
            zero_pending = True
            continue
        if zero_pending and out:
            out.append(_DIGITS[0])
        zero_pending = False
        # 「十五」而非「一十五」，但「一百一十五」保留前導的「一」。
        if digit == 1 and unit == "十" and not out:
            out.append(unit)
        else:
            out.append(_DIGITS[digit] + unit)
    return "".join(out)


def _int_to_zh(value: int) -> str:
    if value < 0:
        return "負" + _int_to_zh(-value)
    for scale, name in _BIG_UNITS:
        if value >= scale:
            head = _int_to_zh(value // scale)
            rest = value % scale
            if rest == 0:
                return head + name
            # 例如 10_005 -> 一萬零五
            if rest < scale // 10:
                return head + name + _DIGITS[0] + _int_to_zh(rest)
            return head + name + _int_to_zh(rest)
    return _int_below_10000(value)


def num_to_zh(value: float, *, decimals: int = 0) -> str:
    """把數字轉為中文唸法。

    Args:
        value: 要轉換的數值。
        decimals: 小數位數。``0`` 表示四捨五入為整數。

    Returns:
        中文數字字串，例如 ``82`` -> ``"八十二"``、``1.2`` -> ``"一點二"``。
    """
    if decimals <= 0:
        return _int_to_zh(round(value))

    quantised = round(float(value), decimals)
    negative = quantised < 0
    quantised = abs(quantised)
    whole = int(quantised)
    frac_text = f"{quantised:.{decimals}f}".split(".")[1].rstrip("0")
    head = _int_to_zh(whole)
    if not frac_text:
        return ("負" if negative else "") + head
    tail = "".join(_DIGITS[int(c)] for c in frac_text)
    return ("負" if negative else "") + head + "點" + tail


def distance_phrase(metres: float) -> str:
    """依距離長短選擇公尺或公里的唸法（§21.2）。

    一公里以上使用「一點二公里」，一公里以內取整十公尺使用「八百公尺」。
    """
    metres = max(0.0, float(metres))
    if metres >= 1000.0:
        return f"{num_to_zh(metres / 1000.0, decimals=1)}公里"
    rounded = int(round(metres / 10.0) * 10)
    return f"{num_to_zh(rounded)}公尺"


DIRECTION_NAMES: dict[str, str] = {
    "southbound": "南下",
    "northbound": "北上",
}

SIGNAL_ASPECT_NAMES: dict[str, str] = {
    "stop": "停止",
    "caution": "注意",
    "clear": "平常",
}

STOP_KIND_NAMES: dict[str, str] = {
    "stop": "停靠站",
    "pass": "通過站",
    "conditional": "依班次停靠",
}


# ----------------------------------------------------------------------
# 操作回饋（§7.2：每次按鍵都應提供文字回饋）
# ----------------------------------------------------------------------
def power_notch(notch: int) -> str:
    """電門段位回饋。"""
    if notch <= 0:
        return "電門切斷。"
    return f"電門{num_to_zh(notch)}段。"


def brake_notch(notch: int) -> str:
    """制軔段位回饋。"""
    if notch <= 0:
        return "制軔緩解。"
    return f"制軔{num_to_zh(notch)}段。"


def coasting() -> str:
    return "惰行。"


def horn() -> str:
    return "鳴笛。"


def emergency_brake_applied() -> str:
    return "緊急制軔。"


def emergency_brake_blocked() -> str:
    """列車尚未停妥就想解除緊急制軔（§8.3）。"""
    return "緊急制軔中，列車停妥後才可解除。"


def emergency_brake_released() -> str:
    return "緊急制軔解除，制軔緩解。"


def power_blocked_by_emergency() -> str:
    return "緊急制軔中，無法加電門。"


# ----------------------------------------------------------------------
# 狀態查詢
# ----------------------------------------------------------------------
def speed_report(speed_kmh: float, permitted_kmh: float | None = None) -> str:
    """目前速度播報。"""
    text = f"目前速度{num_to_zh(speed_kmh)}公里。"
    if permitted_kmh is not None:
        text += f"允許速度{num_to_zh(permitted_kmh)}公里。"
    return text


def position_report(
    line_name: str,
    from_station: str,
    to_station: str,
    distance_to_next_m: float,
) -> str:
    """目前位置播報。"""
    return (
        f"{line_name}，{from_station}至{to_station}間，"
        f"距離{to_station}{distance_phrase(distance_to_next_m)}。"
    )


def next_station(name: str, distance_m: float, stop_kind: str) -> str:
    """前方車站播報（§21.2）。"""
    kind = STOP_KIND_NAMES.get(stop_kind, stop_kind)
    return f"前方車站{name}，距離{distance_phrase(distance_m)}。{name}站為{kind}。"


def signal_report(aspect: str, distance_m: float, permitted_kmh: float) -> str:
    """前方號誌播報（§11.3）。"""
    name = SIGNAL_ASPECT_NAMES.get(aspect, aspect)
    return (
        f"前方號誌：{name}。"
        f"距離：{distance_phrase(distance_m)}。"
        f"目前允許速度：{num_to_zh(permitted_kmh)}公里。"
    )


def no_signal_ahead() -> str:
    return "前方無號誌。"


def train_status(
    train_number: str,
    train_type: str,
    speed_kmh: float,
    power: int,
    brake: int,
    emergency: bool,
    direction: str,
) -> str:
    """列車狀態總覽播報。"""
    direction_zh = DIRECTION_NAMES.get(direction, direction)
    parts = [
        f"{train_type}{train_number}次，{direction_zh}。",
        f"速度{num_to_zh(speed_kmh)}公里。",
    ]
    if emergency:
        parts.append("緊急制軔中。")
    else:
        parts.append(power_notch(power) if power > 0 else brake_notch(brake))
    return "".join(parts)


# ----------------------------------------------------------------------
# 行車事件
# ----------------------------------------------------------------------
def station_approaching(name: str, distance_m: float) -> str:
    return f"接近{name}，距離{distance_phrase(distance_m)}，準備停車。"


def station_arrival(name: str, offset_m: float) -> str:
    """到站停妥播報，含停車位置誤差。

    停車位置誤差以**公尺為單位精確播報**，不套用 :func:`distance_phrase`
    的整十公尺化簡；否則四公尺的誤差會被唸成「零公尺」，對司機沒有意義。
    """
    if abs(offset_m) < 1.0:
        return f"{name}站停妥，停車位置準確。"
    metres = f"{num_to_zh(abs(offset_m))}公尺"
    if offset_m > 0:
        return f"{name}站停妥，超出停車位置{metres}。"
    return f"{name}站停妥，未達停車位置{metres}。"


def station_passed(name: str) -> str:
    return f"通過{name}站。"


def missed_stop(name: str) -> str:
    """應停未停（§9.2）。"""
    return f"應停未停：{name}站。已記錄行車違規，不可倒車。"


def overspeed(speed_kmh: float, permitted_kmh: float) -> str:
    """超速警告（§21.2「超速五公里。」）。"""
    excess = max(0.0, speed_kmh - permitted_kmh)
    return f"超速{num_to_zh(excess)}公里。"


def overspeed_cleared() -> str:
    return "速度已回到允許範圍。"


def approaching_speed_limit(limit_kmh: float, distance_m: float) -> str:
    return f"前方速限{num_to_zh(limit_kmh)}公里，距離{distance_phrase(distance_m)}，請減速。"


def approaching_stop_point(label: str, distance_m: float) -> str:
    """接近必須停車的地點（停車站或停止號誌）。

    與 :func:`approaching_speed_limit` 分開的理由：停車點的允許速度是零，
    若沿用速限的說法會播成「前方速限零公里」，聽起來像速限資料有誤。
    """
    return f"前方{label}，距離{distance_phrase(distance_m)}，請減速準備停車。"


def signal_passed_at_danger(signal_name: str) -> str:
    """冒進號誌（§14.2）。"""
    return f"冒進號誌：{signal_name}。緊急制軔動作。"


def train_stopped() -> str:
    return "列車停妥。"


def service_completed(train_number: str, violations: int) -> str:
    if violations == 0:
        return f"{train_number}次運轉結束，無行車違規。"
    return f"{train_number}次運轉結束，行車違規{num_to_zh(violations)}件。"
