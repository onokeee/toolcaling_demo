"""LLM(OpenAI互換 function calling)に渡すツール定義と実行ロジック。

ツールは3つ:
  - run_sql_query : SELECT文を実行して結果テーブルを返す
  - plot_chart    : SELECT結果をグラフ化する
  - get_schema    : スキーマ情報を返す

dispatch() の戻り値:
  {
    "ok": bool,
    "llm_content": str,        # LLMへ返すテキスト(JSON)。トークン節約のため要約。
    "render": dict | None,     # UI描画用アイテム(app.py が解釈)
  }
"""
from __future__ import annotations

import json

import config
import db

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_sql_query",
            "description": (
                "SQLite DB に対して読み取り専用の SELECT 文を実行し、結果テーブルを取得する。"
                "半導体装置のステータスを問い合わせる際に使用。SELECT(または WITH ... SELECT)以外は実行不可。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "実行する SQLite 用 SELECT 文。SELECT または WITH で始めること。",
                    },
                    "purpose": {
                        "type": "string",
                        "description": "このクエリで何を確認したいかの短い説明(日本語)。",
                    },
                },
                "required": ["sql", "purpose"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plot_chart",
            "description": (
                "SELECT 文の結果をグラフ化する。時系列の推移や分布を可視化したいときに使用。"
                "内部で SELECT を実行し、指定の x / y / color 列でグラフを描画する。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "グラフ用データを取得する SELECT 文。"},
                    "chart_type": {
                        "type": "string",
                        "enum": ["line", "bar", "area", "pie", "scatter"],
                        "description": "グラフ種別。時系列=line、分布=bar/pie。",
                    },
                    "x": {"type": "string", "description": "x軸(pieの場合はカテゴリ)に使う列名。"},
                    "y": {"type": "string", "description": "y軸(pieの場合は値)に使う列名。"},
                    "color": {"type": "string", "description": "系列(色分け)に使う列名。任意。"},
                    "barmode": {
                        "type": "string",
                        "enum": ["group", "stack", "relative"],
                        "description": "棒グラフの積み方。積み上げ=stack、横並び比較=group(既定)。bar以外では無視。",
                    },
                    "title": {"type": "string", "description": "グラフのタイトル。"},
                    "purpose": {"type": "string", "description": "このグラフで示したいことの短い説明。"},
                },
                "required": ["sql", "chart_type", "x", "y", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plot_dual_axis",
            "description": (
                "棒グラフ(左軸)と折れ線グラフ(右軸)を組み合わせた2軸グラフを描く。"
                "件数(棒)と比率・稼働率など単位の異なる指標(折れ線)を同時に見せたいときに使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "x列と数値の指標列を返す SELECT 文(GROUP BYで集計)。"},
                    "x": {"type": "string", "description": "x軸に使う列名。"},
                    "bar_y": {
                        "type": "array", "items": {"type": "string"},
                        "description": "左軸に棒で表示する数値列名のリスト(1つ以上)。例: 件数。",
                    },
                    "line_y": {
                        "type": "array", "items": {"type": "string"},
                        "description": "右軸に折れ線で表示する数値列名のリスト(1つ以上)。例: 稼働率(%)。",
                    },
                    "left_title": {"type": "string", "description": "左軸のラベル(任意)。"},
                    "right_title": {"type": "string", "description": "右軸のラベル(任意)。"},
                    "title": {"type": "string", "description": "グラフのタイトル。"},
                    "purpose": {"type": "string", "description": "このグラフで示したいことの短い説明。"},
                },
                "required": ["sql", "x", "bar_y", "line_y", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_schema",
            "description": "DBのテーブル構成・列・建屋やステータスの種類・データの日付範囲などスキーマ情報を取得する。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _run_sql_query(args: dict) -> dict:
    sql = args.get("sql", "")
    try:
        columns, rows, truncated = db.run_select(sql)
    except Exception as e:
        return {
            "ok": False,
            "llm_content": _json({"error": str(e)}),
            "render": {"role": "assistant", "kind": "error", "message": f"SQL実行エラー: {e}"},
        }

    sample = rows[: config.SAMPLE_ROWS_FOR_LLM]
    llm_content = _json({
        "columns": columns,
        "row_count": len(rows),
        "truncated": truncated,
        "rows": [list(r) for r in sample],
        "note": (f"全{len(rows)}行中 先頭{len(sample)}行を表示" if len(rows) > len(sample) else None),
    })
    return {
        "ok": True,
        "llm_content": llm_content,
        "render": {
            "role": "assistant", "kind": "table",
            "columns": columns, "rows": rows, "truncated": truncated,
        },
    }


def _plot_chart(args: dict) -> dict:
    sql = args.get("sql", "")
    try:
        columns, rows, truncated = db.run_select(sql)
    except Exception as e:
        return {
            "ok": False,
            "llm_content": _json({"error": str(e)}),
            "render": {"role": "assistant", "kind": "error", "message": f"グラフ用SQLの実行エラー: {e}"},
        }

    x, y = args.get("x"), args.get("y")
    for col in (x, y):
        if col and col not in columns:
            msg = f"指定列 '{col}' が結果に存在しません。利用可能な列: {columns}"
            return {
                "ok": False,
                "llm_content": _json({"error": msg}),
                "render": {"role": "assistant", "kind": "error", "message": msg},
            }

    return {
        "ok": True,
        "llm_content": _json({
            "status": "chart_rendered",
            "chart_type": args.get("chart_type"),
            "columns": columns,
            "row_count": len(rows),
        }),
        "render": {
            "role": "assistant", "kind": "chart",
            "columns": columns, "rows": rows,
            "chart_type": args.get("chart_type", "bar"),
            "x": x, "y": y, "color": args.get("color"),
            "barmode": args.get("barmode"),
            "title": args.get("title", ""),
        },
    }


def _plot_dual_axis(args: dict) -> dict:
    sql = args.get("sql", "")
    try:
        columns, rows, truncated = db.run_select(sql)
    except Exception as e:
        return {
            "ok": False,
            "llm_content": _json({"error": str(e)}),
            "render": {"role": "assistant", "kind": "error", "message": f"2軸グラフ用SQLの実行エラー: {e}"},
        }

    x = args.get("x")
    bar_y = args.get("bar_y") or []
    line_y = args.get("line_y") or []
    needed = [x] + list(bar_y) + list(line_y)
    missing = [c for c in needed if c and c not in columns]
    if missing or not bar_y or not line_y:
        msg = (f"指定列が結果に存在しません: {missing} / 利用可能な列: {columns}"
               if missing else "bar_y と line_y にはそれぞれ1つ以上の数値列を指定してください。")
        return {
            "ok": False,
            "llm_content": _json({"error": msg}),
            "render": {"role": "assistant", "kind": "error", "message": msg},
        }

    return {
        "ok": True,
        "llm_content": _json({
            "status": "dual_axis_chart_rendered",
            "columns": columns, "row_count": len(rows),
            "bar_y": bar_y, "line_y": line_y,
        }),
        "render": {
            "role": "assistant", "kind": "chart_dual",
            "columns": columns, "rows": rows,
            "x": x, "bar_y": bar_y, "line_y": line_y,
            "left_title": args.get("left_title"), "right_title": args.get("right_title"),
            "title": args.get("title", ""),
        },
    }


def _get_schema(_args: dict) -> dict:
    text = db.get_schema_text()
    return {"ok": True, "llm_content": text, "render": None}


_HANDLERS = {
    "run_sql_query": _run_sql_query,
    "plot_chart": _plot_chart,
    "plot_dual_axis": _plot_dual_axis,
    "get_schema": _get_schema,
}


def dispatch(name: str, arguments_json: str | None) -> dict:
    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError as e:
        return {"ok": False, "llm_content": _json({"error": f"引数のJSON解析に失敗: {e}"}),
                "render": {"role": "assistant", "kind": "error", "message": f"ツール引数の解析失敗: {e}"}}

    handler = _HANDLERS.get(name)
    if not handler:
        return {"ok": False, "llm_content": _json({"error": f"未知のツール: {name}"}), "render": None}
    return handler(args)
