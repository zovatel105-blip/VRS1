"""Reprocesa VS posts: limpia assets colisionados y regenera HLS/optimized/thumb
con claves únicas (poll+pregunta+opción) en vs_questions[].options[].

Uso:
  python reprocess_vs.py            -> TODOS los posts layout=vs
  python reprocess_vs.py <id> ...   -> sólo esos vs_ids
"""
import asyncio
import sys

sys.path.insert(0, "/app/backend")

import re
from motor.motor_asyncio import AsyncIOMotorClient
from config import config
import video_pipeline

VIDEO_RE = r"\.(mp4|mov|webm|avi|m4v|mkv)(\?|$)"


def _is_video(o):
    mt = (o.get("media_type") or "").lower()
    u = o.get("media_url") or o.get("image") or ""
    return ("video" in mt) or bool(re.search(VIDEO_RE, str(u), re.IGNORECASE))


async def _clear_assets(db, poll):
    """Borra hls_url/optimized_media_url/thumbnail_url de poll.options,
    vs_questions[].options[] y vs_experiences.questions[].options[] para que
    el reprocesamiento regenere todo con claves únicas."""
    poll_id = poll["id"]
    vs_id = poll.get("vs_id") or poll_id
    unset = {
        "options.$[].hls_url": "",
        "options.$[].optimized_media_url": "",
        "options.$[].thumbnail_url": "",
        "vs_questions.$[].options.$[].hls_url": "",
        "vs_questions.$[].options.$[].optimized_media_url": "",
        "vs_questions.$[].options.$[].thumbnail_url": "",
    }
    try:
        await db.polls.update_one({"id": poll_id}, {"$unset": unset})
    except Exception as e:
        print(f"  clear polls failed: {e}")
    try:
        await db.vs_experiences.update_one(
            {"id": vs_id},
            {"$unset": {
                "questions.$[].options.$[].hls_url": "",
                "questions.$[].options.$[].optimized_media_url": "",
                "questions.$[].options.$[].thumbnail_url": "",
            }},
        )
    except Exception as e:
        print(f"  clear vs_experiences failed: {e}")


async def main(ids):
    client = AsyncIOMotorClient(config.MONGO_URL)
    db = client[config.DB_NAME]

    if ids:
        polls = []
        for vid in ids:
            p = await db.polls.find_one({"id": vid})
            if p:
                polls.append(p)
    else:
        polls = await db.polls.find({"layout": "vs"}).to_list(length=1000)

    print(f"Reprocessing {len(polls)} VS posts: {[p['id'] for p in polls]}")

    for poll in polls:
        vid = poll["id"]
        print(f"\n=== VS {vid} ===")
        await _clear_assets(db, poll)
        try:
            await video_pipeline.process_poll_media(db, vid)
        except Exception as e:
            print(f"  ERROR: {e}")
        fresh = await db.polls.find_one({"id": vid}, {"vs_questions": 1})
        for qi, q in enumerate(fresh.get("vs_questions", []) or []):
            for o in q.get("options", []) or []:
                if _is_video(o):
                    print(
                        f"  q{qi} opt={o.get('id')} "
                        f"hls={o.get('hls_url')}"
                    )
    client.close()
    print("\nDONE")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
