"""
gold_match_probe.py — diagnose why gold_port matched nothing. Read-only. Compact
by default: per shared sheet, row counts, how many match keys intersect old&new,
and which key columns are missing. Add --verbose for full column lists.

Usage:
  python gold_match_probe.py --old-gold data/gold_old.xlsx --new-gold data/gold_new.xlsx
"""
import argparse
import pandas as pd

from score_extraction import _lowmap, _get_ci, _rid_key
from gold_port import _fb_key


def _read_all(path):
    xl = pd.ExcelFile(path)
    return {n: pd.read_excel(xl, sheet_name=n, dtype=object, keep_default_na=False).where(
        lambda d: pd.notnull(d), None) for n in xl.sheet_names}


def _keys(df, is_lcf):
    low = _lowmap(df)
    rid, fb = set(), set()
    for _, r in df.iterrows():
        kr = _rid_key(r, low, is_lcf)
        if kr is not None:
            rid.add(kr)
        kf = _fb_key(r, low, is_lcf)
        if kf is not None:
            fb.add(kf)
    return low, rid, fb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old-gold", required=True)
    ap.add_argument("--new-gold", required=True)
    ap.add_argument("--verbose", action="store_true", help="also print full column lists")
    args = ap.parse_args()

    ob, nb = _read_all(args.old_gold), _read_all(args.new_gold)
    print("OLD sheets:", list(ob.keys()))
    print("NEW sheets:", list(nb.keys()))
    nlow = {s.lower(): s for s in nb}
    KEYCOLS = ("record_id", "source_DOI", "material_name", "material_condition")

    for oname, odf in ob.items():
        if oname.lower() not in nlow:
            print(f"\n[{oname}] SHEET-NAME MISMATCH — not in new gold; all rows unplaceable")
            continue
        ndf = nb[nlow[oname.lower()]]
        is_lcf = oname.lower() == "lcf"
        olow, orid, ofb = _keys(odf, is_lcf)
        nlow2, nrid, nfb = _keys(ndf, is_lcf)

        miss_old = [c for c in KEYCOLS if c.lower() not in olow]
        miss_new = [c for c in KEYCOLS if c.lower() not in nlow2]
        print(f"\n[{oname}] old {len(odf)}  new {len(ndf)}  | "
              f"record_id shared {len(orid & nrid)}  | fallback shared {len(ofb & nfb)}")
        print(f"   missing key cols:  old{miss_old}   new{miss_new}")
        if args.verbose:
            print(f"   OLD cols: {list(odf.columns)}")
            print(f"   NEW cols: {list(ndf.columns)}")
        if len(orid & nrid) == 0 and len(ofb & nfb) == 0:
            o = odf.head(1).iloc[0] if len(odf) else None
            n = ndf.head(1).iloc[0] if len(ndf) else None
            if o is not None:
                print(f"   e.g. OLD: record_id={_get_ci(o, olow, 'record_id')!r}  fallback={_fb_key(o, olow, is_lcf)}")
            if n is not None:
                print(f"   e.g. NEW: record_id={_get_ci(n, nlow2, 'record_id')!r}  fallback={_fb_key(n, nlow2, is_lcf)}")


if __name__ == "__main__":
    main()