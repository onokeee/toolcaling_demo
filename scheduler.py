"""常駐スケジューラ(定期実行用)。

  - 15分毎       : 共有フォルダの最新CSVをリアルタイムテーブルへ取込
  - 毎日 AM2:00  : その時点のリアルタイム内容を日次断面として保存(直近14日保持)

Streamlitアプリとは別プロセスで起動する想定:
    python scheduler.py
"""
from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler

import config
import ingest
from db import ensure_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("scheduler")


def job_realtime():
    n = ingest.ingest_latest_from_shared()
    log.info("リアルタイム取込: %s 行 (共有=%s)", n, config.SHARED_FOLDER)


def job_daily():
    n = ingest.take_daily_snapshot()
    log.info("日次断面を保存: %s 行 (%s)", n, datetime.now().date())


def main():
    ensure_db()
    sched = BlockingScheduler()
    sched.add_job(job_realtime, "interval", minutes=15, next_run_time=datetime.now())
    sched.add_job(job_daily, "cron", hour=2, minute=0)
    log.info("スケジューラ開始: 15分毎の取込 + 毎日02:00の断面取得")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("停止しました")


if __name__ == "__main__":
    main()
