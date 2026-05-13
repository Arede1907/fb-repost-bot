"""
İçerik Keşif Modülü — Apify'dan webhook ile gelen trending FB postlarını
saklar, dedup'lar ve worker review akışını yönetir.

Akış:
1) Apify scheduled task günde 1+ çalışır, sonuçları webhook'a POST eder
2) /api/discovery/webhook → bu modüldeki ingest_apify_payload()
3) UI (/newtema/kesfet) → list_items() / set_status()
"""
import hashlib
import json
import os
import threading
from datetime import datetime
from typing import Any

import requests

try:
    from apify_client import ApifyClient
    _HAS_SDK = True
except ImportError:
    _HAS_SDK = False

_BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
STORE_FILE       = os.path.join(_BASE_DIR, "discovery_store.json")
CANDIDATES_FILE  = os.path.join(_BASE_DIR, "discovery_candidates.json")

_lock = threading.Lock()


# ── Apify SDK helper ─────────────────────────────────────────────────────────

def _apify_client() -> Any:
    """Lazy-init ApifyClient. Token .env'den okunur."""
    token = os.getenv("APIFY_API_TOKEN", "")
    if not _HAS_SDK or not token:
        return None
    return ApifyClient(token)


# ── Storage ─────────────────────────────────────────────────────────────────

def _load() -> dict:
    if not os.path.exists(STORE_FILE):
        return {"version": 1, "items": {}}
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[discovery] load failed: {e}")
        return {"version": 1, "items": {}}


def _save(data: dict) -> None:
    try:
        tmp = STORE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STORE_FILE)
    except Exception as e:
        print(f"[discovery] save failed: {e}")


# ── Yardımcılar ─────────────────────────────────────────────────────────────

def _content_id(source_url: str, fallback: str = "") -> str:
    """Aynı post tekrar gelirse aynı id üretir → dedup."""
    base = source_url or fallback or datetime.now().isoformat()
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


def _coerce_int(v: Any) -> int:
    try:
        if v is None:
            return 0
        if isinstance(v, (int, float)):
            return int(v)
        return int(str(v).replace(",", "").replace(".", "").strip() or 0)
    except Exception:
        return 0


def _extract_apify_fields(raw: dict) -> dict | None:
    """Apify Facebook Posts Scraper farklı actor'lerden geliyor olabilir,
    field isimleri değişebilir. Esnek extraction yap."""
    if not isinstance(raw, dict):
        return None

    source_url = (
        raw.get("url")
        or raw.get("postUrl")
        or raw.get("permalink")
        or raw.get("link")
        or ""
    )
    if not source_url:
        return None

    caption = (
        raw.get("text")
        or raw.get("message")
        or raw.get("caption")
        or raw.get("content")
        or ""
    ).strip()

    thumbnail = (
        raw.get("thumbnailUrl")
        or raw.get("thumbnail")
        or raw.get("image")
        or raw.get("previewImage")
        or ""
    )

    # Media (foto / video) listesi
    media: list[dict] = []
    raw_media = raw.get("media") or raw.get("attachments") or []
    if isinstance(raw_media, list):
        for m in raw_media:
            if not isinstance(m, dict):
                continue
            media.append({
                "type":      m.get("type") or m.get("mediaType") or "unknown",
                "url":       m.get("url") or m.get("src") or "",
                "thumbnail": m.get("thumbnail") or m.get("thumbnailUrl") or "",
            })
    video_url = raw.get("videoUrl") or raw.get("video_url") or ""
    if video_url and not any(x["type"] == "video" for x in media):
        media.append({"type": "video", "url": video_url, "thumbnail": thumbnail})

    # Engagement
    metrics = {
        "likes":     _coerce_int(raw.get("likes")
                                 or raw.get("likesCount")
                                 or raw.get("topReactionsCount")),
        "comments":  _coerce_int(raw.get("comments")
                                 or raw.get("commentsCount")),
        "shares":    _coerce_int(raw.get("shares")
                                 or raw.get("sharesCount")),
        "views":     _coerce_int(raw.get("views")
                                 or raw.get("viewsCount")
                                 or raw.get("playCount")),
        "reactions": _coerce_int(raw.get("reactions")
                                 or raw.get("reactionsCount")
                                 or raw.get("totalReactions")),
    }
    # Toplam engagement skoru (sıralama için)
    metrics["score"] = (
        metrics["likes"]
        + metrics["comments"] * 3        # comment daha değerli
        + metrics["shares"] * 5          # share en değerli
        + metrics["reactions"]
        + metrics["views"] // 50         # view'i hafiflet (videolarda şişer)
    )

    # Kaynak sayfa bilgisi
    user_obj = raw.get("user") or raw.get("author") or raw.get("page") or {}
    source_page = {
        "id":   user_obj.get("id")   or user_obj.get("pageId")   or "",
        "name": user_obj.get("name") or user_obj.get("pageName") or "",
        "url":  user_obj.get("url")  or user_obj.get("pageUrl")  or "",
    }

    published_at = (
        raw.get("publishedTime")
        or raw.get("publishedAt")
        or raw.get("time")
        or raw.get("date")
        or ""
    )

    return {
        "source_url":   source_url,
        "caption":      caption,
        "thumbnail":    thumbnail,
        "media":        media,
        "metrics":      metrics,
        "source_page":  source_page,
        "published_at": str(published_at),
    }


# ── Public API ──────────────────────────────────────────────────────────────

def ingest_apify_payload(payload: Any) -> dict:
    """Apify webhook payload'ını işle, yeni item'ları kaydet.
    Apify çeşitli format'larda gelebilir:
      - {"resource": {"defaultDatasetId": "..."}, ...}  → dataset fetch gerekir
      - {"data": [...]} veya direkt [...] → item array
      - tek bir post dict'i
    """
    items_raw: list[dict] = []

    if isinstance(payload, list):
        items_raw = [x for x in payload if isinstance(x, dict)]
    elif isinstance(payload, dict):
        if "data" in payload and isinstance(payload["data"], list):
            items_raw = [x for x in payload["data"] if isinstance(x, dict)]
        elif "items" in payload and isinstance(payload["items"], list):
            items_raw = [x for x in payload["items"] if isinstance(x, dict)]
        elif "resource" in payload:
            # Apify "ACTOR.RUN.SUCCEEDED" event — dataset polling gerek
            return {"ok": False, "reason": "dataset_polling_required",
                    "dataset_id": payload.get("resource", {}).get("defaultDatasetId")}
        else:
            # Tek post mu? source URL benzeri bir alan varsa kabul et
            if any(k in payload for k in ("url", "postUrl", "permalink", "link")):
                items_raw = [payload]

    if not items_raw:
        return {"ok": False, "reason": "no_items"}

    with _lock:
        store = _load()
        items = store.setdefault("items", {})

        new_count       = 0
        updated_count   = 0
        rejected_count  = 0

        for raw in items_raw:
            extracted = _extract_apify_fields(raw)
            if not extracted:
                rejected_count += 1
                continue

            cid = _content_id(extracted["source_url"])
            existing = items.get(cid)

            if existing:
                # Metrikleri güncelle (engagement zamanla artar)
                existing["metrics"] = extracted["metrics"]
                existing["last_seen_at"] = datetime.now().isoformat(timespec="seconds")
                updated_count += 1
            else:
                items[cid] = {
                    "id":            cid,
                    **extracted,
                    "status":        "pending",   # pending | starred | posted | dismissed
                    "discovered_at": datetime.now().isoformat(timespec="seconds"),
                    "last_seen_at":  datetime.now().isoformat(timespec="seconds"),
                    "reviewed_by":   None,
                    "reviewed_at":   None,
                    "notes":         "",
                    "raw":           raw,
                }
                new_count += 1

        _save(store)

    return {
        "ok":       True,
        "new":      new_count,
        "updated":  updated_count,
        "rejected": rejected_count,
        "total":    len(items),
    }


def list_items(status: str | None = None,
               sort_by: str = "score",
               limit:   int  = 200) -> list[dict]:
    store = _load()
    items = list(store.get("items", {}).values())

    if status and status != "all":
        items = [x for x in items if x.get("status") == status]

    if sort_by == "score":
        items.sort(key=lambda x: x.get("metrics", {}).get("score", 0), reverse=True)
    elif sort_by == "date":
        items.sort(key=lambda x: x.get("discovered_at", ""), reverse=True)
    elif sort_by == "views":
        items.sort(key=lambda x: x.get("metrics", {}).get("views", 0), reverse=True)

    return items[:limit]


def counts() -> dict:
    store = _load()
    items = list(store.get("items", {}).values())
    return {
        "all":       len(items),
        "pending":   sum(1 for x in items if x.get("status") == "pending"),
        "starred":   sum(1 for x in items if x.get("status") == "starred"),
        "posted":    sum(1 for x in items if x.get("status") == "posted"),
        "dismissed": sum(1 for x in items if x.get("status") == "dismissed"),
    }


def set_status(content_id: str, status: str, note: str = "") -> bool:
    if status not in ("pending", "starred", "posted", "dismissed"):
        return False
    with _lock:
        store = _load()
        item  = store.get("items", {}).get(content_id)
        if not item:
            return False
        item["status"]      = status
        item["reviewed_at"] = datetime.now().isoformat(timespec="seconds")
        if note:
            item["notes"] = note
        _save(store)
    return True


def delete_item(content_id: str) -> bool:
    with _lock:
        store = _load()
        if content_id in store.get("items", {}):
            del store["items"][content_id]
            _save(store)
            return True
    return False


def get_item(content_id: str) -> dict | None:
    return _load().get("items", {}).get(content_id)


def fetch_apify_dataset(dataset_id: str, api_token: str = "",
                        limit: int = 1000) -> list[dict]:
    """Apify dataset'inden item'ları çek. Önce SDK, yoksa raw HTTP fallback."""
    client = _apify_client()
    if client:
        try:
            return list(client.dataset(dataset_id).iterate_items())
        except Exception as e:
            print(f"[discovery] SDK dataset fetch failed, fallback HTTP: {e}")

    # HTTP fallback
    token = api_token or os.getenv("APIFY_API_TOKEN", "")
    try:
        resp = requests.get(
            f"https://api.apify.com/v2/datasets/{dataset_id}/items",
            params={"token": token, "format": "json",
                    "clean": "true", "limit": str(limit)},
            timeout=60,
        )
        if not resp.ok:
            print(f"[discovery] dataset fetch failed: HTTP {resp.status_code}")
            return []
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[discovery] dataset fetch error: {e}")
        return []


# ── Aday Sayfalar (Candidates) Storage ───────────────────────────────────────
# Apify search-scraper'dan dönen, "havuza alınabilecek" sayfaların listesi.

def _load_candidates() -> dict:
    if not os.path.exists(CANDIDATES_FILE):
        return {"version": 1, "items": {}}
    try:
        with open(CANDIDATES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"version": 1, "items": {}}


def _save_candidates(data: dict) -> None:
    try:
        tmp = CANDIDATES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CANDIDATES_FILE)
    except Exception as e:
        print(f"[discovery] candidates save failed: {e}")


def _extract_candidate(raw: dict) -> dict | None:
    """facebook-search-scraper sonuçlarından aday sayfa çıkar."""
    if not isinstance(raw, dict):
        return None
    page_url = (raw.get("pageUrl") or raw.get("url")
                or raw.get("facebookUrl") or raw.get("permalink") or "")
    if not page_url:
        return None

    name = (raw.get("title") or raw.get("name")
            or raw.get("pageName") or raw.get("displayName") or "")

    followers = raw.get("followers") or raw.get("followersCount") or 0
    likes     = raw.get("likes") or raw.get("likesCount") or 0
    try:
        followers = int(str(followers).replace(",", "").replace(".", "") or 0)
    except Exception:
        followers = 0
    try:
        likes = int(str(likes).replace(",", "").replace(".", "") or 0)
    except Exception:
        likes = 0

    # Kategori — string ya da liste olabilir
    cat = raw.get("category") or raw.get("pageCategory") or raw.get("categories")
    if isinstance(cat, list):
        # "Page" / "Place" gibi generic'leri filtrele
        filtered = [c for c in cat if isinstance(c, str)
                    and c.strip().lower() not in ("page", "place", "")]
        category = filtered[0] if filtered else (cat[0] if cat else "")
    else:
        category = cat or ""

    # Description — intro birinci öncelik, sonra description/bio/about
    description = (raw.get("intro") or raw.get("description")
                   or raw.get("bio") or raw.get("about") or "")

    # Website — list olarak da gelebilir
    website = raw.get("website") or raw.get("websites") or ""
    if isinstance(website, list):
        website = website[0] if website else ""

    return {
        "page_url":     page_url,
        "name":         name,
        "category":     category,
        "is_verified":  bool(raw.get("isVerified") or raw.get("verified")),
        "followers":    followers,
        "likes":        likes,
        "description":  description,
        "address":      raw.get("address") or "",
        "phone":        raw.get("phone") or "",
        "website":      website,
        "photo":        raw.get("photo") or raw.get("profilePictureUrl")
                        or raw.get("profilePhoto") or "",
    }


def ingest_candidates(payload: Any) -> dict:
    """search-scraper webhook'undan gelen veriyi candidates.json'a yaz."""
    items_raw: list[dict] = []
    if isinstance(payload, list):
        items_raw = [x for x in payload if isinstance(x, dict)]
    elif isinstance(payload, dict):
        if "data" in payload and isinstance(payload["data"], list):
            items_raw = [x for x in payload["data"] if isinstance(x, dict)]
        elif "resource" in payload:
            return {"ok": False, "reason": "dataset_polling_required",
                    "dataset_id": payload.get("resource", {}).get("defaultDatasetId")}

    if not items_raw:
        return {"ok": False, "reason": "no_items"}

    with _lock:
        store = _load_candidates()
        items = store.setdefault("items", {})
        new_count = 0
        updated_count = 0

        for raw in items_raw:
            cand = _extract_candidate(raw)
            if not cand:
                continue
            cid = hashlib.sha1(cand["page_url"].encode("utf-8")).hexdigest()[:12]
            existing = items.get(cid)
            if existing:
                # Followers/likes güncelle
                existing.update({k: cand[k] for k in ("followers", "likes")
                                 if cand[k]})
                existing["last_seen_at"] = datetime.now().isoformat(timespec="seconds")
                updated_count += 1
            else:
                items[cid] = {
                    "id":            cid,
                    **cand,
                    "status":        "pending",  # pending | added | dismissed
                    "discovered_at": datetime.now().isoformat(timespec="seconds"),
                    "last_seen_at":  datetime.now().isoformat(timespec="seconds"),
                    "raw":           raw,
                }
                new_count += 1

        _save_candidates(store)

    return {"ok": True, "new": new_count, "updated": updated_count,
            "total": len(items)}


def list_candidates(status: str | None = None,
                    sort_by: str = "followers",
                    min_followers: int = 0,
                    verified_only: bool = False,
                    has_bio: bool = False,
                    category_q: str = "",
                    limit: int = 500) -> list[dict]:
    items = list(_load_candidates().get("items", {}).values())

    # Eski kayıtlar yeni schema'ya backfill
    for it in items:
        if not it.get("description") or not it.get("category"):
            raw = it.get("raw") or {}
            patched = _extract_candidate(raw)
            if patched:
                for k in ("category", "description", "website", "photo"):
                    if patched.get(k) and not it.get(k):
                        it[k] = patched[k]

    if status and status != "all":
        items = [x for x in items if x.get("status") == status]

    if min_followers > 0:
        items = [x for x in items if (x.get("followers") or 0) >= min_followers]

    if verified_only:
        items = [x for x in items if x.get("is_verified")]

    if has_bio:
        items = [x for x in items if (x.get("description") or "").strip()]

    if category_q:
        q = category_q.lower()
        items = [x for x in items if q in (x.get("category") or "").lower()]

    if sort_by == "followers":
        items.sort(key=lambda x: x.get("followers", 0), reverse=True)
    elif sort_by == "name":
        items.sort(key=lambda x: x.get("name", ""))
    elif sort_by == "category":
        items.sort(key=lambda x: ((x.get("category") or "zzz").lower(),
                                  -x.get("followers", 0)))
    else:
        items.sort(key=lambda x: x.get("discovered_at", ""), reverse=True)
    return items[:limit]


def candidates_counts() -> dict:
    items = list(_load_candidates().get("items", {}).values())
    return {
        "all":       len(items),
        "pending":   sum(1 for x in items if x.get("status") == "pending"),
        "added":     sum(1 for x in items if x.get("status") == "added"),
        "dismissed": sum(1 for x in items if x.get("status") == "dismissed"),
    }


def set_candidate_status(cid: str, status: str) -> bool:
    if status not in ("pending", "added", "dismissed"):
        return False
    with _lock:
        store = _load_candidates()
        item = store.get("items", {}).get(cid)
        if not item:
            return False
        item["status"] = status
        item["reviewed_at"] = datetime.now().isoformat(timespec="seconds")
        _save_candidates(store)
    return True


def trigger_candidate_search(keywords: list[str], locations: list[str] | None = None,
                             max_items: int = 50,
                             task_id: str = "kW6KHaDYJLAN046N8") -> dict:
    """fbbot-aday-bul task'ını yeni input'la tetikler.
    Webhook ayarlı olduğu için sonuç otomatik ingest_candidates'e düşer."""
    client = _apify_client()
    if not client:
        return {"ok": False, "error": "SDK or token missing"}

    try:
        run = client.task(task_id).start(task_input={
            "categories": keywords,
            "locations":  locations or [],
            "maxItems":   max_items,
        })
        return {"ok": True, "run_id": run.get("id"), "status": run.get("status")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def trigger_alt_scraper(start_urls: list[str], max_posts: int = 20,
                        actor_id: str = "jezreel06~actor-facebook-scraper-yashodhank"
                        ) -> dict:
    """Alternatif yashodhank scraper'ı tetikler — yedek/karşılaştırma için."""
    client = _apify_client()
    if not client:
        return {"ok": False, "error": "SDK or token missing"}

    try:
        run = client.actor(actor_id).start(run_input={
            "startUrls": [{"url": u} for u in start_urls],
            "maxPosts":  max_posts,
            "proxyConfiguration": {"useApifyProxy": True},
        })
        return {"ok": True, "run_id": run.get("id"), "status": run.get("status")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_alt_run_status(run_id: str) -> dict:
    """Alt scraper run durumu + dataset items (varsa)."""
    client = _apify_client()
    if not client:
        return {"ok": False, "error": "SDK or token missing"}
    try:
        run = client.run(run_id).get()
        result = {
            "ok":      True,
            "status":  run.get("status"),
            "stats":   run.get("stats", {}),
            "started": run.get("startedAt"),
            "ended":   run.get("finishedAt"),
        }
        if run.get("status") == "SUCCEEDED":
            ds = run.get("defaultDatasetId")
            if ds:
                items = list(client.dataset(ds).iterate_items())
                result["items"] = items
                result["item_count"] = len(items)
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}
