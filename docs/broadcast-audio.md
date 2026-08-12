# 車上廣播更新流程

廣播會改版、新站會通車，因此本專案的廣播**不是寫死在程式裡的清單**：索引由
掃描資料夾建立，把檔案放進去就生效。本文說明檔名規則、匯入流程，以及新增
一條路線要改哪裡。

目前收錄 **縱貫線北段**（基隆－竹南，36 站）與 **山線**（造橋－彰化，23 站），
共 124 個音檔。

---

## 檔名規則

```text
data/audio/announcements/
    variants.json                 方向版本的挑選規則（可手改）
    manifest.json                 匯入紀錄，僅供追溯，執行時不會讀
    west_north/                   資料夾名稱＝路線代碼（line_id）
        TAIPEI.next.ogg
        TAIPEI.arrive.ogg
        KEELUNG.terminus.ogg
        BADU.next.keelung.ogg
    mountain/
        TAICHUNG.next.ogg
    common/                       不屬於任何車站的廣播
        DOOR.open.ogg
```

檔名為 `<車站代碼>.<種類>[.<方向版本>].<副檔名>`。

| 種類 | 意義 | 何時播 |
|---|---|---|
| `next` | 下一站 | 列車自車站**啟動之後**，內容是下一個停靠站 |
| `arrive` | 到站 | 到達停靠站**之前**（1500 公尺） |
| `terminus` | 終點 | 到達終點站之前；有這一個就不播 `arrive` |

車站代碼即 `data/stations.json` 的 `id`（例如 `TAIPEI`、`SANXINGQIAO`）。
副檔名可以是 `.ogg`、`.wav`、`.mp3`、`.m4a`、`.flac`、`.opus`——能不能播得出來
取決於後端，索引一律照收。

### 為什麼不用清單檔

使用者的要求是「隨時可以更新，暫時沒有廣播的不能讓程式 error」。掃描資料夾
可以同時滿足兩者：

- 丟一個檔案進去就被索引，不需要同步維護任何清單。
- 檔案不存在只是「查不到」，播文字即可，不是例外。
- 檔名看不懂就跳過並記進警告，不會中斷載入。

---

## 匯入來源資料

來源檔名是給人看的：開頭有站序編號、偶有錯字、分歧站用括號標方向。匯入工具
負責翻譯：

```bash
python -m railway_sim.audio import --dry-run "…/台鐵廣播/1縱貫北" "…/台鐵廣播/2山線"
```

確認結果後拿掉 `--dry-run` 實際匯入。**內容沒變的檔案不會重寫**，因此廣播改版
時把新檔放回來源資料夾再跑一次同一行即可，重跑是安全且便宜的。

翻譯範例：

| 來源 | 匯入後 |
|---|---|
| `1縱貫北/11台北.ogg` | `west_north/TAIPEI.next.ogg` |
| `1縱貫北/14福州到站.ogg` | `west_north/FUZHOU.arrive.ogg` |
| `1縱貫北/1基隆終點.ogg` | `west_north/KEELUNG.terminus.ogg` |
| `1縱貫北/3八堵(往基隆).ogg` | `west_north/BADU.next.keelung.ogg` |
| `2山線/15經舞蹈站.ogg` | `mountain/JINGWU.arrive.ogg` |

### 已修正的來源錯字

對應規則在 [`data/audio/source_map.json`](../data/audio/source_map.json)，程式不寫死
任何站名對應。`station_aliases` 只收「來源檔名與官方站名不一致」的項目：

| 來源檔名 | 實際車站 |
|---|---|
| 福州 | 浮洲 |
| 楠樹林 | 南樹林 |
| 普興 | 埔心 |
| 三性橋 | 三姓橋 |
| 麗玲 | 栗林 |
| 南市 | 南勢 |
| 經五 | 精武 |
| 經舞蹈站 | 精武（到站） |

「台／臺」是異體字，程式會自動正規化，不需要列在別名表裡。

### 對不上的檔案

對不上不會讓匯入失敗，只會列在報告的「未對應」裡。`4縱貫南` 目前檔名還是純
編號（`100.ogg`、`100n.ogg`…），尚未整理完，因此還沒有匯入；整理成
`<站序><站名>[到站|終點]` 的形式之後，加進上面那行指令即可。

---

## 新增一條路線

1. 在 `source_map.json` 的 `line_ids` 加一筆「來源資料夾名稱 → 路線代碼」。
   路線代碼要與 `data/routes.json` 的 `lines` 一致（`west_south`、`coast`、
   `yilan`…）。
2. 把來源資料夾加進匯入指令。
3. 用 `python -m railway_sim.audio check` 確認覆蓋率，它會列出**已經有部分
   廣播**的路線還缺哪幾站（整條線都還沒錄的不會逐站列出，那只是雜訊）。

新站通車時同理：只要 `stations.json` 裡有那一站，把音檔改名成
`<車站代碼>.next.ogg` 放進對應資料夾就會生效。

---

## 方向版本

八堵、七堵的「下一站」廣播有往基隆／往花蓮兩個版本，差別在於提醒旅客要不要在
本站換車，因此取決於**本班列車開往哪裡**，不是取決於車站本身。規則在
[`data/audio/announcements/variants.json`](../data/audio/announcements/variants.json)：

```json
{
  "rules": {
    "BADU": [
      { "variant": "keelung", "when_service_calls_at": ["KEELUNG"] },
      { "variant": "hualien", "when_service_calls_at": ["HUALIEN", "YILAN", "SUAO"] }
    ]
  }
}
```

依序比對，本班次的停靠表只要包含其中任一站就採用該版本。都不符合時退回沒有
方向標記的檔案；沒有的話就**不播音檔**——播錯方向比不播更糟，而文字播報照常
送出，資訊不會少。

---

## 沒有廣播的情形

以下三種都是**正常狀態**，不是故障。行前提要會說明是哪一種：

| 情形 | 行前提要顯示 |
|---|---|
| 本型車沒有廣播設備（DR1000） | `無（DR1000型柴油客車沒有車上廣播設備）` |
| 這條線還沒有音檔 | `本路線尚無廣播音檔，僅提供文字` |
| 正常 | `停靠站 14 / 17 站有廣播音檔` |

DR1000 的例外由 `data/trains.json` 的 `has_broadcast: false` 決定，不是寫死車型
代碼；這型車連廣播文字都不送出，因為那台車根本沒有播出任何東西。

---

## 播放後端

臺鐵廣播是 Ogg Vorbis。Windows 內建的播放元件（wxPython 用的 Media Foundation）
**不解這個格式**——本專案以 `wx.media.MediaCtrl` 實測過，同一個控制項載入 WAV
會回報長度，載入 Ogg 則永遠沒有 `EVT_MEDIA_LOADED`。因此播放後端依序嘗試：

1. `pygame`（`pip install railway-sim[audio]`）— 內建 SDL_mixer，可直接播 Ogg。
2. `ffplay`（`ffmpeg` 隨附，在 `PATH` 上就能用）。
3. 都沒有 → 只播文字。

一次只播一則：廣播動輒數十秒（臺北的「下一站」廣播有 46 秒），站間距離卻可能
更短，疊著播兩則都聽不清楚。新的一則會蓋掉還沒播完的舊的。

用 `--no-audio` 可以關掉聲音。依規格 §20.1「不可只靠音效表達必要資訊」，每一則
廣播都會同時送出一行文字，因此關掉聲音不會少掉任何資訊。

文字是廣播內容的**摘要**而非逐字稿：實際音檔含國語、臺語、客語與英語，逐字稿
無法由檔名得知，寫成摘要才不會虛構內容（規格 §2.3）。
