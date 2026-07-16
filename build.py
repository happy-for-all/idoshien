# ============================================================
# 🚌 移動支援ナビ（idoshien） build.py
#
# 目的：
#   ① 都道府県ごとに個別公開されている「移動支援」CSV（csv/フォルダ内）を読み込む
#   ② 各事業所の住所を、国土地理院のジオコーディングAPIで緯度経度に変換する
#      （一度変換した住所は geocode_cache.json に保存し、次回以降は再利用する）
#   ③ 都道府県別の軽量JSONファイルに分割して dist/ に出力する
#
# 対象データ：移動支援のみ（同行援護・行動援護は対象外）
# ============================================================


# ------------------------------------------------------------
# 1. ライブラリの読み込み
# ------------------------------------------------------------
import os
import re
import json
import time
import shutil
import unicodedata
import urllib.request
import urllib.parse
import pandas as pd


# ------------------------------------------------------------
# 2. 設定（プロジェクトに合わせて調整可能な定数）
# ------------------------------------------------------------
CSV_DIR = "csv"                          # 都道府県ごとのCSVを格納するフォルダ
OUTPUT_DIR = "dist"
GEOCODE_CACHE_PATH = "geocode_cache.json"

# 👑 都道府県が増えたら、ここに1行追加するだけで対応できる設計にしてある。
#    キー：csv/フォルダ内のファイル名（ダウンロード時のファイル名をそのまま使う。
#          リネームすると「どのファイルがどこの都道府県か」が分からなくなり、
#          後から混乱する原因になるため、ファイル名は加工しない方針にした）
#    値　：(都道府県の正式名称, スラッグ（英語表記）, 大阪府市町村フィルタが必要か)
#          ※ ファイル名自体には都道府県情報が含まれていないため、
#            新しい都道府県のCSVを追加する際は、必ず中身を一度確認してから
#            このマッピングに正しい都道府県名を手動で追加すること。
#
#    👑 修正済み（2026-07-17）：ここに書くファイル名は「.」区切りでも「_」区切りでも
#    どちらでも構わない。実際のcsv/フォルダ内のファイルとの照合時に、区切り文字の
#    違いを無視して同一のものとして扱う仕組み（resolve_prefecture_files関数）を
#    導入したため、表記ゆれによって「ファイルが見つからずスキップされる」事故を防げる。
#
#    👑 修正済み（2026-07-17）：ちゃろさんに確認したところ、CSVは「大阪市」と
#    「大阪市以外の市町村」の2種類で構成されていることが判明した。
#    「2026_6_1-31.csv」＝大阪市、「2026_6_1-32.csv」＝大阪市以外の市町村。
#    どちらも同じ「大阪府（osaka）」として1つのデータにまとめて出力する
#    （利用者から見て「大阪府」という1つの地域として検索できるようにするため）。
#    ただし「大阪市以外」側には、他府県（兵庫県・奈良県等）の住所が約20%混入している
#    ことが実データ検証で判明したため、大阪府の正式な市町村名と照合し、
#    該当しない行は自動的に除外する（3番目の True フラグで指定）。
PREFECTURE_FILES = {
    "2026_6_1-31.csv": ("大阪府", "osaka", False),
    "2026_6_1-32.csv": ("大阪府", "osaka", True),
}

# 大阪府の正式な市町村一覧（33市・9町・1村、計43市町村）。
# 「大阪市以外」のCSVに混入している他府県の住所を除外するための照合リストとして使う。
# 出典：大阪府公式サイト（府内市町村の概要）
OSAKA_MUNICIPALITY_PREFIXES = [
    # 33市
    "大阪市", "堺市", "岸和田市", "豊中市", "池田市", "吹田市", "泉大津市", "高槻市", "貝塚市", "守口市",
    "枚方市", "茨木市", "八尾市", "泉佐野市", "富田林市", "寝屋川市", "河内長野市", "松原市", "大東市", "和泉市",
    "箕面市", "柏原市", "羽曳野市", "門真市", "摂津市", "高石市", "藤井寺市", "東大阪市", "泉南市", "四條畷市",
    "交野市", "大阪狭山市", "阪南市",
    # 9町・1村（郡名付きで住所に現れるため、郡名込みで登録する）
    "三島郡島本町", "豊能郡豊能町", "豊能郡能勢町", "泉北郡忠岡町",
    "泉南郡熊取町", "泉南郡田尻町", "泉南郡岬町",
    "南河内郡太子町", "南河内郡河南町", "南河内郡千早赤阪村",
]

# 国土地理院 ジオコーディングAPI（無料・APIキー不要）
# 👑 準公式・実験的サービスという位置づけのため、
#    index.html側の免責事項に必ずその旨を明記すること（デザイン設計書 参照）
GEOCODE_API_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch"
GEOCODE_TIMEOUT_SECONDS = 10   # 修正済み：ネットワークアクセスには必ずタイムアウトを設定する
GEOCODE_WAIT_SECONDS = 0.3     # API側への負荷配慮（新規リクエスト時のみ待機する）

# CSVの更新時点（ダウンロード元ページの表記に合わせて手動で更新してください）
CSV_SOURCE_LABEL = "2026年6月時点（各都道府県公表の移動支援指定事業所一覧）"


# ------------------------------------------------------------
# 3. CSV読み込み（文字コード自動判定）
#
#    実データ確認済み：このCSVは1行目がタイトル行（「移動支援」等）、
#    2行目が本当のヘッダー行という構造になっている。
#    そのため header=1 を指定して読み込む。
# ------------------------------------------------------------
def load_csv_with_encoding_fallback(path):
    """
    複数の文字コードを順に試し、読み込めたものを採用する。
    今回のCSVは基本的にcp932（Shift_JIS拡張）だが、
    万一の文字コード違いに備えて防御的な実装にしておく。
    """
    encodings_to_try = ["cp932", "shift_jis", "utf-8-sig", "utf-8"]
    last_error = None

    for enc in encodings_to_try:
        try:
            return pd.read_csv(path, encoding=enc, dtype=str, header=1)
        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(f"CSVの読み込みに失敗しました（全ての文字コードで失敗）: {last_error}")


# ------------------------------------------------------------
# 4. 文字列の安全な取得
#
#    pandasはCSVの空欄を「NaN（float型）」として読み込むため、
#    そのまま str(値) とすると文字列 "nan" が入ってしまう不具合がある
#    （訪問看護ナビでの実データ検証で発覚した不具合と同種）。
#    また、住所に全角文字が混入しているケースに備え、
#    NFKCで正規化する処理も入れておく。
# ------------------------------------------------------------
def safe_str(value):
    """
    NaN（pandasの欠損値）を確実にNoneとして扱い、
    前後の空白を除去した文字列を返す。
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text if text else None


def safe_str_nfkc(value):
    """
    safe_str に加えて、全角英数字・記号を半角に正規化する。
    住所・電話番号などNFKC正規化が有効な項目に使う。
    """
    text = safe_str(value)
    if text is None:
        return None
    return unicodedata.normalize("NFKC", text)


# ------------------------------------------------------------
# 5. 電話番号・FAX番号の正規化
# ------------------------------------------------------------
def clean_phone(raw):
    """
    表示用の電話番号はそのまま活かしつつ、tel:リンク用に数字だけの
    文字列を別途生成する。
    """
    display = safe_str_nfkc(raw)
    if not display:
        return None, None

    digits_only = re.sub(r"[^\d]", "", display)
    if not digits_only:
        return None, None

    return display, digits_only


# ------------------------------------------------------------
# 6. 「主たる対象者」タグの読み取り
#
#    CSVでは「○」または空欄で表現されている。
#    これはAIによる推定ではなく、都道府県が公表する公式データそのもの。
# ------------------------------------------------------------
def parse_target_tags(row):
    def is_marked(value):
        text = safe_str(value)
        return text is not None and text != ""

    return {
        "physical": is_marked(row.get("主たる対象者：身体")),
        "intellectual": is_marked(row.get("主たる対象者：知的")),
        "mental": is_marked(row.get("主たる対象者：精神")),
        "child": is_marked(row.get("主たる対象者：障がい児")),
    }


# ------------------------------------------------------------
# 7. ジオコーディング（住所→緯度経度）
#
#    国土地理院のAPIを使用。1件ずつ問い合わせるとビルドが長時間化・
#    API側への負荷になるため、一度取得した結果は geocode_cache.json に
#    保存し、次回以降は再利用する。
# ------------------------------------------------------------
def load_geocode_cache(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        # 修正済み：キャッシュファイルが万一壊れていても、ビルド全体を
        # 止めずに「キャッシュ無し」として安全に継続する。
        print(f"警告：{path} の形式が不正なため、キャッシュ無しで続行します：{e}")
        return {}


def save_geocode_cache(path, cache):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def geocode_address(address):
    """
    国土地理院のジオコーディングAPIに問い合わせ、(緯度, 経度) を返す。
    見つからない場合・エラー時は (None, None) を返す（ビルド全体は止めない）。
    """
    encoded = urllib.parse.quote(address)
    url = f"{GEOCODE_API_URL}?q={encoded}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "idoshien-navi-build/1.0"})
        with urllib.request.urlopen(req, timeout=GEOCODE_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))

            if not data:
                return None, None

            lon, lat = data[0]["geometry"]["coordinates"]
            return lat, lon

    except Exception as e:
        print(f"  ⚠️ ジオコーディング失敗（{address}）：{e}")
        return None, None


def geocode_with_cache(address, cache):
    """
    キャッシュにあればそれを使い、無ければAPIに問い合わせてキャッシュに追加する。
    戻り値：(lat, lon, キャッシュが更新されたか)
    """
    if address in cache:
        cached = cache[address]
        # cached は [lat, lon] または null（見つからなかった住所として記録済み）
        if cached is None:
            return None, None, False
        return cached[0], cached[1], False

    lat, lon = geocode_address(address)

    if lat is not None and lon is not None:
        cache[address] = [lat, lon]
    else:
        cache[address] = None  # 見つからなかったことも記録し、毎回再問い合わせしない

    # API側への負荷配慮：新規に問い合わせた時だけ待機する
    time.sleep(GEOCODE_WAIT_SECONDS)

    return lat, lon, True


# ------------------------------------------------------------
# 8-0. ファイル名の表記ゆれ吸収（. と _ の違いなどを無視して照合する）
# ------------------------------------------------------------
def normalize_filename_for_matching(filename):
    """
    ファイル名の「.」「_」「-」「半角スペース」の違いを無視して比較できるように
    正規化する（拡張子は残す）。
    例：'2026.6.1-31.csv' と '2026_6_1-31.csv' を同じものとして扱う。
    """
    stem, ext = os.path.splitext(filename)
    normalized_stem = re.sub(r"[._\-\s]", "", stem).lower()
    return normalized_stem + ext.lower()


def resolve_prefecture_files():
    """
    PREFECTURE_FILES で定義したファイル名と、csv/フォルダ内に実際に存在する
    ファイル名を、区切り文字の表記ゆれを無視して照合する。

    これにより「登録した名前と実ファイル名が1文字でも違うとスキップされてしまう」
    という事故を防ぐ（過去に実際発生した不具合の再発防止）。
    """
    resolved = {}

    if not os.path.isdir(CSV_DIR):
        print(f"  ⚠️ {CSV_DIR}フォルダ自体が見つかりません")
        return resolved

    actual_files = os.listdir(CSV_DIR)
    normalized_to_actual = {normalize_filename_for_matching(f): f for f in actual_files}

    for registered_name, value in PREFECTURE_FILES.items():
        normalized_registered = normalize_filename_for_matching(registered_name)

        if normalized_registered in normalized_to_actual:
            actual_filename = normalized_to_actual[normalized_registered]
            resolved[actual_filename] = value
            if actual_filename != registered_name:
                print(f"  ℹ️ 表記ゆれを吸収して照合しました：{registered_name} → 実ファイル {actual_filename}")
        else:
            print(f"  ⚠️ {registered_name} に一致するファイルが {CSV_DIR}/ 内に見つかりません（表記ゆれも考慮した上で未検出）")

    return resolved


# ------------------------------------------------------------
# 8-1. 「大阪市以外」CSVに混入した他府県住所の除外
# ------------------------------------------------------------
def filter_to_osaka_municipalities(df):
    """
    住所が大阪府の正式な市町村名（OSAKA_MUNICIPALITY_PREFIXES）で
    始まっている行だけを残し、それ以外（兵庫県・奈良県・滋賀県など、
    誤って混入したと見られる行）を除外する。

    戻り値：(除外後のDataFrame, 除外した件数, 除外した住所のサンプル最大5件)
    """
    def is_osaka_address(address):
        if not isinstance(address, str):
            return False
        return any(address.startswith(prefix) for prefix in OSAKA_MUNICIPALITY_PREFIXES)

    mask = df["事業所所在地"].apply(is_osaka_address)
    excluded_samples = df.loc[~mask, "事業所所在地"].head(5).tolist()
    excluded_count = int((~mask).sum())

    return df[mask].reset_index(drop=True), excluded_count, excluded_samples


# ------------------------------------------------------------
# 8. 1事業所分のレコードを組み立てる
# ------------------------------------------------------------
def build_station_record(row, pref_name, cache):
    jigyosho_no = safe_str(row.get("事業所番号")) or ""

    tel_display, tel_clean = clean_phone(row.get("事業所電話番号"))
    fax_display, fax_clean = clean_phone(row.get("事業所FAX番号"))

    # 修正済み：CSVの「事業所所在地」列には都道府県名が含まれていないため
    # （例："大津市坂本七丁目..." のように市区町村から始まる）、
    # ジオコーディング精度と表示の分かりやすさのため、都道府県名を先頭に補う。
    raw_address = safe_str_nfkc(row.get("事業所所在地")) or ""
    full_address = f"{pref_name}{raw_address}" if raw_address else pref_name

    lat, lon, cache_updated = geocode_with_cache(full_address, cache)

    record = {
        "jigyosho_no": jigyosho_no,
        "name": safe_str(row.get("事業所名称")) or "",
        "corporation_name": safe_str(row.get("法人名称")) or "",
        "prefecture": pref_name,
        "address": full_address,
        "lat": lat,
        "lon": lon,
        "tel": tel_display,
        "tel_clean": tel_clean,
        "fax": fax_display,
        "fax_clean": fax_clean,
        "target_tags": parse_target_tags(row),
        "designated_date": safe_str(row.get("指定日")),
    }

    return record, cache_updated


# ------------------------------------------------------------
# 9. メインのビルド処理
# ------------------------------------------------------------
def main():
    print("==========================================")
    print("🚌 移動支援ナビ ビルド開始")
    print("==========================================")

    if os.path.exists(OUTPUT_DIR):
        shutil.rmtree(OUTPUT_DIR)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    geocode_cache = load_geocode_cache(GEOCODE_CACHE_PATH)
    cache_dirty = False

    manifest = {
        "csv_source": CSV_SOURCE_LABEL,
        "geocode_source": "国土地理院 ジオコーディングAPI（準公式・実験的サービス）",
        "prefectures": {},
        "total_count": 0,
    }

    resolved_files = resolve_prefecture_files()

    # 👑 同じスラッグ（例：osaka）を持つ複数のCSVを1つのデータにまとめるため、
    # スラッグ単位でレコードを集約してからJSONに出力する設計にしている。
    prefecture_records = {}   # slug -> {"name": pref_name, "records": [...]}

    for filename, (pref_name, pref_slug, needs_osaka_filter) in resolved_files.items():
        csv_path = os.path.join(CSV_DIR, filename)

        if not os.path.exists(csv_path):
            print(f"  ⚠️ {csv_path} が見つからないため、{pref_name}（{filename}）をスキップしました")
            continue

        df = load_csv_with_encoding_fallback(csv_path)
        print(f"{pref_name}／{filename}：CSV読み込み完了（{len(df)}件）")

        if needs_osaka_filter:
            df, excluded_count, excluded_samples = filter_to_osaka_municipalities(df)
            if excluded_count > 0:
                print(f"  ℹ️ 大阪府の市町村に該当しない{excluded_count}件を除外しました（他府県の混入データ）")
                print(f"     除外した住所の例：{excluded_samples}")
            print(f"  除外後：{len(df)}件を大阪府データとして採用します")

        records = []
        for _, row in df.iterrows():
            record, updated = build_station_record(row, pref_name, geocode_cache)
            records.append(record)
            if updated:
                cache_dirty = True

        if pref_slug not in prefecture_records:
            prefecture_records[pref_slug] = {"name": pref_name, "records": []}
        prefecture_records[pref_slug]["records"].extend(records)

    # スラッグ（都道府県）ごとにまとめて1つのJSONとして書き出す
    for pref_slug, info in prefecture_records.items():
        pref_name = info["name"]
        records = info["records"]

        # 事業所番号が重複している場合（同じ事業所が複数CSVに登場するケース）に備え、
        # 先に登場したものを優先しつつ、重複を除いておく（データの二重表示を防ぐ）
        seen_jigyosho_no = set()
        deduped_records = []
        duplicate_count = 0
        for r in records:
            if r["jigyosho_no"] and r["jigyosho_no"] in seen_jigyosho_no:
                duplicate_count += 1
                continue
            if r["jigyosho_no"]:
                seen_jigyosho_no.add(r["jigyosho_no"])
            deduped_records.append(r)

        if duplicate_count > 0:
            print(f"  ℹ️ {pref_name}：事業所番号が重複していた{duplicate_count}件を除外しました")

        # 座標が取得できた件数を集計し、ビルドログで分かるようにしておく
        geocoded_count = sum(1 for r in deduped_records if r["lat"] is not None)

        output_path = os.path.join(OUTPUT_DIR, f"data_{pref_slug}.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(deduped_records, f, ensure_ascii=False, indent=2)

        manifest["prefectures"][pref_slug] = {
            "name": pref_name,
            "count": len(deduped_records),
            "geocoded_count": geocoded_count,
        }
        manifest["total_count"] += len(deduped_records)

        print(f"  {pref_name}（{pref_slug}）：合計{len(deduped_records)}件（座標取得 {geocoded_count}件）→ {output_path}")

    # ジオコーディングキャッシュは、新しい住所を1件でも問い合わせた場合のみ保存し直す
    if cache_dirty:
        save_geocode_cache(GEOCODE_CACHE_PATH, geocode_cache)
        print(f"ジオコーディングキャッシュを更新しました：{GEOCODE_CACHE_PATH}")
    else:
        print("ジオコーディングキャッシュに変更なし（全件キャッシュ済み）")

    manifest_path = os.path.join(OUTPUT_DIR, "data_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # 👑 重要：CF Workerのassets配信は wrangler.json の "directory": "./dist"
    # 配下のファイルのみを対象とするため、index.html も必ずdist/にコピーする。
    static_files_to_copy = ["index.html", "favicon.ico"]
    for filename in static_files_to_copy:
        if os.path.exists(filename):
            shutil.copy(filename, os.path.join(OUTPUT_DIR, filename))
            print(f"  静的ファイルをコピー：{filename} → {OUTPUT_DIR}/{filename}")
        else:
            print(f"  ⚠️ {filename} が見つからないため、コピーをスキップしました（次のステップで作成予定）")

    print("==========================================")
    print(f"✅ ビルド完了：合計{manifest['total_count']}件")
    print(f"マニフェスト：{manifest_path}")
    print("==========================================")


# ------------------------------------------------------------
# 10. 実行
# ------------------------------------------------------------
if __name__ == "__main__":
    main()
