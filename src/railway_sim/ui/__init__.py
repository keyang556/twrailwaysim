"""介面層。

介面只負責「送入按鍵」與「輸出文字」，不含任何運轉邏輯。所有必要資訊
都能從 :meth:`DriverSession.status_lines` 與播報訊息取得，因此不會出現
只存在於視覺介面的資訊（規格 §25.5）。
"""

__all__ = ["console_app", "wx_app"]
