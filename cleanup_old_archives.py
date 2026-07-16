"""
============================================================
🚌 移動支援ナビ cleanup_old_archives.py

目的：
  もし将来、archive/ フォルダに日付付きのバックアップ
  （例：archive/2026_6_1-31_old.csv）を保管する運用に
  変更した場合に、一定期間より古いファイルを自動削除するための
  「安全弁」スクリプトです。

  現時点（CSVを毎回上書きする運用）では、archive/ フォルダ
  自体を使っていないため、このスクリプトを実行しても削除対象の
  ファイルはありません（安全に空振りします）。
============================================================
"""

# ------------------------------------------------------------
# 1. ライブラリの読み込み
# ------------------------------------------------------------
import os
import time


# ------------------------------------------------------------
# 2. 設定
# ------------------------------------------------------------
ARCHIVE_DIR = "archive"          # 日付付きバックアップを置く想定のフォルダ
RETENTION_DAYS = 180             # これより古いファイルを削除する（約半年）


# ------------------------------------------------------------
# 3. 古いファイルの削除処理
# ------------------------------------------------------------
def cleanup_old_files(directory, retention_days):
    if not os.path.isdir(directory):
        print(f"『{directory}』フォルダが存在しないため、削除対象はありません（正常な状態です）。")
        return

    cutoff_time = time.time() - (retention_days * 86400)
    deleted_count = 0
    kept_count = 0

    for filename in os.listdir(directory):
        filepath = os.path.join(directory, filename)

        if not os.path.isfile(filepath):
            continue  # サブフォルダ等は対象外

        file_mtime = os.path.getmtime(filepath)

        if file_mtime < cutoff_time:
            os.remove(filepath)
            deleted_count += 1
            print(f"  🗑 削除：{filename}（{retention_days}日以上前のファイル）")
        else:
            kept_count += 1

    print(f"完了：削除{deleted_count}件／保持{kept_count}件")


# ------------------------------------------------------------
# 4. 実行
# ------------------------------------------------------------
if __name__ == "__main__":
    print("==========================================")
    print(f"🚌 古いアーカイブファイルのクリーンアップ（{RETENTION_DAYS}日より古いものを削除）")
    print("==========================================")
    cleanup_old_files(ARCHIVE_DIR, RETENTION_DAYS)
