"""国民の祝日の一覧を作る（config/holidays.json）。

外部ライブラリを増やさずに済ませるため、祝日法の規則から日付を計算して
表に書き出す。年が変わったら実行し直す。

  python tools/build_holidays.py 2026 2027 2028

春分・秋分は年ごとに動くため近似式を使う（1980〜2099年で一致することが
知られている式）。振替休日（祝日が日曜なら翌平日）と国民の休日
（祝日に挟まれた平日）も規則どおり補う。
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

WEEKDAYS = "月火水木金土日"


def nth_monday(year: int, month: int, nth: int) -> date:
    """その月の第n月曜日（成人の日・海の日・敬老の日・スポーツの日）。"""
    day = date(year, month, 1)
    day += timedelta(days=(0 - day.weekday()) % 7)  # その月最初の月曜
    return day + timedelta(days=7 * (nth - 1))


def equinox(year: int, spring: bool) -> date:
    """春分日・秋分日（近似式）。"""
    base = 20.8431 if spring else 23.2488
    day = int(base + 0.242194 * (year - 1980) - (year - 1980) // 4)
    return date(year, 3 if spring else 9, day)


def fixed_holidays(year: int) -> dict[date, str]:
    return {
        date(year, 1, 1): "元日",
        nth_monday(year, 1, 2): "成人の日",
        date(year, 2, 11): "建国記念の日",
        date(year, 2, 23): "天皇誕生日",
        equinox(year, True): "春分の日",
        date(year, 4, 29): "昭和の日",
        date(year, 5, 3): "憲法記念日",
        date(year, 5, 4): "みどりの日",
        date(year, 5, 5): "こどもの日",
        nth_monday(year, 7, 3): "海の日",
        date(year, 8, 11): "山の日",
        nth_monday(year, 9, 3): "敬老の日",
        equinox(year, False): "秋分の日",
        nth_monday(year, 10, 2): "スポーツの日",
        date(year, 11, 3): "文化の日",
        date(year, 11, 23): "勤労感謝の日",
    }


def build(year: int) -> dict[date, str]:
    days = fixed_holidays(year)

    # 振替休日: 祝日が日曜なら、その後の最初の平日を休みにする
    for day in sorted(days):
        if day.weekday() != 6:  # 日曜以外はそのまま
            continue
        substitute = day + timedelta(days=1)
        while substitute in days:
            substitute += timedelta(days=1)
        days[substitute] = "振替休日"

    # 国民の休日: 祝日に挟まれた平日（9月のシルバーウィーク等）
    for day in sorted(days):
        candidate = day + timedelta(days=1)
        if (
            candidate not in days
            and candidate.weekday() < 5
            and candidate + timedelta(days=1) in days
        ):
            days[candidate] = "国民の休日"
    return days


def main() -> int:
    years = [int(a) for a in sys.argv[1:]] or [date.today().year, date.today().year + 1]
    table: dict[str, str] = {}
    for year in years:
        days = build(year)
        print(f"=== {year}年（{len(days)}日）")
        for day in sorted(days):
            print(f"  {day} ({WEEKDAYS[day.weekday()]}) {days[day]}")
            table[day.isoformat()] = days[day]

    out = Path(__file__).resolve().parents[1] / "config" / "holidays.json"
    out.write_text(
        json.dumps(
            {
                "_note": "国民の祝日。tools/build_holidays.py で作る。年が変わったら作り直す",
                "covers": [min(years), max(years)],
                "holidays": table,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"\n書き出しました: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
