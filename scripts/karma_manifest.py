#!/usr/bin/env python3
"""karma_manifest.py — regenerate the ONE canonical asset-library index.

The Karma content pipeline has a single source of truth: a `library.json`
manifest that sits at the root of the karma org's studio-output subtree and
indexes every asset Antje has rendered. The storefront (karma.style) and the
socials/blog queue both read THIS file — there is no second index.

  library root (== S3 prefix  s3://hanzo-studio/orgs/karma/output/
                == studio pod  /app/orgs/karma/output/
                == spark local /home/z/work/hanzo/studio/output/orgs/karma/output/)
    designs/<slug>/<kind>_<role>.png     catalog assets  (ecom|product|lifestyle|hover)
    marketing/<channel>/<name>.png       queue assets    (social|blog|campaign)
    library.json                         <- this file, regenerated here

What this does, and nothing more:
  * walk the tree, classify each image by path -> {design, kind, role}
  * PRESERVE status/caption/tags from the existing library.json (keyed by path)
  * brand-new files enter as status="draft"; deleted files drop out
  * write library.json atomically (tmp + rename) so a reader never sees a
    half-written file, and the S3 mirror sidecar carries it up on its next pass

It writes ONLY library.json. It never touches images and never regenerates
products.json (that file carries human-curated pricing/copy the site owns).

Runs one-shot, or with --watch as the studio pod's `build-manifest` sidecar.
Pure stdlib — no PIL, no deps — so it runs in the studio image or anywhere.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
# Written marketing content (blog posts, campaign briefs, social ad copy) is
# first-class in the ONE index too — a .md under marketing/ is a queue asset
# whose body is the post/brief and whose caption/hashtags(=tags) are set with
# karma-queue.py. Restricted to marketing/ so catalog stays image-only.
MARKETING_TEXT_EXTS = {".md"}
MANIFEST_NAME = "library.json"

# The kinds a catalog asset may carry (designs/<slug>/<kind>_<role>.png). The
# storefront consumes ecom + product + lifestyle; hover is a card-hover alt.
CATALOG_KINDS = {"ecom", "product", "lifestyle", "hover"}
# Marketing channels double as the kind for marketing/<channel>/* assets.
MARKETING_KINDS = {"social", "blog", "campaign"}


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify(relpath: str, slugs: set[str]) -> dict | None:
    """Map a path relative to the library root -> {design, kind, role}.

    Returns None for anything that is not a manifestable asset.
    """
    parts = relpath.split("/")
    stem = os.path.splitext(parts[-1])[0]

    if parts[0] == "marketing" and len(parts) >= 3:
        channel = parts[1]
        kind = channel if channel in MARKETING_KINDS else "campaign"
        # design = leading slug token if it names a real design, else cross-cut.
        head, _, rest = stem.partition("_")
        if head in slugs and rest:
            design, role = head, rest
        else:
            design, role = None, stem
        return {"design": design, "kind": kind, "role": role}

    if parts[0] == "designs" and len(parts) >= 3:
        # 4k/print masters live alongside but are not catalog/queue items.
        if "4k" in parts[2:-1]:
            return None
        design = parts[1]
        kind, sep, role = stem.partition("_")
        if not sep:  # e.g. bare "front.png" -> treat stem as role, kind unknown
            kind, role = "other", stem
        if kind not in CATALOG_KINDS:
            kind = kind if kind in ("other",) else "other"
        return {"design": design, "kind": kind, "role": role}

    return None


def scan(root: str) -> list[str]:
    """All manifestable image paths (relative to root), sorted for stable diffs."""
    found: list[str] = []
    for base, _dirs, files in os.walk(root):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            full = os.path.join(base, f)
            rel = os.path.relpath(full, root)
            is_image = ext in IMAGE_EXTS
            is_mktg_text = (ext in MARKETING_TEXT_EXTS
                            and rel.replace(os.sep, "/").startswith("marketing/"))
            if not (is_image or is_mktg_text):
                continue
            try:
                if os.path.getsize(full) == 0:  # skip a render caught mid-write
                    continue
            except OSError:
                continue
            found.append(rel)
    return sorted(found)


def load_prior(path: str) -> dict[str, dict]:
    """Existing entries keyed by path, so status/caption/tags survive a rebuild."""
    try:
        with open(path, "r") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return {a["path"]: a for a in doc.get("assets", []) if "path" in a}


def build(root: str) -> dict:
    manifest_path = os.path.join(root, MANIFEST_NAME)
    prior = load_prior(manifest_path)

    designs_dir = os.path.join(root, "designs")
    slugs = {d for d in os.listdir(designs_dir)} if os.path.isdir(designs_dir) else set()

    assets: list[dict] = []
    for rel in scan(root):
        norm = rel.replace(os.sep, "/")
        if norm == MANIFEST_NAME:
            continue
        info = classify(norm, slugs)
        if info is None:
            continue
        prev = prior.get(norm, {})
        entry = {
            "path": norm,
            "design": info["design"],
            "kind": info["kind"],
            "role": info["role"],
            "status": prev.get("status", "draft"),
            "tags": prev.get("tags", []),
            "updatedAt": iso(os.path.getmtime(os.path.join(root, rel))),
        }
        caption = prev.get("caption")
        if caption:
            entry["caption"] = caption
        assets.append(entry)

    by_status: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for a in assets:
        by_status[a["status"]] = by_status.get(a["status"], 0) + 1
        by_kind[a["kind"]] = by_kind.get(a["kind"], 0) + 1

    return {
        "_meta": {
            "note": "Canonical Karma asset library. ONE index the storefront + "
                    "socials queue read. Regenerated by karma_manifest.py; edit "
                    "status/caption/tags with karma-queue.py (they are preserved).",
            "prefix": "orgs/karma/output",
            "generatedAt": iso(time.time()),
            "count": len(assets),
            "byStatus": by_status,
            "byKind": by_kind,
        },
        "assets": assets,
    }


def write_atomic(root: str, doc: dict) -> str:
    manifest_path = os.path.join(root, MANIFEST_NAME)
    tmp = os.path.join(root, ".library.json.tmp")
    with open(tmp, "w") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, manifest_path)
    return manifest_path


def run_once(root: str, quiet: bool = False, strict: bool = True) -> dict | None:
    if not os.path.isdir(root):
        msg = f"karma_manifest: root does not exist yet: {root}"
        if strict:
            sys.exit(msg)
        if not quiet:
            print(msg, file=sys.stderr)
        return None  # watch mode: tolerate a tenant with no renders yet
    doc = build(root)
    path = write_atomic(root, doc)
    if not quiet:
        m = doc["_meta"]
        print(f"karma_manifest: {m['count']} assets -> {path}  {m['byStatus']}")
    return doc


def main() -> None:
    ap = argparse.ArgumentParser(description="Regenerate the Karma library.json manifest.")
    ap.add_argument("--root", default=os.environ.get(
        "KARMA_LIBRARY_ROOT", "/app/orgs/karma/output"),
        help="Library root (contains designs/ and marketing/).")
    ap.add_argument("--watch", action="store_true", help="Loop forever.")
    ap.add_argument("--interval", type=int, default=30, help="--watch seconds.")
    args = ap.parse_args()

    if not args.watch:
        run_once(args.root)
        return

    print(f"karma_manifest: watching {args.root} every {args.interval}s", flush=True)
    while True:
        try:
            run_once(args.root, quiet=True, strict=False)
        except Exception as e:  # a bad cycle must never kill the sidecar
            print(f"karma_manifest: cycle error: {e}", file=sys.stderr, flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
