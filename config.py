"""アプリ全体の設定値。環境変数(.env)から読み込む。"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# --- パス設定 ---------------------------------------------------------------
DATA_DIR = BASE_DIR / "data"
# 共有フォルダ(15分毎に自動更新されるCSVが置かれる想定)。デモでは data/shared を使用。
SHARED_FOLDER = Path(os.getenv("SHARED_FOLDER", str(DATA_DIR / "shared")))
# 自前のSQLite DB
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "factory.db")))

# --- OpenAI / OpenAI互換API の設定 -----------------------------------------
# OpenAI SDK は base_url の末尾に "/chat/completions" を付けて呼ぶ。
# 公式OpenAIなら base_url は .../v1。/v1 以外のパスのOpenAI互換エンドポイントを使う
# 場合は、そのパスまで（末尾の /chat/completions は付けない）で設定する。
# 認証は Authorization: Bearer <API_KEY>（SDKが api_key から自動付与）。
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

# 生成パラメータ（仕様上いずれも任意）。SQL生成の安定のため temperature は既定0。
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0") or 0)
_top_p = os.getenv("OPENAI_TOP_P")
OPENAI_TOP_P = float(_top_p) if _top_p else None
_max_tokens = os.getenv("OPENAI_MAX_TOKENS")
OPENAI_MAX_TOKENS = int(_max_tokens) if _max_tokens else None

# --- テーブル定義 -----------------------------------------------------------
REALTIME_TABLE = "equipment_status_realtime"   # 最新CSV(現在のステータス)
DAILY_TABLE = "equipment_status_daily"          # 毎日AM2:00の断面(直近14日)
META_TABLE = "ingest_meta"

SNAPSHOT_RETENTION_DAYS = 14   # 日次断面の保持日数
MAX_RESULT_ROWS = 2000         # 1クエリで取得・表示する最大行数
QUERY_TIMEOUT_SEC = 10         # クエリのタイムアウト(秒)
SAMPLE_ROWS_FOR_LLM = 40       # LLMへ返すサンプル行数(トークン節約)
MAX_AGENT_STEPS = 6            # ツール呼び出しの最大反復回数

# --- ドメイン定数 -----------------------------------------------------------
# 半導体装置のステータス(SEMI E10的な状態)
STATUS_VALUES = ["RUN", "IDLE", "SETUP", "PM", "DOWN", "ENG"]
STATUS_LABELS = {
    "RUN": "稼働中",
    "IDLE": "待機",
    "SETUP": "段取り",
    "PM": "予防保全",
    "DOWN": "停止/故障",
    "ENG": "エンジニアリング",
}

# 内部カラム名(DB側の列名)。CSVヘッダはこの5種へ変換できれば取込可能。
REQUIRED_COLUMNS = ["updated_at", "building", "equipment_id", "chamber_id", "status"]

# CSVヘッダ → 内部カラム名 の既定対応。
# 仕様の日本語ヘッダと、英語の内部列名をそのまま許容する。
_DEFAULT_CSV_HEADER_MAP = {
    "更新日時": "updated_at",
    "建屋": "building",
    "装置ID": "equipment_id",
    "チャンバーID": "chamber_id",
    "ステータス": "status",
    "updated_at": "updated_at",
    "building": "building",
    "equipment_id": "equipment_id",
    "chamber_id": "chamber_id",
    "status": "status",
}

# 本番CSVのヘッダ名が既定と違う場合に、後から(コードを触らず)追加できる上書き設定。
#   方法1: JSONファイル   … 既定 csv_mapping.json (環境変数 CSV_HEADER_MAP_FILE でパス変更可)
#   方法2: 環境変数(.env) … CSV_HEADER_MAP_JSON にインラインJSON (ファイルより優先)
# いずれも {"CSVのヘッダ名": "内部列名"} 形式。内部列名は REQUIRED_COLUMNS の5種。
CSV_HEADER_MAP_FILE = Path(os.getenv("CSV_HEADER_MAP_FILE", str(BASE_DIR / "csv_mapping.json")))


def _load_header_overrides() -> dict:
    overrides: dict[str, str] = {}
    if CSV_HEADER_MAP_FILE.exists():
        try:
            data = json.loads(CSV_HEADER_MAP_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                overrides.update({str(k).strip(): str(v).strip() for k, v in data.items()})
        except Exception as e:
            print(f"[config] CSVヘッダ設定ファイルを読めませんでした: {CSV_HEADER_MAP_FILE} ({e})")
    env_json = os.getenv("CSV_HEADER_MAP_JSON", "").strip()
    if env_json:
        try:
            data = json.loads(env_json)
            if isinstance(data, dict):
                overrides.update({str(k).strip(): str(v).strip() for k, v in data.items()})
        except Exception as e:
            print(f"[config] CSV_HEADER_MAP_JSON を解析できませんでした: {e}")
    bad = {k: v for k, v in overrides.items() if v not in REQUIRED_COLUMNS}
    if bad:
        print(f"[config] 注意: 内部列名でない上書き値があります {bad} / 有効値: {REQUIRED_COLUMNS}")
    return overrides


# 既定 + 上書き(上書きが優先)。プロセス起動時に1回構築される。
CSV_HEADER_MAP = {**_DEFAULT_CSV_HEADER_MAP, **_load_header_overrides()}

# 起動時にデータ用フォルダを用意
DATA_DIR.mkdir(parents=True, exist_ok=True)
SHARED_FOLDER.mkdir(parents=True, exist_ok=True)
