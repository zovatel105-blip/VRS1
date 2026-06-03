import asyncio
import sys
sys.path.insert(0, "/app/backend")
from motor.motor_asyncio import AsyncIOMotorClient
from config import config
import video_pipeline


async def main(ids):
    client = AsyncIOMotorClient(config.MONGO_URL)
    db = client[config.DB_NAME]
    if not ids:
        polls = await db.polls.find({"layout": "vs"}, {"id": 1}).to_list(length=1000)
        ids = [p["id"] for p in polls]
    print("Recomposing:", ids)
    for vid in ids:
        print(f"\n=== compose {vid} ===")
        try:
            await video_pipeline.compose_vs_video_task(db, vid)
        except Exception as e:
            print("  ERROR:", e)
        doc = await db.polls.find_one({"id": vid}, {"composed_status": 1, "composed_video_url": 1, "composed_hls_url": 1})
        print("  status=", doc.get("composed_status"))
        print("  mp4=", doc.get("composed_video_url"))
        print("  hls=", doc.get("composed_hls_url"))
    client.close()
    print("\nDONE")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
