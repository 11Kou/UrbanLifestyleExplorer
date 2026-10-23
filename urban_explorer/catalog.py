import json
from importlib.resources import files

from urban_explorer.models import CityGuide, Place, Source


def city_guides() -> dict[str, CityGuide]:
    raw = json.loads(files("urban_explorer").joinpath("data/city-guides.json").read_text(encoding="utf-8"))
    return {item["city"]: CityGuide.model_validate(item) for item in raw}


class DemoCatalog:
    def __init__(self):
        raw = json.loads(files("urban_explorer").joinpath("data/chengdu.json").read_text(encoding="utf-8"))
        self.guide = CityGuide.model_validate(raw)
        self.places = [
            Place.model_validate({
                **p,
                "city": raw["city"],
                "address": f"{p['area']}演示地址（虚构场所）",
                "mode": "demo",
                "sources": [Source(title="虚构场所与消费，仅用于演示", kind="demo")],
            })
            for p in raw["places"]
        ]

    def get(self, place_id: str) -> Place | None:
        return next((p.model_copy(deep=True) for p in self.places if p.id == place_id), None)
