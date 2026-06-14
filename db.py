"""SQLite アクセス層。

最重要: ユーザー(LLM)が生成したSQLは SELECT のみ 実行を許可する。
多層防御で守る:
  1. 構文チェック   : 単一ステートメント / SELECT・WITH で始まる / 書込キーワード禁止
  2. 読み取り専用接続: file:...?mode=ro でそもそも書込不可
  3. オーソライザ    : SQLite の authorizer で SELECT/READ 以外を DENY
  4. タイムアウト    : progress handler で暴走クエリを中断
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

import config

# --- 接続ヘルパ -------------------------------------------------------------

def _connect_rw() -> sqlite3.Connection:
    """読み書き可能な接続(取込・スキーマ作成用)。"""
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _connect_ro() -> sqlite3.Connection:
    """読み取り専用接続(ユーザークエリ実行用)。書込はOS/SQLiteレベルで拒否される。"""
    uri = Path(config.DB_PATH).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


# --- スキーマ ---------------------------------------------------------------

def init_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {config.REALTIME_TABLE} (
            updated_at   TEXT,   -- 更新日時(CSVの値)
            building     TEXT,   -- 建屋
            equipment_id TEXT,   -- 装置ID
            chamber_id   TEXT,   -- チャンバーID
            status       TEXT,   -- ステータス
            ingested_at  TEXT    -- このDBへ取り込んだ時刻
        )
        """
    )
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {config.DAILY_TABLE} (
            snapshot_date TEXT,  -- 断面日付 (YYYY-MM-DD, AM2:00時点)
            snapshot_at   TEXT,  -- 断面を取得した実時刻
            updated_at    TEXT,
            building      TEXT,
            equipment_id  TEXT,
            chamber_id    TEXT,
            status        TEXT
        )
        """
    )
    cur.execute(
        f"CREATE TABLE IF NOT EXISTS {config.META_TABLE} (key TEXT PRIMARY KEY, value TEXT)"
    )
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_rt_building ON {config.REALTIME_TABLE}(building)")
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_rt_status   ON {config.REALTIME_TABLE}(status)")
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_rt_eqid     ON {config.REALTIME_TABLE}(equipment_id)")
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_d_date      ON {config.DAILY_TABLE}(snapshot_date)")
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_d_status    ON {config.DAILY_TABLE}(status)")
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_d_eqid      ON {config.DAILY_TABLE}(equipment_id)")
    conn.commit()


def ensure_db() -> None:
    conn = _connect_rw()
    init_schema(conn)
    conn.close()


def is_empty() -> bool:
    """リアルタイムテーブルが空かどうか(サンプル投入要否の判定に使用)。"""
    try:
        conn = _connect_rw()
        init_schema(conn)
        n = conn.execute(f"SELECT COUNT(*) FROM {config.REALTIME_TABLE}").fetchone()[0]
        conn.close()
        return n == 0
    except Exception:
        return True


# --- メタ情報 ---------------------------------------------------------------

def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        f"INSERT INTO {config.META_TABLE}(key, value) VALUES(?, ?) "
        f"ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(f"SELECT value FROM {config.META_TABLE} WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


# --- SELECT 専用ガード ------------------------------------------------------

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|truncate|attach|detach|"
    r"reindex|vacuum|pragma|grant|revoke|begin|commit|rollback|savepoint|merge)\b",
    re.IGNORECASE,
)


def _strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)          # 行コメント
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)  # ブロックコメント
    return sql


def validate_select(sql: str) -> str:
    """SELECT文として安全か検証し、整形済みSQLを返す。問題があれば ValueError。"""
    if not sql or not sql.strip():
        raise ValueError("SQLが空です。")
    cleaned = _strip_sql_comments(sql).strip().rstrip(";").strip()
    if not cleaned:
        raise ValueError("実行可能なSQLがありません。")
    if ";" in cleaned:
        raise ValueError("複数ステートメントは実行できません(SELECT文を1つだけ指定してください)。")
    low = cleaned.lower()
    if not (low.startswith("select") or low.startswith("with")):
        raise ValueError("SELECT文(または WITH ... SELECT)のみ実行できます。")
    m = _FORBIDDEN.search(cleaned)
    if m:
        raise ValueError(f"書き込み・DDL系のキーワード '{m.group(0)}' は使用できません。読み取り専用です。")
    return cleaned


# SQLite オーソライザで許可するアクション
_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
for _name in ("SQLITE_RECURSIVE",):  # 環境によって存在しない場合がある
    if hasattr(sqlite3, _name):
        _ALLOWED_ACTIONS.add(getattr(sqlite3, _name))


def _authorizer(action, arg1, arg2, db_name, trigger):
    if action in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def run_select(sql: str, max_rows: int | None = None, timeout_s: int | None = None):
    """検証済みSELECTを読み取り専用接続で実行し (columns, rows, truncated) を返す。"""
    safe_sql = validate_select(sql)
    max_rows = max_rows or config.MAX_RESULT_ROWS
    timeout_s = timeout_s or config.QUERY_TIMEOUT_SEC

    conn = _connect_ro()
    try:
        conn.set_authorizer(_authorizer)
        start = time.time()
        conn.set_progress_handler(lambda: 1 if (time.time() - start) > timeout_s else 0, 10000)
        cur = conn.execute(safe_sql)  # 単一ステートメントのみ実行可能
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        rows = [tuple(r) for r in rows[:max_rows]]
        return columns, rows, truncated
    finally:
        conn.close()


# --- スキーマ説明(LLM/UI用) ------------------------------------------------

def get_schema_text() -> str:
    """LLMのsystem promptとUIに渡す、データ概要つきスキーマ説明を作る。"""
    conn = _connect_rw()
    init_schema(conn)

    def q(sql, params=()):
        return conn.execute(sql, params).fetchall()

    rt_count = q(f"SELECT COUNT(*) FROM {config.REALTIME_TABLE}")[0][0]
    buildings = [r[0] for r in q(f"SELECT DISTINCT building FROM {config.REALTIME_TABLE} ORDER BY building") if r[0] is not None]
    statuses = [r[0] for r in q(f"SELECT DISTINCT status FROM {config.REALTIME_TABLE} ORDER BY status") if r[0] is not None]
    rt_min, rt_max = (q(f"SELECT MIN(updated_at), MAX(updated_at) FROM {config.REALTIME_TABLE}")[0]
                      if rt_count else (None, None))
    daily_dates = [r[0] for r in q(f"SELECT DISTINCT snapshot_date FROM {config.DAILY_TABLE} ORDER BY snapshot_date")]
    last_rt = get_meta(conn, "last_realtime_ingest")
    conn.close()

    status_legend = ", ".join(f"{s}({config.STATUS_LABELS.get(s, s)})" for s in (statuses or config.STATUS_VALUES))
    daily_range = f"{daily_dates[0]} 〜 {daily_dates[-1]}（{len(daily_dates)}日分）" if daily_dates else "（データなし）"

    return f"""# データベース概要（SQLite）

半導体工場の装置ステータス一覧。1行 = 1チャンバーの状態。

## テーブル1: {config.REALTIME_TABLE} （現在のステータス / 最新CSV）
15分毎に共有フォルダのCSVで全置換される「今の状態」。「現在」「今」「直近」を聞かれたらこちら。
- updated_at   TEXT  : 更新日時
- building     TEXT  : 建屋
- equipment_id TEXT  : 装置ID
- chamber_id   TEXT  : チャンバーID
- status       TEXT  : ステータス
- ingested_at  TEXT  : DB取込時刻
行数: {rt_count} / updated_at範囲: {rt_min} 〜 {rt_max} / 最終取込: {last_rt}

## テーブル2: {config.DAILY_TABLE} （日次断面 / 直近{config.SNAPSHOT_RETENTION_DAYS}日）
毎日AM2:00時点のスナップショットを日付ごとに保持。「推移」「変化」「トレンド」「過去◯日」「{config.SNAPSHOT_RETENTION_DAYS}日間」など
時系列の問いはこちら。snapshot_date でグループ化して日次比較する。
- snapshot_date TEXT : 断面日付 (YYYY-MM-DD)
- snapshot_at   TEXT : 断面取得時刻
- updated_at, building, equipment_id, chamber_id, status : 上と同じ
収録日: {daily_range}

## 値の凡例
- 建屋: {", ".join(buildings) if buildings else "（データなし）"}
- ステータス: {status_legend}

## SQL作成のヒント
- SQLite方言。日付比較は date('now','-7 day') など。snapshot_date は 'YYYY-MM-DD' 文字列。
- 集計は GROUP BY を使い、列に AS で分かりやすい別名を付ける。
- 「稼働率」=  status='RUN' の件数 / 全件数。CASE/AVG等で算出可。
- 装置単位は equipment_id、チャンバー単位は (equipment_id, chamber_id)。
"""


if __name__ == "__main__":
    # SELECT専用ガードのセルフテスト
    ensure_db()
    ok_cases = [
        "SELECT 1",
        "select status, count(*) from equipment_status_realtime group by status",
        "WITH t AS (SELECT 1 AS a) SELECT a FROM t",
    ]
    ng_cases = [
        "DELETE FROM equipment_status_realtime",
        "DROP TABLE equipment_status_realtime",
        "UPDATE equipment_status_realtime SET status='X'",
        "SELECT 1; DELETE FROM equipment_status_realtime",
        "INSERT INTO equipment_status_realtime VALUES(1)",
        "PRAGMA table_info(equipment_status_realtime)",
    ]
    for s in ok_cases:
        validate_select(s)
        print("OK ", s)
    for s in ng_cases:
        try:
            validate_select(s)
            print("!! ガードすり抜け:", s)
        except ValueError as e:
            print("BLOCK", s, "=>", e)
