"""Fixture poller: a conditional GET of a made-up feed; emits the items it has not seen."""
import json


def poll(batch, ctx) -> list[dict]:
    cursor = ctx.load_state("cursor", {"etag": "", "seen": []})
    headers = {"If-None-Match": cursor["etag"]} if cursor["etag"] else {}
    status, text, resp = ctx.http.get_text(ctx.settings["url"], headers, params={"feed": ctx.settings["feed"]},
                                           bearer=ctx.secret("api_key"), label="items")
    if status == 304:
        ctx.log(f"batch {batch.id}: not modified")
        return []
    data = json.loads(text) if text.strip() else {}
    seen = set(cursor["seen"])
    out = []
    for item in data.get("items") or []:
        if item["id"] not in seen:
            seen.add(item["id"])
            out.append({"event_type": "item", "key": item["id"], "id": item["id"], "feed": ctx.settings["feed"]})
    ctx.save_state("cursor", {"etag": resp.get("etag", ""), "seen": sorted(seen)})
    ctx.log(f"batch {batch.id}: {len(out)} new item(s)")
    return out
