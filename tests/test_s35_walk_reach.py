"""S35 **徒歩が出ないことがある**（2026-09-30・ユーザー指摘）。

「**徒歩経路が出ないことがあるのはなぜか。電車が出ないなら分かるが、
徒歩で到達できない場所は基本的には存在しないはず**」。

流山市 165 件・港区 108 件の行き先を全部さがして数えたら、**20 件で徒歩が出ていなかった**。
原因は 2 つあって、どちらも「歩けないから」ではなかった。

1. **上限が 5km のままだった。** さがす範囲のスライダは 6km まで伸びるのに、
   `Planner.MAX_STRAIGHT_M["walk"]` が 5,000m で止まっていた。**19 件がこれ**。
   公共交通も無い行き先では「経路は見つかりませんでした」と出ていた（歩けば 1 時間強）
2. **道への当て方が近すぎた。** いちばん近い節点が**本線とつながっていない小島**のことがある。
   ららぽーと柏の葉は敷地内の歩道 29 節点だけの島で、23m 先にあるのに道が出なかった。
   **本線の節点は 42m 先にあった**（近い 8 件が島で埋まって届かなかっただけ）
"""
import pytest

from backend import plan as plan_mod, profile as profile_mod, walk as walk_mod

HOME = (35.8697, 139.9236)                  # 流山おおたかの森（住宅地）
LALAPORT = (35.89335, 139.95119)            # ららぽーと柏の葉。敷地内の歩道が島になっている


def test_the_walk_limit_covers_the_whole_search_radius():
    """**スライダの上限より短くしてはいけない。**

    ここがずれていたのが今回の原因。片方だけ伸ばすと、また同じことが起きる。
    """
    import scripts.build_static as bs
    assert plan_mod.Planner.MAX_STRAIGHT_M["walk"] >= max(bs.RADII)


def test_walking_is_offered_to_a_destination_five_kilometres_away(client):
    """**5km 台の行き先にも徒歩を出す。** 1 時間強で歩ける（遠いが、歩けないわけではない）。"""
    d = client.get("/api/nearby?stage=nagareyama&home=otakanomori"
                   "&radius_m=6000&date=2026-09-24&time=10:00").get_json()
    far = [s for s in d["spots"] if 5000 < s["distance_m"] <= 6000]
    assert len(far) >= 10                   # そもそも 5km 台の行き先があること
    home = d["home"]
    for s in far[:4]:
        r = client.post("/api/routes", json={
            "stage": "nagareyama", "from_lat": home["lat"], "from_lon": home["lon"],
            "to_lat": s["lat"], "to_lon": s["lon"],
            "date": "2026-09-24", "time": "10:00", "modes": ["walk"]}).get_json()
        assert r["walk"] and not r["walk"].get("unavailable"), s["name"]


def test_walking_reaches_a_place_whose_nearest_paths_are_an_island(nagareyama):
    """**本線とつながっていない小島に当ててしまわない。**

    ららぽーと柏の葉は敷地内の歩道だけで 1 つの島になっていて、いちばん近い 8 節点が
    全部そこに入る。当て方を広げると 42m 先に本線の節点がある。
    """
    graph = walk_mod.WalkGraph(nagareyama.walk_db_path)
    prof = profile_mod.for_mode("walk")
    path = graph.route(HOME, LALAPORT, prof, "walk")
    assert path, "ららぽーと柏の葉までの道が出ない"
    assert 3600 < path["meters"] < 6000      # 直線 3.6km に対して道なり 4km 前後


def test_the_island_is_really_there(nagareyama):
    """**思い込みで直していないこと。** 島が実在することを、ここで押さえておく。

    これが将来 OSM 側で本線につながったら、このテストが落ちて気づける。
    """
    graph = walk_mod.WalkGraph(nagareyama.walk_db_path)
    graph.load_area([LALAPORT], 2000)
    near = graph.nearest_nodes(*LALAPORT, max_m=400, mode="walk", k=8)
    assert near and near[0][1] < 40          # 23m 先に節点はある

    def component(start, cap=400):
        seen, stack = {start}, [start]
        while stack and len(seen) < cap:
            n = stack.pop()
            for m, e in graph.adj.get(n, ()):
                if e["walk"] and m not in seen:
                    seen.add(m)
                    stack.append(m)
        return len(seen)

    assert component(near[0][0]) < 100       # いちばん近いのは小さな島
    wider = graph.nearest_nodes(*LALAPORT, max_m=800, mode="walk", k=32)
    assert any(component(nid) >= 400 for nid, _ in wider)   # 広げれば本線に当たる


@pytest.mark.parametrize("stage_id,home_id", [("nagareyama", "otakanomori"),
                                              ("minato", "shibaura")])
def test_no_destination_is_left_without_any_route(client, stage_id, home_id):
    """**「経路は見つかりませんでした」で終わる行き先を作らない。**

    公共交通が無くても歩けば着く。半径 1.5km までの行き先で確かめる（速さのため）。
    """
    d = client.get(f"/api/nearby?stage={stage_id}&home={home_id}"
                   "&radius_m=1500&date=2026-09-24&time=10:00").get_json()
    home = d["home"]
    for s in d["spots"][:5]:
        r = client.post("/api/routes", json={
            "stage": stage_id, "from_lat": home["lat"], "from_lon": home["lon"],
            "to_lat": s["lat"], "to_lon": s["lon"],
            "date": "2026-09-24", "time": "10:00",
            "modes": ["total", "train", "bus", "walk"]}).get_json()
        shown = [k for k in ("total", "train", "bus", "walk")
                 if r.get(k) and not r[k].get("unavailable")]
        assert shown, f'{stage_id} {s["name"]} に出せる経路が 1 つも無い'
