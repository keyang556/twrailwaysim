"""角色模式。

第一階段只實作司機員模式（規格 §4）。列車長、車站運轉員、行車調度員與
售票員模式尚未實作，不得宣稱已完成（規格 §2.3）。
"""

from railway_sim.roles.driver import DriverSession, StationProgress

__all__ = ["DriverSession", "StationProgress"]
