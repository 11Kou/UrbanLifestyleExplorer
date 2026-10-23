"""Streamlit content-first UI. Run with `python run.py`."""
import html
import re
import uuid

import httpx
import streamlit as st

from urban_explorer.config import get_settings
from urban_explorer.location_ui import location_button
from urban_explorer.models import ExploreFilters, Place
from urban_explorer.providers import map_url
from urban_explorer.recommendations import place_introduction

st.set_page_config(page_title="城市初见 · 探索你的城市", page_icon="🧭", layout="wide")
settings = get_settings()
PAGES = {"认识城市": "home", "吃什么": "food", "去哪玩": "fun", "收藏夹": "saved"}
LABELS = {"city": "城市认知", "food": "美食推荐", "fun": "娱乐推荐"}
PREFERENCES = ["不吃辣", "清淡", "安静", "一个人", "朋友", "室内", "户外", "晚上"]


def api(method: str, endpoint: str, **kwargs):
    try:
        response = httpx.request(method, settings.api_base_url.rstrip("/") + endpoint, timeout=180, **kwargs)
    except httpx.RequestError:
        raise RuntimeError("暂时无法连接城市助手，请确认后端已启动。") from None
    if not response.is_success:
        try:
            detail = response.json().get("detail", "请求失败")
        except ValueError:
            detail = "服务返回了无法识别的响应"
        if not isinstance(detail, str):
            detail = "输入条件无效，请检查城市、预算和问题内容。"
        raise RuntimeError(detail)
    return response.json()


if "session_id" not in st.session_state:
    sid = str(st.query_params.get("sid", ""))
    st.session_state.session_id = sid if re.fullmatch(r"[A-Za-z0-9_-]{16,100}", sid) else uuid.uuid4().hex
    st.query_params["sid"] = st.session_state.session_id
    st.session_state.update(filters=ExploreFilters().model_dump(), nav="认识城市", city_field="成都", messages=[], chat_results=[], chat_notices=[], trace=[], selected_place_id=None, budgets={"food": None, "fun": None})
    try:
        restored = api("GET", f"/sessions/{st.session_state.session_id}")
        st.session_state.filters = restored["filters"]
        st.session_state.city_field = restored["filters"]["city"]
        st.session_state.messages = restored["messages"]
        st.session_state.budgets = restored.get("budgets", {"food": None, "fun": None})
        restored_filters = restored["filters"]
        if restored_filters["budget_scope"] in ("food", "fun"):
            st.session_state.budgets[restored_filters["budget_scope"]] = restored_filters["budget"]
        st.session_state.nav = next(label for label, key in PAGES.items() if key == restored["page"])
    except RuntimeError:
        pass

sid = st.session_state.session_id

try:
    health = api("GET", "/health")
except RuntimeError as error:
    st.title("城市初见")
    st.error(str(error))
    st.write("在项目目录启动完整应用：")
    st.code(".venv\\Scripts\\python.exe run.py", language="powershell")
    st.stop()

st.markdown("""
<style>
:root{--city-green:#246548;--city-soft:#e8f1e8;--city-ink:#243a30}
.stApp{background-color:light-dark(#f5f6f2,#171c1a)}
header[data-testid="stHeader"]{background:transparent}
[data-testid="stToolbar"]{display:none}
.block-container{padding-top:1.6rem;padding-bottom:2rem;max-width:1250px}
.city-brand{font-size:1.45rem;font-weight:600;color:light-dark(#243a30,#e5ede7);margin:0 0 .2rem}
.city-subtitle{color:light-dark(#65716a,#a8b6ad);font-size:.85rem;margin-bottom:1rem}
.city-hero{background:light-dark(#e8f1e8,#293c31);border-radius:16px;padding:25px;margin:8px 0 20px;color:light-dark(#243a30,#e5ede7)}
.city-hero h1{font-size:1.8rem;line-height:1.4;padding:0;margin:8px 0 12px}
.city-hero p{margin:0;font-size:.95rem}
.city-eyebrow{font-size:.8rem;letter-spacing:.08em;opacity:.75}
.city-place-title{font-size:1.08rem;font-weight:600;margin:6px 0}
.city-place-label{font-size:.8rem;color:light-dark(#65716a,#a8b6ad)}
[data-testid="stImageContainer"] img{width:100%;height:200px;object-fit:cover;border-radius:10px}
[role="dialog"] [data-testid="stImageContainer"] img{height:auto;max-height:380px;object-fit:contain}
.st-key-assistant{background:light-dark(#fff,#222925);border:1px solid light-dark(#dce3dc,#3c4941);border-radius:16px;padding:18px}
div[data-testid="stVerticalBlockBorderWrapper"]{border-radius:14px}
button[kind="primary"]{background-color:#246548;border-color:#246548}
div[data-testid="stChatMessage"]{font-size:.9rem}
@media(max-width:700px){.block-container{padding-top:1rem}.city-hero{padding:18px}.city-hero h1{font-size:1.45rem}}
</style>
""", unsafe_allow_html=True)


def apply_chat_reply(reply: dict):
    state = reply["session"]
    st.session_state.filters = state["filters"]
    st.session_state.budgets = state.get("budgets", {"food": None, "fun": None})
    scope = state["filters"]["budget_scope"]
    if scope in ("food", "fun"):
        st.session_state.budgets[scope] = state["filters"]["budget"]
    st.session_state.city_field = state["filters"]["city"]
    st.session_state.messages = state["messages"]
    st.session_state.chat_results = reply["results"]
    st.session_state.chat_notices = reply["notices"]
    st.session_state.trace = reply["agents"]
    st.session_state.selected_place_id = state["selected_place"]["id"] if state["selected_place"] else None
    for key in ["area_filter", "budget_filter", "category_filter", "query_filter", "preferences_filter"]:
        st.session_state.pop(key, None)
    types = [r["agent"] for r in reply["results"] if r["agent"] != "city"]
    if len(types) == 1:
        st.session_state.nav = "吃什么" if types[0] == "food" else "去哪玩"
    elif len(types) > 1:
        st.session_state.nav = "认识城市"


if pending := st.session_state.pop("pending_query", None):
    try:
        with st.spinner("正在理解需求、查询资料并整理推荐…"):
            reply = api("POST", "/chat", json={
                "session_id": sid, "message": pending["message"],
                "context": st.session_state.filters,
                "page": PAGES[st.session_state.nav],
                "selected_place_id": pending.get("place_id") or st.session_state.selected_place_id,
            })
        apply_chat_reply(reply)
    except RuntimeError as error:
        st.error(str(error))

pending_preferences = st.session_state.pop("pending_preferences", None)
if pending_preferences is not None:
    st.session_state.filters["preferences"] = pending_preferences
    st.session_state.pop("preferences_filter", None)
    st.toast("偏好已保存")


def queue(message: str, place_id: str | None = None):
    st.session_state.pending_query = {"message": message, "place_id": place_id}
    st.rerun()


def city_changed():
    city = st.session_state.city_field.strip().removesuffix("市") or "成都"
    st.session_state.filters = ExploreFilters(city=city).model_dump()
    st.session_state.update(chat_results=[], chat_notices=[], messages=[], selected_place_id=None, trace=[], budgets={"food": None, "fun": None})
    for key in ["area_filter", "budget_filter", "category_filter", "query_filter"]:
        st.session_state.pop(key, None)


def use_location(location: dict):
    st.session_state.city_field = location["city"]
    city_changed()
    label = "浏览器位置" if location["source"] == "browser" else "本机网络 IP（粗略）"
    st.session_state.location_notice = f"已按{label}切换到{location['city']}，可手动修改。"
    st.rerun()


top, picker, locator = st.columns([3, 1.25, 1.25])
# Process location before creating the city input, so its widget state can change.
with locator:
    position = location_button(key="locate_city", on_position_change=lambda: None).position
    if position:
        if position.get("error"):
            st.warning(position["error"])
        else:
            try:
                with st.spinner("正在识别城市…"):
                    located = api("POST", "/location/browser", json=position)
                use_location(located)
            except RuntimeError as error:
                st.warning(str(error))
    if st.button("按本机网络定位", key="locate_network", use_container_width=True, disabled=health["mode"] == "demo"):
        try:
            with st.spinner("正在定位网络所在城市…"):
                located = api("POST", "/location/network")
            use_location(located)
        except RuntimeError as error:
            st.warning(str(error))
    st.caption("位置经高德识别城市，不保存坐标。网络定位按本机出口城市估算，代理可能影响结果。")
with top:
    st.markdown('<div class="city-brand">🧭 城市初见</div><div class="city-subtitle">认识一座城，找到自己的吃喝玩乐</div>', unsafe_allow_html=True)
with picker:
    st.text_input("当前城市（可直接修改）", key="city_field", max_chars=40, on_change=city_changed)
    st.caption("输入城市后按回车，如杭州、上海。")

if notice := st.session_state.pop("location_notice", None):
    st.success(notice)

if health["mode"] == "demo":
    st.caption("🟢 演示模式 · 已收录成都样例；场所、价格和位置均为虚构数据，对话由规则模拟协作。")
else:
    st.caption("🔎 实时模式 · 地点由外部服务查询；推荐均需结合来源与更新时间判断。")

def navigation_changed():
    target = PAGES[st.session_state.nav]
    filters = st.session_state.filters
    if target in ("food", "fun") and filters["kind"] != target:
        st.session_state.budgets[filters["kind"]] = filters["budget"] if filters["budget_scope"] in (filters["kind"], "all") else None
        filters.update(kind=target, category="", query="", budget=st.session_state.budgets[target], budget_scope=target)
        st.session_state.chat_results = []
        for key in ["budget_filter", "category_filter", "query_filter"]:
            st.session_state.pop(key, None)


st.radio("探索导航", list(PAGES), key="nav", horizontal=True, on_change=navigation_changed, label_visibility="collapsed")
page = PAGES[st.session_state.nav]
city = st.session_state.filters["city"]

try:
    guide = api("GET", f"/cities/{city}")
    favorite_list = api("GET", f"/sessions/{sid}/favorites")
except RuntimeError as error:
    st.warning(str(error))
    guide = {"city": city, "introduction": "暂时未能获取城市资料，可以继续尝试地点查询。", "areas": [], "foods": [], "tags": [], "sources": []}
    favorite_list = []
favorite_ids = {p["id"] for p in favorite_list}


def source_lines(sources: list[dict]):
    for source in sources:
        title = source.get("title", "资料来源")
        if source.get("url"):
            st.link_button(title, str(source["url"]))
        else:
            st.caption(title)
        date = source.get("published_at")
        checked = str(source.get("checked_at", ""))[:10]
        st.caption((f"发布时间：{date} · " if date else "") + f"查询时间：{checked or '未记录'}")


@st.dialog("场所详情", width="large")
def details(raw: dict):
    p = Place.model_validate(raw)
    st.subheader(p.name)
    if p.mode == "demo":
        st.info("这是虚构的演示场所，不能作为实际出行或消费依据。")
    if p.photos:
        st.image(str(p.photos[0].url), caption="地点图片 · 来源：高德地图", width="stretch")
    st.write(place_introduction(p) if p.mode == "live" else p.reason)
    st.write(f"**所在片区：** {p.area or '待确认'}")
    st.write(f"**地址：** {p.address or '待确认'}")
    st.write(f"**人均参考：** {'待确认' if p.cost is None else f'¥{p.cost:g}'}")
    st.write(f"**营业时间：** {p.opening_hours or '待确认'}")
    if p.distance_m is not None:
        st.caption(f"距选中地点约 {p.distance_m:g} 米（直线距离）")
    if p.rating is not None:
        st.caption(f"地图评分：{p.rating:g}；评分不代表实时热度或榜单排名。")
    if p.kind == "fun":
        st.caption("展览、演出等限时活动，请以场馆官方日期、场次与预约信息为准。")
    st.markdown("**资料来源**")
    source_lines(raw.get("sources", []))
    if url := map_url(p):
        st.link_button("在高德地图查看", url)
    if st.button("问问附近还能怎么玩", type="primary", key="dialog_nearby_" + p.id):
        queue("这家附近有什么好玩的？", p.id)


def cards(places: list[dict], prefix: str):
    if not places:
        st.info("没有符合条件的场所。试试放宽片区、预算或类别。")
        return
    for offset in range(0, len(places), 2):
        columns = st.columns(2)
        for col, p in zip(columns, places[offset:offset + 2]):
            with col, st.container(border=True):
                if p.get("photos"):
                    st.image(p["photos"][0]["url"], caption="地点图片 · 高德地图", width="stretch")
                icon = "🍜" if p["kind"] == "food" else "✦"
                category = html.escape(p["category"])
                st.markdown(f'<div class="city-place-label">{icon} {category}</div><div class="city-place-title">{html.escape(p["name"])}</div>', unsafe_allow_html=True)
                cost = "消费待确认" if p.get("cost") is None else f'参考 ¥{p["cost"]:g}/人'
                st.caption(f'{p.get("area") or "片区待确认"} · {cost}')
                introduction = place_introduction(Place.model_validate(p)) if p["mode"] == "live" else p.get("reason", "")
                st.write(introduction)
                reason = p.get("reason", "")
                if p["mode"] == "live" and reason and reason != introduction and not reason.startswith("地图检索候选"):
                    st.caption("推荐理由：" + reason)
                tags = p.get("tags", [])[:3]
                if tags:
                    st.caption(" · ".join(tags))
                if p.get("distance_m") is not None:
                    st.caption(f'距起点直线约 {p["distance_m"]:g} 米')
                st.caption("演示推荐" if p["mode"] == "demo" else "地图候选 · 热度未核实")
                left, right = st.columns(2)
                if left.button("查看详情", key=f"{prefix}_detail_{p['id']}", use_container_width=True):
                    details(p)
                saved = p["id"] in favorite_ids
                if right.button("已收藏 ✓" if saved else "收藏", key=f"{prefix}_save_{p['id']}", use_container_width=True):
                    try:
                        api("DELETE" if saved else "PUT", f"/sessions/{sid}/favorites/{p['id']}")
                        st.rerun()
                    except RuntimeError as error:
                        st.error(str(error))


def search(filters: dict) -> dict:
    try:
        return api("POST", f"/sessions/{sid}/search", json=filters)
    except RuntimeError as error:
        st.error(str(error))
        return {"places": [], "notices": []}


def chosen_area(area: str):
    st.session_state.filters["area"] = area
    st.session_state.filters["anchor_id"] = None
    st.session_state.selected_place_id = None
    st.session_state.pop("area_filter", None)
    st.session_state.chat_results = []


main, assistant = st.columns([3, 1.15], gap="large")
with main:
    if page == "home" and not any(r["places"] for r in st.session_state.chat_results):
        st.markdown(f'<div class="city-hero"><div class="city-eyebrow">从一顿饭、一条街开始</div><h1>先认识{html.escape(city)}，再找到喜欢的地方</h1><p>{html.escape(guide["introduction"])}</p></div>', unsafe_allow_html=True)
        for notice in guide.get("notices", []):
            st.warning(notice)
        if guide["tags"]:
            st.caption(" · ".join(guide["tags"]))
        start_food, start_fun = st.columns(2)
        def go(label: str):
            st.session_state.nav = label
            navigation_changed()
        start_food.button("🍜 找点好吃的", type="primary", use_container_width=True, on_click=go, args=("吃什么",))
        start_fun.button("✦ 发现好去处", use_container_width=True, on_click=go, args=("去哪玩",))
        if guide["areas"]:
            st.subheader("先认识几个片区")
            area_cols = st.columns(min(3, len(guide["areas"])))
            for col, area in zip(area_cols, guide["areas"]):
                with col, st.container(border=True):
                    st.markdown(f"**{area['name']}**")
                    st.caption(" · ".join(area["tags"]))
                    st.write(area["description"])
                    st.button("探索这个片区", key="area_" + area["name"], on_click=chosen_area, args=(area["name"],), use_container_width=True)
        if guide["foods"]:
            st.subheader("从这些味道开始")
            st.write(" · ".join(guide["foods"]))
        if guide["sources"]:
            with st.expander("城市资料来源"):
                source_lines(guide["sources"])
        st.subheader("从一次体验开始")
        sampled = []
        for kind in ["food", "fun"]:
            current = {**st.session_state.filters, "kind": kind, "category": "", "query": ""}
            sampled.extend(search(current)["places"][:1])
        cards(sampled, "home")

    elif page in ("food", "fun"):
        st.subheader("今天，想吃点什么？" if page == "food" else "给今天留一点好玩的")
        st.caption("按条件找到候选，再让助手帮你比较。热门与推荐依据以实际来源为准。")
        filters = ExploreFilters.model_validate(st.session_state.filters)
        if filters.kind != page:
            filters.category, filters.query = "", ""
        filters.kind = page
        options = ["不限片区"] + [a["name"] for a in guide["areas"]]
        if filters.area and filters.area not in options:
            options.append(filters.area)
        region_col, budget_col = st.columns(2)
        if "area_filter" not in st.session_state:
            st.session_state.area_filter = filters.area or "不限片区"
        with region_col:
            area = st.selectbox("片区", options, key="area_filter")
        budget_default = filters.budget if filters.budget_scope in (page, "all") else None
        if "budget_filter" not in st.session_state:
            st.session_state.budget_filter = float(budget_default or 0)
        with budget_col:
            budget = st.number_input("人均预算上限（元，0 表示不限）", min_value=0.0, max_value=100000.0, step=10.0, key="budget_filter")
        base_categories = ["不限类别"] + (["地方菜", "小吃", "火锅", "面食", "轻食"] if page == "food" else ["休闲", "文化", "音乐", "街区漫步", "朋友"])
        if filters.category and filters.category not in base_categories:
            base_categories.append(filters.category)
        if "category_filter" not in st.session_state:
            st.session_state.category_filter = filters.category or "不限类别"
        category_col, query_col = st.columns(2)
        with category_col:
            category = st.selectbox("类别", base_categories, key="category_filter")
        if "query_filter" not in st.session_state:
            st.session_state.query_filter = filters.query
        with query_col:
            query = st.text_input("关键词", key="query_filter", placeholder="例如：茶馆、地方菜", max_chars=100)
        prefs_options = list(dict.fromkeys(PREFERENCES + filters.preferences))
        if "preferences_filter" not in st.session_state:
            st.session_state.preferences_filter = filters.preferences
        prefs = st.multiselect("偏好", prefs_options, key="preferences_filter", max_selections=20)
        filters.area = "" if area == "不限片区" else area
        filters.budget = budget or None
        filters.budget_scope = page
        st.session_state.budgets[page] = filters.budget
        filters.category = "" if category == "不限类别" else category
        filters.query = query
        filters.preferences = prefs
        if filters.anchor_id:
            st.caption("当前正在查询已选地点周边；距离是直线距离。")
            if st.button("取消周边限制"):
                filters.anchor_id = None
                st.session_state.selected_place_id = None
                st.session_state.filters = filters.model_dump()
                st.session_state.chat_results = []
                st.rerun()
        previous = ExploreFilters.model_validate(st.session_state.filters)
        if previous.model_dump() != filters.model_dump():
            st.session_state.chat_results = []
        st.session_state.filters = filters.model_dump()
        data = search(filters.model_dump())
        chat_places = [p for r in st.session_state.chat_results if r["agent"] == page for p in r["places"]]
        places = chat_places or data["places"]
        st.caption(f"当前展示 {len(places)} 个候选")
        cards(places, page)
        for notice in data["notices"]:
            st.caption(notice)

    elif page == "home":
        st.subheader("为你找到的吃喝玩乐")
        st.caption("可以先选一家餐厅，再从详情继续查询附近的娱乐。")
        if st.button("返回城市介绍", key="back_to_guide", on_click=lambda: st.session_state.update(chat_results=[])):
            st.rerun()
        for result in st.session_state.chat_results:
            if result["places"]:
                st.markdown("### " + LABELS[result["agent"]])
                cards(result["places"], "combined_" + result["agent"])

    else:
        st.subheader("把想去的地方，留在这里")
        st.caption("收藏保存在本机数据库；同一会话刷新页面后仍可继续查看。")
        kind_label = st.radio("收藏分类", ["全部", "美食", "娱乐"], horizontal=True)
        saved = [p for p in favorite_list if p["city"] == city and (kind_label == "全部" or p["kind"] == ("food" if kind_label == "美食" else "fun"))]
        if saved:
            cards(saved, "saved")
        else:
            st.info("这里还没有收藏，去吃什么或去哪玩保存一个感兴趣的场所吧。")

with assistant, st.container(key="assistant"):
    st.subheader("和城市助手聊聊")
    filters = st.session_state.filters
    st.caption(f"当前：{city} · {filters['area'] or '不限片区'}")
    if filters.get("anchor_id"):
        st.caption(f"以已选场所为起点，查询直线距离 {filters['radius_m'] / 1000:g} 公里内的地点。")
    if filters.get("budget") is not None:
        scope = {"food": "餐饮", "fun": "娱乐", "all": "每类"}[filters["budget_scope"]]
        st.caption(f"{scope}人均上限：¥{filters['budget']:g}")
    for message in st.session_state.messages[-6:]:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
    if not st.session_state.messages:
        st.write("刚到这座城？可以先认识片区，也可以告诉我想吃什么、想怎么玩。")
    if st.button("先认识这座城市", key="ask_city", use_container_width=True):
        queue("介绍一下这座城市的主要片区和饮食特色")
    if st.button("人均100元，有什么好吃的？", key="ask_food", use_container_width=True):
        queue("推荐人均100元以内的美食")
    if st.button("晚上吃点东西，再找地方玩", key="ask_both", use_container_width=True):
        queue("晚上想吃点东西，再找地方玩")
    if prompt := st.chat_input("例如：换玉林附近，便宜一点", key="city_chat"):
        queue(prompt)
    for notice in st.session_state.chat_notices:
        st.caption(notice)
    for result in st.session_state.chat_results:
        if result.get("sources"):
            with st.expander(LABELS[result["agent"]] + "参考资料"):
                source_lines(result["sources"])
    with st.expander("保存我的偏好"):
        selected = st.multiselect("希望下次也记住", PREFERENCES, default=[p for p in filters["preferences"] if p in PREFERENCES], key="saved_preferences")
        if st.button("保存偏好", key="save_preferences"):
            try:
                updated = api("PUT", f"/sessions/{sid}/preferences", json={"preferences": selected})
                st.session_state.pending_preferences = updated["active_preferences"]
                st.rerun()
            except RuntimeError as error:
                st.error(str(error))
    if st.session_state.trace:
        with st.expander("本次协作过程"):
            st.caption("主控 → " + "、".join(LABELS[a] for a in st.session_state.trace) + " → 汇总与校验")
            st.caption("规则演示" if health["mode"] == "demo" else "模型协作" if health["capabilities"]["llm"] else "规则分流与真实接口")
