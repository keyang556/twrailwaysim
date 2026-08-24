"""開門側（左側／右側）的判斷。

到站廣播之後要接一句「左側開門」或「右側開門」，否則看不見月台的旅客不
知道往哪一邊走。問題是**月台側別沒有可靠的公開來源**（規格 §22、§27）：
時刻表只有時刻，車站資料沒有月台配置。

因此這裡不假裝知道每一站的月台在哪一側，而是照使用者指定的規則推：

- 標準情況一律是**左側**（臺鐵左側通行，正線月台在左）。
- 這班車在某一站**明顯比別的車待得久**，表示它在該站**待避**（讓後面的
  車先過）——待避時列車進的是待避線而不是正線，開門側因此改為**右側**。

「明顯比別的車久」怎麼算
------------------------

拿本班次在「前一停靠站 → 本站」這一段花的時間，跟**同車種、同一對停靠站**
的班次在同一段花的最短時間比。以停靠站配對而不是以里程比，是因為里程本身
也是由時刻推估出來的（見 :mod:`railway_sim.dataset.build`），拿它當基準等於
用同一份資料繞一圈；同時限定車種，是因為自強號與區間車跑同一段本來就有
速差，不分開比會把速差當成待避。

時刻表印的是**開車**時刻，因此 ``開車(本站) − 開車(前一站)`` 已經包含了
本站的停站時間；待避多出來的那幾分鐘就落在這個差值裡。

判定門檻同時看絕對值與比例（見 :data:`OVERTAKE_MARGIN_MIN` 與
:data:`OVERTAKE_MARGIN_RATIO`）：只看絕對值的話，長區間裡不同車種的速差
會被誤判成待避；只看比例的話，兩三分鐘的短區間又會太敏感。

這是**推導**出來的結果，不是臺鐵公布的月台側別；資料可信度等同
``derived_from_timetable``。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from itertools import pairwise

from railway_sim.timetable.service import Service

__all__ = [
    "DEFAULT_DOOR_SIDE",
    "OVERTAKE_MARGIN_MIN",
    "OVERTAKE_MARGIN_RATIO",
    "DoorSideRule",
]

#: 標準情況的開門側。
DEFAULT_DOOR_SIDE = "left"

#: 待避側。
OVERTAKE_DOOR_SIDE = "right"

#: 判定待避所需的最少額外分鐘數。
#:
#: 待避一次至少要讓後面的車追上並通過，實務上是好幾分鐘；兩三分鐘的差距
#: 更可能只是排點餘裕。
OVERTAKE_MARGIN_MIN = 4.0

#: 判定待避所需的最少額外比例。
#:
#: 只有絕對門檻的話，一段本來就要跑三十分鐘的長區間差四分鐘很正常，卻會被
#: 判成待避。加上比例門檻，長區間要差得夠多才算數。
#:
#: 這兩個門檻套在本專案隨附的時刻表上，約有百分之五的停靠站被判為待避
#: （九百五十三個班次中的一千餘站）。這是**推導**的結果不是公布資料，因此
#: 門檻集中在這裡，要調整只要改這兩個值。
OVERTAKE_MARGIN_RATIO = 0.5

#: 一天的分鐘數，用來還原跨午夜的時刻差。
_DAY_MINUTES = 24 * 60


def _minutes(text: str) -> float | None:
    """把 ``HH:MM`` 轉成分鐘數；認不出來回傳 ``None``。"""
    head, _, tail = text.partition(":")
    try:
        return int(head) * 60 + int(tail)
    except ValueError:
        return None


def section_minutes(service: Service, first: str, second: str) -> float | None:
    """本班次由 *first* 開車到 *second* 開車之間的分鐘數。

    含 *second* 站的停站時間——時刻表印的是開車時刻，待避多出來的時間就在
    這裡面。任一站沒有時刻時回傳 ``None``。
    """
    start = _minutes(service.departure_times.get(first, ""))
    end = _minutes(service.departure_times.get(second, ""))
    if start is None or end is None:
        return None
    if end < start:
        end += _DAY_MINUTES  # 跨午夜
    return end - start


@dataclass(frozen=True)
class DoorSideRule:
    """由整份時刻表算出的開門側規則。

    Attributes:
        fastest: ``{(前一停靠站, 本站, 車種): 最短分鐘數}``。基準取最短而不是
            平均，因為平均會被一堆待避的班次自己拉高。
    """

    fastest: Mapping[tuple[str, str, str], float] = field(default_factory=dict)

    @classmethod
    def from_services(cls, services: Iterable[Service]) -> DoorSideRule:
        fastest: dict[tuple[str, str, str], float] = {}
        for service in services:
            for first, second in pairwise(service.stop_station_ids):
                elapsed = section_minutes(service, first, second)
                if elapsed is None or elapsed < 0:
                    continue
                key = (first, second, service.train_type)
                if elapsed < fastest.get(key, float("inf")):
                    fastest[key] = elapsed
        return cls(fastest=fastest)

    # ------------------------------------------------------------------
    def waits_for_overtake(self, service: Service, first: str, second: str) -> bool:
        """本班次是否在 *second* 站待避。

        同一段只有這一班同車種的車時，基準就是它自己，差值為零——沒有可比
        的對象時不猜，維持預設側。
        """
        elapsed = section_minutes(service, first, second)
        baseline = self.fastest.get((first, second, service.train_type))
        if elapsed is None or baseline is None:
            return False
        extra = elapsed - baseline
        return extra >= max(OVERTAKE_MARGIN_MIN, baseline * OVERTAKE_MARGIN_RATIO)

    def sides_for(self, service: Service) -> dict[str, str]:
        """本班次各停靠站的開門側。

        只列出**不是預設側**的車站：其餘一律是左側，逐站列出只會讓「哪幾站
        不一樣」淹沒在一片相同的值裡。
        """
        sides: dict[str, str] = {}
        for first, second in pairwise(service.stop_station_ids):
            if self.waits_for_overtake(service, first, second):
                sides[second] = OVERTAKE_DOOR_SIDE
        return sides
