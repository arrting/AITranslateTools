# AITranslateTools

Kindle 自動翻譯筆記工作流:在 Windows 上邊讀 Kindle 邊自動產生「原文 + 繁體中文翻譯」的閱讀筆記。

## 運作原理

執行 `fixed_region_watcher.py` 後，程式會定時截取 Kindle 視窗的固定文字區域，偵測到畫面變化（＝你翻頁了）就自動:

1. 等翻頁動畫穩定後截圖
2. 用 Windows 內建 OCR 辨識文字
3. 呼叫 Gemini API 翻譯成繁體中文
4. 依章節寫入筆記檔（原文與翻譯可分檔或合併）
5. 螢幕右下角跳出完成通知

你只需要專心閱讀、翻頁。

## 檔案說明

| 檔案 | 用途 |
|---|---|
| `fixed_region_watcher.py` | 主程式:自動偵測翻頁 → OCR → 翻譯 → 寫入筆記 |
| `find_coordinates.py` | 一次性座標校準工具，找出 Kindle 文字區域的 `REGION` 座標 |
| `使用說明.md` | 完整安裝設定步驟、每次閱讀操作流程、常見問題排查 |

## 快速開始

```
python -m pip install pyautogui winocr pillow google-genai win11toast
setx GEMINI_API_KEY "你的API金鑰"
```

1. 安裝 Windows 英文 OCR 套件（設定 → 時間與語言 → 新增語言 English (United States)）
2. 到 [Google AI Studio](https://aistudio.google.com/) 申請 Gemini API Key，設定環境變數後重開終端機
3. 打開 `fixed_region_watcher.py` 設定區，改好筆記檔路徑、書名、章節
4. 執行 `python find_coordinates.py` 校準座標，把結果貼回 `REGION`
5. 執行 `python fixed_region_watcher.py`，開始閱讀（啟動時會先翻譯當前頁）

詳細步驟與疑難排解請見 [使用說明.md](使用說明.md)。

## 主要特性

- 自動偵測翻頁，完整譯文即時顯示在終端機
- 自動過濾 Kindle 頁首書名、頁尾頁碼等 OCR 雜訊
- **自動偵測章節頁並切章**（「SIX / JAMIE」「CHAPTER 6」「第2章」等格式），重啟自動接續上次章節
- 人名對照表（`NAME_GLOSSARY`）確保譯名前後一致
- 依章節分組寫入筆記，回頭補看不會亂序；重啟不會重複翻譯已翻過的頁面
- Gemini 過載時自動重試、自動切換備援模型，不漏頁
- 支援英文與日文書（`OCR_LANG` 一行切換，需安裝對應的 Windows OCR 語言套件）

## 環境需求

- Windows 10/11（OCR 使用 winocr、通知使用 win11toast）＋ 英文 OCR 語言套件
- Python 3.9+
- Gemini API Key（模型鏈 `gemini-3.1-pro-preview` → `gemini-3.5-flash` → `gemini-3.1-flash-lite`，過載或無權限時自動逐級退）
