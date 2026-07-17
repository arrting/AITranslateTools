"""
座標校準工具
------------
用途：找出你 Kindle 閱讀區域的固定座標，設定給 fixed_region_watcher.py 使用。

使用前準備：
  pip install pyautogui

使用方式：
  python find_coordinates.py
  依照終端機提示，把滑鼠移到指定角落，等待倒數結束會自動印出座標。
"""

import pyautogui
import time


def countdown_and_capture(label: str, seconds: int = 5):
    print(f"\n請在 {seconds} 秒內，把滑鼠移到「{label}」")
    for i in range(seconds, 0, -1):
        print(f"  {i}...", end="\r")
        time.sleep(1)
    pos = pyautogui.position()
    print(f"✅ {label} 座標：{pos}                    ")
    return pos


def main():
    print("=== Kindle 閱讀區域座標校準 ===")
    print("請先把 Kindle 視窗開好、調整到你平常閱讀習慣的大小跟位置。")

    top_left = countdown_and_capture("文字內容區塊的【左上角】")
    bottom_right = countdown_and_capture("文字內容區塊的【右下角】")

    print("\n=== 結果 ===")
    print(f"REGION = ({top_left.x}, {top_left.y}, {bottom_right.x}, {bottom_right.y})")
    print("\n把上面這行 REGION 座標複製到 fixed_region_watcher.py 的設定區即可。")


if __name__ == "__main__":
    main()
