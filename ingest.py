"""CSV → SQLite 取込処理。

- ingest_realtime        : 共有フォルダのCSVを読み、リアルタイムテーブルを全置換
- ingest_latest_from_shared : 共有フォルダ内の最新CSVを取り込む(15分毎ジョブ用)
- take_daily_snapshot    : 現在のリアルタイム内容をAM2:00断面として保存し、古い断面を削除
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import config
from db import _connect_rw, init_schema, set_meta


def read_status_csv(path: str | Path) -> pd.DataFrame:
    """ステータスCSVを読み込み、内部カラム名に正規化したDataFrameを返す。"""
    path = Path(path)
    df = None
    last_err: Exception | None = None
    for enc in ("utf-8-sig", "cp932", "utf-8"):
        try:
            df = pd.read_csv(path, dtype=str, encoding=enc)
            break
        except Exception as e:  # エンコーディング違いなどを順に試す
            last_err = e
    if df is None:
        raise RuntimeError(f"CSVを読み込めませんでした: {path} ({last_err})")

    df = df.rename(columns={c: config.CSV_HEADER_MAP.get(str(c).strip(), str(c).strip())
                            for c in df.columns})
    missing = [c for c in config.REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"CSVに必要な列がありません: {missing} / 検出された列: {list(df.columns)}"
        )
    return df[config.REQUIRED_COLUMNS].fillna("")


def ingest_realtime(path: str | Path) -> int:
    """CSVを読み、リアルタイムテーブルを全置換する。取り込んだ行数を返す。"""
    df = read_status_csv(path)
    now = datetime.now().isoformat(timespec="seconds")

    conn = _connect_rw()
    init_schema(conn)
    conn.execute(f"DELETE FROM {config.REALTIME_TABLE}")
    conn.executemany(
        f"INSERT INTO {config.REALTIME_TABLE}"
        f"(updated_at, building, equipment_id, chamber_id, status, ingested_at) "
        f"VALUES(?,?,?,?,?,?)",
        [(r.updated_at, r.building, r.equipment_id, r.chamber_id, r.status, now)
         for r in df.itertuples(index=False)],
    )
    set_meta(conn, "last_realtime_ingest", now)
    set_meta(conn, "last_realtime_file", str(path))
    set_meta(conn, "last_realtime_rows", len(df))
    conn.commit()
    conn.close()
    return len(df)


def ingest_latest_from_shared() -> int:
    """共有フォルダ内で最も新しいCSVを取り込む。CSVが無ければ0。"""
    files = sorted(config.SHARED_FOLDER.glob("*.csv"), key=lambda p: p.stat().st_mtime)
    if not files:
        return 0
    return ingest_realtime(files[-1])


def take_daily_snapshot(snapshot_date: str | None = None) -> int:
    """現在のリアルタイム内容を日次断面として保存し、保持日数を超えた断面を削除。

    同一日付の断面は冪等に上書きする。保存した行数を返す。
    """
    conn = _connect_rw()
    init_schema(conn)

    snap_date = snapshot_date or datetime.now().date().isoformat()
    snap_at = datetime.now().isoformat(timespec="seconds")

    conn.execute(f"DELETE FROM {config.DAILY_TABLE} WHERE snapshot_date=?", (snap_date,))
    conn.execute(
        f"""INSERT INTO {config.DAILY_TABLE}
            (snapshot_date, snapshot_at, updated_at, building, equipment_id, chamber_id, status)
            SELECT ?, ?, updated_at, building, equipment_id, chamber_id, status
            FROM {config.REALTIME_TABLE}""",
        (snap_date, snap_at),
    )
    n = conn.execute(
        f"SELECT COUNT(*) FROM {config.DAILY_TABLE} WHERE snapshot_date=?", (snap_date,)
    ).fetchone()[0]

    # 保持日数を超えた古い断面を削除(直近N日分のみ保持)
    cutoff = (datetime.now().date() - timedelta(days=config.SNAPSHOT_RETENTION_DAYS - 1)).isoformat()
    conn.execute(f"DELETE FROM {config.DAILY_TABLE} WHERE snapshot_date < ?", (cutoff,))

    set_meta(conn, "last_snapshot_date", snap_date)
    set_meta(conn, "last_snapshot_at", snap_at)
    conn.commit()
    conn.close()
    return n
