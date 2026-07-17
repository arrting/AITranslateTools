"""
Kindle 閱讀筆記自動化工作流（固定範圍版）
--------------------------------
不用每次手動框選，改成：
1. 先用 find_coordinates.py 找出 Kindle 文字區域的固定座標，填進下面 REGION
2. 執行本程式後，只管專心翻頁閱讀
3. 程式會每隔一小段時間自動截圖該固定範圍，跟上一次畫面比對
4. 偵測到畫面內容改變（=你翻頁了）→ 自動等畫面穩定 → OCR 辨識 → 呼叫 Gemini 翻譯 → 寫入筆記

使用前準備：
  pip install pyautogui winocr pillow google-genai win11toast
  設定環境變數 GEMINI_API_KEY

執行：
  python fixed_region_watcher.py
  （不需要系統管理員身分，一般權限即可）

停止：
  Ctrl+C
"""

import time
import os
import re
from datetime import datetime

import pyautogui
from PIL import Image, ImageStat, ImageChops
import winocr
from google import genai
from google.genai import types
from win11toast import notify

# ==== 設定區：請依你的環境修改 ====
REGION = (1, 122, 1248, 1026)  # 用 find_coordinates.py 找出的 (左, 上, 右, 下) 座標，換成你的

NOTE_FILE = r"D:\Codes\TEST\HIM.txt"
TRANSLATION_FILE = r"D:\Codes\TEST\HIM_翻譯.txt"
SEPARATE_FILES = True
BOOK_TITLE = "HIM"
CHAPTER = "第五章"          # 章節編號行,必須是「第X章」格式(用來分組與辨識章節邊界)
CHAPTER_TITLE = "衛斯(WES)"  # 章節副標題,會顯示在編號下一行;沒有的話留空字串 ""
OCR_LANG = "en"            # OCR 辨識語言:英文 "en"、日文 "ja"(需安裝對應的 Windows OCR 語言套件)
MIN_LENGTH = 20            # OCR 結果低於這個字元數視為辨識失敗/空白頁，不觸發翻譯
GEMINI_MODEL = "gemini-3.5-flash"
FALLBACK_MODEL = "gemini-3.1-flash-lite"  # 主模型過載/額度用完時自動改用的備援模型
RETRY_COUNT = 3            # 每個模型遇到暫時性錯誤(503/429)時最多重試幾次
RETRY_WAIT = 3             # 重試前等待秒數(每次重試會遞增)

POLL_INTERVAL = 1.0        # 每隔幾秒檢查一次畫面有沒有變化
STABILIZE_DELAY = 0.6      # 偵測到變化後，等畫面穩定（翻頁動畫跑完）再確認一次的間隔秒數
CHANGE_THRESHOLD = 5       # 兩張截圖平均像素差異超過這個值，視為「畫面變了」（0-255，數字越小越敏感）

client = genai.Client()


def screenshot_region():
    left, top, right, bottom = REGION
    return pyautogui.screenshot(region=(left, top, right - left, bottom - top))


def images_differ(img1, img2, threshold: float = CHANGE_THRESHOLD) -> bool:
    """比較兩張截圖是否有明顯差異（用來判斷是否翻頁了）"""
    diff = ImageChops.difference(img1.convert("L"), img2.convert("L"))
    mean_diff = ImageStat.Stat(diff).mean[0]
    return mean_diff > threshold


# Kindle 頁首/頁尾雜訊(書名、頁碼、百分比、剩餘時間)的辨識規則,英文/日文介面都涵蓋
_JUNK_LINE_RES = [
    re.compile(rf"^{re.escape(BOOK_TITLE)}$", re.IGNORECASE),          # 頁首書名
    re.compile(r"minutes?\s+left\s+in", re.IGNORECASE),                # 1 minute left in chapter/book
    re.compile(r"location\b.*\d", re.IGNORECASE),                      # Location 533 of 4215
    re.compile(r"^[\d\s•·.%ofin/]+$", re.IGNORECASE),                  # 純頁碼/百分比行(含 OCR 誤讀變體)
    re.compile(r"位置\s*No", re.IGNORECASE),                            # 日文版頁尾:位置No. 533 / 4215
    re.compile(r"残り\s*\d+\s*分"),                                     # 日文版頁尾:残り○分(章/本)
    re.compile(r"^ページ\s*\d"),                                        # 日文版頁尾:ページ 12
]


def _is_junk_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    # 頁首頁尾都是短行,長句正文不套用規則以免誤刪
    if len(s) > 60:
        return False
    return any(p.search(s) for p in _JUNK_LINE_RES)


def ocr_image(img) -> str:
    result = winocr.recognize_pil_sync(img, lang=OCR_LANG)
    lines = [l.get("text", "") for l in result.get("lines", [])]
    if not lines:
        lines = result.get("text", "").splitlines()
    kept = [l for l in lines if not _is_junk_line(l)]
    # 英文行與行之間用空格接;日文不用空格(日文書寫沒有詞間空格)
    joiner = "" if OCR_LANG == "ja" else " "
    return joiner.join(kept).strip()


def _is_transient_error(e: Exception) -> bool:
    """503 過載 / 429 額度暫時超限,這類錯誤重試或換模型有機會成功"""
    msg = str(e)
    return any(k in msg for k in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "overloaded"))


def translate(text: str) -> tuple[str, str]:
    """呼叫 Gemini API 翻譯:主模型失敗自動重試,重試用盡自動換備援模型。
    回傳 (譯文, 實際使用的模型名稱)"""
    prompt = (
        "把這段內容翻譯成繁體中文（要通順）。"
        "原文因為 OCR 的關係失去了分段，請你依內容重新分段："
        "每句對話獨立成一行，敘事部分依語意適當分段，段落之間留一個空行。"
        "只要譯文本身，不用任何前綴詞（例如不要寫「這段內容為您翻譯如下：」"
        "或任何說明、標題），直接給我翻譯後的繁體中文本文：\n\n"
        f"{text}"
    )
    last_error = None
    for model in (GEMINI_MODEL, FALLBACK_MODEL):
        for attempt in range(1, RETRY_COUNT + 1):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    # 關閉思考模式:翻譯不需要推理,關掉後回應速度大幅提升
                    config=types.GenerateContentConfig(
                        thinking_config=types.ThinkingConfig(thinking_budget=0)
                    ),
                )
                return resp.text.strip(), model
            except Exception as e:
                last_error = e
                if not _is_transient_error(e):
                    raise
                if attempt < RETRY_COUNT:
                    wait = RETRY_WAIT * attempt
                    print(f"   ⏳ {model} 暫時過載，{wait} 秒後重試（第 {attempt}/{RETRY_COUNT - 1} 次）...")
                    time.sleep(wait)
        if model == GEMINI_MODEL:
            print(f"   🔁 {GEMINI_MODEL} 持續過載，改用備援模型 {FALLBACK_MODEL}...")
    raise last_error


# 章節邊界的辨識規則:整行只有「第X章」(X 為中文數字或阿拉伯數字)才算章節標題行
_CHAPTER_LINE_RE = re.compile(r"(?m)^第[0-9一二三四五六七八九十百千零兩两]{1,6}章\s*$")


def _chapter_heading() -> str:
    return f"{CHAPTER}\n{CHAPTER_TITLE}" if CHAPTER_TITLE else CHAPTER


def _insert_into_file(file_path: str, entry: str):
    """通用插入邏輯：依章節分類插入內容，往回翻閱補看不會打亂順序（純文字格式）"""
    chapter_heading = _chapter_heading()
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    if not os.path.exists(file_path):
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(f"{BOOK_TITLE}\n\n{chapter_heading}\n{entry}")
        return

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    heading_match = re.search(rf"(?m)^{re.escape(chapter_heading)}$", content)
    if heading_match:
        start = heading_match.end()
        next_heading = _CHAPTER_LINE_RE.search(content, start)
        if next_heading is None:
            new_content = content + entry
        else:
            insert_at = next_heading.start()
            new_content = content[:insert_at] + entry + "\n" + content[insert_at:]
    else:
        new_content = content.rstrip() + f"\n\n\n{chapter_heading}\n{entry}"

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(new_content)


def append_to_note(original: str, translated: str):
    if SEPARATE_FILES:
        _insert_into_file(NOTE_FILE, f"\n{original}\n")
        _insert_into_file(TRANSLATION_FILE, f"\n{translated}\n")
    else:
        entry = f"\n【原文】\n\n{original}\n\n【翻譯】\n\n{translated}\n"
        _insert_into_file(NOTE_FILE, entry)


def show_toast(original: str, translated: str):
    preview_original = original if len(original) <= 80 else original[:80] + "..."
    preview_translated = translated if len(translated) <= 120 else translated[:120] + "..."
    try:
        # notify 是非阻塞版通知:發出後立刻返回,不會卡住翻頁監測
        notify(
            f"✅ 翻譯完成｜{CHAPTER}",
            f"原文：{preview_original}\n\n翻譯：{preview_translated}",
            duration="long",
        )
    except Exception as e:
        print(f"（通知彈窗顯示失敗：{e}）")


def process_page(img, last_text: str):
    """OCR + 翻譯 + 寫入筆記 + 通知，回傳這次辨識到的文字（給主迴圈記錄用）"""
    text = ocr_image(img)
    if len(text) < MIN_LENGTH:
        print(f"⚠️ 辨識結果太短（{len(text)} 字），可能是翻頁動畫還沒跑完或空白頁，略過")
        return None

    if text == last_text:
        print("⚠️ 內容與上一頁相同（可能是同一頁上的反白/游標閃動），略過")
        return None

    print(f"🔍 辨識到 {len(text)} 字，翻譯中...")
    try:
        translated, used_model = translate(text)
        append_to_note(text, translated)
        show_toast(text, translated)
        print(f"✅ 已寫入筆記（模型：{used_model}），完整翻譯如下：")
        print("─" * 60)
        print(translated)
        print("─" * 60 + "\n")
        return text
    except Exception as e:
        print(f"⚠️ 翻譯失敗：{e}")
        print("   這一頁稍後會自動重試\n")
        return "RETRY"


def main():
    print("📖 自動偵測翻頁模式已啟動")
    print(f"監控範圍：{REGION}")
    if SEPARATE_FILES:
        print(f"原文將寫入：{NOTE_FILE}")
        print(f"翻譯將寫入：{TRANSLATION_FILE}")
    else:
        print(f"筆記（合併）將寫入：{NOTE_FILE}")
    print("\n你只要專心翻頁閱讀，程式會自動偵測畫面變化並翻譯。按 Ctrl+C 結束。\n")

    last_stable_img = screenshot_region()
    last_ocr_text = ""

    # 啟動時先處理當前已顯示的頁面，避免第一頁漏掉
    print("📄 先處理目前畫面上的這一頁...")
    result = process_page(last_stable_img, last_ocr_text)
    if result == "RETRY":
        # 換成全黑的假基準圖，讓下一輪迴圈把當前頁視為「有變化」而重新處理
        last_stable_img = Image.new("RGB", last_stable_img.size)
    elif result:
        last_ocr_text = result

    while True:
        time.sleep(POLL_INTERVAL)
        current_img = screenshot_region()

        if not images_differ(last_stable_img, current_img):
            continue  # 畫面沒變化，繼續等待

        # 偵測到變化，先等翻頁動畫跑完，再確認畫面是否已經穩定
        time.sleep(STABILIZE_DELAY)
        confirm_img = screenshot_region()

        if images_differ(current_img, confirm_img):
            # 畫面還在變動中（翻頁動畫尚未結束），這輪先跳過，下一輪迴圈會再檢查
            continue

        # 畫面已穩定，處理這一頁
        result = process_page(confirm_img, last_ocr_text)
        if result == "RETRY":
            # 不更新基準圖，下一輪迴圈會把這一頁再當成「有變化」自動重試
            continue
        if result:
            last_ocr_text = result
        last_stable_img = confirm_img


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 已停止監聽")
