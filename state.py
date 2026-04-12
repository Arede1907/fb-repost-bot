"""45 günlük paylaşım geçmişini yönetir."""
import json
import os
from datetime import datetime, timedelta
from config import STATE_FILE

HISTORY_DAYS = 45


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"history": []}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _prune_old_entries(history: list) -> list:
    """45 günden eski girişleri temizler."""
    cutoff = datetime.now() - timedelta(days=HISTORY_DAYS)
    return [
        e for e in history
        if datetime.fromisoformat(e["shared_at"]) > cutoff
    ]


def get_shared_ids(channel_id: str = None, page_ids: list = None) -> set:
    """
    Son 45 gün içinde paylaşılan video ID'lerini döndürür.
    channel_id + page_ids verilirse yalnızca o (kanal, sayfa) ikililerine ait kayıtlar döner.
    Yani: Ekon Kollama → Pandevu kombinasyonuna özel üstü çizili.
    """
    state = load_state()
    history = _prune_old_entries(state.get("history", []))

    if channel_id is None or page_ids is None:
        # Filtre yok → tümünü döndür (geriye dönük uyumluluk)
        return {e["video_id"] for e in history}

    page_ids_set = set(page_ids)
    result = set()
    for e in history:
        e_channel = e.get("channel_id")
        e_page = e.get("page_id")
        # Eski format (channel_id/page_id yok) → atla
        if e_channel is None or e_page is None:
            continue
        if e_channel == channel_id and e_page in page_ids_set:
            result.add(e["video_id"])
    return result


def mark_shared(
    video_id: str,
    title: str,
    channel_id: str = None,
    page_id: str = None,
) -> None:
    """
    Videoyu (channel_id, page_id) kombinasyonu için paylaşıldı olarak işaretler.
    Aynı kombinasyon zaten varsa üstüne yazar.
    """
    state = load_state()
    history = _prune_old_entries(state.get("history", []))

    # Aynı (video, channel, page) üçlüsü varsa önce sil
    history = [
        e for e in history
        if not (
            e["video_id"] == video_id
            and e.get("channel_id") == channel_id
            and e.get("page_id") == page_id
        )
    ]

    entry = {
        "video_id": video_id,
        "title": title,
        "shared_at": datetime.now().isoformat(),
    }
    if channel_id is not None:
        entry["channel_id"] = channel_id
    if page_id is not None:
        entry["page_id"] = page_id

    history.append(entry)
    state["history"] = history
    save_state(state)


def is_shared(video_id: str, channel_id: str = None, page_id: str = None) -> bool:
    page_ids = [page_id] if page_id else None
    return video_id in get_shared_ids(channel_id, page_ids)
