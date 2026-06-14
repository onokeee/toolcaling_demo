# 装置ステータス アシスタント

半導体工場の装置ステータス(更新日時 / 建屋 / 装置ID / チャンバーID / ステータス)を、
**自然言語で質問 → AIがSELECTを生成・実行 → 表・グラフ・自然言語で回答**するチャット型Webアプリ。

OpenAI(または OpenAI互換 API)の **tool calling(function calling)** を使い、
ツール実行の要否はLLMが判断します。SQLは **SELECT のみ** 実行できます(多層防御)。

## 主な機能

- 自然言語の質問から **SELECT文を自動生成・実行**（「現在」「推移」どちらも対応）
- 実行前に **生成SQLをチャットに表示**／任意で **実行前承認 (Human-in-the-loop)**
- 結果を **テーブル** と **グラフ**（棒/折れ線/円/面/散布、積み上げ、棒+折れ線の2軸）で表示
- **SELECT専用ガード**（多層防御）で更新系SQLは実行不可
- CSV→SQLite 取込（15分毎のリアルタイム全置換 + 毎日02:00の断面・直近14日保持）
- 同梱サンプルDBでクローン後すぐ動作

## アーキテクチャ

```
ブラウザ(チャットUI)
   │  質問 ▲ 表/グラフ/回答
   ▼      │
app.py ── Streamlit / エージェントループ
   │
   ├──▶ llm.py ──HTTPS──▶ OpenAI / OpenAI互換 chat API   ← tool callingで実行要否を判断
   │
   └──▶ tools.py ──▶ db.py(SELECT専用ガード) ──▶ SQLite(factory.db)
                                                      ▲
                                                      │ 取込(全置換 / 日次断面)
                                          ingest.py ◀── scheduler.py (15分毎 / 毎日02:00)
                                                      ▲
                                                      │ CSV読込
                                              共有フォルダ status_*.csv
```

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

## 仕組み（処理の流れ）

### 1. 質問への回答フロー（tool calling エージェント）

```
ユーザー質問
  └▶ app.py が「会話履歴 + ツール定義」を LLM に送信
        └▶ LLM が "ツールを使うか" を判断 (tool_choice="auto")
              ├─ 使わない → そのまま自然言語で回答
              └─ 使う(tool_calls) →
                    ① 生成SQLをチャットに表示（実行の前）
                    ② 承認モードONなら 承認/却下 を待つ
                    ③ db.run_select() で SELECT のみ実行
                    ④ 結果テーブル/グラフを描画
                    ⑤ 結果を LLM に戻し、最終回答(自然言語)を生成
```

- ツールを使うか・どのSQLを書くかはLLMが判断します。
- 1回の質問で複数ツール（例: 集計SQL → グラフ描画）を順に呼ぶこともあります（最大 `MAX_AGENT_STEPS` 回）。
- 実行できるのは `SELECT`（または `WITH ... SELECT`）のみ。更新系・DDLは拒否されます。

### 2. データ取込フロー（CSV → SQLite）

実データ運用では `scheduler.py` を常駐させ、共有フォルダのCSVを取り込みます。

| ジョブ | 周期 | 処理 |
|---|---|---|
| `job_realtime` | 15分毎 | 共有フォルダの**最新CSV**を読み、`equipment_status_realtime` を**全置換** |
| `job_daily` | 毎日 02:00 | その時点の realtime の内容を `equipment_status_daily` に**断面コピー**し、**直近14日**より古い断面を削除 |

- CSVは5列(`更新日時,建屋,装置ID,チャンバーID,ステータス`)。日本語/英語ヘッダ両対応、文字コードは UTF-8 / Shift_JIS(cp932) を自動判定。
- realtime は毎回**全置換**（各CSVが全装置の完全スナップショットである前提）。
- daily は「02:00時点の realtime のコピー」。直前の15分毎取込に依存します。
- **Streamlitアプリ単体では取込は動きません。** 15分毎の更新には `scheduler.py` を別プロセスで起動してください（初回起動時にDBが空ならサンプルデータを自動生成）。

## データモデル

- **`equipment_status_realtime`** … 現在のステータス。15分毎に共有フォルダの最新CSVで全置換。「今/現在」の質問用。
  - 列: `updated_at, building, equipment_id, chamber_id, status, ingested_at`
- **`equipment_status_daily`** … 毎日AM2:00時点の断面を**直近14日分**保持。「推移/変化/トレンド」の質問用。
  - 列: `snapshot_date, snapshot_at, updated_at, building, equipment_id, chamber_id, status`

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

(任意) 実データを15分毎に取り込むワーカーを別ターミナルで常駐:
```powershell
python scheduler.py
```

## 使い方

1. ブラウザでアプリを開き、**サンプル質問ボタン**を押すか、入力欄に自由に質問します。
2. AIが必要に応じてSQLを生成・実行し、**表・グラフ・自然言語**でまとめて回答します。

### 質問の例

現在の状態（realtimeテーブル）:
- 「今、各建屋ごとにステータスの内訳を教えて」
- 「現在DOWNしている装置を一覧で出して」
- 「建屋ごとの稼働率(RUN比率)を棒グラフで」

時系列・推移（dailyテーブル, 直近14日）:
- 「直近14日のDOWN件数の推移を折れ線グラフで見せて」
- 「ETCH-A01 のこの2週間のステータス変化を教えて」
- 「日別のステータス内訳を積み上げ棒グラフで」
- 「日別のDOWN件数(棒)と稼働率(折れ線)を2軸グラフで」

### グラフ

「グラフ」「推移」「割合」「内訳」「2軸」などの語を入れると描画します。対応種別:
- 折れ線 / 棒（横並び・積み上げ）/ 円 / 面 / 散布
- **2軸グラフ**（棒=左軸 + 折れ線=右軸。件数と比率を同時に表示）

### 実行前承認（Human-in-the-loop）

画面上部の **「🔒 SQL実行前に承認する」** をONにすると、生成SQLを表示したうえで
**承認ボタンを押すまで実行しません**（却下も可能）。OFFのときはそのまま実行します。

### その他

- ツール実行時は、**実行の前に生成SQLを必ずチャットに表示**します。
- 新しい返答が来ると、チャットは自動で最下部までスクロールします。
- `.env` 未設定でも、データ生成・SQLガードの動作確認やサンプル質問ボタンの表示までは可能です（回答生成にはLLM設定が必要）。

## API設定（重要）

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

## サンプルデータ

このリポジトリには動作確認用のサンプル `data/factory.db`（半導体装置28チャンバー・日次断面14日分）を同梱しています。
そのまま起動すれば質問を試せます。DBが無い場合は初回起動時に `sample_data.py` が自動生成します。
再生成は `python sample_data.py` で行えます。
