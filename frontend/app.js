/* 子育てナビ フロントエンド（素の JS ＋ MapLibre GL JS）
   地図の下地は地理院タイル（鍵が要らない）。舞台は ?stage= で切り替える。 */
const state = {
  stages: [], stage: null, home: null, dest: null,
  snapshots: [], snapshot: null,                     // 出発時刻（事前計算のスナップショット）
  site: {},                                          // 公開時の情報（問い合わせ先）
  radius: 1000, reqSeq: 0, homePoint: null,          // 行き先をさがす半径（m）
  // **ベビーカーは既定でオン**（2026-09-30・ユーザー指示）。この作品が見せたいのは
  // 段差をよけた道なので、何もしなくてもその条件でさがす
  spots: [], picks: [], plans: null, planKey: null, stroller: true, popup: null,
  compare: true,                                     // 当て馬（よけない道）を並べて出すか
  // **経路探索が使っている公共交通**（地図に重ねる下敷き）。チェックで消せる
  // **既定は消しておく**（2026-09-30・ユーザー指示）。主役は行き先で、これは下敷き
  transit: null, transitOn: { train: false, bus: false },
  // 下地を入れ替えると自分のレイヤも消えるので、描いたものは手元に持っておく
  boundary: null, routeFC: null, entrancesFC: null, compareFC: null, blockFC: null,
};
const $ = (id) => document.getElementById(id);
const api = (path) => fetch(path).then((r) => r.json());
// いま選べる舞台か。`enabled: false` の舞台はデータを持たず、名前だけ返ってくる
const isUsable = (s) => s.enabled !== false;

/* ---------- データの取り方 ----------
   同じ画面を 2 通りで動かす。

   - **サーバ版**（開発中。Flask が計算して返す）
   - **静的版**（GitHub Pages。**出発時刻を固定して結果を先に計算したファイル**を読む）

   静的版は config.js が `KOSODATE_STATIC` を立てる。違いはここだけに閉じ込めてあり、
   返ってくる JSON の形は同じ（事前計算はこの API をそのまま呼んで保存しているため）。 */
const STATIC = !!window.KOSODATE_STATIC;
const API_BASE = (window.KOSODATE_API_BASE || "api").replace(/\/$/, "");
// 作り直したときに古いファイルが残らないよう、生成した時刻を問い合わせに付ける
const V = window.KOSODATE_BUILD ? `?v=${window.KOSODATE_BUILD}` : "";
const fileId = (id) => String(id).replace(/[^A-Za-z0-9_-]/g, "_");
const jget = (url) => fetch(url).then((r) => {
  if (!r.ok) throw new Error(`${url} が読めません（${r.status}）`);
  return r.json();
});

const API = {
  stages: () => (STATIC ? jget(`${API_BASE}/stages.json${V}`) : api("/api/stages")),
  snapshots: () => (STATIC ? jget(`${API_BASE}/snapshots.json${V}`) : api("/api/snapshots")),
  site: () => (STATIC ? jget(`${API_BASE}/site.json${V}`) : api("/api/site")),
  boundary: (stage) => (STATIC ? jget(`${API_BASE}/${stage}/boundary.json${V}`)
    : api(`/api/boundary?stage=${stage}`)),
  stations: (stage) => (STATIC ? jget(`${API_BASE}/${stage}/stations.json${V}`)
    : api(`/api/stations?stage=${stage}`)),
  coverage: (stage) => (STATIC ? jget(`${API_BASE}/${stage}/coverage.json${V}`)
    : api(`/api/coverage?stage=${stage}`)),
  network: (stage) => (STATIC ? jget(`${API_BASE}/${stage}/network.json${V}`)
    : api(`/api/network?stage=${stage}`)),
  events: (stage, snap) => (STATIC ? jget(`${API_BASE}/${stage}/${snap.id}/events.json${V}`)
    : api(`/api/events?stage=${stage}&date=${snap.date}`)),
  spot: (stage, id) => (STATIC ? jget(`${API_BASE}/${stage}/spot/${fileId(id)}.json${V}`)
    : api(`/api/spot/${encodeURIComponent(id)}?stage=${stage}`)),
  nearby: (stage, snap, home, radius) => (STATIC
    ? jget(`${API_BASE}/${stage}/${snap.id}/nearby/${home}/${radius}.json${V}`)
    : api(`/api/nearby?${new URLSearchParams({ stage, home, radius_m: radius,
      date: snap.date, time: snap.time })}`)),
  /* 経路は静的版だと**1 ファイルに 6 通りとも入っている**ので 1 回で済む。
     サーバ版は公共交通と自転車・車を分けて投げる（片方が遅くても先に出すため）。 */
  routes: (stage, snap, home, profile, spot, body, modes) => (STATIC
    ? jget(`${API_BASE}/${stage}/${snap.id}/routes/${home}/${profile}/${fileId(spot.id)}.json${V}`)
    : fetch("/api/routes", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, modes }) }).then((r) => r.json())),
};

/* 時刻表のライセンス。ODPT のデータカタログで 1 件ずつ確認したもの（2026-09-24）。
   **同じ ODPT センター経由でもライセンスは事業者ごとに違う**
   （東京都交通局は CC BY 4.0、つくばエクスプレスは公共交通オープンデータ基本ライセンス）。 */
const FEED_LICENSE = {
  "東京都交通局": "CC BY 4.0",
  "首都圏新都市鉄道": "公共交通オープンデータ基本ライセンス",
  "流山市": "CC BY 4.0",
  "東武鉄道": "チャレンジ2026 限定ライセンス",
  "東武バス": "チャレンジ2026 限定ライセンス",
  "JR東日本": "チャレンジ2026 限定ライセンス",
};
/* **本体は舞台の設定**（`data/stages/*.yaml` の `license`）で、ここはそれを画面の言葉に直す表。
   設定に書いていないもの（チャレンジ限定枠は書く欄がない）は事業者名から引く。
   **どちらでも引けないと「—」になる**ので、舞台を足したらここも見ること
   （東京メトロが「—」で出ていたのを 2026-09-30 に直した）。 */
const LICENSE_LABEL = {
  "CC BY 4.0": "CC BY 4.0",
  "ODPT": "公共交通オープンデータ基本ライセンス",
};
const licenseOf = (f) => LICENSE_LABEL[f.license] || FEED_LICENSE[f.organization] || "—";

const MODE_LABEL = { walk: "徒歩", bike: "自転車", car: "車", transit: "乗る" };
const ROUTE_ICON = { 0: "路面電車", 1: "地下鉄", 2: "電車", 3: "バス", 11: "バス" };

/* さがす範囲の描き方。**自宅を中心にした半径 N メートルの円**（2026-09-27・ユーザー指示）。
   以前は到達圏（徒歩・自転車・徒歩＋公共交通で N 分）を塗っていたが、
   手段を替えると出る行き先が変わるのが分かりにくかった。
   **行き先さがしに移動手段は使わない。** 手段の話は、行き先を決めたあとの経路のペインに寄せた。
   円は中心と半径だけで引けるので、サーバから面を送る必要がない（画面側で作る）。 */
// 公共交通の下敷き。**行き先の印より下に、薄く**引く（主役は行き先なので）。
// **下地から浮く色をえらぶ。** 下地（OpenFreeMap positron）は薄いグレーと
// 薄い緑（公園・緑地）でできているので、**緑と灰色の系統は使えない**
// （2026-09-30・ユーザー指摘「バスの線が薄いグリーンで、地図と色味が似ていて見づらい」。
// 前は #0f7f7f の青緑だった）。電車の紫は見やすいと言われたのでそのまま。
const TRANSIT = {
  train: { label: "電車", color: "#6d4c9f" },
  bus: { label: "バス", color: "#c2185b" },
};
// 線をどこから引いたか。**「停留所を結んだだけ」も隠さずに言う**
const SHAPE_SOURCE = {
  gtfs: "GTFS の shapes.txt（便ごとの実際の形）",
  osm: "OpenStreetMap の線路",
  mixed: "OpenStreetMap の線路（一部の区間だけ。残りは駅を結んだ線）",
  stops: "停留所・駅を結んだ線（この GTFS は shapes.txt を出していません）",
};
const RANGE_FILL = "#e4ab7d";     // 塗り（薄め）
const RANGE_LINE = "#cf6b3c";     // へりの線（濃いめ。チップの色も兼ねる）
const CIRCLE_STEPS = 128;         // 円を何角形で描くか

/* 自宅を中心にした半径 `meters` の円（GeoJSON のポリゴン）。
   経度は緯度で縮むので、東西方向だけ cos(lat) で割り戻す。 */
function circleOf(lat, lon, meters) {
  const dLat = meters / 111320;
  const dLon = dLat / Math.max(0.2, Math.cos((lat * Math.PI) / 180));
  const ring = [];
  for (let i = 0; i <= CIRCLE_STEPS; i += 1) {
    const t = (i / CIRCLE_STEPS) * 2 * Math.PI;
    ring.push([lon + dLon * Math.cos(t), lat + dLat * Math.sin(t)]);
  }
  return { type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [ring] } };
}

/* 半径の見せ方。1km 未満は「800m」、それ以上は「1.2km」。 */
const radiusText = (m) => (m < 1000 ? `${m}m` : `${(m / 1000).toFixed(1)}km`);

/* スライダの範囲。**index.html の `#radius` と、事前計算の刻み（build_static.py の
   `RADII`）と必ずそろえる。** 静的版はこの刻みのファイルしか持っていない。
   上限を 6km にしてあるのは、**隣の市の行き先がいちばん近いもので 3.3km** だから
   （5km で松戸市・柏市が出てくる。3km までだと市外が 1 件も出ない）。 */
const RADIUS_MIN = 200;
const RADIUS_MAX = 6000;
const RADIUS_STEP = 200;

function ageText(m) {
  if (m < 0) return `産前（あと${-m}か月）`;
  return `${Math.floor(m / 12)}歳${m % 12}か月`;
}

/* ---------- 地図の下地 ----------
   鍵が要らない提供元だけにしている（鍵が切れると作品が死ぬため）。
   `wash` は下地の上にかぶせる薄い紙。寄るほど濃くして、印と到達圏を前に出す。 */
/* **地図の下地は OpenFreeMap positron で固定。**
   見比べ用に 7 種類から選べるようにしていたが、選ぶものではないので 1 つに決めた。
   鍵（API キー）が要らないこと、地名だけ残って道路の名前と番号が消せることが決め手。 */
const MAP_STYLE = {
  label: "OpenFreeMap positron",
  vector: "https://tiles.openfreemap.org/styles/positron",
  // **下地にかぶせる紙の色**（2026-09-30）。positron の素の灰色のままだと、
  // まわりの画面だけ生成りで**地図だけ浮いて見えた**ので、同じクリーム色をかける。
  // 画面の地色（style.css の --bg）とそろえること
  washColor: "#e7d4b8",
  wash: 0.46,
  hide: /^(highway-name|highway-shield|road_shield)/,
};

let map, homeMarker, layersReady = false;
const hasSource = (id) => !!(map && map.getSource(id));

/* 下地を入れ替えても作り直せるように、自分のレイヤは 1 か所にまとめる。 */
/* 下地に載っている、行き先さがしに要らないレイヤを消す。
   道路の名前と番号（国道の盾）は、印と重なって読みにくくなるだけ。 */
function hideBaseLayers(def) {
  if (!def.hide) return;
  (map.getStyle().layers || []).forEach((l) => {
    if (def.hide.test(l.id)) map.setLayoutProperty(l.id, "visibility", "none");
  });
}

async function addAppLayers() {
  const def = MAP_STYLE;
  hideBaseLayers(def);
  // 下地の上にかぶせる薄い紙。寄るほど濃くする
  map.addLayer({ id: "wash", type: "background",
    paint: { "background-color": def.washColor,
      "background-opacity": ["interpolate", ["linear"], ["zoom"],
        12, (def.wash || 0) * 0.43, 14.5, (def.wash || 0) * 0.78,
        16, (def.wash || 0), 18, (def.wash || 0)] } });
  map.addSource("mask", { type: "geojson", data: emptyFC() });
  map.addLayer({ id: "mask", type: "fill", source: "mask",
    paint: { "fill-color": "#6b6b66", "fill-opacity": 0.06 } });
  // さがす範囲（自宅からの半径）。へりに線を引いて「どこまでさがしているか」をはっきりさせる
  map.addSource("range", { type: "geojson", data: emptyFC() });
  map.addLayer({ id: "range-fill", type: "fill", source: "range",
    paint: {
      "fill-color": RANGE_FILL,
      // 寄るほど少し薄くして、地図と印を前に出す
      "fill-opacity": ["interpolate", ["linear"], ["zoom"],
        11.5, 0.45, 14, 0.36, 16, 0.24, 18, 0.18],
    } });
  map.addLayer({ id: "range-line", type: "line", source: "range",
    paint: {
      "line-color": RANGE_LINE, "line-width": 1.2,
      "line-opacity": ["interpolate", ["linear"], ["zoom"], 11.5, 0.7, 16, 0.45],
    } });
  // **経路探索が使っている路線と乗り場**（2026-09-30・ユーザー指示）。
  // 行き先の印と経路の線より**下**に置く。主役は行き先で、これはその下敷き。
  // 電車とバスでレイヤを分けてあるので、チェックボックスは visibility を切るだけで済む
  map.addSource("transit", { type: "geojson", data: emptyFC() });
  map.addSource("transit-stops", { type: "geojson", data: emptyFC() });
  Object.entries(TRANSIT).forEach(([kind, def]) => {
    map.addLayer({ id: `transit-line-${kind}`, type: "line", source: "transit",
      filter: ["==", ["get", "kind"], kind],
      layout: { "line-cap": "round", "line-join": "round",
        visibility: state.transitOn[kind] ? "visible" : "none" },
      paint: {
        "line-color": def.color,
        // 電車は太め・バスは細め。引いたときに線だらけにならないよう、寄るほど太くする
        "line-width": ["interpolate", ["linear"], ["zoom"],
          10, kind === "train" ? 1.2 : 0.7, 13, kind === "train" ? 2.4 : 1.4,
          16, kind === "train" ? 4 : 2.4],
        // さがす範囲の円（オレンジ）の上でも沈まないように、少し濃いめ
        "line-opacity": ["interpolate", ["linear"], ["zoom"], 10, 0.55, 13, 0.7, 16, 0.65],
      } });
  });
  Object.entries(TRANSIT).forEach(([kind, def]) => {
    map.addLayer({ id: `transit-stop-${kind}`, type: "circle", source: "transit-stops",
      filter: ["==", ["get", "kind"], kind],
      // **バス停は寄らないと出さない**（港区は 1,933 か所あり、引いた画面では点の霧になる）
      minzoom: kind === "train" ? 10 : 13.5,
      layout: { visibility: state.transitOn[kind] ? "visible" : "none" },
      paint: {
        "circle-color": def.color,
        // **行き先の印とぶつからない大きさ**にとどめる（2026-09-30・ユーザー指示）。
        // 行き先の印は z14 で 26px 角なので、その半分より小さくしておく
        "circle-radius": ["interpolate", ["linear"], ["zoom"],
          11, kind === "train" ? 3 : 2, 14, kind === "train" ? 4.6 : 3.2,
          17, kind === "train" ? 6.5 : 4.6],
        "circle-opacity": 0.8,
        "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 12, 0.8, 16, 1.4],
        "circle-stroke-color": "#fff",
      } });
  });
  await loadSpotIcons();
  map.addSource("spots", { type: "geojson", data: emptyFC() });
  map.addLayer({ id: "spots", type: "symbol", source: "spots",
    layout: {
      "icon-image": ["concat", "spot-", ["get", "kind"]],
      // zoom の式は一番外側にしか書けないので、倍率は各段の中で掛ける。
      // **引いたときほど小さくする。** 印が小さいほど重ならず、そのぶん多く残る
      // （以前は z11 で 0.48 あり、少し引いただけでごっそり消えていた）
      "icon-size": ["interpolate", ["linear"], ["zoom"],
        10, ["*", ["get", "scale"], 0.26],
        12, ["*", ["get", "scale"], 0.38],
        14, ["*", ["get", "scale"], 0.62],
        17, ["*", ["get", "scale"], 0.92]],
      // **重なったら間引く。** これが地図をすっきりさせる要。
      // 残す順は sort（子育て支援センター・児童館 → イベント → その他 → 公園）
      "icon-allow-overlap": false,
      "icon-padding": 0,
      "symbol-sort-key": ["get", "sort"],
    },
    // 市外（自前整備）は少し薄くして、市のオープンデータと区別できるようにする
    paint: { "icon-opacity": ["case", ["get", "outside"], 0.7, 1] } });
  // **当て馬の道**（段差をよけない、ふつうの最短）。えらんだ道の**下**に、
  // 色を抜いた細い点線で描く。灰色にするのは「こちらは薦めていない」を
  // 色で言うため。分かれてから合流するまでの区間しか来ない
  map.addSource("compare", { type: "geojson", data: emptyFC() });
  map.addLayer({ id: "compare-line", type: "line", source: "compare",
    layout: { "line-cap": "round" },
    paint: { "line-color": "#8c887e", "line-width": 2.4,
      "line-dasharray": [1, 1.9], "line-opacity": 0.95 } });
  map.addSource("route", { type: "geojson", data: emptyFC() });
  // **えらんだ道は白いふちで浮かせる。** 2 本が並んだとき、どちらが上か迷わない
  map.addLayer({ id: "route-casing", type: "line", source: "route",
    paint: { "line-color": "#fff", "line-width": 8, "line-opacity": 0.9 } });
  map.addLayer({ id: "route-walk", type: "line", source: "route",
    filter: ["==", ["get", "mode"], "walk"],
    paint: { "line-color": "#c2582a", "line-width": 4, "line-dasharray": [1.4, 1.2] } });
  map.addLayer({ id: "route-other", type: "line", source: "route",
    filter: ["!=", ["get", "mode"], "walk"],
    paint: { "line-color": "#2f6f4f", "line-width": 5 } });
  await loadBlockIcons();
  map.addSource("blocks", { type: "geojson", data: emptyFC() });
  map.addLayer({ id: "blocks", type: "symbol", source: "blocks",
    layout: {
      "icon-image": ["concat", "block-", ["get", "kind"]],
      // 行き先の印と同じくらいの大きさにする（小さいと絵が読めない）
      "icon-size": ["interpolate", ["linear"], ["zoom"], 12, 0.36, 14, 0.52, 17, 0.72],
      // 当て馬の道の上にしか出ない少数の印なので、間引かずに全部出す
      "icon-allow-overlap": true,
    } });
  map.addSource("entrances", { type: "geojson", data: emptyFC() });
  map.addLayer({ id: "entrances", type: "circle", source: "entrances",
    paint: { "circle-radius": 7, "circle-color": ["case", ["get", "stepFree"], "#2f6f4f", "#b0b0a8"],
      "circle-stroke-width": 1.5, "circle-stroke-color": "#fff" } });
  layersReady = true;
}

function initMap(center) {
  map = new maplibregl.Map({
    container: "map",
    style: MAP_STYLE.vector,
    // ベクターの下地でも日本語が出るように、漢字かなは端末のフォントで描く
    localIdeographFontFamily: "'Noto Sans JP','Hiragino Sans','Yu Gothic',sans-serif",
    center: [center.lon, center.lat], zoom: 13,
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  // レイヤに紐づく操作は 1 回だけ登録する（下地を替えても生き残る）
  map.on("click", "spots", (e) => openSpot(e.features[0].properties.id, e.lngLat));
  map.on("mouseenter", "spots", () => { map.getCanvas().style.cursor = "pointer"; });
  map.on("mouseleave", "spots", () => { map.getCanvas().style.cursor = ""; });
  Object.keys(TRANSIT).forEach((kind) => {
    map.on("click", `transit-line-${kind}`, (e) => {
      const p = e.features[0].properties;
      new maplibregl.Popup({ closeButton: true }).setLngLat(e.lngLat)
        .setHTML(`<h3>${p.route}${p.route_long && p.route_long !== p.route
          ? `<br><span class="hint">${p.route_long}</span>` : ""}</h3>
          <p>${p.feed_name}／${p.stops} 停留所</p>
          <p class="src">線の出どころ: ${SHAPE_SOURCE[p.shape] || p.shape}</p>`)
        .addTo(map);
    });
    map.on("click", `transit-stop-${kind}`, (e) => {
      const p = e.features[0].properties;
      new maplibregl.Popup({ closeButton: true }).setLngLat(e.lngLat)
        .setHTML(`<h3>${p.name}</h3><p>${p.feeds}</p>
          <p class="src">経路探索は、ここを 1 つの乗り場として扱います`
          + (Number(p.platforms) > 1 ? `（時刻表の停留所 ${p.platforms} 件ぶん）` : "")
          + `</p>`)
        .addTo(map);
    });
    [`transit-line-${kind}`, `transit-stop-${kind}`].forEach((id) => {
      map.on("mouseenter", id, () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", id, () => { map.getCanvas().style.cursor = ""; });
    });
  });
  map.on("click", "blocks", (e) => {
    const b = e.features[0].properties;
    new maplibregl.Popup({ closeButton: true }).setLngLat(e.lngLat)
      .setHTML(`<h3>${b.label}</h3>
        <p>段差をよけない最短ルートは、ここを通ります。</p>
        <p class="src">出典: ${b.source}</p>`)
      .addTo(map);
  });
  map.on("mouseenter", "blocks", () => { map.getCanvas().style.cursor = "pointer"; });
  map.on("mouseleave", "blocks", () => { map.getCanvas().style.cursor = ""; });
  map.on("click", "entrances", (e) => {
    const p = e.features[0].properties;
    new maplibregl.Popup({ closeButton: true }).setLngLat(e.lngLat)
      .setHTML(`<h3>${p.station} ${p.name} 出入口</h3><p>${p.stepFree === "true" || p.stepFree === true
        ? "階段を使わずにホームまで行けます" : "この出入口は構内データ上、段差のない経路が確認できません"}</p>`)
      .addTo(map);
  });
  map.on("load", async () => {
    await addAppLayers();
    loadStage(state.stage.stage);
  });
}
const emptyFC = () => ({ type: "FeatureCollection", features: [] });

/* 凡例のチップ。地図の塗りと同じ色の四角を出すだけ。 */
function chip(color, size = 15) {
  return `<span class="chip" style="background:${color};width:${size}px;height:${size}px"></span>`;
}

function setMask(boundary) {
  if (!layersReady) return;
  const world = [[-180, -85], [180, -85], [180, 85], [-180, 85], [-180, -85]];
  const holes = [];
  boundary.features.forEach((f) => {
    const c = f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates;
    c.forEach((poly) => holes.push(poly[0]));
  });
  map.getSource("mask").setData({
    type: "FeatureCollection",
    features: [{ type: "Feature", properties: {},
      geometry: { type: "Polygon", coordinates: [world, ...holes] } }],
  });
}

/* ---------- スポットの印 ----------
   丸い点だけだと種類が分からず、数が増えると読めない。**種類ごとに絵の印**にして、
   地図には symbol レイヤで置く（MapLibre が重なりを自動で間引いてくれる）。
   日本語のラベルは字の画像（glyphs）を配る必要があるので、印だけで伝える。 */
/* **色は種類ごとに、はっきり違う色相から取る。**
   もとは子育て支援センター #2f6f4f と公園 #3f8f57 がどちらも緑で、
   並ぶと見分けられなかった（ユーザーの指摘）。いまは
   **子育て支援センター＝青／公園＝緑**で、色の差は OKLab で ΔE 29。

   地図の上ではどの 2 つも隣り合いうるので、**実際によく出る 4 種**
   （公園・授乳・子育て支援センター・イベント）は総当たりで検証した
   （通常視での最小 ΔE 16.3＝授乳と支援センター、P 型色覚で 9.2・D 型で 10.2
   ＝イベントと公園（Viénot 1999 で模擬）、下地とのコントラストは全て 3:1 以上）。
   まれにしか出ない図書館・トイレ・公民館どうしは基準を下回るが、
   **印の絵（本・トイレ・柱の建物）が別物**なので色だけに頼っていない。

   `sort` は**重なったときにどれを残すか**（小さいほど残る）。ユーザー指定の順で、
   子育て支援センター・児童館 → イベント → その他のスポット → 公園。
   公園がいちばん多いので、引いたときは公園から消える。 */
const SPOT_KINDS = {
  hiroba: { label: "子育て支援センター・児童館", color: "#2a78d6", sort: 1 },
  event: { label: "イベント", color: "#e34948", sort: 2 },
  library: { label: "図書館", color: "#d55181", sort: 3 },
  hall: { label: "公民館・区民センター", color: "#c98500", sort: 3 },
  nursing: { label: "授乳・おむつ替え", color: "#4a3aa7", sort: 3 },
  toilet: { label: "おむつ替えのできるトイレ", color: "#eb6834", sort: 3 },
  other: { label: "その他", color: "#6b7280", sort: 3 },
  park: { label: "公園", color: "#008300", sort: 4 },
};

/* 印の中身。白抜きで描く。`c` は丸の色で、くり抜きに使う */
const SPOT_GLYPH = {
  park: () => '<path fill="#fff" d="M11 4.2 6.7 10.6h2.5L5.6 15.9h10.8l-3.6-5.3h2.5Z"/>'
    + '<rect fill="#fff" x="10.1" y="15.6" width="1.8" height="2.6" rx=".4"/>',
  hiroba: () => '<path fill="#fff" fill-rule="evenodd" d="M11 3.9 3.6 10.5h2.2v7.6h10.4v-7.6h2.2Z'
    + 'M9.7 12.6h2.6v5.5H9.7Z"/>',
  library: () => '<path fill="#fff" d="M4.4 5.6c1.9-.9 4.2-.9 6 .1v11.6c-1.8-1-4.1-1-6-.1Z'
    + 'M11.6 5.7c1.8-1 4.1-1 6-.1v11.6c-1.9-.9-4.2-.9-6 .1Z"/>',
  // 公民館は「家」と紛れないよう、柱の並んだ公共の建物にする
  hall: () => '<path fill="#fff" d="M4.4 8.8 11 4.5l6.6 4.3v1.2H4.4Z"/>'
    + '<g fill="#fff"><rect x="6.5" y="11" width="1.9" height="5.4"/>'
    + '<rect x="10.05" y="11" width="1.9" height="5.4"/>'
    + '<rect x="13.6" y="11" width="1.9" height="5.4"/>'
    + '<rect x="4.9" y="16.9" width="12.2" height="1.5"/></g>',
  nursing: (c) => '<path fill="#fff" d="M9.1 3.6h3.8v1.7H9.1Z"/>'
    + '<path fill="#fff" d="M8.3 5.9h5.4v1.8H8.3Z"/>'
    + '<path fill="#fff" d="M8 8.2h6v8.4a1.8 1.8 0 0 1-1.8 1.8H9.8A1.8 1.8 0 0 1 8 16.6Z"/>'
    + `<g fill="${c}"><rect x="9.4" y="11" width="3" height=".9"/>`
    + '<rect x="9.4" y="13.2" width="3" height=".9"/></g>',
  toilet: () => '<path fill="#fff" d="M7.2 4.2h7.6v3.4H7.2Z"/>'
    + '<path fill="#fff" d="M8.4 7.6h5.2v1.7H8.4Z"/>'
    + '<path fill="#fff" d="M6.3 9.6h9.4v1.7c0 2.8-1.6 4.7-3.5 5.1v1.6H9.8v-1.6'
    + 'c-1.9-.4-3.5-2.3-3.5-5.1Z"/>',
  event: () => '<path fill="#fff" d="m11 3.6 2.4 5.1 5.6.7-4.1 3.9 1 5.5L11 16l-4.9 2.7 1-5.5'
    + 'L3 9.4l5.6-.7Z"/>',
  other: () => '<circle fill="#fff" cx="11" cy="11" r="4.4"/>',
};

// 印の基準の大きさ（CSS px）。画像は 3 倍で描いて pixelRatio 3 で置くのでぼやけない
const ICON_BASE_PX = 44;

/* ---------- 当て馬の道にある「困るもの」の印 ----------
   **行き先の印とは逆に、白丸に赤いふち**で描く。行き先の印は「色の丸＋白い絵」なので、
   地図の上で取り違えない（当て馬の道の上にしか出ない印なので、赤は警告の意味になる）。
   種類は 2 つだけにする。階段はそれ自体が絵で分かり、それ以外（段差・狭い・急な坂）は
   ひとまとめに「！」でよい。**細かく描き分けるほど地図は読みにくくなる。** */
const BLOCK_COLOR = "#b23b2e";
const BLOCK_GLYPH = {
  // **塗りつぶした階段の形**にする。細い線の絵は、地図の上の 20px では読めない
  steps: (c) => `<path fill="${c}" d="M4.4 17.4V13.2h4.3V9.4h4.3V5.6h4.6v11.8Z"/>`,
  warn: (c) => `<path fill="${c}" d="M11 5 17.9 17.2H4.1Z"/>`
    + '<rect fill="#fff" x="10.25" y="9.5" width="1.5" height="4.1" rx=".7"/>'
    + '<circle fill="#fff" cx="11" cy="15.2" r=".9"/>',
};

function blockIconSvg(kind, px) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${px}" height="${px}" viewBox="0 0 22 22"`
    + ` class="bi"><circle cx="11" cy="11" r="9.3" fill="#fff" stroke="${BLOCK_COLOR}"`
    + ` stroke-width="2"/>${BLOCK_GLYPH[kind](BLOCK_COLOR)}</svg>`;
}

function loadBlockIcons() {
  return Promise.all(Object.keys(BLOCK_GLYPH).map((kind) => new Promise((done) => {
    const img = new Image();
    img.onload = () => { map.addImage(`block-${kind}`, img, { pixelRatio: 3 }); done(); };
    img.onerror = () => done();
    img.src = "data:image/svg+xml;charset=utf-8,"
      + encodeURIComponent(blockIconSvg(kind, ICON_BASE_PX * 3));
  })));
}

function spotIconSvg(kind, px) {
  const c = SPOT_KINDS[kind].color;
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${px}" height="${px}" viewBox="0 0 22 22">`
    + `<circle cx="11" cy="11" r="9.6" fill="${c}" stroke="#fff" stroke-width="1.6"/>`
    + SPOT_GLYPH[kind](c) + "</svg>";
}

function loadSpotIcons() {
  // SVG を画像にしてから map.addImage に渡す。3 倍で描いて pixelRatio 3 で置く
  return Promise.all(Object.keys(SPOT_KINDS).map((kind) => new Promise((done) => {
    const img = new Image();
    img.onload = () => { map.addImage(`spot-${kind}`, img, { pixelRatio: 3 }); done(); };
    img.onerror = () => done();
    img.src = "data:image/svg+xml;charset=utf-8,"
      + encodeURIComponent(spotIconSvg(kind, ICON_BASE_PX * 3));
  })));
}

function spotKind(s) {
  const c = s.facility_type || s.category || "";
  if (c.includes("イベント")) return "event";
  if (c.includes("トイレ")) return "toilet";
  if (c.includes("図書館")) return "library";
  // 公民館にあたるものは市区町村で呼び名が違う（港区は「区民センター」「区民協働施設」）。
  // 科学館・郷土歴史館も同じ「公共の建物」の印で出す
  if (c.includes("公民館") || c.includes("文化会館") || c.includes("区民センター")
      || c.includes("協働施設") || c.includes("科学館") || c.includes("郷土歴史館")) return "hall";
  // **子育ての施設を公園より先に見る。** 市外の市は「つどいの広場」のように
  // 「広場」を名前に持つ施設があり、公園を先に見ると木の印になってしまう
  if (c.includes("支援センター") || c.includes("ひろば") || c.includes("児童館")
      || c.includes("こども館") || c.includes("つどい")) return "hiroba";
  // 呼び名は市ごとに違う（流山市「赤ちゃんほっとスペース」／柏市「あかちゃんほっと
  // ステーション」／松戸市「赤ちゃんぽけっと」）。市外は「授乳・おむつ替え」にそろえてある
  if (c.includes("赤ちゃん") || c.includes("ほっと") || c.includes("授乳")
      || c.includes("おむつ")) return "nursing";
  if (c.includes("公園") || c.includes("緑地") || c.includes("広場")) return "park";
  return "other";
}

/* 地図に出すスポット。**半径で絞った結果がそのまま出る**ので、ここでの追加の絞り込みはない
   （以前は「選んでいる手段では届かないスポット」を落としていた）。 */
function shownSpots() {
  return state.spots;
}

function drawSpots() {
  const items = shownSpots();
  if (!layersReady) return;                 // 下地の入れ替え中
  map.getSource("spots").setData({
    type: "FeatureCollection",
    features: items.map((s) => {
      const kind = spotKind(s);
      return {
        type: "Feature",
        properties: {
          id: s.id, kind, outside: !!s.outside,
          // 公園どうしでは「わざわざ行く公園」を先に残す。
          // ほかの種類を追い越さないよう、下駄は段の中に収める
          sort: SPOT_KINDS[kind].sort - (s.park_rank === "major" ? 0.4 : 0),
          scale: s.park_rank === "major" ? 1.18 : 1,   // 大きい公園は印も少し大きく
        },
        geometry: { type: "Point", coordinates: [s.lon, s.lat] },
      };
    }),
  });
  const out = items.filter((s) => s.outside).length;
  const kinds = new Set(items.map(spotKind));
  // 公共交通の下敷きを出しているときだけ、その行を足す
  const tr = Object.entries(TRANSIT)
    .filter(([k]) => state.transitOn[k] && state.transit
      && (state.transit.counts || {})[k])
    .map(([, def]) => `<span>${chip(def.color, 12)} ${def.label}</span>`).join("");
  $("legend").innerHTML =
    `<div class="lg-reach"><span>${chip(RANGE_FILL)} `
      + `自宅から半径 ${radiusText(state.radius)}以内</span></div>` +
    (tr ? `<div class="lg-spots">${tr}</div>` : "") +
    `<div class="lg-spots">${Object.keys(SPOT_KINDS).filter((k) => kinds.has(k))
      .map((k) => `<span>${spotIconSvg(k, 15)} ${SPOT_KINDS[k].label}</span>`).join("")}</div>` +
    `${items.length} 件（半径 ${radiusText(state.radius)}以内）`
    + (out ? `／うち市外 ${out} 件（印が少し薄いもの）` : "")
    + `<br>印が重なるところは、寄ると出ます`;
}

/* ---------- 天気 ----------
   静的版は**結果を先に計算して配る**が、天気だけは焼き込めない
   （数日後に「今日の天気」が嘘になる）。気象庁の JSON は CORS が開いているので、
   **ブラウザから直接取って、その場で判断する**（backend/weather.py と同じ見方）。 */
const JMA_URL = (area) => `https://www.jma.go.jp/bosai/forecast/data/forecast/${area}.json`;
const WEATHER_NG = ["雨", "雪", "雷"];
let weatherCache = {};

async function forecastOf(stage) {
  const area = stage.jma_area;
  if (!area) return null;
  if (weatherCache[area] !== undefined) return weatherCache[area];
  try {
    const data = await jget(JMA_URL(area));
    const series = data[0].timeSeries;
    const out = { weather: series[0].areas[0].weathers[0], pops: null, temp_max: null };
    if (series[1] && series[1].areas[0].pops) out.pops = series[1].areas[0].pops;
    for (const sr of series) {
      const temps = (sr.areas[0].temps || []).map(Number).filter((t) => !Number.isNaN(t));
      if (temps.length) out.temp_max = Math.max(...temps, out.temp_max === null ? -99 : out.temp_max);
    }
    weatherCache[area] = out;
  } catch (e) {
    weatherCache[area] = null;      // 取れなくても画面は壊さない
  }
  return weatherCache[area];
}

function weatherAdvice(spot, fc) {
  if (!fc) return [];
  const notes = [];
  const outdoor = !spot.indoor;
  const weather = fc.weather || "";
  const temp = fc.temp_max;
  const pops = (fc.pops || []).map(Number).filter((p) => !Number.isNaN(p));
  const maxPop = pops.length ? Math.max(...pops) : null;
  if (outdoor) {
    if (WEATHER_NG.some((k) => weather.includes(k))) {
      notes.push({ level: "warn",
        text: `今日の天気は「${weather}」です。屋根のある行き先も見てみてください` });
    } else if (maxPop !== null && maxPop >= 50) {
      notes.push({ level: "warn", text: `降水確率が${maxPop}%あります。雨具の用意を` });
    }
    if (temp !== null && temp >= 35) {
      notes.push({ level: "warn",
        text: `最高気温${temp}℃の猛暑日です。ベビーカーは地面に近く暑くなります` });
    } else if (temp !== null && temp >= 31) {
      notes.push({ level: "info", text: `最高気温${temp}℃です。日陰と水分を用意してください` });
    }
  } else if (WEATHER_NG.some((k) => weather.includes(k))) {
    notes.push({ level: "info", text: "屋内なので、雨の日でも過ごせます" });
  }
  return notes;
}

/* ---------- スポットの吹き出し ---------- */
async function openSpot(id, lngLat) {
  const d = await API.spot(state.stage.stage, id);
  const s = d.spot;
  // **種類は見出しの札**に出すので、表には入れない（同じことを 2 度書かない）
  const rows = [];
  if (s.place_name) rows.push(["場所", s.place_name]);
  if (s.where) rows.push(["授乳室の場所", s.where]);
  if (s.playground) rows.push(["遊具", s.playground]);
  if (s.area) rows.push(["広さ", s.area]);
  else if (s.area_m2) {
    // 広さの出どころは 2 つある。**市区町村のページに書いてあればそちら**、
    // 無ければ OpenStreetMap のポリゴンから計算したもの
    const src = s.outside ? "公式ページより" : "OpenStreetMap の形から計算";
    rows.push(["広さ", s.area_m2 >= 10000
      ? `約${(s.area_m2 / 10000).toFixed(1)}ヘクタール（${src}）`
      : `約${Math.round(s.area_m2 / 100) * 100}m²（${src}）`]);
  }
  if (s.event_start) rows.push(["開催日",
    s.event_end && s.event_end !== s.event_start ? `${s.event_start} 〜 ${s.event_end}` : s.event_start]);
  const hours = s.open_time || s.close_time
    ? `${(s.open_time || "").slice(0, 5)}〜${(s.close_time || "").slice(0, 5)}` : s.open_hours;
  if (hours) rows.push(["利用時間", hours]);
  if (s.closed_days) rows.push(["休み", s.closed_days]);
  if (s.fee_text) rows.push(["料金", s.fee_text]);
  else if (s.fee_free) rows.push(["料金", "無料"]);
  if (s.barrier_free) rows.push(["設備", s.barrier_free]);
  const eq = [s.nursing_room && "授乳", s.diaper_table && "おむつ替え",
    s.hot_water && "調乳用のお湯", s.stroller_ok && "ベビーカーのまま"].filter(Boolean);
  if (eq.length) rows.push(["できること", eq.join("・")]);
  if (s.capacity) rows.push(["定員", typeof s.capacity === "string" ? s.capacity : ""]);
  rows.push(["対象", `${ageText(s.age_min)} 〜 ${ageText(s.age_max)}`]);
  // 静的版は天気を焼き込んでいないので、ブラウザで取った予報から判断する
  const notes = STATIC ? weatherAdvice(s, await forecastOf(state.stage)) : (d.advice || []);
  const advice = notes.map((a) =>
    `<div class="${a.level === "warn" ? "warn" : "note"}">${a.text}</div>`).join("");
  const visit = s.visit_note
    ? `<div class="${s.dropin === false ? "warn" : "note"}">${s.visit_note}</div>` : "";
  const big = s.park_rank === "major"
    ? `<div class="note">${s.outside
      ? "この市区町村が<b>代表的な公園として紹介している行き先</b>です"
      : "広さや遊具から、<b>わざわざ行く価値のある公園</b>と見ています"}</div>` : "";
  // 確認日が無いものもあるので、**あるときだけ**書く（「／ 確認」が宙に浮かないように）
  const checked = s.detail_checked_at || s.checked_at;
  const official = s.official_url
    ? `<p>公式ページ: <a href="${s.official_url}" target="_blank" rel="noopener">${
      s.official_label || "リンク"}</a>（自前整備${checked ? `／${checked} 確認` : ""}）</p>` : "";
  // **地図の印・左の一覧・この吹き出しで、同じ色と同じ絵をつかう**（2026-09-30・
  // ユーザー指示「見た目が微妙、統一感がない」）。種類は見出しの札にして、
  // 中身は「まず読むもの（概要と注意）→ 事実の表 → 出どころ」の 3 段に固定する
  const kind = spotKind(s);
  const tint = SPOT_KINDS[kind].color;
  const also = (s.also_types || []).length
    ? `<span class="sp-also">${s.also_types.join("・")}でもあります</span>` : "";
  // 自宅からの距離は `/api/spot` には入っていない（半径でさがしたときに付く欄）ので、
  // いま地図に出しているものから引く
  const near = state.spots.find((x) => x.id === s.id);
  const dist = near && near.distance_m !== undefined
    ? `<span class="sp-dist">自宅から ${Math.round(near.distance_m / 100) / 10}km</span>` : "";
  const sources = [`<a href="${s.source_url}" target="_blank" rel="noopener">${
    s.source_label || s.source_id}</a>${s.checked_at ? `（${s.checked_at} 確認）` : ""}`]
    .concat((s.also_sources || []).filter((x) => x && x.url).map((x) =>
      `<a href="${x.url}" target="_blank" rel="noopener">${x.label || "別の出典"}</a>`));
  const html = `<div class="sp" style="--tint:${tint}">
    <header class="sp-head">
      <span class="sp-icon">${spotIconSvg(kind, 26)}</span>
      <div>
        <h3>${s.name}</h3>
        <p class="sp-meta"><span class="tag">${s.facility_type || "行き先"}</span>${dist}${also}</p>
      </div>
    </header>
    ${s.summary ? `<p class="summary">${s.summary}</p>` : ""}
    ${visit}${big}${advice}
    <dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>
    ${s.note ? `<p class="sp-note">${s.note}</p>` : ""}
    <footer class="sp-src">
      ${official}
      <p>出典: ${sources.join(" ／ ")}</p>
    </footer>
    <button id="setDest">ここを目的地にする</button>
  </div>`;
  if (state.popup) state.popup.remove();   // 吹き出しは 1 つだけ開く
  const popup = new maplibregl.Popup({ closeButton: true, maxWidth: "310px" })
    .setLngLat(lngLat || [s.lon, s.lat]).setHTML(html).addTo(map);
  state.popup = popup;
  setTimeout(() => {
    const btn = document.getElementById("setDest");
    if (btn) btn.onclick = () => { setDest(s); popup.remove(); };
    // **開いた直後は必ず先頭から読ませる。** 中身が入りきらないと、
    // ブラウザが中のどれかに焦点を当てた拍子に途中までスクロールした状態で開く
    // （名前も種類も見えないまま「料金」から始まって見えた）
    const box = document.querySelector(".maplibregl-popup .sp");
    if (box) box.scrollTop = 0;
    fitPopup(popup);
  }, 0);
}

/* **目的地の旗。** 自宅は丸いピン、目的地は旗にして、形で見分けられるようにする
   （色だけだと地図の印と紛れる）。色は**経路の線と同じ緑**にして、
   「線はこの旗まで来ている」が一目で分かるようにした（2026-09-30・ユーザー指示）。 */
let destMarker;
const DEST_FLAG = `<svg xmlns="http://www.w3.org/2000/svg" width="30" height="38" viewBox="0 0 30 38">
  <path d="M15 36 V5" stroke="#fff" stroke-width="6" stroke-linecap="round"/>
  <path d="M15 36 V5" stroke="#2f6f4f" stroke-width="3" stroke-linecap="round"/>
  <path d="M16.6 5.5 L28 10 L16.6 14.8 Z" fill="#2f6f4f" stroke="#fff"
    stroke-width="1.8" stroke-linejoin="round"/></svg>`;

function setDestMarker(spot) {
  if (!map) return;
  if (!destMarker) {
    const el = document.createElement("div");
    el.className = "dest-pin";
    el.innerHTML = DEST_FLAG;
    destMarker = new maplibregl.Marker({ element: el, anchor: "bottom" });
  }
  destMarker.setLngLat([spot.lon, spot.lat]).addTo(map);
}

function clearDestMarker() {
  if (destMarker) destMarker.remove();
}

/* 経路の結果を片づける。**目的地を変えたとき**と**閉じるボタン**の両方で使う
   （古い線が残っていると、どちらの行き先の経路か分からなくなる）。 */
function clearRoute() {
  showRoutePane(false);
  state.plans = null;
  state.planKey = null;
  state.routeFC = emptyFC();
  state.compareFC = emptyFC();
  state.blockFC = emptyFC();
  if (layersReady) {
    map.getSource("route").setData(state.routeFC);
    map.getSource("compare").setData(state.compareFC);
    map.getSource("blocks").setData(state.blockFC);
  }
  drawRouteLegend([]);
  $("results").innerHTML = "";
}

/* **吹き出しが地図の外に出たら、地図のほうをずらして収める。**

   地図は中身をはみ出させない（印が外に飛び出さないように MapLibre が切っている）ので、
   はみ出した吹き出しは切れて読めなくなる。**スマホでは地図が 58vh しかなく**、
   縦に長い吹き出しはほぼ必ずはみ出す。吹き出しを縮める代わりに地図を動かす。 */
function fitPopup(popup) {
  const el = popup && popup.getElement();
  if (!el || !map) return;
  const box = map.getContainer().getBoundingClientRect();
  const r = el.getBoundingClientRect();
  const M = 8;
  let dy = 0;
  if (r.height < box.height - M * 2) {
    if (r.bottom > box.bottom - M) dy = r.bottom - (box.bottom - M);
    else if (r.top < box.top + M) dy = r.top - (box.top + M);
  } else {
    dy = r.top - (box.top + M);      // 入りきらないときは、せめて頭を見せる
  }
  if (Math.abs(dy) > 2) map.panBy([0, dy], { duration: 200 });
}

function setDest(spot) {
  // **行き先を変えたら、前の経路は片づける**（2026-09-30・ユーザー指示）
  if (state.dest && state.dest.id !== spot.id) clearRoute();
  state.dest = spot;
  $("destText").textContent = `目的地: ${spot.name}`;
  $("searchBtn").disabled = !(state.homePoint && state.dest);
  setDestMarker(spot);
}

/* ---------- 舞台 ---------- */
async function loadStage(stageId) {
  const stage = state.stages.find((s) => s.stage === stageId);
  state.stage = stage;
  history.replaceState(null, "", `?stage=${stageId}`);
  document.querySelectorAll("#stageSwitch button:not([disabled])").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.id === stageId)));
  const boundary = await API.boundary(stageId);
  state.boundary = boundary;
  setMask(boundary);
  map.jumpTo({ center: [stage.center.lon, stage.center.lat], zoom: 12.4 });
  // 自宅の候補は舞台ごと。1 つでもプルダウンのまま出す（あとで増やせるように）
  $("homeSelect").innerHTML = (stage.homes || []).map((h) =>
    `<option value="${h.id}">${h.name}</option>`).join("");
  state.home = (stage.homes || [{ id: "center" }])[0].id;
  await loadNearby(true);
  renderCoverage();
  loadEvents();
  loadTransit(stageId);          // 重いので待たない（あとから下敷きが敷かれる）
  if (stage.indoor) {
    const st = await API.stations(stageId);
    state.entrancesFC = {
      type: "FeatureCollection",
      features: (st.stations || []).map((s) => ({
        type: "Feature",
        properties: { station: s.name, name: `${s.step_free}/${s.entrances}`,
          stepFree: s.step_free > 0 },
        geometry: { type: "Point", coordinates: [s.lon, s.lat] } })),
    };
  } else {
    state.entrancesFC = emptyFC();
  }
  if (layersReady) map.getSource("entrances").setData(state.entrancesFC);
}

/* ---------- 公共交通の下敷き（2026-09-30・ユーザー指示） ----------
   **経路探索が使っている路線と乗り場**をそのまま地図に重ねる。飾りの路線図ではない
   （点は探索の単位＝`transit.Network.places`）。港区は 200 路線・2,776 か所あるので、
   引いたときはバス停を出さず、線も細くして下敷きに徹させる。 */
async function loadTransit(stageId) {
  state.transit = null;
  drawTransit();
  try {
    const d = await API.network(stageId);
    if (state.stage && state.stage.stage !== stageId) return;   // 切り替えが追い越した
    state.transit = d && d.available === false ? null : d;
  } catch (e) {
    state.transit = null;                     // 無くても画面は成り立つ
  }
  drawTransit();
  renderTransitBox();
  drawSpots();                   // 凡例に電車・バスの行を足す（待たずに走らせているので後から）
}

function drawTransit() {
  if (!layersReady) return;
  const d = state.transit;
  map.getSource("transit").setData(d ? d.lines : emptyFC());
  map.getSource("transit-stops").setData(d ? d.stops : emptyFC());
  Object.keys(TRANSIT).forEach((kind) => {
    const on = !!state.transitOn[kind];
    [`transit-line-${kind}`, `transit-stop-${kind}`].forEach((id) =>
      map.setLayoutProperty(id, "visibility", on ? "visible" : "none"));
  });
}

/* チェックボックス。**どのフィードが何路線ぶん効いているか**まで出す
   （右ペインの「使えるデータ」と同じ考え方で、数で語る）。 */
function renderTransitBox() {
  const box = $("transitBox");
  const d = state.transit;
  if (!d || !(d.feeds || []).length) { box.hidden = true; return; }
  box.hidden = false;
  const c = d.counts || {};
  $("transitChecks").innerHTML = Object.entries(TRANSIT).map(([kind, def]) => {
    const feeds = d.feeds.filter((f) => f.kind === kind);
    if (!feeds.length) return "";
    const routes = kind === "train" ? c.train : c.bus;
    const stops = kind === "train" ? c.train_stops : c.bus_stops;
    return `<label class="check"><input type="checkbox" data-kind="${kind}"
        ${state.transitOn[kind] ? "checked" : ""}>
        ${chip(def.color, 12)} ${def.label}
        <span class="hint">${routes} 路線・${stops} か所</span></label>
      <p class="hint transit-feeds">${feeds.map((f) =>
        `${f.name}（${f.routes}）`).join("・")}</p>`;
  }).join("");
  $("transitChecks").querySelectorAll("input[type=checkbox]").forEach((el) => {
    el.onchange = () => {
      state.transitOn[el.dataset.kind] = el.checked;
      drawTransit();
      drawSpots();                 // 凡例に電車・バスの行を出し入れする
    };
  });
  // 線をどこから引いたかは、まとめて 1 行で言う
  const by = {};
  d.feeds.forEach((f) => Object.entries(f.by_shape || {}).forEach(([k, n]) => {
    by[k] = (by[k] || 0) + n;
  }));
  $("transitNote").innerHTML = Object.entries(by)
    .map(([k, n]) => `${n} 路線: ${SHAPE_SOURCE[k] || k}`).join("<br>");
}

function renderPicks(all) {
  const picks = all || [];
  const box = $("pickBox");
  if (!picks.length) { box.hidden = true; return; }
  box.hidden = false;
  $("picks").innerHTML = picks.map((s) => {
    const hours = s.open_time ? `${s.open_time.slice(0, 5)}〜${(s.close_time || "").slice(0, 5)}` : "";
    const size = s.area_m2 >= 10000 ? `／約${(s.area_m2 / 10000).toFixed(1)}ha`
      : s.area_m2 ? `／約${Math.round(s.area_m2 / 100) * 100}m²` : "";
    return `<div class="pick" data-id="${s.id}">
      <div>${spotIconSvg(spotKind(s), 15)} <b>${s.name}</b>
        <span class="tag">${s.facility_type || ""}</span></div>
      <div class="hint">${Math.round(s.distance_m / 100) / 10}km ${hours ? `／ ${hours}` : ""}
        ${size} ${s.dropin ? "／予約なしで行けます" : ""}</div>
      ${s.summary ? `<div class="hint summary">${s.summary}</div>` : ""}</div>`;
  }).join("");
  document.querySelectorAll("#picks .pick").forEach((el) => {
    el.onclick = () => {
      const s = state.spots.find((x) => x.id === el.dataset.id)
        || picks.find((x) => x.id === el.dataset.id);
      map.flyTo({ center: [s.lon, s.lat], zoom: 15 });
      openSpot(s.id, [s.lon, s.lat]);
    };
  });
}

async function loadEvents() {
  const d = await API.events(state.stage.stage, state.snapshot);
  const box = $("eventBox");
  if (!d.total_in_data) { box.hidden = true; return; }
  box.hidden = false;
  $("events").innerHTML = d.events.length
    ? d.events.map((e) => `<div class="leg"><span class="mode">${e.event_start || ""}</span>
        <span><b>${e.name}</b>${e.place_name ? ` / ${e.place_name}` : ""}</span></div>`).join("")
      + `<p class="hint">出典: 市のイベント一覧（${d.total_in_data}件）</p>`
    : `<p class="hint">この日以降のイベントは、公開データ（${d.total_in_data}件）にありませんでした。
       子育て向けのイベントを機械が読める形で出している自治体はまだ少なく、ここは伸びしろです。</p>`;
}

function renderCoverage() {
  const s = state.stage;
  const hk = s.hokonavi_detail, ind = s.indoor_detail, sp = s.spots_detail;
  const line = (label, value, ok) =>
    `<div><b>${label}</b>: ${value} ${ok === false ? "（この舞台にはありません）" : ""}</div>`;
  $("coverage").innerHTML =
    line("面積", `${s.area_km2} km²`) +
    line("歩道の段差データ（ほこナビ）", hk.available ? `${hk.km} km` : "なし", hk.available) +
    line("駅の中（GTFS-Pathways）", ind.available
      ? `${ind.stations}駅・出入口${ind.entrances}か所のうち段差なし${ind.step_free}か所` : "なし",
      ind.available) +
    // **どれだけ入っているか**まで出す。名前だけだと「バスは入っていないのでは」と読める
    line("時刻表", (s.feeds || []).map((f) => f.feed_name
      + (f.stops ? `（${f.routes}${f.route_word || "系統"}・${f.stops}${f.place_word || "停留所"}`
        + (f.trips ? `・のべ${f.trips}便` : "") + "）" : "")).join("、") || "なし") +
    ((s.missing_feeds || []).length
      ? line("取り込めていない時刻表",
        s.missing_feeds.map((f) => `${f.name}（${f.reason}）`).join("、")) : "") +
    // JSON API から組み直したフィードは、**位置をどう補ったか**まで出す
    (s.feeds || []).filter((f) => f.built).map((f) => line(
      `${f.feed_name}の停留所の位置`,
      `この路線バスは GTFS が配信されておらず、<b>時刻表の JSON に停留所の座標がありません</b>。`
      + `名前で照合して、${f.built.stops_from_gtfs} 件は<b>ほかの事業者が出した GTFS</b> から、`
      + `${f.built.stops_from_osm} 件は<b>OpenStreetMap</b> から位置を補いました`
      + (f.built.stops_without_location
        ? `／位置が分からなかった ${f.built.stops_without_location} 停留所は出していません` : ""))
    ).join("") +
    line("スポットの概要・公式ページ（自前整備）",
      `${sp.detailed} 件に概要と公式ページを付けました` +
      (sp.closed ? `／オープンデータに残っていた廃止済みの施設 ${sp.closed} 件は出していません` : "")) +
    line("公園の広さ（市区町村のページ／OpenStreetMap のポリゴン）",
      sp.parks
        ? `${sp.parks.inside + sp.parks.outside} 件のうち ${sp.parks.with_area} 件`
          + `（市のオープンデータには広さの欄がありません）`
          + `／わざわざ行く公園 ${(sp.parks.ranks.major || 0)
            + (sp.parks.outside_ranks.major || 0)} 件` : "—") +
    line("市外の行き先（自前整備）",
      `${(sp.outside && sp.outside.count) || 0} 件` +
      (sp.outside && sp.outside.by_city && sp.outside.by_city.length
        ? `（${sp.outside.by_city.slice(0, 3).map(([n, c]) => `${n} ${c}`).join("・")}…）` : "")
      + (sp.outside && sp.outside.by_type && sp.outside.by_type.length
        ? `／${sp.outside.by_type.map(([n, c]) => `${n} ${c}`).join("・")}` : "")
      + (sp.outside && sp.outside.source_note ? `／${sp.outside.source_note}` : "")
      + (sp.outside && sp.outside.osm_candidates
        ? `（OpenStreetMap の候補 ${sp.outside.osm_candidates} 件から選びました）` : "")) +
    line("web で中身を確かめられなかったスポット",
      sp.no_web_info_total
        ? `${sp.no_web_info_total} 件は地図に出していません`
          + `（${(sp.no_web_info || []).slice(0, 3).map(([k, n]) => `${k} ${n}`).join("・")}）`
        : "なし") +
    // **持っているのに出していないもの**も理由つきで出す（舞台の設定 `hide_types`）
    (sp.hidden_total
      ? line("行き先として出していない種類",
        `${(sp.hidden_types || []).map(([k, n]) => `${k} ${n} 件`).join("・")}`
        + `（データは持っていますが、行き先にはならないので出していません）`) : "") +
    line("入園・入会が要る施設",
      `${sp.enrollment_excluded} 件は取り込んでいません` +
      ((sp.enrollment_types || []).length
        ? `（${sp.enrollment_types.map(([k, n]) => `${k} ${n}`).join("・")}）` : "")) +
    line("スポット", `${sp.total} 件（すべて予約なしで行けます）` +
      (sp.sources.some((x) => x.no_location)
        ? `（座標が無くて地図に出せないもの ${sp.sources.reduce((a, b) => a + (b.no_location || 0), 0)} 件）`
        : ""));
}

/* ---------- 自宅とさがす範囲 ---------- */

/* さがす範囲を塗り直す。**自宅を中心にした半径 N メートルの円**を画面側で引く。
   面をサーバから送る必要がないので、スライダを動かしても地図は即座に描き替わる
   （スポットの取り直しだけがサーバ往復になる）。 */
function drawRange() {
  if (!layersReady) return;
  const h = state.homePoint;
  map.getSource("range").setData(
    h ? { type: "FeatureCollection", features: [circleOf(h.lat, h.lon, state.radius)] }
      : emptyFC());
}

/* **さがす範囲の下に説明を並べない**（2026-09-30・ユーザー指示「ごちゃごちゃ説明が
   書いてあるので消して」）。件数と種類の内訳は**地図の凡例に出ている**ので、
   左ペインで繰り返さない。さがしているあいだのぐるぐるだけ残す
   （港区は 6 秒ほどかかるので、止まって見えないように）。 */
function renderRangeHint() {
  $("reachHint").innerHTML = "";
}

function setHomeMarker(home) {
  if (!homeMarker) {
    homeMarker = new maplibregl.Marker({ color: RANGE_LINE })
      .setLngLat([home.lon, home.lat]).addTo(map);
  } else {
    homeMarker.setLngLat([home.lon, home.lat]);
  }
}

/* 行き先の読み込み。**半径の中にあるスポットを取ってくるだけ**なので 1 回で済む
   （到達圏のときは手段ごとに計算が要り、2 段構えで取りに行っていた）。
   円そのものは画面側で引くので、スライダを動かした瞬間に地図の範囲は変わり、
   スポットだけが少し遅れて入れ替わる。 */
async function loadNearby(fit) {
  // スライダを続けて動かすと古い応答が後から返ることがある。最後の 1 つだけを描く
  const seq = ++state.reqSeq;
  $("reachHint").innerHTML = '<span class="spinner"></span>行き先をさがしています…';
  const d = await API.nearby(state.stage.stage, state.snapshot, state.home, state.radius);
  if (seq !== state.reqSeq) return;
  state.spots = d.spots;
  state.picks = d.picks || [];
  state.homePoint = d.home;
  setHomeMarker(d.home);
  drawRange();
  renderRangeHint();
  renderPicks(state.picks);
  drawSpots();
  if (fit) fitToRange();
}

/* さがす範囲の円がぜんぶ入るように地図を寄せる。 */
function fitToRange() {
  const h = state.homePoint;
  if (!h) return;
  const b = new maplibregl.LngLatBounds();
  circleOf(h.lat, h.lon, state.radius).geometry.coordinates[0].forEach((p) => b.extend(p));
  map.fitBounds(b, { padding: 40, duration: 400 });
}

/* ---------- 経路 ---------- */

/* 経路のペイン（左ペインの右隣）。結果が出るまでは畳んでおき、列そのものを消す。 */
function showRoutePane(on) {
  $("routePane").hidden = !on;
  $("app").classList.toggle("has-route", !!on);
  if (map) map.resize();      // 列が増減すると地図の幅が変わる
}

const spinner = (text) => `<p class="loading"><span class="spinner"></span>${text}</p>`;

/* 探索は数秒かかる。**待っているあいだ、かたまったように見えない**ようにする。 */
function setSearching(on) {
  const btn = $("searchBtn");
  btn.disabled = on || !state.dest;
  btn.innerHTML = on ? '<span class="spinner"></span>さがしています…' : "経路をさがす";
}

async function search() {
  showRoutePane(true);
  setSearching(true);
  state.planKey = null;        // さがし直したら、地図はトータルナビから出し直す
  $("results").innerHTML = spinner("経路をさがしています…");
  // 到達圏は 3 通り計算するが、経路は 1 通りに決めて出す。
  // ベビーカーだけは通れる道が変わるので、ここで選べるようにしている
  const profile = state.stroller ? "stroller" : "walk";
  const body = {
    stage: state.stage.stage,
    from_lat: state.homePoint.lat, from_lon: state.homePoint.lon, from_name: "自宅",
    to_lat: state.dest.lat, to_lon: state.dest.lon, to_name: state.dest.name,
    mode: profile,
    date: state.snapshot.date, time: state.snapshot.time,
  };
  const ask = (modes) => API.routes(state.stage.stage, state.snapshot, state.home,
    profile, state.dest, body, modes);
  try {
    // **自転車と車はさがさない**（2026-09-30・ユーザー指示）。公共交通ではなく、
    // 推奨されているオープンデータも使っていないため。**待ち時間も無くなった**
    // （以前は公共交通と自転車・車を別々に投げ、あとから来たほうを重ねていた）
    const d = await ask(["total", "train", "bus", "walk"]);
    state.plans = d;
    renderPlans(d);
  } catch (e) {
    $("results").innerHTML = `<p class="warn">経路をさがせませんでした（${e}）</p>`;
  } finally {
    setSearching(false);
  }
}

/* **よけた甲斐を 1 行で言う。** 「遠回り」だけが見えて「何をよけたか」が
   見えないと、遠回りは損にしか見えない。**代わりに通らずに済んだもの**を太字で出す。
   `hint` はカードの中で 1 回だけ（地図の見方は繰り返さない）。 */
function compareLine(c, hint) {
  // **数えるのは印の数**（地図に出ている丸の数）。ひとつの階段が OSM では
  // 何本もの way に割れているので、辺の数を数えると地図と合わなくなる
  const by = {};
  (c.obstacles || []).forEach((o) => { by[o.label] = (by[o.label] || 0) + 1; });
  const what = Object.entries(by)
    .map(([k, n]) => `${k}${n > 1 ? ` ${n} か所` : ""}`).join("・");
  const far = c.extra_m > 20
    ? `${c.extra_m}m（約${Math.max(1, Math.round(c.extra_seconds / 60))}分）遠回りして、`
    : "遠回りせずに、";
  // カードの印は**地図に出ている印と同じ絵**にする（階段があれば階段の絵）
  const icon = (c.obstacles || []).some((o) => o.kind === "steps") ? "steps" : "warn";
  return `<div class="cmp">${blockIconSvg(icon, 13)}
    <span class="txt">${far}<b>${what}</b>を通らない道です</span>
    ${hint ? `<span class="cmp-hint">グレーの点線が、よけない最短の道（${c.meters}m）です</span>` : ""}
    </div>`;
}

function legLine(l, hint) {
  const label = l.mode === "transit"
    ? `${ROUTE_ICON[l.route_type] || "乗る"}　${(l.route_name || "").slice(0, 18)}`
    : MODE_LABEL[l.mode] || l.mode;
  const detail = l.mode === "transit"
    ? `${l.from} → ${l.to}`
    // **エレベーターは通ったことを言う。** 段差をよけた道の中身そのものなので、
    // 「よけた」と言うだけでなく「どう通るか」を出す
    : `${l.meters ? `${l.meters}m` : ""} ${l.steps ? `階段${l.steps}か所` : ""}`
      + `${l.elevators ? ` エレベーター${l.elevators}か所` : ""}`;
  return `<div class="leg"><span class="mode">${l.dep_text || ""}</span>
    <span><b>${label}</b> ${detail}</span></div>`
    + (l.stroller_note ? `<div class="note">${l.stroller_note}</div>` : "")
    + (l.entrance ? `<div class="${l.entrance.step_free ? "note" : "warn"}">${l.entrance.text}</div>` : "")
    + (l.compare && state.compare ? compareLine(l.compare, hint) : "");
}

/* 並べる順。**経路が出なかった手段は、行ごと出さない**
   （「見つかりませんでした」と並べても読む人にできることが無い）。 */
const PLAN_ROWS = [["total", "トータルナビ"], ["train", "電車"], ["bus", "バス"],
                   ["walk", "徒歩"]];
const hasPlan = (p) => !!p && !p.unavailable;

function planCard(key, title, p, selected) {
  if (!hasPlan(p)) return "";
  let hinted = false;
  const legs = p.legs.map((l) => {
    const first = !!(l.compare && state.compare) && !hinted;
    if (first) hinted = true;
    return legLine(l, first);
  }).join("");
  const notes = (p.notes || []).map((n) => `<div class="note">${n}</div>`).join("")
    + (p.warnings || []).map((n) => `<div class="warn">${n}</div>`).join("")
    + (p.legs || []).flatMap((l) => (l.warnings || []).concat(l.notes || []))
      .map((n) => `<div class="note">${n}</div>`).join("");
  // 押すと地図の線がその手段に変わる。いま地図に出ているものには印を付ける
  return `<div class="plan${selected ? " on" : ""}" data-plan="${key}"
    role="button" tabindex="0" aria-pressed="${!!selected}"><h3>${title}
    <span class="time">${p.depart_text} → ${p.arrive_text}（${Math.round(p.duration / 60)}分）</span></h3>
    ${legs}${notes}</div>`;
}

/* **線が 2 本あるときだけ、地図の上でどちらがどちらかを言う。**
   経路のカードは右のペインにあり、地図だけを見ている人には色の意味が分からない。
   1 本しか無いときは出さない（説明の要らない地図に説明を足さない）。 */
function drawRouteLegend(marks) {
  const box = $("routeLegend");
  if (!box) return;
  box.hidden = !marks.length;
  if (!marks.length) return;
  const by = {};
  marks.forEach((m) => { by[m.properties.kind] = (by[m.properties.kind] || 0) + 1; });
  const what = Object.entries(by)
    .map(([k, n]) => `${blockIconSvg(k, 13)} ${k === "steps" ? "階段" : "段差・狭い道"} ${n} か所`)
    .join("　");
  box.innerHTML = `<div><span class="ln ln-route"></span><b>この経路</b>（段差をよけた道）</div>`
    + `<div><span class="ln ln-cmp"></span>よけない最短の道</div>`
    + `<div class="lg-blocks">${what}</div>`;
}

/* 選んだ手段の経路を地図に描く。**カードを押すたびにここを通る。** */
function drawPlan(key, fit = true) {
  const p = state.plans && state.plans[key];
  if (!hasPlan(p)) return;
  state.planKey = key;
  const feats = p.legs.map((l) => ({
    type: "Feature", properties: { mode: l.mode },
    geometry: { type: "LineString",
      coordinates: l.coords || [[l.from_lon, l.from_lat], [l.to_lon, l.to_lat]] },
  }));
  state.routeFC = { type: "FeatureCollection", features: feats };
  // 当て馬の道と、その上の印
  const lines = [];
  const marks = [];
  if (state.compare) {
    p.legs.forEach((l) => {
      if (!l.compare) return;
      lines.push({ type: "Feature", properties: {},
        geometry: { type: "LineString", coordinates: l.compare.coords } });
      (l.compare.obstacles || []).forEach((o) => marks.push({
        type: "Feature",
        properties: { kind: o.kind === "steps" ? "steps" : "warn",
          label: o.label, source: o.source, count: o.count || 1 },
        geometry: { type: "Point", coordinates: [o.lon, o.lat] } }));
    });
  }
  state.compareFC = { type: "FeatureCollection", features: lines };
  state.blockFC = { type: "FeatureCollection", features: marks };
  if (layersReady) {
    map.getSource("route").setData(state.routeFC);
    map.getSource("compare").setData(state.compareFC);
    map.getSource("blocks").setData(state.blockFC);
  }
  drawRouteLegend(marks);
  document.querySelectorAll("#results .plan").forEach((el) => {
    const on = el.dataset.plan === key;
    el.classList.toggle("on", on);
    el.setAttribute("aria-pressed", String(on));
  });
  if (fit && feats.length) {
    const b = new maplibregl.LngLatBounds();
    feats.concat(lines).forEach((f) => f.geometry.coordinates.forEach((c) => b.extend(c)));
    map.fitBounds(b, { padding: 60 });
  }
}

function renderPlans(d, fit = true) {
  const rows = PLAN_ROWS;
  // 地図に出す手段は、いま選んでいるものを保つ。無ければ出ている中の先頭
  const shown = rows.filter(([k]) => hasPlan(d[k])).map(([k]) => k);
  const key = shown.includes(state.planKey) ? state.planKey : shown[0];
  // **比べるものがあるときだけ**スイッチを出す（ベビーカーでよけた道があるとき）。
  // 見くらべが要らない人は切れるようにしておく（線が 2 本になるため）
  const canCompare = rows.some(([k]) => hasPlan(d[k])
    && (d[k].legs || []).some((l) => l.compare));
  $("results").innerHTML =
    `<p class="dest">${state.dest ? state.dest.name + "まで" : ""}</p>`
    + (canCompare ? `<label class="check cmp-toggle"><input type="checkbox" id="compareChk"
        ${state.compare ? "checked" : ""}>段差をよけない道と見くらべる</label>` : "")
    + rows.map(([k, title]) => planCard(k, title, d[k], k === key)).join("")
    + (shown.length ? "" : '<p class="hint">この行き先までの経路は見つかりませんでした</p>');
  if (canCompare) {
    // 見くらべを切り替えただけなので、**地図は動かさない**（勝手に寄ると迷子になる）
    $("compareChk").onchange = (e) => {
      state.compare = e.target.checked;
      renderPlans(d, false);
    };
  }
  $("results").querySelectorAll(".plan").forEach((el) => {
    el.onclick = () => drawPlan(el.dataset.plan);
    el.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); drawPlan(el.dataset.plan); }
    };
  });
  if (key) drawPlan(key, fit);
}

/* ---------- このサービスについて ---------- */
async function showAbout() {
  const app = $("app"), about = $("about");
  if (!about.hidden) { about.hidden = true; app.hidden = false; map.resize(); return; }
  // 舞台が 1 つしか作られていない静的版でも開けるように、取れなかった舞台は飛ばす
  const rows = (await Promise.all(state.stages.filter(isUsable).map((s) =>
    API.coverage(s.stage).catch(() => null)))).filter(Boolean);
  about.innerHTML = `<h2>子育てナビについて</h2>
  <p>子どもと出かけられる場所を、<b>自宅からの到達圏</b>でさがします。
  行き先が決まったら、そこまでの行き方（徒歩・ベビーカー・公共交通）も同じ画面で出します。</p>
  ${STATIC ? `<h2>出発時刻は決まっています（結果を先に計算して配っています）</h2>
  <p>このサイトは<b>サーバを持っていません。</b>到達圏も経路も<b>あらかじめ全部計算して</b>
  JSON ファイルとして置き、画面はそれを読んでいるだけです。
  そのぶん<b>出発時刻は選べる組み合わせに限られます</b>
  （いま用意してあるのは ${state.snapshots.map((x) => `「${x.label}」`).join("・")}）。</p>
  <p>こうした理由は、計算が重いからです。サーバで動かすと
  <b>1 回の到達圏の計算で最大 1.5GB のメモリ</b>を使います
  （大都市の時刻表は <b>stop_time が 100 万行</b>を超え、これをメモリに載せて探索するため）。
  無料で借りられるサーバは 512MB 級なので載りません。
  <b>出発時刻を決めてしまえば答えは有限</b>なので、先に計算して配る形にしました。
  サーバが要らないぶん、待ち時間もほとんどありません。</p>
  <p>時刻表や施設のデータを更新したときは、計算し直して置き直します。
  <b>天気だけは焼き込んでいません</b>（数日後に「今日の天気」が嘘になるため)。
  気象庁の予報はブラウザから直接取っているので、こちらはいつ見ても今日のものです。</p>` : ""}

  <h2>到達圏は 1 つだけ選んで出します</h2>
  <p>自宅と時間の上限を決めると、その中で行ける範囲を地図に描きます。
  手段は<b>徒歩・自転車・徒歩＋公共交通から 1 つ</b>選んでください。
  はじめは 3 つを重ねて出していましたが、<b>重ねると地図が読めなくなった</b>ので、
  1 つだけ出す形に変えました。</p>
  <p>とはいえ<b>3 つとも計算して持っています。</b>選び直しても計算はやり直さないので待ちません。
  選んでいない手段の広さも数字だけ並べているので、
  「歩きならここまで、自転車ならこれだけ広がる」は数字で比べられます。</p>
  <p>スポットは <b>選んだ手段で行ける範囲に入るものだけ</b>を出します。
  自転車は同じ道路網を自転車で通れる道だけに絞って探索し、
  徒歩＋公共交通は出発時刻の時刻表で探索するので、朝と夜でも形が変わります。</p>

  <h2>地図に出すのは、web で中身を確かめたスポットだけです</h2>
  <p>オープンデータの CSV に入っているのは<b>名称・所在地・座標・電話だけ</b>で、
  「そこに何があるか」は 1 件も入っていません。名前しか分からない場所を地図に出しても、
  行き先としては選べません。</p>
  <p>そこで、<b>市区町村のホームページを 1 件ずつ当たって、事実（遊具・広さ・トイレ・
  利用時間）と公式ページのリンクをそろえたものだけ</b>を地図に出しています。
  そろっていないものは出さずに、件数を下に並べています。</p>
  <p>市の外も同じです。以前は隣の市を <b>OpenStreetMap</b> の公園で埋めていましたが
  （流山市のまわりだけで 2,097 件）、OSM にあるのは名前と形だけなので<b>やめました</b>。
  いまは<b>隣の市が自分のホームページで紹介している行き先</b>を 1 件ずつ調べて並べています
  （松戸市「まつどDE子育て・市内の公園」、柏市の施設案内、東京都公園協会「公園へ行こう！」など）。
  <b>自治体が紹介している＝わざわざ行く価値がある</b>という選び方です。</p>
  <p class="hint">OpenStreetMap は<b>公園の広さを測るため</b>には使っています
  （オープンデータに広さの欄が無いため）。地図に出す行き先としては使いません。</p>

  <h2>公園は「わざわざ行く価値があるもの」を選びます</h2>
  <p>公園は数が多すぎます（流山市だけで 429 件）。全部出すと地図が公園で埋まります。
  流山市の公園は<b>広さの中央値が 717m²</b>（20m×35m ほど）で、ほとんどが街区公園です。</p>
  <p>ところが<b>公園のオープンデータに広さの欄はありません</b>。そこで
  <b>OpenStreetMap のポリゴンの面積を測って</b>広さを付けました。
  市区町村が自分のページで面積を出しているときは、そちらを優先しています。
  遊具の種類は、市区町村の施設ページから自前で集めたものです。</p>
  <p>広さ・遊具・トイレで 4 つに分けて、<b>近ければ小さくても出し、遠いところは大きいものだけ</b>
  出します（600m までは全部、1.5km までは「広さ 3,000m² 以上か遊具 3 種類以上」、
  それより先は「1ha 以上／遊具がそろってトイレもある」公園だけ）。
  <b>トイレがあるだけでは行く理由にしません</b>（公園のトイレはあることのほうが多く、
  それだけで選ぶと絞れません）。
  <b>広さも遊具も分からない公園を「小さい」とは決めつけません。</b>
  そこは出さなかった件数として画面に出しています。</p>

  <h2>地図はできるだけすっきりさせます</h2>
  <p>印は<b>種類ごとに絵を変えています</b>（公園は木、子育て支援センター・児童館は家、
  図書館は本、公民館は建物、授乳・おむつ替えは哺乳瓶、トイレ、イベントは星）。
  印が重なるところは<b>自動で間引いて</b>、寄ると出てきます
  （<b>残す順番は、子育て支援センター・児童館 → イベント → その他のスポット → 公園</b>。
  公園がいちばん多いので、引いたときは公園から消えます）。</p>
  <p>到達圏の塗りと地図の下地は、<b>寄るほど薄くします。</b>
  範囲の境目は輪郭線で分かるので、塗りは薄くても困りません。</p>

  <h2>地図の下地は ${MAP_STYLE.label} です</h2>
  <p>地理院の淡色地図やベクトルタイルなど 7 種類を見比べたうえで、
  <b>${MAP_STYLE.label} に決めました</b>（以前は左のペインで選べるようにしていましたが、
  選ぶものではないので 1 つに固定しました）。</p>
  <p class="hint"><b>鍵（API キー）が要りません。</b>
  鍵の要る下地は、鍵が切れた瞬間に作品が動かなくなるためです。
  地理院の淡色地図は<b>地名の文字がタイルに焼き込まれていて外せません</b>が、
  ベクトルタイルなら<b>道路の名前と国道の番号だけ消せる</b>ので、
  行き先さがしに要らない文字を減らせます。</p>

  <h2>経路の線は、実際の道の形で描きます</h2>
  <p>駅と駅、停留所と停留所を<b>直線で結ぶと、線路や道の曲がりが消えて</b>、
  地図の上で嘘になります。<b>歩く区間も同じ</b>で、家から駅までを直線で結ぶと
  建物や運河を突き抜けてしまいます。区間ごとに、出どころの違う線を引いています。</p>
  <table><tr><th>手段</th><th>線の出どころ</th></tr>
  <tr><td>歩く区間（家→駅・駅→行き先・乗り換え）</td><td><b>歩行ネットワーク</b>
    （OpenStreetMap の道に、ほこナビの歩道をつないだもの。
    <b>所要時間を測ったのと同じ道</b>をそのまま線にしています）</td></tr>
  <tr><td>バス（流山ぐりーんバス）</td><td><b>GTFS の <code>shapes.txt</code></b>
    （便ごとの形が時刻表のデータに入っています）</td></tr>
  <tr><td>電車（つくばエクスプレス・東武アーバンパークライン）</td><td><b>OpenStreetMap の線路</b>
    （どちらも GTFS に <code>shapes.txt</code> が入っていないためです）</td></tr>
  <tr><td>バス（東武バス）</td><td><b>停留所を結んだ線</b>
    （このバスは GTFS そのものが無く、JSON の時刻表から組み直しているため、
    通る道の形が手に入りません）</td></tr>
  </table>
  <p class="hint">乗り降りする駅・停留所をその線に下ろして、あいだを切り出しています。
  <b>時刻や所要時間は時刻表のままで、変わるのは地図に描く線だけ</b>です。
  形が見つからなかったときだけ、2 点を結んだ線で描きます。</p>
  <p class="hint">歩く区間は、<b>ベビーカーで行くと線も変わります</b>。
  階段を通らない道で時間を測っているので、線もその道を引きます。</p>

  <h2>よけた道は、よけない道と並べて出します</h2>
  <p>「ベビーカーで行く」を選ぶと、階段や段差を通らない道でさがします。けれども
  <b>よけた道を 1 本だけ出しても、何をよけたのかは画面に出ません</b>。
  遠回りだけが見えて、損をしたように見えてしまいます。</p>
  <p>そこで<b>同じ区間を「段差をよけない、ふつうの最短の道」でも引いて</b>、
  地図に<b>グレーの点線</b>で並べ、その道にある階段や段差に印を置いています。
  「57m（約1分）遠回りして、階段 2 か所を通らない道です」と読めるようにするためです。</p>
  <table><tr><th>印</th><th>出どころ</th></tr>
  <tr><td>階段</td><td><b>OpenStreetMap の <code>highway=steps</code></b></td></tr>
  <tr><td>段差・狭い道・急な坂</td><td><b>ほこナビ（歩行空間ネットワークデータ）</b>の
    段差・有効幅員・縦断勾配。<b>このデータがある地区でだけ</b>出ます</td></tr>
  </table>
  <p class="hint">線は<b>2 本が分かれてから合流するまで</b>しか描きません（同じ道を
  2 本重ねると、どちらが案内なのか分からなくなるため）。
  <b>よけるものが無いときや、2 本が同じ道になったときは出しません。</b>
  見くらべが要らないときは、経路の欄のチェックを外すと消えます。</p>

  <h2>東武バスは、時刻表から自分で GTFS を組み直しています</h2>
  <p>流山市を走るバスのうち、<b>GTFS で配信されているのは市の流山ぐりーんバスだけ</b>です。
  東武バスは<b>時刻表の JSON（チャレンジ2026 限定データ）しか公開されていません</b>。
  そのままでは地図にも探索にも使えないので、停留所・系統・時刻表を取り寄せて
  <b>GTFS に組み直してから</b>取り込んでいます。</p>
  <p><b>いちばんの壁は停留所の位置です。</b>この JSON には
  <b>停留所の座標が 1 件も入っていません</b>（東武バスの停留所 4,728 件すべて）。
  そこで停留所の名前で照合して、次の順で位置を補いました。</p>
  <ol><li><b>ほかの事業者が出した GTFS の停留所</b>（同じ停留所を使っている場合。事業者の公式の座標）</li>
  <li><b>OpenStreetMap のバス停</b></li></ol>
  <p class="hint">どちらでも見つからなかった停留所は、<b>位置を推測して置かずに、出していません</b>
  （件数は右の「この舞台で使えるデータ」に出しています）。
  同じ名前の停留所が離れて複数あるときは、かたまりでまとめて、
  その事業者の名前が付いているほうを選んでいます。</p>

  <h2>行き先は「今日、思い立って行ける場所」だけです</h2>
  <p>保育所・幼稚園・学童クラブは入園や入会の手続きが要るので、
  <b>データそのものから外しています</b>（オープンデータには入っていますが、取り込みません）。
  残したのは、子育て支援センター・児童館・公園・図書館・赤ちゃんほっとスペース・
  おむつ替えのできるトイレ・イベントです。</p>
  <p><b>到達圏は市の境で止まりません。</b>ところがスポットのオープンデータは市ごとなので、
  隣の市は空白になります。そこは<b>隣の市の公式ページを当たって自前で整備した行き先</b>で
  埋めています（市内は市のオープンデータと自前整備のほうが詳しいので、そちらを使います）。</p>

  <h2>舞台を切り替えると、データの偏りが見えます</h2>
  <table><tr><th>舞台</th>${rows.map((r) => `<th>${r.name}</th>`).join("")}</tr>
  <tr><td>面積</td>${rows.map((r) => `<td>${r.area_km2} km²</td>`).join("")}</tr>
  <tr><td>歩道の段差（ほこナビ）</td>${rows.map((r) =>
    `<td>${r.hokonavi.available ? `${r.hokonavi.km} km` : "なし"}</td>`).join("")}</tr>
  <tr><td>駅の中（GTFS-Pathways）</td>${rows.map((r) => `<td>${r.indoor.available
    ? `${r.indoor.stations}駅／出入口${r.indoor.entrances}か所のうち段差なし${r.indoor.step_free}か所` : "なし"}</td>`).join("")}</tr>
  <tr><td>時刻表</td>${rows.map((r) =>
    `<td>${r.feeds.map((f) => f.feed_name + (f.stops
      ? `<br><span class="hint">${f.routes}${f.route_word || "系統"}・`
        + `${f.stops}${f.place_word || "停留所"}・のべ${f.trips}便</span>` : ""))
      .join("<br>")}</td>`).join("")}</tr>
  <tr><td>取り込めていない時刻表</td>${rows.map((r) => `<td>${r.missing_feeds.length
    ? r.missing_feeds.map((f) => `${f.name}（${f.reason}）`).join("<br>") : "—"}</td>`).join("")}</tr>
  <tr><td>市外の行き先<br>（自前整備）</td>${rows.map((r) => `<td>${
    (r.spots.outside && r.spots.outside.count) || 0} 件${
    r.spots.outside && r.spots.outside.by_city
      ? "<br>" + r.spots.outside.by_city.slice(0, 3).map(([n, c]) => `${n} ${c}`).join("・")
      : ""}</td>`).join("")}</tr>
  <tr><td>スポットの概要と公式ページ<br>（自前整備）</td>${rows.map((r) => `<td>${
    r.spots.detailed ? `${r.spots.detailed} 件に付けました` : "まだありません"}${
    r.spots.closed ? `<br>廃止済みで出していない施設 ${r.spots.closed} 件<br>`
      + r.spots.closed_names.map((c) => c.name).join("・") : ""}</td>`).join("")}</tr>
  <tr><td>公園の広さ<br>（OSM のポリゴンから計算）</td>${rows.map((r) => `<td>${
    r.spots.parks ? `${r.spots.parks.inside + r.spots.parks.outside} 件のうち `
      + `${r.spots.parks.with_area} 件<br>わざわざ行く公園 `
      + `${(r.spots.parks.ranks.major || 0) + (r.spots.parks.outside_ranks.major || 0)} 件` : "—"
  }</td>`).join("")}</tr>
  <tr><td>入園・入会が要る施設<br>（取り込んでいません）</td>${rows.map((r) => `<td>${
    r.spots.enrollment_excluded} 件<br>${(r.spots.enrollment_types || [])
      .map(([k, n]) => `${k} ${n}`).join("・")}</td>`).join("")}</tr>
  <tr><td>スポット</td>${rows.map((r) => `<td>${r.spots.total} 件<br>` +
    r.spots.sources.map((s) => `${s.label}: ${s.count}件` +
      (s.no_location ? `（座標なしで出せない ${s.no_location} 件）` : "")).join("<br>") + "</td>").join("")}</tr>
  </table>
  <p class="hint"><b>バスの時刻表は、出している事業者の分しか入りません。</b>
  流山市内を走る路線バスのうち、GTFS を公開しているのは市の「流山ぐりーんバス」だけです
  （千葉県内で GTFS データリポジトリに登録があるのは 10 フィード）。
  市内を走る東武バスは公共交通オープンデータセンターにありますが、
  <b>チャレンジ2026 限定のデータ</b>なので、専用トークンが届くまでは入れられません。
  上の「取り込めていない時刻表」は、その空白をそのまま出したものです。</p>

  <h2>公共交通データについて</h2>
  <div class="note">
  <p>このサービスが利用する公共交通データは、<b>公共交通オープンデータセンター</b>において
  提供されるものです。公共交通事業者により提供されたデータを元にしていますが、
  <b>必ずしも正確・完全なものとは限りません</b>。
  このサービスの表示内容について、<b>公共交通事業者への直接の問合せは行わないでください</b>。</p>
  <p>このサービスに関するお問い合わせは ${state.site && state.site.contact_url
    ? `<a href="${state.site.contact_url}" target="_blank" rel="noopener">${
      state.site.contact_label || state.site.contact_url}</a>へお願いします。`
    : "<b>（公開前に data/site.yaml に問い合わせ先を設定してください）</b>"}</p>
  </div>
  <p>取り込んだ時刻表と、その<b>取得日</b>（元データは更新されることがあります）:</p>
  <table><tr><th>舞台</th><th>時刻表</th><th>取得日</th><th>ライセンス</th></tr>
  ${rows.flatMap((r) => (r.feeds || []).map((f) => `<tr><td>${r.name}</td><td>${f.feed_name}</td>`
    + `<td>${f.fetched_at || "—"}</td><td>${licenseOf(f)}</td></tr>`)).join("")}
  </table>

  <h2>出典</h2>
  <p>公共交通オープンデータセンター（ODPT）／GTFSデータリポジトリ／歩行空間ナビ・プロジェクト（国土交通省）／
  東京都オープンデータカタログサイト／流山市オープンデータ／国土数値情報／
  OpenStreetMap contributors／OpenFreeMap（OpenMapTiles）／気象庁</p>
  <p class="hint">都営地下鉄・都営バス（東京都交通局）と流山ぐりーんバス（流山市）のデータは
  <a href="https://creativecommons.org/licenses/by/4.0/deed.ja" target="_blank" rel="noopener">CC BY 4.0</a>、
  つくばエクスプレス（首都圏新都市鉄道）のデータは
  <a href="https://developer.odpt.org/terms/data_basic_license.html" target="_blank" rel="noopener">公共交通オープンデータ基本ライセンス</a>、
  東武鉄道・東武バスのデータは
  <a href="https://developer.odpt.org/challenge_license" target="_blank" rel="noopener">チャレンジ2026 限定ライセンス</a>
  にもとづいて利用しています。</p>
  <p class="hint">スポットの<b>概要・公式ページ・利用時間・遊具・授乳やおむつ替えの可否</b>は、
  オープンデータにほとんど入っていません（座標と電話だけの CSV がほとんどです）。
  市のホームページの施設案内から事実を集めて、1 件ずつ出典と確認日をつけて自前で整備しています。
  市のページの文章はそのまま載せず、集めた事実から自分の言葉で書き直しています。</p>
  <p class="hint">整備の途中で、<b>すでに廃止された施設がオープンデータに残っている</b>ことも分かりました。
  こうした施設は地図に出さず、件数だけを上の表に出しています。</p>`;
  about.hidden = false; app.hidden = true;
}

/* ---------- 起動 ---------- */
(async function init() {
  let saved = null;
  try {
    saved = Number(localStorage.getItem("kosodate.radius"));
  } catch (e) { /* 無くても動く */ }
  if (saved && saved >= RADIUS_MIN && saved <= RADIUS_MAX) state.radius = saved;
  state.stages = await API.stages();
  // **まだ使えない舞台**（`enabled: false`）はボタンだけ出して押せなくする。
  // data/stages/<id>.yaml に見出しを置いておけばここに並ぶので、
  // 舞台を足すときは設定を書き戻すだけでよい（画面は触らない）
  const usable = state.stages.filter(isUsable);
  const want = new URLSearchParams(location.search).get("stage");
  state.stage = usable.find((s) => s.stage === want) || usable[0];
  $("stageSwitch").innerHTML = state.stages.map((s) => (isUsable(s)
    ? `<button data-id="${s.stage}" aria-pressed="${s.stage === state.stage.stage}">${s.name}</button>`
    : `<button data-id="${s.stage}" disabled title="${s.note || "いまは選べません"}"
        >${s.name}</button>`)).join("");
  document.querySelectorAll("#stageSwitch button:not([disabled])").forEach((b) => {
    b.onclick = () => { state.dest = null; $("destText").textContent = "目的地: 未設定";
      $("searchBtn").disabled = true; clearDestMarker(); clearRoute();
      loadStage(b.dataset.id); };
  });
  // 出発時刻。**事前計算した組み合わせから選ぶ**（静的版は結果がファイルになっているため）。
  // いまは 1 つだけだが、data/snapshots.yaml に足せばそのままここに並ぶ
  state.site = await API.site().catch(() => ({}));
  state.snapshots = await API.snapshots();
  state.snapshot = state.snapshots[0];
  $("snapshot").innerHTML = state.snapshots.map((s) =>
    `<option value="${s.id}">${s.label}</option>`).join("");
  $("snapshot").value = state.snapshot.id;
  $("snapshotNote").textContent = state.snapshot.note || "";
  $("snapshot").onchange = (e) => {
    state.snapshot = state.snapshots.find((x) => x.id === e.target.value) || state.snapshots[0];
    $("snapshotNote").textContent = state.snapshot.note || "";
    loadEvents();
    loadNearby(false);
  };
  $("homeSelect").onchange = (e) => { state.home = e.target.value; loadNearby(true); };
  // つまみを動かしているあいだは**地図の円だけ**を追わせる（サーバには聞きに行かない）。
  // スポットを取り直すのは指を離したとき（`onchange`）
  $("radius").oninput = (e) => {
    state.radius = Number(e.target.value);
    $("radiusLabel").textContent = radiusText(state.radius);
    drawRange();
  };
  $("radius").onchange = () => {
    try { localStorage.setItem("kosodate.radius", String(state.radius)); }
    catch (e) { /* 無くても動く */ }
    loadNearby(true);
  };
  $("stroller").onchange = (e) => { state.stroller = e.target.checked; };
  // チェックの初期値は **state から書き戻す**（index.html と二重に持たない）
  $("stroller").checked = state.stroller;
  $("radius").value = String(state.radius);
  $("radiusLabel").textContent = radiusText(state.radius);
  $("searchBtn").onclick = search;
  $("routeClose").onclick = clearRoute;   // 閉じたら地図の線も消す（出しっぱなしにしない）
  $("aboutBtn").onclick = showAbout;
  initMap(state.stage.center);
})();
