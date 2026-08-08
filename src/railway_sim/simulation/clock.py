"""模擬時鐘（規格 §8.1）。

固定步長 0.1 秒。實際經過的時間會被切成固定大小的 tick，讓模擬結果與
畫面更新頻率無關，也讓測試可完全決定性地推進時間。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["SIMULATION_TICK_S", "SimulationClock"]

#: 規格 §8.1 建議的模擬步長（秒）。
SIMULATION_TICK_S = 0.1


@dataclass
class SimulationClock:
    """把任意長度的實際經過時間切成固定步長。"""

    tick_s: float = SIMULATION_TICK_S
    elapsed_s: float = 0.0
    _carry_s: float = field(default=0.0, init=False, repr=False)

    def advance(self, real_dt_s: float) -> int:
        """累積實際時間並回傳應執行的 tick 數。

        本方法**不動** :attr:`elapsed_s`：模擬時間由實際執行的那一步（
        ``DriverSession.tick``）累加，才不會出現「排定了幾步」與「真的跑了
        幾步」兩份時間。兩邊都加會讓運轉時間以兩倍速前進。
        """
        if real_dt_s <= 0:
            return 0
        self._carry_s += real_dt_s
        ticks = int(self._carry_s / self.tick_s)
        if ticks:
            self._carry_s -= ticks * self.tick_s
        return ticks

    def reset(self) -> None:
        self.elapsed_s = 0.0
        self._carry_s = 0.0

    @property
    def clock_text(self) -> str:
        """``時:分:秒`` 格式的模擬時間。"""
        total = int(self.elapsed_s)
        return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"
