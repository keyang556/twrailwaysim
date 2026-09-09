"""區間車與區間快的車輛共通運用（規格 §22、§27）。

為什麼車輛型式不能寫死在時刻表裡
--------------------------------

臺鐵的通勤電聯車是**共通運用**：EMU500、EMU600、EMU700、EMU800、EMU900 五型
在同一段路線上混跑，今天跑某一個車次的是哪一型，隔天不一定一樣。時刻表也
沒有印車輛型式，因此「2712 次一定是 EMU800」這種寫法本身就是錯的。

所以匯入時填進 ``timetables.json`` 的車輛型式在這裡只當作**「這一班屬於共通
運用池」的標記**，真正開哪一型由本模組逐日抽籤決定。規則（哪些路段有哪些
車型、各佔多少）全部放在 ``trains.json`` 的 ``rolling_stock_pools``，程式不寫死
任何車型代碼與路線代碼。

為什麼抽籤要「同一天固定」
--------------------------

抽中的型式會影響最高速度、加減速度、編組長度與開關門聲，玩家在車次選單裡看到
的必須就是他真的會開到的那一台。因此抽籤不用亂數產生器，而是以「日期＋車次」
做雜湊：同一天之內、同一個車次，不管問幾次、由哪一個介面問，答案都一樣；換一天
才會重抽。這也正是使用者要的「今天這班和明天可能不同車型」。

「哪一天」則由 :func:`pinned_service_day` 在本次執行第一次抽籤時決定，之後不
再看時鐘——否則玩家在午夜前打開車次選單、午夜後才按下開始，兩次抽籤會落在
不同的日期，開到的車與選單所寫的不一樣。

用 :mod:`hashlib` 而不是 :func:`hash`：Python 的字串雜湊每次啟動都會加鹽，
同一天重開程式就會換一台車。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

__all__ = [
    "RollingStockPools",
    "RollingStockRule",
    "pinned_service_day",
    "service_day",
]

#: 本次執行採用的營運日，由 :func:`pinned_service_day` 在第一次抽籤時填入。
_pinned_day: date | None = None


def service_day() -> date:
    """抽籤用的「今天」。獨立成一個函式，測試才能換掉它。"""
    return date.today()


def pinned_service_day() -> date:
    """本次執行從頭到尾採用的營運日。

    車次選單、行前提要與駕駛畫面各自都會問一次「今天派哪一型」。若每次都重新
    看時鐘，跨過午夜的那一刻答案就會變：玩家在午夜前挑好的車次，午夜後啟動時
    會換成另一型，長度、性能與開關門聲全都和選單上寫的對不上。因此第一次問的
    時候就把日期定下來，本次執行沿用同一個答案，重新啟動遊戲才換成新的一天。
    """
    global _pinned_day
    if _pinned_day is None:
        _pinned_day = service_day()
    return _pinned_day


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if str(item))


@dataclass(frozen=True)
class RollingStockRule:
    """一條運用規則。

    Attributes:
        service_class_ids: 適用的車種。空集合代表不限車種。
        line_ids: 適用的線別。空集合代表不限線別；有值時的意思是「本班次的
            行駛路徑**經過**其中任何一條線」，不是「本班次屬於哪一條線」——
            新竹至六家的區間車同時經過縱貫線北段與六家線，六家線的限制仍然
            適用，因此限定車型的規則必須排在前面。
        rolling_stock_ids: 這條規則允許出現的車輛型式。
        multipliers: 各型的配屬倍率，沒有列到的型式一律為 1。
    """

    id: str
    service_class_ids: frozenset[str] = frozenset()
    line_ids: frozenset[str] = frozenset()
    rolling_stock_ids: tuple[str, ...] = ()
    multipliers: Mapping[str, float] = field(default_factory=dict)
    name_zh_tw: str = ""

    def matches(self, service_class_id: str, line_ids: Iterable[str]) -> bool:
        """本規則是否適用於某一個班次。"""
        if self.service_class_ids and service_class_id not in self.service_class_ids:
            return False
        if self.line_ids and not self.line_ids.intersection(line_ids):
            return False
        return True

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RollingStockRule:
        multipliers: dict[str, float] = {}
        for key, value in (raw.get("multipliers") or {}).items():
            try:
                multipliers[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return cls(
            id=str(raw.get("id", "")),
            service_class_ids=frozenset(_as_str_tuple(raw.get("service_class_ids"))),
            line_ids=frozenset(_as_str_tuple(raw.get("line_ids"))),
            rolling_stock_ids=_as_str_tuple(raw.get("rolling_stock_ids")),
            multipliers=multipliers,
            name_zh_tw=str(raw.get("name_zh_tw", "")),
        )


@dataclass(frozen=True)
class RollingStockPools:
    """``trains.json`` 的 ``rolling_stock_pools`` 區段。

    沒有這個區段是合法狀態（舊資料檔、捷運資料）：那代表「沒有共通運用」，
    每一班都照時刻表指定的車輛型式行駛，因此 :meth:`select` 原封不動回傳。
    """

    managed_rolling_stock_ids: frozenset[str] = frozenset()
    fleet_cars: Mapping[str, float] = field(default_factory=dict)
    rules: tuple[RollingStockRule, ...] = ()

    @classmethod
    def empty(cls) -> RollingStockPools:
        """沒有共通運用的空規則。"""
        return cls()

    @classmethod
    def from_dict(cls, raw: Any) -> RollingStockPools:
        """由 JSON 建立。格式不對的項目直接忽略，不拋例外。"""
        if not isinstance(raw, dict):
            return cls.empty()
        fleet: dict[str, float] = {}
        for key, value in (raw.get("fleet_cars") or {}).items():
            try:
                fleet[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        rules = tuple(
            RollingStockRule.from_dict(item)
            for item in (raw.get("rules") or ())
            if isinstance(item, dict)
        )
        return cls(
            managed_rolling_stock_ids=frozenset(
                _as_str_tuple(raw.get("managed_rolling_stock_ids"))
            ),
            fleet_cars=fleet,
            rules=rules,
        )

    # ------------------------------------------------------------------
    def rule_for(
        self, service_class_id: str, line_ids: Iterable[str]
    ) -> RollingStockRule | None:
        """由上而下比對，回傳第一條適用的規則。"""
        line_ids = tuple(line_ids)
        for rule in self.rules:
            if rule.matches(service_class_id, line_ids):
                return rule
        return None

    def weights(self, rule: RollingStockRule) -> tuple[tuple[str, float], ...]:
        """某一條規則各車型的抽中權重 ``(車輛型式, 權重)``。

        權重 ＝ 現役車輛數 × 配屬倍率。查不到車輛數的型式權重為 0，等於不會
        被抽到——寧可少一型，也不要用一個編出來的數字。
        """
        pairs = []
        for stock_id in rule.rolling_stock_ids:
            weight = self.fleet_cars.get(stock_id, 0.0) * rule.multipliers.get(
                stock_id, 1.0
            )
            if weight > 0.0:
                pairs.append((stock_id, weight))
        return tuple(pairs)

    def select(
        self,
        *,
        service_class_id: str,
        line_ids: Sequence[str],
        train_number: str,
        rolling_stock_id: str,
        day: date | None = None,
    ) -> str:
        """回傳某一班次在 *day* 這一天實際擔當的車輛型式。

        任何一步走不通都原封不動回傳 ``rolling_stock_id``：不在共通運用池裡的
        班次（對號列車、支線柴客）、沒有規則可用、規則裡一型都抽不出來，全都
        是正常狀態，不是資料錯誤。
        """
        if rolling_stock_id not in self.managed_rolling_stock_ids:
            return rolling_stock_id
        rule = self.rule_for(service_class_id, line_ids)
        if rule is None:
            return rolling_stock_id
        weights = self.weights(rule)
        if not weights:
            return rolling_stock_id

        total = sum(weight for _, weight in weights)
        threshold = _fraction(day or pinned_service_day(), train_number) * total
        cumulative = 0.0
        for stock_id, weight in weights:
            cumulative += weight
            if threshold < cumulative:
                return stock_id
        return weights[-1][0]


def _fraction(day: date, train_number: str) -> float:
    """由「日期＋車次」得到一個穩定的 ``[0, 1)`` 亂數。

    同一組輸入永遠得到同一個值，且不受行程重啟影響（見模組說明）。
    """
    digest = hashlib.blake2b(
        f"{day.isoformat()}|{train_number}".encode(), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big") / float(1 << 64)
