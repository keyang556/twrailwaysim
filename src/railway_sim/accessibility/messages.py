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
    "DOOR_SIDE_NAMES",
    "REVERSER_NAMES",
    "SIGNAL_ASPECT_NAMES",
    "STOP_KIND_NAMES",
    "approaching_platform_pass",
    "approaching_speed_limit",
    "approaching_stop_point",
    "ato_awaiting_departure",
    "ato_departed",
    "ato_disengaged",
    "ato_engaged",
    "ato_not_engaged",
    "ato_unavailable",
    "brake_notch",
    "broadcast_arriving",
    "broadcast_destination",
    "broadcast_do_not_board",
    "broadcast_door_side",
    "broadcast_doors",
    "broadcast_next_stop",
    "broadcast_notice",
    "broadcast_terminus",
    "broadcast_unscheduled_stop",
    "distance_phrase",
    "door_blocked_by_movement",
    "door_closed",
    "door_opened",
    "door_status",
    "emergency_brake_applied",
    "emergency_brake_released",
    "horn",
    "missed_stop",
    "next_station",
    "no_stop_point_ahead",
    "num_to_zh",
    "overspeed",
    "position_report",
    "power_blocked_by_doors",
    "power_notch",
    "quantise_stop_distance",
    "reverser_at_end",
    "reverser_blocked_by_movement",
    "reverser_position",
    "signal_report",
    "speed_report",
    "station_arrival",
    "station_passed",
    "station_phrase",
    "station_realigned",
    "stop_accuracy_grade",
    "stop_countdown",
    "stop_countdown_start",
    "stop_distance_phrase",
    "stop_point_reached",
    "stop_point_report",
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


def station_phrase(name: str) -> str:
    """把站名接上「站」字，站名本身已含「站」時不重複。

    捷運有台北車站、鶯歌車站這種**站名本身就含「站」**的車站，直接接上去會
    唸成「台北車站站」。臺鐵沒有這種站名，因此以前不需要處理。
    """
    return name if name.endswith("站") else f"{name}站"


def distance_phrase(metres: float) -> str:
    """依距離長短選擇公尺或公里的唸法（§21.2）。

    一公里以上使用「一點二公里」，一公里以內取整十公尺使用「八百公尺」。
    """
    metres = max(0.0, float(metres))
    if metres >= 1000.0:
        return f"{num_to_zh(metres / 1000.0, decimals=1)}公里"
    rounded = int(round(metres / 10.0) * 10)
    return f"{num_to_zh(rounded)}公尺"


def stop_distance_phrase(metres: float) -> str:
    """對準停車位置專用的距離唸法。

    與 :func:`distance_phrase` 分開的理由是**解析度**：一般距離取整十公尺
    就夠了（「距離八百公尺」），但對準停車位置時十公尺的誤差是天差地遠，
    而在最後幾公尺內，一公尺的解析度同樣不夠——「一公尺」與「停在位置上」
    對司機是兩件事。因此距離愈近，報得愈細：

    - 一百公尺以上：沿用一般唸法（整十公尺或公里）。
    - 十到一百公尺：逐公尺。
    - 十公尺以內：半公尺。
    """
    metres = max(0.0, float(metres))
    if metres >= 100.0:
        return distance_phrase(metres)
    if metres >= 10.0:
        return f"{num_to_zh(metres)}公尺"
    return f"{num_to_zh(quantise_stop_distance(metres), decimals=1)}公尺"


def quantise_stop_distance(metres: float) -> float:
    """把距離量化到 :func:`stop_distance_phrase` 實際唸出來的值。

    評價與播報必須用同一個數字，否則會出現「超出停車位置零點五公尺，良好」
    這種數字與評語對不上的情形——實際誤差是 0.51 公尺，唸出來卻是 0.5。
    """
    metres = float(metres)
    if abs(metres) >= 10.0:
        return round(metres)
    return round(metres * 2.0) / 2.0


DIRECTION_NAMES: dict[str, str] = {
    "southbound": "南下",
    "northbound": "北上",
}

#: 方向把手位置的名稱。鍵是 :mod:`railway_sim.simulation.train` 的
#: ``REVERSER_*`` 常數值（與 OpenBVE 的 ``ReverserPosition`` 相同）。
REVERSER_NAMES: dict[int, str] = {
    1: "前進",
    0: "切",
    -1: "後退",
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

DOOR_SIDE_NAMES: dict[str, str] = {
    "left": "左側",
    "right": "右側",
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


# ----------------------------------------------------------------------
# 方向把手（鍵位取自 OpenBVE 的 REVERSER_FORWARD／REVERSER_BACKWARD）
# ----------------------------------------------------------------------
def reverser_position(position: int) -> str:
    """方向把手移動後的回饋。

    看不見手把的司機只有這一句能確認把手在哪一格，因此「切」也要說出來，
    不能只在前進與後退時出聲。
    """
    return f"方向把手，{REVERSER_NAMES.get(position, str(position))}。"


def reverser_at_end(position: int) -> str:
    """已經在端點，再按沒有東西可動。按鍵一定要有回饋（§7.2）。"""
    return f"方向把手已在{REVERSER_NAMES.get(position, str(position))}位。"


def reverser_blocked_by_movement() -> str:
    """行進中不得改變方向把手。"""
    return "列車行進中，停妥後才可改變方向把手。"


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
# 車門（§16.1、§20.2；鍵位取自 OpenBVE 的 DOORS_LEFT／DOORS_RIGHT）
# ----------------------------------------------------------------------
def _side_name(side: str) -> str:
    return DOOR_SIDE_NAMES.get(side, side)


def door_opened(side: str) -> str:
    return f"{_side_name(side)}車門開啟。"


def door_closed(side: str) -> str:
    return f"{_side_name(side)}車門關閉。"


def door_blocked_by_movement(side: str) -> str:
    """行進中不得開門（§9.3：通過站不開門，延伸為列車未停妥不得開門）。"""
    return f"列車尚未停妥，無法開啟{_side_name(side)}車門。"


def power_blocked_by_doors() -> str:
    """車門未關妥不得起動（§16.2 出發流程：關門確認在出發之前）。"""
    return "車門開啟中，無法加電門。請先關閉車門。"


def door_status(left_open: bool, right_open: bool) -> str:
    """車門狀態總覽。"""
    if not left_open and not right_open:
        return "車門：全部關閉。"
    sides = [
        name
        for name, is_open in ((DOOR_SIDE_NAMES["left"], left_open),
                              (DOOR_SIDE_NAMES["right"], right_open))
        if is_open
    ]
    return f"車門：{'、'.join(sides)}開啟中。"


# ----------------------------------------------------------------------
# 車上廣播（§20.2）
# ----------------------------------------------------------------------
# 以下是廣播內容的**摘要**，不是逐字稿：實際音檔含國語、臺語、客語與英語，
# 逐字稿無法由檔名得知，寫成摘要才不會虛構內容（§2.3）。
def broadcast_next_stop(name: str) -> str:
    return f"車內廣播：下一站，{name}。"


def broadcast_arriving(name: str) -> str:
    return f"車內廣播：{station_phrase(name)}快到了。"


def broadcast_terminus(name: str) -> str:
    return f"車內廣播：終點站{name}快到了。"


def broadcast_doors(side: str, *, opening: bool) -> str:
    action = "開啟" if opening else "關閉"
    return f"車內廣播：{_side_name(side)}車門即將{action}。"


def broadcast_door_side(side: str) -> str:
    """接在到站廣播之後，提醒旅客往哪一側下車。"""
    return f"車內廣播：{_side_name(side)}開門。"


def broadcast_do_not_board() -> str:
    """全車對號列車在開門中持續播放的提醒。

    對象是月台上**沒有買這班列車車票**的旅客，不是車上的人。
    """
    return "車內廣播：本列車為對號列車，未持本車車票之旅客請勿上車。"


def broadcast_unscheduled_stop() -> str:
    """臨時停車（不在月台的地方停下來）的廣播。

    停車原因不會出現在音檔檔名裡，因此文字只說明「這是臨時停車」而不編造
    理由（§2.3）。
    """
    return "車內廣播：本列車臨時停車，請旅客稍候。"


def broadcast_destination(name: str) -> str:
    """往○○的廣播（捷運）。內容是這班車開往哪裡，不是下一站。"""
    return f"車內廣播：本列車開往{name}。"


def broadcast_notice() -> str:
    """宣導廣播（捷運）。

    內容是搭乘須知一類的宣導，不是任何一站的資訊，因此文字只說「宣導廣播」
    而不編造內容——音檔逐字稿無法由檔名得知（§2.3）。
    """
    return "車內廣播：宣導廣播。"


# ----------------------------------------------------------------------
# 自動駕駛（ATO，僅捷運）
# ----------------------------------------------------------------------
def ato_engaged(*, driverless: bool) -> str:
    if driverless:
        return "自動駕駛已啟動。本線為無人駕駛，車門與發車全部由電腦控制。"
    return "自動駕駛已啟動。請負責開關車門，關門後按發車鍵啟動列車。"


def ato_disengaged() -> str:
    return "自動駕駛已解除，改為手動駕駛。"


def ato_unavailable() -> str:
    return "本系統沒有自動駕駛功能。"


def ato_not_engaged() -> str:
    return "自動駕駛未啟動，發車鍵無作用。"


def ato_awaiting_departure(name: str) -> str:
    return f"{station_phrase(name)}停妥，車門作業完成後請按發車鍵。"


def ato_departed() -> str:
    return "已發車，自動駕駛接管。"


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
    return f"前方車站{name}，距離{distance_phrase(distance_m)}。{station_phrase(name)}為{kind}。"


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


def no_station_ahead() -> str:
    return "前方無車站，已至路線終點。"


def service_report(
    train_number: str, class_name: str, stock_name: str, route_name: str
) -> str:
    """車次資訊播報。"""
    return f"{class_name}{train_number}次，{stock_name}，{route_name}。"


def run_summary(elapsed_text: str, violations: int) -> str:
    """運轉摘要播報。"""
    if violations == 0:
        return f"運轉時間{elapsed_text}，無行車違規。"
    return f"運轉時間{elapsed_text}，行車違規{num_to_zh(violations)}件。"


def train_status(
    train_number: str,
    train_type: str,
    speed_kmh: float,
    power: int,
    brake: int,
    emergency: bool,
    direction: str,
    reverser: int = 1,
) -> str:
    """列車狀態總覽播報。

    方向把手也在這一句裡：看不見手把的司機沒有別的辦法確認它在哪一格，而
    「加了電門卻不走」多半就是把手在「切」。
    """
    direction_zh = DIRECTION_NAMES.get(direction, direction)
    parts = [
        f"{train_type}{train_number}次，{direction_zh}。",
        f"方向把手{REVERSER_NAMES.get(reverser, str(reverser))}。",
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


def _offset_phrase(name: str, offset_m: float) -> str:
    """停車位置誤差的共用說法（到站與前進修正都用它）。

    誤差以 :func:`stop_distance_phrase` 播報，不套用 :func:`distance_phrase`
    的整十公尺化簡；否則四公尺的誤差會被唸成「零公尺」，對司機沒有意義。
    """
    # 先量化到播報用的解析度再評價，數字與評語才會一致。
    offset_m = quantise_stop_distance(offset_m)
    grade = stop_accuracy_grade(offset_m)
    if abs(offset_m) <= STOP_ACCURACY_GRADES[0][0]:
        return f"停車位置{grade}。"
    metres = stop_distance_phrase(abs(offset_m))
    if offset_m > 0:
        return f"超出停車位置{metres}，{grade}。"
    return f"未達停車位置{metres}，{grade}。可再前進{metres}。"


def station_arrival(name: str, offset_m: float) -> str:
    """到站停妥播報，含停車位置誤差與評價。

    未達停車位置時一併說出「可再前進多少」：這是司機唯一還能補救的方向，
    而看不見月台標記的人沒辦法自己估。超出時不說，因為不可倒車（§9.2）。
    """
    return f"{station_phrase(name)}停妥，{_offset_phrase(name, offset_m)}"


def stop_countdown_start(name: str, distance_m: float) -> str:
    """進入停車位置倒數的第一句：說清楚接下來報的是什麼。

    後續每一句只報距離（見 :func:`stop_countdown`），因此開頭這一句必須把
    「這是在報距離停車位置多遠」講明白，否則之後的「三十公尺」會與號誌、
    速限的距離播報混在一起分不出來。
    """
    return f"距離{station_phrase(name)}停車位置{stop_distance_phrase(distance_m)}。"


def stop_countdown(distance_m: float) -> str:
    """停車位置倒數的後續每一句。

    刻意只有距離，沒有任何前綴：最後幾公尺內每一句之間只隔一兩秒，多一個
    字就可能來不及唸完下一句就被蓋掉。前後文由 :func:`stop_countdown_start`
    建立。
    """
    return f"{stop_distance_phrase(distance_m)}。"


def stop_point_reached() -> str:
    """車頭到達停車位置的瞬間。

    這是對準停車位置最關鍵的一句：看不見月台標記的司機需要一個明確的
    「就是現在」，而不是自己從遞減的數字推算。
    """
    return "停車位置。"


def stop_point_report(name: str, distance_m: float) -> str:
    """查詢距離停車位置多遠（狀態查詢項目）。

    距離為負代表車頭已經越過停車位置。兩種情形要用不同的說法：司機聽到
    「距離三公尺」會繼續前進，聽到「已超出三公尺」才知道停過頭了。
    """
    if distance_m > 0:
        return f"距離{station_phrase(name)}停車位置{stop_distance_phrase(distance_m)}。"
    if distance_m < 0:
        return f"已超出{station_phrase(name)}停車位置{stop_distance_phrase(-distance_m)}。"
    return f"{station_phrase(name)}停車位置。"


def no_stop_point_ahead() -> str:
    return "前方沒有停靠站。"


#: 停車位置誤差的評價門檻（公尺）與說法。
#:
#: 分級的用途是讓司機**不必自己換算**：聽到「準確」就知道不用再動，聽到
#: 「偏差過大」才需要考慮前進修正。門檻為第一版的值，與 STOP_WINDOW_M
#: 同樣沒有可靠的公開來源（§27）。
STOP_ACCURACY_GRADES: tuple[tuple[float, str], ...] = (
    (0.5, "準確"),
    (2.0, "良好"),
    (5.0, "可接受"),
)


def stop_accuracy_grade(offset_m: float) -> str:
    """停車位置誤差的評價。"""
    for limit, grade in STOP_ACCURACY_GRADES:
        if abs(offset_m) <= limit:
            return grade
    return "偏差過大"


def station_realigned(name: str, offset_m: float) -> str:
    """停妥之後前進修正停車位置的回饋。

    未達停車位置時列車還可以往前推一點，這在真實運轉上也是允許的；但看不見
    月台標記的司機需要每動一次就知道現在差多少，否則修正等於盲猜。
    """
    return f"修正後：{_offset_phrase(name, offset_m)}"


def station_passed(name: str) -> str:
    return f"通過{station_phrase(name)}。"


def missed_stop(name: str) -> str:
    """應停未停（§9.2）。"""
    return f"應停未停：{station_phrase(name)}。已記錄行車違規，不可倒車。"


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


def approaching_platform_pass(label: str, limit_kmh: float, distance_m: float) -> str:
    """接近不停靠車站的月台（通過速限）。

    與 :func:`approaching_speed_limit` 分開：一般速限沿線都有標，司機聽到
    「前方速限」會去對照號誌牌；通過月台的速限是**這一班車不停這一站**才
    有的規定，說出站名司機才知道理由，也才能同時確認自己沒有記錯停靠站。
    """
    return (
        f"前方{label}，本站通過，"
        f"距離{distance_phrase(distance_m)}，通過速限{num_to_zh(limit_kmh)}公里。"
    )


def signal_passed_at_danger(signal_name: str) -> str:
    """冒進號誌（§14.2）。"""
    return f"冒進號誌：{signal_name}。緊急制軔動作。"


def train_stopped() -> str:
    return "列車停妥。"


def service_completed(train_number: str, violations: int) -> str:
    if violations == 0:
        return f"{train_number}次運轉結束，無行車違規。"
    return f"{train_number}次運轉結束，行車違規{num_to_zh(violations)}件。"
