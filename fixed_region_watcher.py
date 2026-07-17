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
from datetime import datetime

import pyautogui
from PIL import ImageStat, ImageChops
import winocr
from google import genai
from win11toast import toast

# ==== 設定區：請依你的環境修改 ====
REGION = (100, 150, 1200, 900)   # 用 find_coordinates.py 找出的 (左, 上, 右, 下) 座標，換成你的

NOTE_FILE = r"C:\Users\YourName\Documents\ObsidianVault\Kindle小說筆記.txt"
TRANSLATION_FILE = r"C:\Users\YourName\Documents\ObsidianVault\Kindle小說筆記_翻譯.txt"
SEPARATE_FILES = True
BOOK_TITLE = "你的小說書名"
CHAPTER = "第5章"
MIN_LENGTH = 20            # OCR 結果低於這個字元數視為辨識失敗/空白頁，不觸發翻譯
GEMINI_MODEL = "gemini-3.5-flash"

POLL_INTERVAL = 1.0        # 每隔幾秒檢查一次畫面有沒有變化
STABILIZE_DELAY = 0.6      # 偵測到變化後，等畫面穩定（翻頁動畫跑完）再確認一次的間隔秒數
CHANGE_THRESHOLD = 5       # 兩張截圖平均像素差異超過這個值，視為「畫面變了」（0-255，數字越小越敏感）

client = genai.Client()


def screenshot_region():
    return pyautogui.screenshot(region=REGION)


def images_differ(img1, img2, threshold: float = CHANGE_THRESHOLD) -> bool:
    """比較兩張截圖是否有明顯差異（用來判斷是否翻頁了）"""
    diff = ImageChops.difference(img1.convert("L"), img2.convert("L"))
    mean_diff = ImageStat.Stat(diff).mean[0]
    return mean_diff > threshold


def ocr_image(img) -> str:
    result = winocr.recognize_pil_sync(img, lang="en")
    return result.get("text", "").strip()


def translate(text: str) -> str:
    """呼叫 Gemini API 翻譯"""
    prompt = (
        "把這段內容翻譯成繁體中文（要通順）。"
        "只要內容就好，不用任何前綴詞（例如不要寫「這段內容為您翻譯如下：」"
        "或任何說明、標題、引號），直接給我翻譯後的繁體中文本文：\n\n"
        f"{text}"
    )
    resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    return resp.text.strip()


def _insert_into_file(file_path: str, entry: str):
    """通用插入邏輯：依章節分類插入內容，往回翻閱補看不會打亂順序（純文字格式）"""
    chapter_heading = f"===== {CHAPTER} ====="
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    if not os.path.exists(file_path):
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(f"{BOOK_TITLE}\n\n{chapter_heading}\n{entry}")
        return

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    if chapter_heading in content:
        start = content.index(chapter_heading) + len(chapter_heading)
        rest = content[start:]
        next_heading_pos = rest.find("\n===== ")
        if next_heading_pos == -1:
            new_content = content + entry
        else:
            insert_at = start + next_heading_pos
            new_content = content[:insert_at] + entry + content[insert_at:]
    else:
        new_content = content.rstrip() + f"\n\n{chapter_heading}\n{entry}"

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
        toast(
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

    print(f"🔍 偵測到翻頁，辨識到 {len(text)} 字，翻譯中...")
    try:
        translated = translate(text)
        append_to_note(text, translated)
        show_toast(text, translated)
        print("✅ 已寫入筆記")
        print(f"   原文：{text[:60]}...")
        print(f"   翻譯：{translated[:60]}...\n")
        return text
    except Exception as e:
        print(f"⚠️ 翻譯失敗：{e}\n")
        return None


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
        text = process_page(confirm_img, last_ocr_text)
        if text:
            last_ocr_text = text
        last_stable_img = confirm_img


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 已停止監聽")
