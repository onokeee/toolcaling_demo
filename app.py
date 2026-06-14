"""Streamlit チャットアプリ本体。

フロー:
  ユーザー入力 → LLM呼び出し → (ツール呼び出しがあれば)生成SQLを表示 →
  SELECT実行 → 結果テーブル/グラフ描画 →
  ツール結果をLLMへ返し最終回答(自然言語) を生成。
"""
from __future__ import annotations

import json

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components
from plotly.subplots import make_subplots

import config
import db
import llm
import tools

st.set_page_config(page_title="装置ステータス アシスタント", page_icon="🏭", layout="wide")

TOOL_LABELS = {
    "run_sql_query": "SQL実行 (SELECT)",
    "plot_chart": "グラフ描画",
    "plot_dual_axis": "2軸グラフ描画 (棒+折れ線)",
    "get_schema": "スキーマ取得",
}


# --- 初期化 -----------------------------------------------------------------

def _bootstrap():
    db.ensure_db()
    if db.is_empty():
        import sample_data
        sample_data.generate_sample()


def _init_state():
    if st.session_state.get("booted"):
        return
    _bootstrap()
    st.session_state.booted = True
    st.session_state.messages = [{"role": "system", "content": llm.build_system_prompt()}]
    st.session_state.render_log = []        # UI描画用アイテムの並び
    st.session_state.pending = None          # 承認待ちのツール呼び出し
    st.session_state.approval_mode = False   # 実行前承認(Human-in-the-loop)
    st.session_state.uid = 0
    st.session_state.queued_input = None
    st.session_state.scroll_to_bottom = False  # 新規返答時に最下部へ自動スクロール


def _new_uid() -> int:
    st.session_state.uid += 1
    return st.session_state.uid


# --- LLMメッセージ変換 ------------------------------------------------------

def _msg_to_dict(m) -> dict:
    d = {"role": "assistant", "content": m.content}
    if m.tool_calls:
        d["tool_calls"] = [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in m.tool_calls
        ]
    return d


def _extract_calls(m) -> list[dict]:
    if not m.tool_calls:
        return []
    return [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
            for tc in m.tool_calls]


# --- ツール実行 -------------------------------------------------------------

def _execute_calls(calls: list[dict]):
    for c in calls:
        res = tools.dispatch(c["name"], c["arguments"])
        st.session_state.messages.append(
            {"role": "tool", "tool_call_id": c["id"], "content": res["llm_content"]}
        )
        if res.get("render"):
            item = dict(res["render"])
            item["id"] = _new_uid()
            st.session_state.render_log.append(item)


def _append_sql_previews(calls: list[dict]):
    """ツール実行『前』に、生成SQLをチャットへ表示する。"""
    for c in calls:
        try:
            args = json.loads(c["arguments"]) if c["arguments"] else {}
        except json.JSONDecodeError:
            args = {}
        if "sql" in args:
            st.session_state.render_log.append({
                "role": "assistant", "kind": "sql", "id": _new_uid(),
                "tool": c["name"], "sql": args["sql"], "purpose": args.get("purpose", ""),
            })


# --- エージェントループ -----------------------------------------------------

def _advance():
    """最終回答が出るまで、LLM呼び出しとツール実行を繰り返す。"""
    for _ in range(config.MAX_AGENT_STEPS):
        try:
            with st.spinner("LLMが考え中..."):
                msg = llm.chat(st.session_state.messages)
        except Exception as e:
            st.session_state.render_log.append(
                {"role": "assistant", "kind": "error", "id": _new_uid(),
                 "message": f"LLM呼び出しに失敗しました: {e}"}
            )
            return

        st.session_state.messages.append(_msg_to_dict(msg))
        if msg.content:
            st.session_state.render_log.append(
                {"role": "assistant", "kind": "text", "id": _new_uid(), "content": msg.content}
            )

        calls = _extract_calls(msg)
        if not calls:
            return  # 最終回答

        _append_sql_previews(calls)  # 実行前にSQLを提示

        if st.session_state.approval_mode:
            st.session_state.pending = calls   # 承認待ちにして抜ける(再描画でボタン表示)
            return

        with st.spinner("SQLを実行中..."):
            _execute_calls(calls)
    # 反復上限
    st.session_state.render_log.append(
        {"role": "assistant", "kind": "text", "id": _new_uid(),
         "content": "(ツール呼び出しの上限に達しました。質問を分けてお試しください。)"}
    )


def _handle_user(text: str):
    st.session_state.messages.append({"role": "user", "content": text})
    st.session_state.render_log.append(
        {"role": "user", "kind": "text", "id": _new_uid(), "content": text}
    )
    _advance()
    st.session_state.scroll_to_bottom = True


def _approve_pending():
    """承認待ちのSQLを実行し、エージェントループを継続する。"""
    calls = st.session_state.pending
    st.session_state.pending = None
    with st.spinner("SQLを実行中..."):
        _execute_calls(calls)
    _advance()
    st.session_state.scroll_to_bottom = True


def _reject_pending():
    """承認待ちのSQLを却下し、その旨をLLMへ返してループを継続する。"""
    calls = st.session_state.pending
    st.session_state.pending = None
    for c in calls:
        st.session_state.messages.append(
            {"role": "tool", "tool_call_id": c["id"],
             "content": json.dumps({"error": "ユーザーが実行を却下しました。"}, ensure_ascii=False)}
        )
    st.session_state.render_log.append(
        {"role": "assistant", "kind": "error", "id": _new_uid(), "message": "SQL実行を却下しました。"}
    )
    _advance()
    st.session_state.scroll_to_bottom = True


# --- 描画 -------------------------------------------------------------------

def _build_fig(item: dict):
    df = pd.DataFrame(item["rows"], columns=item["columns"])
    x, y, color = item.get("x"), item.get("y"), item.get("color")
    title, ct = item.get("title", ""), item.get("chart_type", "bar")
    if y in df.columns:
        df[y] = pd.to_numeric(df[y], errors="coerce").fillna(df[y])
    color = color if (color and color in df.columns) else None
    if ct == "line":
        return px.line(df, x=x, y=y, color=color, title=title, markers=True)
    if ct == "area":
        return px.area(df, x=x, y=y, color=color, title=title)
    if ct == "pie":
        return px.pie(df, names=x, values=y, title=title)
    if ct == "scatter":
        return px.scatter(df, x=x, y=y, color=color, title=title)
    return px.bar(df, x=x, y=y, color=color, title=title,
                  barmode=(item.get("barmode") or "group"))


def _build_dual_fig(item: dict):
    """棒(左軸)+折れ線(右軸)の2軸グラフを graph_objects で生成する。"""
    df = pd.DataFrame(item["rows"], columns=item["columns"])
    x = item["x"]
    bar_y = item.get("bar_y") or []
    line_y = item.get("line_y") or []
    for col in bar_y + line_y:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    for col in bar_y:
        fig.add_trace(go.Bar(x=df[x], y=df[col], name=col), secondary_y=False)
    for col in line_y:
        fig.add_trace(
            go.Scatter(x=df[x], y=df[col], name=col, mode="lines+markers"),
            secondary_y=True,
        )
    fig.update_layout(title=item.get("title", ""), barmode="group",
                      legend=dict(orientation="h", yanchor="bottom", y=1.02))
    fig.update_xaxes(title_text=x)
    fig.update_yaxes(title_text=item.get("left_title") or "（左軸）", secondary_y=False)
    fig.update_yaxes(title_text=item.get("right_title") or "（右軸）", secondary_y=True)
    return fig


def _render_item(item: dict):
    kind = item.get("kind")
    if kind == "text":
        st.markdown(item["content"])
    elif kind == "sql":
        label = TOOL_LABELS.get(item["tool"], item["tool"])
        head = f"**🛠 {label}**"
        if item.get("purpose"):
            head += f" — {item['purpose']}"
        st.markdown(head)
        st.code(item["sql"], language="sql")
    elif kind == "table":
        df = pd.DataFrame(item["rows"], columns=item["columns"])
        st.dataframe(df, use_container_width=True, hide_index=True)
        cap = f"{len(df)} 行"
        if item.get("truncated"):
            cap += f"（上限 {config.MAX_RESULT_ROWS} 行で切り詰め）"
        st.caption(cap)
    elif kind == "chart":
        try:
            st.plotly_chart(_build_fig(item), use_container_width=True, key=f"chart_{item['id']}")
        except Exception as e:
            st.error(f"グラフ描画エラー: {e}")
    elif kind == "chart_dual":
        try:
            st.plotly_chart(_build_dual_fig(item), use_container_width=True, key=f"chart_{item['id']}")
        except Exception as e:
            st.error(f"2軸グラフ描画エラー: {e}")
    elif kind == "error":
        st.error(item["message"])


def _render_history():
    prev_role = None
    box = None
    for item in st.session_state.render_log:
        role = item["role"]
        if role != prev_role:
            box = st.chat_message(role, avatar=("🧑‍🔧" if role == "user" else "🏭"))
            prev_role = role
        with box:
            _render_item(item)


def _scroll_to_bottom(token):
    """親フレーム(チャット本体)を最下部までスクロールする。token を変えて毎回再実行させる。"""
    components.html(
        f"""
        <script>
        (function() {{
            // token={token}
            const doc = window.parent.document;
            function toBottom() {{
                // スクロール対象になり得る要素を全て最下部へ(取りこぼし防止)
                const sels = ['section.main', '[data-testid="stMain"]',
                              '[data-testid="stAppViewContainer"]'];
                for (const s of sels) {{
                    const el = doc.querySelector(s);
                    if (el) {{ el.scrollTop = el.scrollHeight; }}
                }}
                if (doc.scrollingElement) {{
                    doc.scrollingElement.scrollTop = doc.scrollingElement.scrollHeight;
                }}
                try {{ window.parent.scrollTo(0, doc.body.scrollHeight); }} catch (e) {{}}
            }}
            // 描画完了を待って数回実行する
            toBottom();
            [100, 300, 600].forEach(function(ms) {{ setTimeout(toBottom, ms); }});
        }})();
        </script>
        """,
        height=0,
    )


# --- メイン -----------------------------------------------------------------

_EXAMPLES = [
    "今、各建屋ごとにステータスの内訳を教えて",
    "現在DOWNしている装置を一覧で出して",
    "建屋ごとの稼働率(RUN比率)を棒グラフで",
    "直近14日のDOWN件数の推移を折れ線グラフで見せて",
    "ETCH-A01 のこの2週間のステータス変化を教えて",
]


def main():
    _init_state()

    st.title("🏭 装置ステータス アシスタント")
    st.caption("自然言語で質問 → AIがSELECTを生成・実行し、表とグラフ＋自然言語で回答します（読み取り専用）。")

    st.session_state.approval_mode = st.toggle(
        "🔒 SQL実行前に承認する（Human-in-the-loop）",
        value=st.session_state.approval_mode,
        help="ONにすると、生成SQLを表示した後、承認ボタンを押すまで実行しません。",
    )

    if not st.session_state.pending and len(st.session_state.render_log) == 0:
        st.write("**質問の例：**")
        cols = st.columns(len(_EXAMPLES))
        for i, ex in enumerate(_EXAMPLES):
            if cols[i].button(ex, key=f"ex_{i}", use_container_width=True):
                st.session_state.queued_input = ex
                st.rerun()

    _render_history()

    # 実行前承認(Human-in-the-loop): 承認待ちなら承認/却下ボタンを表示
    if st.session_state.pending:
        with st.chat_message("assistant", avatar="🏭"):
            st.warning("上記のSQLを実行しますか？（承認モード）")
            a, b = st.columns(2)
            if a.button("✅ 実行を承認", use_container_width=True):
                _approve_pending()
                st.rerun()
            if b.button("⛔ 実行を却下", use_container_width=True):
                _reject_pending()
                st.rerun()

    user_text = st.chat_input("装置ステータスについて質問してください…")
    if st.session_state.queued_input and not st.session_state.pending:
        user_text = st.session_state.queued_input
        st.session_state.queued_input = None

    if user_text and not st.session_state.pending:
        if not llm.is_configured():
            st.warning("LLMが未設定のため回答できません。.env に OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL を設定してください。")
        else:
            _handle_user(user_text)
            st.rerun()

    # 新しい返答が来た直後だけ最下部へ自動スクロール
    if st.session_state.scroll_to_bottom:
        st.session_state.scroll_to_bottom = False
        _scroll_to_bottom(len(st.session_state.render_log))


if __name__ == "__main__":
    main()
