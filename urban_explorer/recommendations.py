import math

from urban_explorer.models import Coordinates, ExploreFilters, Place


def place_introduction(place: Place) -> str:
    location = place.area or place.city
    sentences = [f"位于{location}的{place.category or '场所'}。"]
    if place.tags:
        sentences.append(f"地图收录的特色有{'、'.join(place.tags[:3])}。")
    if place.cost is not None:
        sentences.append(f"人均参考约 {place.cost:g} 元。")
    if place.opening_hours:
        sentences.append(f"营业时间：{place.opening_hours}。")
    if not place.tags and place.cost is None and not place.opening_hours and place.address:
        sentences.append(f"地址在{place.address}。")
    return "".join(sentences)


def distance_m(a: Coordinates, b: Coordinates) -> float:
    lat1, lat2 = math.radians(a.latitude), math.radians(b.latitude)
    dlat = lat2 - lat1
    dlon = math.radians(b.longitude - a.longitude)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return round(6371000 * 2 * math.asin(min(1, math.sqrt(h))))


def filter_places(places: list[Place], filters: ExploreFilters, anchor: Place | None = None) -> list[Place]:
    filtered = []
    seen_ids = set()
    for original in places:
        p = original.model_copy(deep=True)
        # Recompute query-specific evidence; saved results may belong to older filters.
        p.match_reasons = []
        p.supervisor_reason = ""
        p.unverified_preferences = []
        p.distance_m = None
        if p.city.removesuffix("市") != filters.city or p.kind != filters.kind:
            continue
        if filters.area and filters.area not in (p.area + p.address):
            continue
        if filters.category and filters.category not in (p.category + " ".join(p.tags)):
            continue
        if filters.budget is not None and filters.budget_scope in (filters.kind, "all"):
            if p.cost is None or p.cost > filters.budget:
                continue
        if filters.query and filters.query not in (p.name + p.category + " ".join(p.tags)):
            continue
        if "不吃辣" in filters.preferences and "辣味" in p.tags:
            continue
        if anchor and anchor.location:
            if not p.location or p.id == anchor.id:
                continue
            p.distance_m = distance_m(anchor.location, p.location)
            if p.distance_m > filters.radius_m:
                continue
        if p.id in seen_ids:
            continue
        seen_ids.add(p.id)
        matched = [pref for pref in dict.fromkeys(filters.preferences) if pref in p.tags]
        p.unverified_preferences = [pref for pref in dict.fromkeys(filters.preferences) if pref not in p.tags]
        if matched:
            p.match_reasons.append("标签匹配：" + "、".join(matched))
        if filters.area:
            p.match_reasons.append(f"地址或片区包含{filters.area}")
        if filters.budget is not None and filters.budget_scope in (filters.kind, "all"):
            p.match_reasons.append(f"人均参考 {p.cost:g} 元，在 {filters.budget:g} 元预算内")
        if p.distance_m is not None:
            p.match_reasons.append(f"距所选地点直线约 {p.distance_m:g} 米")
        if p.rating is not None:
            p.match_reasons.append(f"地图评分 {p.rating:g}/5")
        filtered.append(p)
    filtered.sort(key=lambda p: (
        -len(set(filters.preferences) & set(p.tags)),
        p.distance_m if p.distance_m is not None else 0,
        -(p.rating or 0),
        p.cost if p.cost is not None else float("inf"), p.id,
    ))
    return filtered
