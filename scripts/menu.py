#!/usr/bin/env python3
"""献立スキルの計算スクリプト(Python標準ライブラリのみ)。

  python menu.py plan [--days 3] [--people 2] [--max-effort 2] [--use 食材,..] [--avoid 食材,..] [--date YYYY-MM-DD] [--auto]
  python menu.py dishes [--kind 主菜|副菜|汁|主食]      入っている料理の一覧
  python menu.py check                                   自己点検

主菜・副菜・汁をそろえて日数分の献立を作り、材料を合算した買い物リストを売り場ごとに出す。
同じ日付・同じ条件なら同じ献立になる(連続して同じ主材料にならないよう選ぶ)。
--auto: 今日は何の日(行事食)・気分日記(低調なら手間を減らす)・記念日(お祝いの日は手の込んだ主菜)を読んで反映する。
文章(一言・コツ)は呼び出し側のAIが書く。
注意: 調味料と米は常備品として買い物リストに含めない(米を使う行事食は「米」を含む)。
アレルギーや医療上の食事制限には対応しない。表示と医師の指示を確認してください。
"""
import argparse
import datetime
import hashlib
import json
import math
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WEEK = "月火水木金土日"
COUNT_UNITS = {"個", "本", "尾", "切れ", "枚", "束", "玉", "丁", "袋", "缶", "箱", "かけ", "パック"}
SHOP_ORDER = ["野菜・果物", "肉・魚", "卵・乳・豆腐", "乾物・麺・缶・その他"]
EVENT_ONLY = {"toshikoshi", "chirashi", "ehomaki", "nanakusa", "kuri_gohan", "ohagi"}  # 行事の日にだけ出す料理
ALLERGENS = ["卵", "乳", "小麦", "そば", "落花生", "えび", "かに", "くるみ"]  # 表示義務のある特定原材料



def date_arg(text):
    """argparse用: 日付の指定ミスを日本語で知らせる。"""
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError("日付は YYYY-MM-DD の形で、実在する日を指定してください(入力: %s)" % text)


def load_dishes():
    with open(os.path.join(HERE, "dishes.json"), encoding="utf-8") as f:
        return json.load(f)


def season_of(d):
    return "春" if d.month in (3, 4, 5) else "夏" if d.month in (6, 7, 8) else "秋" if d.month in (9, 10, 11) else "冬"


def rank(day, dish_id, salt=""):
    return hashlib.sha256(("%s:%s:%s" % (day.isoformat(), dish_id, salt)).encode("utf-8")).hexdigest()


def usable(dish, day, max_effort, avoid, event=False):
    if dish["id"] in EVENT_ONLY and not event:
        return False
    if day is not None and season_of(day) not in dish["季節"]:
        return False
    if dish["手間"] > max_effort:
        return False
    names = [m["名"] for m in dish["材料"]] + dish["アレルゲン"] + [dish["主材料"], dish["名前"]]
    return not any(a and any(a in n for n in names) for a in avoid)


def pick(dishes, kind, day, max_effort, avoid, use, prev_main):
    pool = [d for d in dishes if d["区分"] == kind and usable(d, day, max_effort, avoid)]
    if not pool:   # 季節を外して探す(条件が厳しすぎる時)
        pool = [d for d in dishes if d["区分"] == kind and usable(d, None, max_effort, avoid)]
    if not pool:
        return None
    def key(d):
        has_use = any(u in m["名"] for u in use for m in d["材料"])
        same = d["主材料"] == prev_main and kind == "主菜"
        return (not has_use, same, rank(day, d["id"], kind))
    return min(pool, key=key)


def run_other(skill, script, args):
    """他のスキルのスクリプトを実行してJSONを返す。無い・失敗した時はNone。"""
    home = os.path.expanduser("~")
    for base in (os.path.join(HERE, "..", ".."), os.path.join(home, ".claude", "skills"),
                 os.path.join(home, ".gemini", "skills"), os.path.join(home, ".agents", "skills")):
        p = os.path.normpath(os.path.join(base, skill, "scripts", script))
        if os.path.exists(p):
            try:
                r = subprocess.run([sys.executable, p] + args, capture_output=True, timeout=60,
                                   env=dict(os.environ, PYTHONIOENCODING="utf-8"))
                if r.returncode == 0:
                    return json.loads(r.stdout.decode("utf-8"))
            except (OSError, subprocess.TimeoutExpired, ValueError):
                pass
            return None
    return None


def find_event_dishes(dishes, event_foods, avoid):
    """行事食の名前に合う料理を、区分ごとに1つずつ {区分: (料理, 行事食名)}。"""
    found = {}
    first = sorted(event_foods, key=lambda f: not any(d['id'] in EVENT_ONLY and f in d['行事食名'] for d in dishes))
    for f in first:
        for d in dishes:
            if (f in d["行事食名"] or f == d["名前"]) and d["区分"] not in found and usable(d, None, 9, avoid, event=True):
                found[d["区分"]] = (d, f)
    return found


def plan(args):
    if args.people < 1 or args.days < 1 or args.days > 31:
        raise ValueError("人数は1以上、日数は1〜31で指定してください")
    dishes = load_dishes()
    start = args.date
    avoid = [a for a in (args.avoid or "").split(",") if a]
    use = [u for u in (args.use or "").split(",") if u]
    max_effort = args.max_effort
    notes = []
    if args.auto:
        w = run_other("気分日記", "mood.py", ["week", "--date", start.isoformat()])
        if w and w.get("平均") is not None and w["平均"] <= 2.5 and max_effort > 1:
            max_effort = 1
            notes.append("最近の気分の平均が低め(%.1f)なので、手間の少ない料理にしました" % w["平均"])
    days, shop = [], {}
    prev_main = None
    for i in range(args.days):
        day = start + datetime.timedelta(days=i)
        reasons = []
        effort = max_effort
        event_foods, party = [], None
        if args.auto:
            n = run_other("今日は何の日", "nanohi.py", ["today", "--date", day.isoformat()])
            if n:
                event_foods = n.get("行事食", [])
            a = run_other("記念日", "anniv.py", ["today", "--date", day.isoformat(), "--days", "0"])
            if a and a.get("今日の記念日"):
                party = a["今日の記念日"][0]
                effort = max(effort, 3)
        menu = {}
        events = find_event_dishes(dishes, event_foods, avoid) if event_foods else {}
        for kind in ("主菜", "副菜", "汁"):
            if kind in events:
                menu[kind] = events[kind][0]
                reasons.append("行事食「%s」を入れました" % events[kind][1])
                continue
            d = pick(dishes, kind, day, effort if kind == "主菜" else min(effort, 2), avoid, use, prev_main)
            if kind == "主菜" and party and d and d["手間"] < 3:
                d3 = pick(dishes, kind, day, 3, avoid, use, prev_main)
                d = d3 or d
                reasons.append("記念日(%s)なので、手の込んだ主菜にしました" % party["名前"])
            if d:
                menu[kind] = d
        if "主食" in events:
            menu["主食"] = events["主食"][0]
            reasons.append("行事食「%s」を主食にしました" % events["主食"][1])
        noted = [f for f in event_foods if not any(f in d["行事食名"] or f == d["名前"] for d, _ in events.values())]
        if noted:
            reasons.append("行事食(%s)は献立の枠に入らなかったか、料理一覧に無いものです。買ってきても作ってもよい一品として添えられます" % "、".join(noted))
        if "主菜" in menu:
            prev_main = menu["主菜"]["主材料"]
        for dsh in menu.values():
            for m in dsh["材料"]:
                k = (m["名"], m["単位"], m["売り場"])
                shop[k] = shop.get(k, 0) + m["量"] * args.people
        days.append({"日付": day.isoformat(), "曜日": WEEK[day.weekday()], "季節": season_of(day),
                     "献立": {k: v["名前"] for k, v in menu.items()}, "手間": max((v["手間"] for v in menu.values()), default=0),
                     "ひとこと用": reasons})
    items = {}
    for (name, unit, where), q in sorted(shop.items()):
        if unit in COUNT_UNITS:
            amount = math.ceil(round(q, 6))
            text = "%d%s" % (amount, unit)
        else:
            text = "%d%s" % (int(math.ceil(q / 10.0) * 10) if unit == "g" else round(q), unit)
        items.setdefault(where, []).append({"品名": name, "量": text})
    allergens = sorted({a for dm in dishes for a in dm["アレルゲン"] if any(
        dm["名前"] == nm for day in days for nm in day["献立"].values())})
    out = {"開始日": start.isoformat(), "人数": args.people, "日数": args.days, "献立": days,
           "買い物リスト": {w: items[w] for w in SHOP_ORDER if w in items},
           "含まれるアレルゲン": allergens,
           "注": "調味料は常備品として含めていません。量は目安で、買い物の前に冷蔵庫の中を確認してください。"
                 "アレルギーや医療上の食事制限には対応していません"}
    if notes:
        out["連携のメモ"] = notes
    if args.auto and len(days) and not any(d["ひとこと用"] for d in days):
        out["連携のメモ"] = out.get("連携のメモ", []) + ["今日は行事食・記念日の指定はありませんでした"]
    return out


def cmd_dishes(args):
    ds = [d for d in load_dishes() if not args.kind or d["区分"] == args.kind]
    return {"件数": len(ds), "料理": [{"名前": d["名前"], "区分": d["区分"], "季節": "".join(d["季節"]) if len(d["季節"]) < 4 else "通年",
                                   "手間": d["手間"]} for d in ds]}


def cmd_check(args):
    ds = load_dishes()
    ok = True

    def t(good, label):
        nonlocal ok
        print(("OK  " if good else "NG  ") + label)
        ok = ok and good

    t(len({d["id"] for d in ds}) == len(ds), "料理のidが重複しない")
    t(all(d["区分"] in ("主菜", "副菜", "汁", "主食") and d["材料"] and 1 <= d["手間"] <= 3 for d in ds), "区分・材料・手間が正しい")
    t(all(set(d["アレルゲン"]) <= set(ALLERGENS) | {"大豆"} for d in ds), "アレルゲンは特定原材料(と大豆)の範囲")
    t(all(all(m["売り場"] in SHOP_ORDER for m in d["材料"]) for d in ds), "売り場がすべて既知")
    for s in ("春", "夏", "秋", "冬"):
        for kind in ("主菜", "副菜", "汁"):
            if not any(d["区分"] == kind and s in d["季節"] for d in ds):
                t(False, "%sの%sがある" % (s, kind))
    args2 = argparse.Namespace(days=7, people=2, max_effort=3, use="", avoid="", date=datetime.date(2026, 10, 14), auto=False)
    p = plan(args2)
    mains = [d["献立"].get("主菜") for d in p["献立"]]
    t(all(m for m in mains) and len(set(mains)) >= 5, "7日分の主菜が出て、同じものが多く重ならない")
    args2.avoid = "卵,小麦"
    p2 = plan(args2)
    t(not p2["含まれるアレルゲン"] or all(a not in ("卵", "小麦") for a in p2["含まれるアレルゲン"]), "アレルゲンを避けると含まれない")
    args2.avoid, args2.use, args2.days = "", "鶏", 1
    t("鶏" in json.dumps(plan(args2)["買い物リスト"], ensure_ascii=False), "使いたい食材を優先する")
    args3 = argparse.Namespace(days=1, people=2, max_effort=3, use="", avoid="", date=datetime.date(2026, 7, 1), auto=False)
    t(all(s in "夏通" or True for s in ["夏"]) and "ゴーヤ" not in json.dumps(plan(argparse.Namespace(
        days=1, people=1, max_effort=3, use="", avoid="", date=datetime.date(2026, 12, 20), auto=False)), ensure_ascii=False),
        "冬に夏の料理(ゴーヤ)は出ない")
    sys.exit(0 if ok else 1)



# --- ページ(HTML)の書き出し(形だけの初版。見た目の磨きは後日) ---
def _page_escape(x):
    import html
    return html.escape(str(x))


def render_page(data, skill_name, tpl_path):
    """AIが組み立てた内容(JSON)をHTMLページにする。
    内容の形: {"日付": "…", "副題": "…", "一言": "…", "注記": "…",
              "節": [{"題": "…", "行": [["題","文"],…]} | {"題": "…", "本文": "…"} | {"題": "…", "棒": [["名","値の文",0〜100],…]}]}"""
    e = _page_escape
    secs = []
    for s in data.get("節", []):
        body = ""
        if s.get("行"):
            body += '<dl class="rows">%s</dl>' % "".join(
                '<div class="row"><dt>%s</dt><span class="leader"></span><dd>%s</dd></div>' % (e(a), e(b)) for a, b in s["行"])
        if s.get("本文"):
            body += "".join('<p class="para">%s</p>' % e(p) for p in str(s["本文"]).split("\n") if p.strip())
        if s.get("棒"):
            body += '<ul class="bars">%s</ul>' % "".join(
                '<li class="bar"><span>%s</span><span class="track"><span class="fill" style="width:%d%%"></span></span><span class="val">%s</span></li>'
                % (e(n), max(0, min(100, int(v))), e(t)) for n, t, v in s["棒"])
        secs.append('<section class="sec"><h2>%s</h2>%s</section>' % (e(s.get("題", "")), body))
    with open(tpl_path, encoding="utf-8") as f:
        tpl = f.read()
    vals = {"日付": e(data.get("日付", "")),
            "副題": ('<p class="sub">%s</p>' % e(data["副題"])) if data.get("副題") else "",
            "一言": ('<p class="lead">%s</p>' % e(data["一言"])) if data.get("一言") else "",
            "節": "\n  ".join(secs),
            "注記": ('<p class="note">%s</p>' % e(data["注記"])) if data.get("注記") else ""}
    for k, v in vals.items():
        tpl = tpl.replace("{{%s}}" % k, v)
    return tpl


def cmd_page(args):
    import json as _json
    from pathlib import Path as _Path
    here = _Path(os.path.dirname(os.path.abspath(__file__)))
    data = _json.loads(_Path(args.データ).read_text(encoding="utf-8"))
    _Path(args.out).write_text(render_page(data, "献立", str(here / "page.html")), encoding="utf-8")
    return {"書き出し": args.out}


def main():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    _pg = sub.add_parser("page", help="内容(JSON)をHTMLページ(Artifact用)に書き出す")
    _pg.add_argument("--データ", required=True, help="あなた(AI)が組み立てた内容のJSONファイル")
    _pg.add_argument("--out", required=True, help="書き出すHTMLのパス")
    _pg.set_defaults(fn=cmd_page)
    p = sub.add_parser("plan")
    p.add_argument("--days", type=int, default=3)
    p.add_argument("--people", type=int, default=2)
    p.add_argument("--max-effort", dest="max_effort", type=int, choices=[1, 2, 3], default=2)
    p.add_argument("--use", default="")
    p.add_argument("--avoid", default="")
    p.add_argument("--date", type=date_arg, default=datetime.date.today())
    p.add_argument("--auto", action="store_true")
    p.set_defaults(fn=plan)
    p = sub.add_parser("dishes")
    p.add_argument("--kind", choices=["主菜", "副菜", "汁", "主食"])
    p.set_defaults(fn=cmd_dishes)
    p = sub.add_parser("check")
    p.set_defaults(fn=cmd_check)
    args = ap.parse_args()
    try:
        print(json.dumps(args.fn(args), ensure_ascii=False, indent=2))
    except ValueError as e:
        print(json.dumps({"エラー": str(e)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
