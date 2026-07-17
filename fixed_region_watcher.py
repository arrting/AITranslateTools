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
AUTO_CHAPTER = True        # 自動偵測章節頁(如「SIX / JAMIE」)並自動切章,不用手動改設定重啟
NAME_GLOSSARY = {          # 這本書的人名/章節標題固定譯名(換書時換成新書的;不需要可清空成 {})
    "Wes": "衛斯",
    "Wesley": "衛斯理",
    "Jamie": "傑米",
    "Canning": "坎寧",
    "Holly": "荷莉",
    "Cassel": "卡塞爾",
    "Blake": "布雷克",
    "Rainier": "雷尼爾",
    "April": "四月",
    "June": "六月",
    "July": "七月",
    "August": "八月",
}
OCR_LANG = "en"            # OCR 辨識語言:英文 "en"、日文 "ja"(需安裝對應的 Windows OCR 語言套件)
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


def ocr_lines(img) -> list[str]:
    result = winocr.recognize_pil_sync(img, lang=OCR_LANG)
    lines = [l.get("text", "") for l in result.get("lines", [])]
    if not lines:
        lines = result.get("text", "").splitlines()
    return [l for l in lines if not _is_junk_line(l)]


# ==== 章節頁自動偵測 ====
_EN_UNITS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5,
             "SIX": 6, "SEVEN": 7, "EIGHT": 8, "NINE": 9}
_EN_TEENS = {"TEN": 10, "ELEVEN": 11, "TWELVE": 12, "THIRTEEN": 13, "FOURTEEN": 14,
             "FIFTEEN": 15, "SIXTEEN": 16, "SEVENTEEN": 17, "EIGHTEEN": 18, "NINETEEN": 19}
_EN_TENS = {"TWENTY": 20, "THIRTY": 30, "FORTY": 40, "FIFTY": 50,
            "SIXTY": 60, "SEVENTY": 70, "EIGHTY": 80, "NINETY": 90}
_JP_CHAPTER_RE = re.compile(r"^(第[0-9〇一二三四五六七八九十百]+章)\s*(.*)$")


def _parse_en_chapter_number(line: str):
    """英文章節行 → 章節數字。支援「SIX」「CHAPTER 6」「CHAPTER SIX」「TWENTY-ONE」等,認不出回傳 None"""
    s = line.strip().upper().strip(".:")
    if not s or len(s) > 25:
        return None
    s = re.sub(r"^CHAPTER\s*", "", s)
    if s.isdigit():
        return int(s) if len(s) <= 3 else None
    parts = re.split(r"[-\s]+", s)
    if len(parts) == 1:
        p = parts[0]
        return _EN_UNITS.get(p) or _EN_TEENS.get(p) or _EN_TENS.get(p)
    if len(parts) == 2 and parts[0] in _EN_TENS and parts[1] in _EN_UNITS:
        return _EN_TENS[parts[0]] + _EN_UNITS[parts[1]]
    return None


def _to_chinese_num(n: int) -> str:
    digits = "一二三四五六七八九"
    if n <= 10:
        return "十" if n == 10 else digits[n - 1]
    if n < 20:
        return "十" + digits[n % 10 - 1]
    tens, unit = divmod(n, 10)
    return digits[tens - 1] + "十" + (digits[unit - 1] if unit else "")


def _translate_name(raw: str) -> str:
    """章節副標的人名對照:在譯名表裡就用「譯名(原文)」,否則保留原文"""
    for k, v in NAME_GLOSSARY.items():
        if k.upper() == raw.upper():
            return f"{v}({raw})"
    return raw


def _detect_chapter(lines: list[str]) -> list[str]:
    """偵測頁面開頭是否為章節頁;是則自動更新 CHAPTER/CHAPTER_TITLE,並把標題行從內文剔除"""
    global CHAPTER, CHAPTER_TITLE
    if not AUTO_CHAPTER or not lines:
        return lines

    first = lines[0].strip()

    # 日文書:章節頁直接是「第X章 標題」或 プロローグ/エピローグ
    if OCR_LANG == "ja":
        if first in ("プロローグ", "エピローグ", "序章", "終章"):
            CHAPTER, CHAPTER_TITLE = first, ""
            print(f"📑 偵測到新章節:{CHAPTER}")
            return lines[1:]
        m = _JP_CHAPTER_RE.match(first)
        if m:
            CHAPTER, CHAPTER_TITLE = m.group(1), m.group(2).strip()
            print(f"📑 偵測到新章節:{CHAPTER} {CHAPTER_TITLE}")
            return lines[1:]
        return lines

    # 英文書:章節頁第一行是數字(SIX / CHAPTER 6)或 PROLOGUE/EPILOGUE,下一行可能是副標(視角人名等)
    specials = {"PROLOGUE": "序章", "EPILOGUE": "尾聲"}
    if first.upper().strip(".:") in specials:
        new_chapter = specials[first.upper().strip(".:")]
    else:
        num = _parse_en_chapter_number(first)
        if num is None:
            return lines
        new_chapter = f"第{_to_chinese_num(num)}章"
    rest = lines[1:]
    title = ""
    # 副標判斷:短、全大寫(Kindle 章節副標通常是「WES」「JAMIE」這種全大寫人名)
    if rest:
        cand = rest[0].strip()
        if 0 < len(cand) <= 30 and cand.isupper():
            title = _translate_name(cand)
            rest = rest[1:]

    if new_chapter != CHAPTER:
        CHAPTER, CHAPTER_TITLE = new_chapter, title
        print(f"📑 偵測到新章節:{CHAPTER} {CHAPTER_TITLE}")
    return rest


class TranslationBlocked(Exception):
    """模型拒絕輸出(安全過濾等),重試無效,應記錄後繼續"""


# 翻譯自有書籍屬正當用途,把安全過濾整個關閉(OFF 比 BLOCK_NONE 更徹底,連評分都不跑),
# 避免小說內容(親密場景等)被擋;Google 另有一層不可調整的核心過濾,由分段重試機制補救
_SAFETY_OFF = [
    types.SafetySetting(category=c, threshold=types.HarmBlockThreshold.OFF)
    for c in (
        types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
    )
]


def _is_transient_error(e: Exception) -> bool:
    """503 過載 / 429 額度暫時超限,這類錯誤重試或換模型有機會成功"""
    msg = str(e)
    return any(k in msg for k in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "overloaded"))


def _system_instruction() -> str:
    glossary = ""
    if NAME_GLOSSARY:
        pairs = "、".join(f"{k}={v}" for k, v in NAME_GLOSSARY.items())
        glossary = f"人名與專有名詞請固定使用以下譯名：{pairs}。表裡沒有的名字也請維持前後一致的譯法。"
    return (
        "你是專業的小說譯者。使用者輸入的是已出版虛構小說的內文（可能包含親密或情感張力場景），"
        "屬於正當的文學翻譯用途。請把內容翻譯成通順的繁體中文。"
        "原文因 OCR 擷取失去了分段，請依內容重新分段："
        "每句對話獨立成一行，敘事部分依語意適當分段，段落之間留一個空行。"
        f"{glossary}"
        "只輸出譯文本身，不要任何前綴詞、說明或標題。"
    )


def translate(text: str) -> tuple[str, str]:
    """呼叫 Gemini API 翻譯:主模型失敗自動重試,重試用盡自動換備援模型。
    回傳 (譯文, 實際使用的模型名稱)"""
    last_error = None
    for model in (GEMINI_MODEL, FALLBACK_MODEL):
        for attempt in range(1, RETRY_COUNT + 1):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=text,
                    config=types.GenerateContentConfig(
                        system_instruction=_system_instruction(),
                        # 關閉思考模式:翻譯不需要推理,關掉後回應速度大幅提升
                        thinking_config=types.ThinkingConfig(thinking_budget=0),
                        safety_settings=_SAFETY_OFF,
                        temperature=0.3,  # 翻譯任務調低,譯文更穩定
                    ),
                )
                if resp.text is None:
                    reason = ""
                    try:
                        if resp.candidates:
                            reason = str(resp.candidates[0].finish_reason)
                        elif resp.prompt_feedback:
                            reason = str(resp.prompt_feedback.block_reason)
                    except Exception:
                        pass
                    raise TranslationBlocked(reason or "模型未回傳內容")
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


def _translate_split(sentences: list[str], joiner: str, depth: int = 0) -> str:
    """翻譯一組句子;被擋就重試,重試不過就對半再切,直到單句仍被擋才保留原文"""
    chunk = joiner.join(sentences)
    last_error = None
    for _ in range(2):  # 硬性過濾的判定時好時壞,同樣內容多試常常就過
        try:
            t, _ = translate(chunk)
            return t
        except TranslationBlocked as e:
            last_error = e
            continue
        except Exception as e:
            return f"（此段翻譯失敗：{e}，以下保留原文）\n\n{chunk}"
    if len(sentences) > 1 and depth < 4:
        mid = len(sentences) // 2
        print(f"   ✂️ 段落仍被擋，對半再切（第 {depth + 1} 層）...")
        left = _translate_split(sentences[:mid], joiner, depth + 1)
        right = _translate_split(sentences[mid:], joiner, depth + 1)
        return left + "\n\n" + right
    print(f"   ⛔ 已切到最小仍被擋（{last_error}），保留原文")
    return f"（此段被安全機制擋下：{last_error}，以下保留原文）\n\n{chunk}"


def _translate_blocked_page(text: str) -> str:
    """整頁被硬性過濾擋下時的補救:切成小段分別翻譯,卡住的段落自動越切越細"""
    if OCR_LANG == "ja":
        sentences = [s for s in re.split(r"(?<=[。！？])", text) if s.strip()]
        joiner = ""
    else:
        sentences = [s for s in re.split(r"(?<=[.!?\"”]) ", text) if s.strip()]
        joiner = " "
    size = max(1, (len(sentences) + 3) // 4)  # 先約略切成 4 段
    groups = [sentences[i:i + size] for i in range(0, len(sentences), size)]

    results = []
    for idx, group in enumerate(groups, 1):
        print(f"   🧩 分段翻譯 {idx}/{len(groups)}...")
        results.append(_translate_split(group, joiner))
    return "\n\n".join(results)


# 章節邊界的辨識規則:整行只有「第X章」(或序章/尾聲等)才算章節標題行
_CHAPTER_LINE_RE = re.compile(
    r"(?m)^(第[0-9一二三四五六七八九十百千零兩两]{1,6}章|序章|尾聲|終章|プロローグ|エピローグ)\s*$"
)


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


def _already_recorded(text: str) -> bool:
    """檢查這段原文是否已經在筆記檔裡(重啟程式後避免把同一頁再翻一次)"""
    if not os.path.exists(NOTE_FILE):
        return False
    with open(NOTE_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    return text[:120] in content


def process_page(img, last_text: str):
    """OCR + 翻譯 + 寫入筆記 + 通知，回傳這次辨識到的文字（給主迴圈記錄用）"""
    lines = _detect_chapter(ocr_lines(img))
    # 英文行與行之間用空格接;日文不用空格(日文書寫沒有詞間空格)
    joiner = "" if OCR_LANG == "ja" else " "
    text = joiner.join(lines).strip()
    if not text:
        print("⚠️ 這一頁沒有辨識到文字（空白頁/圖片頁），略過")
        return None
    if text.lower().count("gemini") >= 3:
        print("⚠️ 監控範圍似乎被其他視窗（終端機/瀏覽器）遮住了，略過。請確認 Kindle 沒被遮擋")
        return None
    if len(text) < 20:
        # 章節結尾常有只剩一句話的頁面,照常翻譯,只提示一下
        print(f"ℹ️ 這一頁只有 {len(text)} 字（章節結尾的短頁很正常），照常處理")

    if text == last_text:
        print("⚠️ 內容與上一頁相同（可能是同一頁上的反白/游標閃動），略過")
        return None

    if _already_recorded(text):
        print("⚠️ 筆記檔裡已經有這一頁的內容（之前翻過），略過")
        return text

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
    except TranslationBlocked as e:
        # 整頁被硬性過濾擋下:切小段分別翻譯補救,只有觸發過濾的小段會留標記
        print(f"⚠️ 整頁翻譯被安全機制擋下（{e}），改用分段翻譯補救...")
        translated = _translate_blocked_page(text)
        append_to_note(text, translated)
        show_toast(text, translated)
        print("✅ 分段翻譯完成，已寫入筆記：")
        print("─" * 60)
        print(translated)
        print("─" * 60 + "\n")
        return text
    except Exception as e:
        print(f"⚠️ 翻譯失敗：{e}")
        print("   這一頁稍後會自動重試\n")
        return "RETRY"


def _resume_chapter_from_notes():
    """啟動時從筆記檔讀出最後一個章節標題,自動接續(重啟後不用手動改設定)"""
    global CHAPTER, CHAPTER_TITLE
    if not os.path.exists(NOTE_FILE):
        return
    with open(NOTE_FILE, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if _CHAPTER_LINE_RE.match(lines[i]):
            CHAPTER = lines[i].strip()
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            CHAPTER_TITLE = nxt  # 副標(緊接在章節編號下一行;沒有副標時該行為空)
            print(f"📑 從筆記檔接續章節:{CHAPTER} {CHAPTER_TITLE}")
            return


def main():
    print("📖 自動偵測翻頁模式已啟動")
    _resume_chapter_from_notes()
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
