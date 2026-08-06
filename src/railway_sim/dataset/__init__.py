"""臺鐵公開時刻表的匯入工具。

本套件把臺鐵公布的 ``.ods`` 時刻表轉成 ``data/`` 底下的遊戲資料檔，讓資料
可以隨著臺鐵改點重新產生，而不必手動編輯 JSON：

```bash
python -m railway_sim.dataset --source <時刻表資料夾> --out data
```

模組分工：

- :mod:`~railway_sim.dataset.ods` — 只用標準函式庫讀 ``.ods``。
- :mod:`~railway_sim.dataset.tra_parser` — 讀懂臺鐵的兩種表格排版。
- :mod:`~railway_sim.dataset.registry` — 中文站名到內部代碼的對照。
- :mod:`~railway_sim.dataset.build` — 產生 ``stations.json`` 等資料檔。
"""

from __future__ import annotations

__all__ = ["build", "ods", "registry", "tra_parser"]
