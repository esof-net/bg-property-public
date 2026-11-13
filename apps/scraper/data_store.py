"""Persistent scraper data store: GitHub Release assets instead of commits to data/.

Why: committing data/ (hundreds of MB of history_*/leads_* JSON, rewritten every run) pushed the scrape repo over
GitHub's repository size quota. The mutable files now live as assets of ONE fixed release (tag `data-store`) and
workflows `pull` them before scraping and `push` them afterwards. Nothing here touches git.

    python apps/scraper/data_store.py pull [--all | name-or-@alias ...]
    python apps/scraper/data_store.py push name-or-@alias [...]

Aliases (PORTAL_FILES below): @homes @alo @imot @olx @bazar @imoti_bg @bcpea @net, @geocode, @home-stats,
@all-history, @all-leads. A file in a push list that was never changed (same sha256 as at pull) is skipped.

Environment:
    DATA_STORE_REPO   owner/repo that holds the release (default esof-net/bg-property-public)
    DATA_STORE_TAG    release tag (default data-store)
    GITHUB_TOKEN      required for push; optional for pull (the release is public)
    DATA_STORE_DIR    local folder (default paths.DATA_DIR)

Concurrency (replaces `git pull --rebase`): `pull` records each asset's id/updated_at/sha256 in
data/.store_manifest.json. `push` re-reads the release first; if another workflow replaced an asset after our pull,
history*.json(.gz) files are merged with merge_history_conflict.merge_history (union of snapshots, nothing from either
side discarded) and derived files (leads_*, stats) keep the remote copy, which self-heals on the next run. Up to 5 attempts.
"""
import argparse
import gzip
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from merge_history_conflict import is_history_file, merge_history  # noqa: E402
from paths import DATA_DIR  # noqa: E402

DEFAULT_REPO = "esof-net/bg-property-public"
DEFAULT_TAG = "data-store"
MANIFEST_NAME = ".store_manifest.json"
TMP_MARKER = ".uploading-"  # temp asset names: <name>.uploading-<run id>
MAX_ASSET_BYTES = 2 * 1024 ** 3  # GitHub's per-asset limit
MAX_PUSH_ATTEMPTS = 5
API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"


class StoreError(RuntimeError):
    pass


# One place that knows which files belong to which portal. Workflows say `@homes` instead of listing files.
PORTAL_FILES = {
    "net": ["history.json", "leads.json"],  # imoti.net (scraper.py)
    "alo": ["history_alo.json.gz", "leads_alo.json.gz"],
    "homes": ["history_homes.json.gz", "leads_homes.json.gz"],
    "imot": ["history_imot.json.gz", "leads_imot.json.gz", "imot_grid_state.json"],
    "olx": ["history_olx.json.gz", "leads_olx.json.gz", "olx_grid_state.json"],
    "bazar": ["history_bazar.json.gz", "leads_bazar.json.gz", "bazar_grid_state.json"],
    "imoti_bg": ["history_imoti_bg.json", "leads_imoti_bg.json"],
    "bcpea": ["history_bcpea.json", "leads_bcpea.json"],
}
ALIASES = {f"@{k}": v for k, v in PORTAL_FILES.items()}
ALIASES["@geocode"] = ["geocode_cache.json"]
ALIASES["@home-stats"] = ["home_stats.json", "area_index.json"]
ALIASES["@all-history"] = [f for v in PORTAL_FILES.values() for f in v if f.startswith("history")]
ALIASES["@all-leads"] = [f for v in PORTAL_FILES.values() for f in v if f.startswith("leads")]


def expand(names):
    """Expand @aliases, keep order, drop duplicates. An unknown @alias is an error: a typo must not silently skip a file."""
    out = []
    for n in names:
        if n.startswith("@"):
            if n not in ALIASES:
                raise StoreError(f"unknown alias {n} (known: {', '.join(sorted(ALIASES))})")
            items = ALIASES[n]
        else:
            items = [n]
        out.extend(i for i in items if i not in out)
    return out


def is_cache_file(name):
    return Path(name).name == "geocode_cache.json"


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _check(resp, what):
    """Raise with the status AND the response body: a failure that logs no body teaches nothing (CLAUDE.md)."""
    if resp.status_code >= 400:
        raise StoreError(f"{what} failed: HTTP {resp.status_code}: {resp.text[:1000]}")
    return resp


class GitHubStore:
    """The only class that talks HTTP. Tests replace it with an in-memory fake exposing the same four methods."""

    def __init__(self, repo, tag, token=None, session=None):
        self.repo, self.tag, self.token = repo, tag, token
        self.session = session or requests.Session()
        self._release_id = None

    def _headers(self, accept="application/vnd.github+json"):
        h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _release(self):
        r = _check(self.session.get(f"{API}/repos/{self.repo}/releases/tags/{self.tag}", headers=self._headers(), timeout=60),
                   f"look up release {self.repo}@{self.tag} (create it once: see docs/decisions.md)")
        data = r.json()
        self._release_id = data["id"]
        return data

    def list_assets(self):
        """name -> {id, name, updated_at, size}; temp upload leftovers are hidden."""
        assets = {}
        page = 1
        rel = self._release()
        # the release payload carries at most 30 assets; page through the assets endpoint for the full list
        while True:
            r = _check(self.session.get(f"{API}/repos/{self.repo}/releases/{rel['id']}/assets", headers=self._headers(),
                                        params={"per_page": 100, "page": page}, timeout=60), "list release assets")
            batch = r.json()
            for a in batch:
                if TMP_MARKER not in a["name"]:
                    assets[a["name"]] = {k: a[k] for k in ("id", "name", "updated_at", "size")}
            if len(batch) < 100:
                return assets
            page += 1

    def download(self, asset, dest):
        url = f"{API}/repos/{self.repo}/releases/assets/{asset['id']}"
        with self.session.get(url, headers=self._headers("application/octet-stream"), stream=True, timeout=300) as r:
            _check(r, f"download {asset['name']}")
            tmp = Path(str(dest) + ".part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    f.write(chunk)
        os.replace(tmp, dest)

    def upload_replace(self, name, path, old_asset, run_id="local"):
        """Upload under a temp name, delete the old asset, rename: the window with no asset is as short as possible."""
        if self._release_id is None:
            self._release()
        tmp_name = f"{name}{TMP_MARKER}{run_id}"
        with open(path, "rb") as f:
            r = _check(self.session.post(f"{UPLOADS}/repos/{self.repo}/releases/{self._release_id}/assets",
                                         params={"name": tmp_name}, data=f, timeout=900,
                                         headers={**self._headers(), "Content-Type": "application/octet-stream"}),
                       f"upload {name}")
        new_id = r.json()["id"]
        if old_asset is not None:
            _check(self.session.delete(f"{API}/repos/{self.repo}/releases/assets/{old_asset['id']}", headers=self._headers(),
                                       timeout=60), f"delete old {name}")
        _check(self.session.patch(f"{API}/repos/{self.repo}/releases/assets/{new_id}", headers=self._headers(),
                                  json={"name": name}, timeout=60), f"rename {tmp_name} -> {name}")


def load_manifest(data_dir):
    p = Path(data_dir) / MANIFEST_NAME
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_manifest(data_dir, manifest):
    (Path(data_dir) / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def _read_text(path):
    raw = Path(path).read_bytes()
    if Path(path).name.endswith(".gz"):
        raw = gzip.decompress(raw)
    return raw.decode("utf-8")


def _write_json_text_like(path, merged):
    """Same on-disk formats as geo_utils.save_json_any: gzip+compact for .gz, indent=2 otherwise."""
    if Path(path).name.endswith(".gz"):
        with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
            json.dump(merged, f, ensure_ascii=False, separators=(",", ":"))
    else:
        Path(path).write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")


def pull(store, names, data_dir=DATA_DIR, retries=3, sleep=time.sleep):
    """Download `names` (or every asset when names is None) into data_dir and record them in the manifest."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(data_dir)
    assets = store.list_assets()
    wanted = sorted(assets) if names is None else expand(names)
    missing = [n for n in wanted if n not in assets]
    if missing:
        raise StoreError(f"not in release {store.repo}@{store.tag}: {', '.join(missing)} (available: {', '.join(sorted(assets))})")
    for n in wanted:
        a = assets[n]
        for attempt in range(1, retries + 1):
            try:
                store.download(a, data_dir / n)
                break
            except StoreError as e:
                # an asset replaced mid-pull 404s once: re-list and retry before giving up
                print(f"::warning::pull {n} attempt {attempt}/{retries}: {e}")
                if attempt == retries:
                    raise
                sleep(attempt * 5)
                a = store.list_assets().get(n) or a
        manifest[n] = {"id": a["id"], "updated_at": a["updated_at"], "sha256": sha256_of(data_dir / n)}
        print(f"pulled {n} ({a['size'] / 1e6:.1f} MB)")
    save_manifest(data_dir, manifest)
    return wanted


def push(store, names, data_dir=DATA_DIR, run_id="local", sleep=time.sleep):
    """Upload every named file whose content changed since pull. Returns the list of names uploaded."""
    data_dir = Path(data_dir)
    manifest = load_manifest(data_dir)
    uploaded = []
    for n in expand(names):
        path = data_dir / n
        if not path.exists():
            raise StoreError(f"push {n}: {path} does not exist")
        for attempt in range(1, MAX_PUSH_ATTEMPTS + 1):
            size = path.stat().st_size
            if size > MAX_ASSET_BYTES:
                raise StoreError(f"{n} is {size / 1e9:.2f} GB, over GitHub's 2 GB per-asset limit - not uploading")
            local_sha = sha256_of(path)
            seen = manifest.get(n)
            if seen and seen.get("sha256") == local_sha:
                print(f"unchanged, skipping {n}")
                break
            remote = store.list_assets().get(n)
            moved = remote is not None and (seen is None or remote["updated_at"] != seen.get("updated_at") or remote["id"] != seen.get("id"))
            if moved:
                # another workflow replaced it after our pull (or we never pulled it): reconcile, then retry
                remote_copy = data_dir / ("remote-" + n)  # keep the .gz suffix: _read_text decides by filename
                store.download(remote, remote_copy)
                if is_history_file(n):
                    merged = merge_history(_read_text(remote_copy), _read_text(path))
                    _write_json_text_like(path, merged)
                    print(f"{n}: remote changed since pull - merged {len(merged)} listings (nothing from either side discarded)")
                    manifest[n] = {"id": remote["id"], "updated_at": remote["updated_at"], "sha256": sha256_of(remote_copy)}
                    remote_copy.unlink()
                    save_manifest(data_dir, manifest)
                    continue  # re-check, then upload the merged file
                if is_cache_file(n):
                    # many workflows add to the geocode cache: union the two dicts (our new entries win) so nobody's lookups are lost
                    remote_obj, local_obj = json.loads(_read_text(remote_copy)), json.loads(_read_text(path))
                    if isinstance(remote_obj, dict) and isinstance(local_obj, dict):
                        remote_obj.update(local_obj)
                        _write_json_text_like(path, remote_obj)
                        print(f"{n}: remote changed since pull - unioned to {len(remote_obj)} entries")
                        manifest[n] = {"id": remote["id"], "updated_at": remote["updated_at"], "sha256": sha256_of(remote_copy)}
                        remote_copy.unlink()
                        save_manifest(data_dir, manifest)
                        continue
                os.replace(remote_copy, path)
                print(f"::warning::{n}: remote changed since pull and it is derived data - keeping the remote copy, it self-heals next run")
                manifest[n] = {"id": remote["id"], "updated_at": remote["updated_at"], "sha256": sha256_of(path)}
                save_manifest(data_dir, manifest)
                break
            try:
                store.upload_replace(n, path, remote, run_id=run_id)
            except StoreError as e:
                print(f"::warning::push {n} attempt {attempt}/{MAX_PUSH_ATTEMPTS}: {e}")
                if attempt == MAX_PUSH_ATTEMPTS:
                    raise
                sleep(attempt * 5)
                continue
            fresh = store.list_assets().get(n)
            manifest[n] = {"id": fresh["id"] if fresh else None, "updated_at": fresh["updated_at"] if fresh else None, "sha256": local_sha}
            save_manifest(data_dir, manifest)
            uploaded.append(n)
            print(f"pushed {n} ({size / 1e6:.1f} MB)")
            break
        else:
            raise StoreError(f"push {n}: still conflicting after {MAX_PUSH_ATTEMPTS} attempts - giving up")
    return uploaded


def _store_from_env():
    return GitHubStore(os.environ.get("DATA_STORE_REPO", DEFAULT_REPO), os.environ.get("DATA_STORE_TAG", DEFAULT_TAG),
                       os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pull")
    p.add_argument("names", nargs="*")
    p.add_argument("--all", action="store_true")
    q = sub.add_parser("push")
    q.add_argument("names", nargs="+")
    args = ap.parse_args(argv)
    data_dir = Path(os.environ.get("DATA_STORE_DIR") or DATA_DIR)
    store = _store_from_env()
    try:
        if args.cmd == "pull":
            if not args.all and not args.names:
                ap.error("pull needs --all or at least one file name")
            pull(store, None if args.all else args.names, data_dir)
        else:
            if not store.token:
                raise StoreError("push needs GITHUB_TOKEN")
            push(store, args.names, data_dir, run_id=os.environ.get("GITHUB_RUN_ID", "local"))
    except StoreError as e:
        print(f"::error::data_store {args.cmd}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
