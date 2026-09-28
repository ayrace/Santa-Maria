from __future__ import annotations

from pathlib import Path
from io import StringIO
from datetime import datetime
from zoneinfo import ZoneInfo
import csv
import html
import math
import re
import time
import unicodedata

import pandas as pd
import pydeck as pdk
import requests
import streamlit as st

st.set_page_config(
    page_title="Painel Geográfico de Nodes — Santa Maria",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
BASE_FILE = ROOT / "base_nodes_santa_maria.csv"
LOCAL_CSV = DATA_DIR / "SANTA MARIA.csv"
OUTAGES_FILE = DATA_DIR / "outages_atual.csv"
LOCAL_TZ = ZoneInfo("America/Sao_Paulo")

# Pasta criada dentro do mesmo Drive usado pelas demais cidades.
DRIVE_FOLDER_ID = "1TkKRryLA1Tk5XZ2OTFUPPBtjdBMHXpss"
DRIVE_FILE_NAME = "SANTA MARIA.csv"
# Pode permanecer vazio: o painel descobre o ID pelo nome dentro da pasta pública.
DRIVE_FILE_ID_FALLBACK = ""

REFRESH_MINUTES = 60
STALE_AFTER_MINUTES = 90
CRITICAL_MAX_SCORE = 20
LIGHT_MAP_STYLE = "https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json"


def now_local():
    return datetime.now(LOCAL_TZ)


def norm_txt(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip().upper()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s)


def norm_col(v) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", norm_txt(v)).strip("_")


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def fmt_int(v) -> str:
    try:
        return f"{int(v):,}".replace(",", ".")
    except Exception:
        return "—"


@st.cache_data(show_spinner=False)
def load_base() -> pd.DataFrame:
    d = pd.read_csv(BASE_FILE)
    for c in ["Node", "Map_Node", "Origem", "Regiao", "Bairro", "Endereco", "Precisao", "Observacao"]:
        if c not in d.columns:
            d[c] = ""
        d[c] = d[c].fillna("").astype(str)
    for c in ["Latitude", "Longitude", "DisplayLatitude", "DisplayLongitude"]:
        if c not in d.columns:
            d[c] = pd.NA
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d["Node"] = d["Node"].map(norm_txt)
    d["Map_Node"] = d["Map_Node"].map(norm_txt)
    return d


@st.cache_data(show_spinner=False)
def read_csv_bytes(data: bytes) -> pd.DataFrame:
    last_error = None
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = data.decode(enc, errors="strict")
            df = pd.read_csv(StringIO(text), sep=None, engine="python")
            if df.shape[1] == 1 and "," in str(df.columns[0]):
                outer = list(csv.reader(StringIO(text)))
                inner = [next(csv.reader([r[0]])) if len(r) == 1 else r for r in outer]
                if inner and len(inner[0]) > 1:
                    width = len(inner[0])
                    rows = [r for r in inner[1:] if len(r) == width]
                    return pd.DataFrame(rows, columns=inner[0])
            return df
        except Exception as e:
            last_error = e
    raise ValueError(f"Não foi possível abrir o CSV: {last_error}")


@st.cache_data(ttl=60, show_spinner=False)
def fetch_public_drive_collection(cache_minute: int):
    """Busca SANTA MARIA.csv na pasta pública e força uma leitura nova a cada minuto."""
    headers = {
        "User-Agent": "Mozilla/5.0 Painel-Nodes-Santa-Maria/1.0",
        "Cache-Control": "no-cache, no-store, max-age=0",
        "Pragma": "no-cache",
    }
    file_id = DRIVE_FILE_ID_FALLBACK
    folder_url = f"https://drive.google.com/drive/folders/{DRIVE_FOLDER_ID}?usp=sharing&_cb={cache_minute}"
    try:
        fr = requests.get(folder_url, headers=headers, timeout=25)
        fr.raise_for_status()
        page = fr.text
        pos = page.lower().find(DRIVE_FILE_NAME.lower())
        if pos >= 0:
            window = page[max(0, pos - 3000): pos + 3000]
            ids = re.findall(r"([A-Za-z0-9_-]{20,})", window)
            for cand in ids:
                u = f"https://drive.google.com/uc?export=download&id={cand}&_cb={cache_minute}"
                rr = requests.get(u, headers=headers, timeout=20, allow_redirects=True)
                head = rr.content[:1200]
                if rr.ok and b"Node" in head and (b"Pontua" in head or b"Pontua" in rr.content[:3000]):
                    file_id = cand
                    break
    except Exception:
        pass

    if not file_id:
        raise FileNotFoundError(f"{DRIVE_FILE_NAME} ainda não foi localizado na pasta pública do Drive.")

    url = f"https://drive.google.com/uc?export=download&id={file_id}&_cb={cache_minute}"
    r = requests.get(url, headers=headers, timeout=35, allow_redirects=True)
    r.raise_for_status()
    if b"Node" not in r.content[:1500]:
        raise ValueError(f"{DRIVE_FILE_NAME} não retornou o CSV esperado.")
    return r.content, r.headers.get("Last-Modified", ""), file_id


def schema(df: pd.DataFrame):
    by = {norm_col(c): c for c in df.columns}
    if "NODE" not in by or "PONTUACAO" not in by:
        return None
    return {
        "NODE": by["NODE"],
        "PONTUACAO": by["PONTUACAO"],
        "IMPACTADO": by.get("IMPACTADO"),
        "ESTRESSADO": by.get("ESTRESSADO"),
        "TOTAL": by.get("TOTAL"),
    }


def split_node_port(v):
    s = norm_txt(v)
    m = re.match(r"^(.+?)[\s_/]+([1-4])$", s)
    if m:
        return m.group(1).strip(), int(m.group(2))
    m = re.match(r"^(.+?)-([1-4])$", s)
    if m:
        return m.group(1).strip(), int(m.group(2))
    # Cadastro atual aparece sem sufixo de porta; tratamos como leitura única.
    if s == "CMBAO-CNTAV":
        return s, 1
    return s, 1


def port_state(score):
    if score is None or pd.isna(score):
        return ""
    try:
        v = float(score)
    except Exception:
        return ""
    if v == 0:
        return "OFF"
    if 0 < v <= CRITICAL_MAX_SCORE:
        return "CRÍTICA"
    return "ONLINE"


def build_status(xdf: pd.DataFrame) -> pd.DataFrame:
    sch = schema(xdf)
    if not sch:
        return pd.DataFrame()
    d = xdf.copy()
    parsed = d[sch["NODE"]].map(split_node_port)
    d["_node"] = [x[0] for x in parsed]
    d["_port"] = [x[1] for x in parsed]
    d["_score"] = pd.to_numeric(d[sch["PONTUACAO"]], errors="coerce")
    for key, target in [("IMPACTADO", "_impactado"), ("ESTRESSADO", "_estressado"), ("TOTAL", "_total")]:
        col = sch.get(key)
        d[target] = pd.to_numeric(d[col], errors="coerce").fillna(0) if col else 0

    rows = []
    for node, g in d.groupby("_node"):
        scores, states = {}, {}
        ports = sorted(set(int(x) for x in g["_port"].dropna().tolist() if int(x) in (1, 2, 3, 4)))
        for p in ports:
            vals = g.loc[g["_port"] == p, "_score"].dropna()
            score = float(vals.min()) if len(vals) else None
            scores[p] = score
            states[p] = port_state(score)
        total_ports = len(ports)
        off = sum(states[p] == "OFF" for p in ports)
        critical = sum(states[p] == "CRÍTICA" for p in ports)
        if total_ports and off == total_ports:
            status = "SS TOTAL"
        elif off > 0:
            status = "SS PARCIAL"
        else:
            status = "ONLINE"
        row = {
            "Node": norm_txt(node),
            "Status": status,
            "Total_portas": total_ports,
            "Portas_OFF": int(off),
            "Portas_Criticas": int(critical),
            "Impactado": int(g["_impactado"].sum()),
            "Estressado": int(g["_estressado"].sum()),
            "Total": int(g["_total"].sum()),
        }
        details = []
        for p in ports:
            sc = scores.get(p)
            stt = states.get(p, "")
            row[f"P{p}_estado"] = stt
            row[f"P{p}_score"] = sc
            sc_txt = "—" if sc is None or pd.isna(sc) else str(int(sc) if float(sc).is_integer() else round(float(sc), 1))
            details.append(f"P{p}: {stt} ({sc_txt})")
        row["Portas_popup"] = " | ".join(details) if details else "Sem leitura de porta"
        rows.append(row)
    return pd.DataFrame(rows)


def load_outages() -> int:
    if not OUTAGES_FILE.exists():
        return 0
    try:
        d = pd.read_csv(OUTAGES_FILE)
        for c in ["Outages_Sem_Sinal", "outages", "Outages"]:
            if c in d.columns and len(d):
                return int(pd.to_numeric(d[c], errors="coerce").fillna(0).iloc[-1])
    except Exception:
        pass
    return 0


def color_for(status):
    return {
        "ONLINE": [16, 165, 90, 235],
        "SS PARCIAL": [245, 183, 0, 240],
        "SS TOTAL": [239, 51, 64, 245],
    }.get(status, [145, 155, 170, 220])


def status_order(status):
    return {"SS TOTAL": 3, "SS PARCIAL": 2, "ONLINE": 1}.get(status, 0)


# ---------- visual ----------
st.markdown(
    """
<style>
:root { --navy:#0b2443; --blue:#1677ff; --bg:#f4f7fb; --card:#fff; --text:#0b1736; --muted:#6b7892; --line:#e4eaf2; --yellow:#f5b700; --red:#ef3340; --green:#10a55a; }
html, body, [class*="css"] { font-family: Inter, "Segoe UI", Arial, sans-serif; }
[data-testid="stAppViewContainer"] { background:var(--bg); color:var(--text); }
[data-testid="stHeader"] { background:transparent; }
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] { display:none !important; }
#MainMenu, footer { visibility:hidden; }
.block-container { padding-top:.7rem; padding-bottom:1.5rem; max-width:1900px; }
.topbar { background:#0b2443; color:white; border-radius:15px; padding:18px 22px; margin-bottom:13px; box-shadow:0 8px 24px rgba(11,36,67,.12); }
.topbar h1 { margin:0; color:white; font-size:28px; line-height:1.08; }
.topbar .sub { color:#cbd8ea; margin-top:5px; font-size:14px; }
.topbar .meta { color:#9eb1cb; margin-top:6px; font-size:12px; }
.kpi-grid { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:10px; margin:10px 0 14px; }
.kpi { background:white; border:1px solid #e4eaf2; border-radius:13px; padding:13px 15px; min-height:93px; box-shadow:0 3px 12px rgba(11,36,67,.05); }
.kpi .label { color:#6b7892; font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:.03em; }
.kpi .value { color:#0b1736; font-size:30px; font-weight:800; margin-top:5px; }
.kpi .sub { color:#6b7892; font-size:12px; margin-top:3px; }
.kpi.online { border-top:4px solid #10a55a; }
.kpi.partial { border-top:4px solid #f5b700; }
.kpi.total { border-top:4px solid #ef3340; }
.kpi.blue { border-top:4px solid #1677ff; }
.alert { background:#fff4f4; border:1px solid #ffd6d9; border-left:5px solid #ef3340; border-radius:12px; padding:12px 15px; margin:8px 0 12px; }
.panel { background:white; border:1px solid #e4eaf2; border-radius:13px; padding:13px 15px; }
.focus { background:white; border:1px solid #dfe8f5; border-left:5px solid #1677ff; border-radius:12px; padding:12px 15px; margin:8px 0 10px; }
.focus b { color:#0b2443; }
.route { display:inline-block; margin-top:9px; padding:8px 12px; background:#1677ff; color:white !important; text-decoration:none; border-radius:9px; font-weight:700; }
.small { color:#6b7892; font-size:12px; }
@media (max-width:1000px){ .kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr));} .topbar h1{font-size:22px;} }
</style>
""",
    unsafe_allow_html=True,
)

# force page refresh every hour, same operating behavior as the finalized city panels
st.markdown(f"<meta http-equiv='refresh' content='{REFRESH_MINUTES * 60}'>", unsafe_allow_html=True)

base = load_base()
xraw = None
source_file_name = ""
source_updated = None
source_mode = ""
source_error = ""

# botão no topo, fora do bloco de outages
head_left, head_right = st.columns([8, 1.2])
with head_right:
    if st.button("🔄 Atualizar", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

cache_minute = int(time.time() // 60)
try:
    raw, last_mod, drive_file_id = fetch_public_drive_collection(cache_minute)
    xraw = read_csv_bytes(raw)
    source_file_name = DRIVE_FILE_NAME
    source_mode = "Google Drive"
    if last_mod:
        try:
            source_updated = pd.to_datetime(last_mod, utc=True).tz_convert(LOCAL_TZ).to_pydatetime()
        except Exception:
            source_updated = None
except Exception as e:
    source_error = str(e)
    if LOCAL_CSV.exists():
        try:
            xraw = read_csv_bytes(LOCAL_CSV.read_bytes())
            source_file_name = LOCAL_CSV.name
            source_mode = "fallback do GitHub"
            source_updated = datetime.fromtimestamp(LOCAL_CSV.stat().st_mtime, tz=LOCAL_TZ)
        except Exception as e2:
            source_error += f" | fallback: {e2}"

status_df = build_status(xraw) if xraw is not None and not xraw.empty else pd.DataFrame()
logical = base.merge(status_df, on="Node", how="left")
logical["Status"] = logical["Status"].fillna("AGUARDANDO STATUS")
logical["Portas_OFF"] = pd.to_numeric(logical.get("Portas_OFF", 0), errors="coerce").fillna(0).astype(int)
logical["Portas_Criticas"] = pd.to_numeric(logical.get("Portas_Criticas", 0), errors="coerce").fillna(0).astype(int)
logical["Total_portas"] = pd.to_numeric(logical.get("Total_portas", 0), errors="coerce").fillna(0).astype(int)
logical["Portas_popup"] = logical.get("Portas_popup", pd.Series(index=logical.index, dtype=str)).fillna("Sem leitura no XPERTrack")
logical["color"] = logical["Status"].map(color_for)
logical["Latitude_Map"] = logical["DisplayLatitude"].where(logical["DisplayLatitude"].notna(), logical["Latitude"])
logical["Longitude_Map"] = logical["DisplayLongitude"].where(logical["DisplayLongitude"].notna(), logical["Longitude"])

updated_txt = source_updated.strftime("%d/%m/%Y %H:%M") if source_updated else "horário não informado pela fonte"
with head_left:
    st.markdown(
        f"""<div class='topbar'><h1>Painel Geográfico de Nodes – Santa Maria</h1>
        <div class='sub'>XPERTrack • Pontuação por porta • padrão operacional consolidado</div>
        <div class='meta'>Fonte: {esc(source_mode or 'não disponível')} • {esc(source_file_name or DRIVE_FILE_NAME)} • {esc(updated_txt)}</div></div>""",
        unsafe_allow_html=True,
    )

if source_error and source_mode == "fallback do GitHub":
    st.caption("Drive ainda sem CSV válido; usando a coleta incluída nesta versão até SANTA MARIA.csv ser colocado na pasta do Drive.")

if status_df.empty:
    st.error("A coleta do XPERTrack não foi carregada. Verifique SANTA MARIA.csv.")

monitored = int(len(status_df))
online = int((status_df["Status"] == "ONLINE").sum()) if not status_df.empty else 0
partial = int((status_df["Status"] == "SS PARCIAL").sum()) if not status_df.empty else 0
total_off = int((status_df["Status"] == "SS TOTAL").sum()) if not status_df.empty else 0
ports_off = int(status_df["Portas_OFF"].sum()) if not status_df.empty else 0
mapped = int(logical["Latitude"].notna().sum())

cards = [
    ("Nodes monitorados", monitored, f"{mapped} com posição resolvida", "blue"),
    ("Online", online, "sem porta zerada", "online"),
    ("SS Parcial", partial, "1+ porta OFF e não total", "partial"),
    ("SS Total", total_off, "todas as portas existentes OFF", "total"),
    ("Portas OFF", ports_off, "Pontuação = 0", "blue"),
]
st.markdown("<div class='kpi-grid'>" + "".join(
    f"<div class='kpi {cl}'><div class='label'>{lab}</div><div class='value'>{fmt_int(val)}</div><div class='sub'>{sub}</div></div>"
    for lab, val, sub, cl in cards
) + "</div>", unsafe_allow_html=True)

outages = load_outages()
if outages > 0:
    st.markdown(f"<div class='alert'><b>⚠️ Outages sem sinal:</b> {outages}. Informação operacional manual; fica oculta quando o valor é zero.</div>", unsafe_allow_html=True)

# Busca apenas centraliza; não altera indicadores
nodes_search = sorted(logical["Node"].dropna().unique().tolist())
search_col, info_col = st.columns([2.2, 4.8])
with search_col:
    chosen = st.selectbox("🔎 Localizar node", [""] + nodes_search, format_func=lambda x: "Digite ou selecione o node..." if not x else x)

mapdf = logical[logical["Latitude_Map"].notna() & logical["Longitude_Map"].notna()].copy()
focus = mapdf[mapdf["Node"] == chosen].copy() if chosen else pd.DataFrame()

impact_base = logical[logical['Portas_OFF'] > 0].copy() if not logical.empty else pd.DataFrame()
if not impact_base.empty:
    region_impact = (impact_base[impact_base['Regiao'].fillna('').astype(str).str.strip() != '']
        .groupby('Regiao', dropna=False)
        .agg(Nodes_impactados=('Node', 'count'), Portas_OFF=('Portas_OFF', 'sum'))
        .reset_index()
        .sort_values(['Portas_OFF', 'Nodes_impactados', 'Regiao'], ascending=[False, False, True]))
    bairro_impact = (impact_base[impact_base['Bairro'].fillna('').astype(str).str.strip() != '']
        .groupby('Bairro', dropna=False)
        .agg(Nodes_impactados=('Node', 'count'), Portas_OFF=('Portas_OFF', 'sum'))
        .reset_index()
        .sort_values(['Portas_OFF', 'Nodes_impactados', 'Bairro'], ascending=[False, False, True]))
else:
    region_impact = pd.DataFrame(columns=['Regiao', 'Nodes_impactados', 'Portas_OFF'])
    bairro_impact = pd.DataFrame(columns=['Bairro', 'Nodes_impactados', 'Portas_OFF'])

with info_col:
    if not focus.empty:
        r = focus.iloc[0]
        route = f"https://www.google.com/maps/dir/?api=1&destination={float(r['Latitude']):.7f},{float(r['Longitude']):.7f}&travelmode=driving"
        anchor_note = f" • posição KML: {esc(r['Map_Node'])}" if r.get("Map_Node") and r.get("Map_Node") != r.get("Node") else ""
        critical_note = f" • {int(r['Portas_Criticas'])} porta(s) crítica(s) 1–{CRITICAL_MAX_SCORE}" if int(r.get("Portas_Criticas", 0)) else ""
        bairro_txt = esc(r.get('Bairro', '')) if str(r.get('Bairro', '')).strip() else '—'
        regiao_txt = esc(r.get('Regiao', '')) if str(r.get('Regiao', '')).strip() else '—'
        st.markdown(
            f"<div class='focus'><b>📍 {esc(r['Node'])}</b> — {esc(r['Status'])}{anchor_note}{critical_note}<br>"
            f"<span class='small'><b>Região/Bairro:</b> {regiao_txt} / {bairro_txt}</span><br>"
            f"<span class='small'>{esc(r['Portas_popup'])}</span><br>"
            f"<a class='route' href='{route}' target='_blank'>🧭 Traçar rota no Google Maps</a></div>",
            unsafe_allow_html=True,
        )

summary_1, summary_2, summary_3 = st.columns(3)

with summary_1:
    st.markdown("### ⚠️ Nodes com sem sinal")
    crit = status_df[status_df["Portas_OFF"] > 0].copy() if not status_df.empty else pd.DataFrame()
    if crit.empty:
        st.success("Nenhum node com porta OFF na leitura atual.")
    else:
        crit["_sev"] = crit["Status"].map(status_order)
        crit = crit.sort_values(["_sev", "Portas_OFF", "Node"], ascending=[False, False, True])
        crit_order = crit[["Node", "_sev", "Portas_OFF"]].copy()
        crit_view = logical[logical["Node"].isin(crit_order["Node"])].copy()
        crit_view = crit_view.merge(crit_order, on="Node", how="left", suffixes=("", "_ord"))
        crit_view = crit_view.sort_values(["_sev", "Portas_OFF_ord", "Node"], ascending=[False, False, True])
        st.dataframe(crit_view[["Node", "Regiao", "Bairro", "Status", "Portas_OFF"]], use_container_width=True, hide_index=True)

with summary_2:
    st.markdown("### 📊 Regiões mais impactadas")
    if region_impact.empty:
        st.info("Nenhuma região impactada na leitura atual.")
    else:
        st.dataframe(region_impact[["Regiao", "Nodes_impactados", "Portas_OFF"]], use_container_width=True, hide_index=True)

with summary_3:
    st.markdown("### 🏘️ Bairros mais impactados")
    if bairro_impact.empty:
        st.info("Nenhum bairro impactado na leitura atual.")
    else:
        st.dataframe(bairro_impact.head(10)[["Bairro", "Nodes_impactados", "Portas_OFF"]], use_container_width=True, hide_index=True)

with st.expander("🟡 Portas críticas 1–20", expanded=False):
    q = status_df[status_df["Portas_Criticas"] > 0].copy() if not status_df.empty else pd.DataFrame()
    if q.empty:
        st.info("Nenhuma porta crítica nesta coleta.")
    else:
        q = q.sort_values(["Portas_Criticas", "Node"], ascending=[False, True])
        q_order = q[["Node", "Portas_Criticas"]].copy().rename(columns={"Portas_Criticas":"_crit_order"})
        q_view = logical[logical["Node"].isin(q_order["Node"])].copy()
        q_view = q_view.merge(q_order, on="Node", how="left")
        q_view = q_view.sort_values(["_crit_order", "Node"], ascending=[False, True])
        st.dataframe(q_view[["Node", "Regiao", "Bairro", "Portas_Criticas", "Portas_popup"]], use_container_width=True, hide_index=True)


if mapdf.empty:
    st.warning("Nenhum node com coordenada disponível para o mapa.")
else:
    if not focus.empty:
        center_lat = float(focus.iloc[0]["Latitude_Map"])
        center_lon = float(focus.iloc[0]["Longitude_Map"])
        zoom = 13.5
    else:
        center_lat = float(mapdf["Latitude_Map"].mean())
        center_lon = float(mapdf["Longitude_Map"].mean())
        zoom = 11.3

    layers = [
        pdk.Layer(
            "ScatterplotLayer",
            data=mapdf,
            get_position="[Longitude_Map, Latitude_Map]",
            get_radius=55,
            radius_min_pixels=6,
            radius_max_pixels=15,
            get_fill_color="color",
            get_line_color=[255, 255, 255, 230],
            line_width_min_pixels=1.8,
            stroked=True,
            pickable=True,
            auto_highlight=True,
        )
    ]
    if not focus.empty:
        ring = focus.copy()
        ring["focus_line"] = [[[22, 119, 255, 255]][0]] * len(ring)
        layers.append(pdk.Layer(
            "ScatterplotLayer", data=ring, get_position="[Longitude_Map, Latitude_Map]",
            get_radius=95, radius_min_pixels=12, radius_max_pixels=22, filled=False, stroked=True,
            get_line_color="focus_line", line_width_min_pixels=4, pickable=False,
        ))

    tooltip = {
        "html": "<div style='font-size:13px;line-height:1.5;min-width:235px'>"
                "<div style='font-size:18px;font-weight:800;margin-bottom:5px'>{Node}</div>"
                "<div><b>Status:</b> {Status}</div>"
                "<div><b>Portas OFF:</b> {Portas_OFF}/{Total_portas}</div>"
                "<div><b>Portas críticas:</b> {Portas_Criticas}</div>"
                "<div><b>Região:</b> {Regiao}</div>"
                "<div><b>Bairro:</b> {Bairro}</div>"
                "<div style='border-top:1px solid rgba(255,255,255,.2);margin:7px 0 5px'></div>"
                "<div>{Portas_popup}</div>"
                "<div style='border-top:1px solid rgba(255,255,255,.2);margin:7px 0 5px'></div>"
                "<div><b>Ponto KML:</b> {Map_Node}</div>"
                "</div>",
        "style": {"backgroundColor": "rgba(9,26,51,.97)", "color": "white", "borderRadius": "10px", "padding": "10px 12px"},
    }
    deck = pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=zoom, pitch=0),
        tooltip=tooltip,
        map_style=LIGHT_MAP_STYLE,
    )
    st.pydeck_chart(deck, use_container_width=True, height=600)

pending = logical[logical["Latitude"].isna()].copy()
if not pending.empty:
    with st.expander(f"📍 Pendências de localização ({len(pending)})", expanded=False):
        st.caption("Esses nodes existem na coleta do XPERTrack, mas não têm ponto inequívoco no KML enviado. Foram mantidos fora do mapa até validação, sem estimar coordenadas.")
        st.dataframe(pending[["Node", "Precisao"]], use_container_width=True, hide_index=True)

st.caption("Regra principal do mapa: verde = Online; amarelo = SS Parcial; vermelho = SS Total. Porta crítica (pontuação 1–20) aparece nos detalhes e não cria uma quarta cor principal.")
