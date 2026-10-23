"""Kit fixture processor: asks the API for the account's items, emits the new ones."""


def process(batch, ctx) -> list[dict]:
    seen = set(ctx.load_state("seen", []))
    data = ctx.http.get("https://api.example.com/v1/items", params={"account": batch.key},
                        bearer=ctx.secret("api_key"), label="items")
    out = []
    for item in (data.get("items") or [])[: ctx.settings["limit"]]:
        if item["id"] not in seen:
            out.append({"event_type": "changed", "id": item["id"], "owner": ctx.settings["owner"]})
            seen.add(item["id"])
    ctx.save_state("seen", sorted(seen))
    ctx.log(f"batch {batch.id}: {len(out)} new item(s)")
    return out
