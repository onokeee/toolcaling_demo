# 装置ステータス アシスタント

半導体工場の装置ステータス(更新日時 / 建屋 / 装置ID / チャンバーID / ステータス)を、
**自然言語で質問 → AIがSELECTを生成・実行 → 表・グラフ・自然言語で回答**するチャット型Webアプリ。

OpenAI(または OpenAI互換 API)の **tool calling(function calling)** を使い、
ツール実行の要否はLLMが判断します。SQLは **SELECT のみ** 実行できます(多層防御)。

## 構成

| ファイル | 役割 |
|---|---|
| `app.py` | Streamlit チャットUI / エージェントループ / 表・グラフ描画 |
| `llm.py` | OpenAI / OpenAI互換API クライアント / system prompt |
| `tools.py` | LLMへ渡すツール定義と実行(`run_sql_query` / `plot_chart` / `plot_dual_axis` / `get_schema`) |
| `db.py` | SQLite接続 / スキーマ / **SELECT専用ガード** |
| `ingest.py` | CSV→SQLite取込(リアルタイム全置換 / 日次断面) |
| `scheduler.py` | 15分毎取込 + 毎日AM2:00断面 の常駐ワーカー |
| `sample_data.py` | デモ用ダミーデータ生成(14日分の推移つき) |
| `config.py` | 設定値(.envから読込) |

## データモデル

- **`equipment_status_realtime`** … 現在のステータス。15分毎に共有フォルダの最新CSVで全置換。「今/現在」の質問用。
- **`equipment_status_daily`** … 毎日AM2:00時点の断面を**直近14日分**保持。「推移/変化/トレンド」の質問用。

## セットアップ

```powershell
# 1) 依存をインストール
pip install -r requirements.txt

# 2) OpenAI APIの接続情報を設定
copy .env.example .env
#   .env を編集（下記「API設定」を参照）

# 3) アプリ起動(初回はサンプルデータを自動生成)
streamlit run app.py
```

### API設定（重要）

公式 OpenAI を使う場合は `base_url` を `https://api.openai.com/v1` にします。

```
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=<API_KEY>          # Authorization: Bearer <API_KEY> としてSDKが自動付与
OPENAI_MODEL=gpt-4o-mini          # gpt-4o も可（tool calling 対応）
```

OpenAI互換のエンドポイント（`/v1` 以外のパスを使うゲートウェイ等）を利用する場合は、
OpenAI SDK が `base_url` の末尾に自動で `/chat/completions` を付与する点に注意します。
たとえばエンドポイントが `https://<host>/api/chat/completions` なら、
**`OPENAI_BASE_URL` は `/api` までを指定**します（`/chat/completions` は付けない）。

- 認証はAPIキーのみ。
- `temperature` / `top_p` / `max_tokens` は任意。SQL生成の安定のため `temperature=0`（既定）。
- レスポンスにSDK未対応の追加フィールドが含まれていても、SDKが無視するため問題なし。

ブラウザが開いたら、サンプル質問ボタンを押すか、自由に質問してください。
（`.env` 未設定でも、データ生成・SQLガードの動作確認やサンプル質問ボタンの表示までは可能です。回答生成にはLLM設定が必要です。）

## 本番データへの切替

1. `.env` の `SHARED_FOLDER` を実際の共有フォルダ(15分毎に更新されるCSVの場所)に設定。
2. 常駐スケジューラを起動:
   ```powershell
   python scheduler.py   # 15分毎取込 + 毎日02:00断面
   ```
3. CSVは5列(`更新日時,建屋,装置ID,チャンバーID,ステータス`)。ヘッダは日本語/英語どちらも可。
   文字コードは UTF-8 / Shift_JIS(cp932)を自動判定。

> 手動で取り込みたい場合は次を実行します:
> ```powershell
> python -c "import ingest; ingest.ingest_latest_from_shared(); ingest.take_daily_snapshot()"
> ```

## 安全設計(SELECTのみ実行)

`db.run_select()` は次の多層防御でSELECT以外を拒否します:

1. **構文チェック** — 単一ステートメント / `SELECT`・`WITH` で開始 / 書込・DDLキーワード禁止
2. **読み取り専用接続** — `file:...?mode=ro` でそもそも書込不可
3. **オーソライザ** — SQLiteの authorizer で `SELECT`/`READ` 以外を `DENY`
4. **タイムアウト** — progress handler で暴走クエリを中断

ガードのセルフテスト:
```powershell
python db.py
```

## ツール実行のUX

- LLMがツール(SQL)を使うと判断したら、**実行前に生成SQLをチャットへ表示**します。
- 実行結果は**テーブル**で表示。可視化が有効なら**グラフ**も描画します
  （棒 / 折れ線 / 円 / 面 / 散布、棒は積み上げ可、棒＋折れ線の**2軸グラフ**も対応）。
- 画面上部の **「🔒 SQL実行前に承認する」** をONにすると Human-in-the-loop モードになり、
  生成SQLを表示したうえで **承認ボタンを押すまで実行しません**（却下も可能）。
- 新しい返答が来ると、チャットは自動で最下部までスクロールします。

## サンプルデータ

このリポジトリには動作確認用のサンプル `data/factory.db`（半導体装置28チャンバー・日次断面14日分）を同梱しています。
そのまま起動すれば質問を試せます。DBが無い場合は初回起動時に `sample_data.py` が自動生成します。
再生成は `python sample_data.py` で行えます。
