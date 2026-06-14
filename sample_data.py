"""デモ用のサンプルデータ生成。

- 共有フォルダに「現在のステータス」CSVを1本書き出し、リアルタイムテーブルへ取込
- 日次断面テーブルに直近14日分を投入(時系列で変化が見えるよう作為的な事象を仕込む)

仕込んだ傾向(質問のデモ用):
  * 装置 ETCH-A01 は -5〜-2日前まで DOWN が続き、その後復旧
  * C棟 は直近3日ほど DOWN/PM が増加(メンテ集中)
"""
from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta

import config
from db import _connect_rw, init_schema, set_meta

# 建屋ごとの装置タイプと台数
_BUILDINGS = {
    "A棟": {"ETCH": 2, "CVD": 2, "CMP": 1},
    "B棟": {"LITHO": 2, "IMP": 1, "DIFF": 1},
    "C棟": {"ETCH": 1, "CVD": 1, "CMP": 1, "CLEAN": 1},
}
# 装置タイプごとのチャンバー数
_CHAMBERS = {"ETCH": 3, "CVD": 2, "CMP": 2, "LITHO": 1, "IMP": 2, "DIFF": 4, "CLEAN": 1}


def _build_fleet() -> list[tuple[str, str, str]]:
    """(建屋, 装置ID, チャンバーID) のリストを構築する。"""
    fleet: list[tuple[str, str, str]] = []
    for building, types in _BUILDINGS.items():
        letter = building[0]  # 'A棟' -> 'A'
        for typ, count in types.items():
            for n in range(1, count + 1):
                eqid = f"{typ}-{letter}{n:02d}"
                for c in range(1, _CHAMBERS[typ] + 1):
                    fleet.append((building, eqid, f"CH{c}"))
    return fleet


def _status_for(building: str, eqid: str, chamber: str, day_offset: int) -> str:
    """指定日(day_offset<=0)のステータスを決める。再現性のためseed固定。"""
    rng = random.Random(f"{eqid}|{chamber}|{day_offset}")
    weights = {"RUN": 60, "IDLE": 15, "SETUP": 5, "PM": 8, "DOWN": 7, "ENG": 5}

    # 仕込み1: ETCH-A01 が数日間 DOWN
    if eqid == "ETCH-A01" and day_offset in (-5, -4, -3, -2):
        return "DOWN"
    # 仕込み2: C棟が直近3日でメンテ集中
    if building == "C棟" and day_offset in (-3, -2, -1, 0):
        weights["DOWN"] += 18
        weights["PM"] += 12

    items = list(weights.items())
    total = sum(w for _, w in items)
    r = rng.uniform(0, total)
    acc = 0
    for s, w in items:
        acc += w
        if r <= acc:
            return s
    return items[-1][0]


def generate_sample() -> None:
    fleet = _build_fleet()
    now = datetime.now()

    # --- 1) 現在のステータスCSVを共有フォルダへ出力 -------------------------
    csv_path = config.SHARED_FOLDER / f"status_{now:%Y%m%d_%H%M}.csv"
    rng = random.Random("realtime")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["更新日時", "建屋", "装置ID", "チャンバーID", "ステータス"])
        for building, eqid, chamber in fleet:
            updated = (now - timedelta(minutes=rng.randint(0, 14))).isoformat(timespec="seconds")
            w.writerow([updated, building, eqid, chamber, _status_for(building, eqid, chamber, 0)])

    # CSV → リアルタイムテーブル
    from ingest import ingest_realtime
    ingest_realtime(csv_path)

    # --- 2) 日次断面(直近14日)を投入 -------------------------------------
    conn = _connect_rw()
    init_schema(conn)
    conn.execute(f"DELETE FROM {config.DAILY_TABLE}")
    today = now.date()
    for off in range(config.SNAPSHOT_RETENTION_DAYS - 1, -1, -1):  # 13日前 〜 当日
        d = today - timedelta(days=off)
        snap_date = d.isoformat()
        snap_at = datetime(d.year, d.month, d.day, 2, 0, 0).isoformat()
        updated = datetime(d.year, d.month, d.day, 1, 55, 0).isoformat()
        rows = [
            (snap_date, snap_at, updated, building, eqid, chamber,
             _status_for(building, eqid, chamber, -off))
            for building, eqid, chamber in fleet
        ]
        conn.executemany(
            f"""INSERT INTO {config.DAILY_TABLE}
                (snapshot_date, snapshot_at, updated_at, building, equipment_id, chamber_id, status)
                VALUES(?,?,?,?,?,?,?)""",
            rows,
        )
    set_meta(conn, "last_snapshot_date", today.isoformat())
    set_meta(conn, "last_snapshot_at", datetime(today.year, today.month, today.day, 2, 0).isoformat())
    conn.commit()
    conn.close()
    print(f"サンプル生成完了: {len(fleet)} チャンバー / CSV={csv_path}")


if __name__ == "__main__":
    generate_sample()
