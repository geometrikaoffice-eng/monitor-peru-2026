#!/usr/bin/env python3
"""
Monitor Estadístico — Segunda Vuelta Perú 2026
Recalcula la proyección completa y escribe docs/resumen.json + docs/historial.json

Fuentes:
  1. ONPE oficial (brecha contabilizada)  → API directa con curl_cffi
  2. Datos mesa-por-mesa                  → repo oscarzamora/onpe-scraper-2026-2
"""
import json, io, sys, math, datetime, urllib.request
from pathlib import Path

import pandas as pd

RAW = "https://raw.githubusercontent.com/oscarzamora/onpe-scraper-2026-2/main/output/"
ONPE = "https://resultadosegundavuelta.onpe.gob.pe/presentacion-backend"
DOCS = Path(__file__).parent / "docs"
ID_FP, ID_JP = 8, 10          # Fuerza Popular / Juntos por el Perú
SUPERVIVENCIA_JEE = 0.85       # tasa histórica de actas observadas que se cuentan tras cotejo

# ---------------------------------------------------------------- utilidades
def fetch_tsv(name: str) -> pd.DataFrame:
    with urllib.request.urlopen(RAW + name, timeout=120) as r:
        return pd.read_csv(io.BytesIO(r.read()), sep="\t",
                           dtype={"codigo_mesa": str, "id_ubigeo": str, "ubigeo": str})

def scraper_last_update():
    """Fecha del último commit del scraper (para detectar si está congelado)."""
    try:
        url = "https://api.github.com/repos/oscarzamora/onpe-scraper-2026-2/commits?per_page=1"
        req = urllib.request.Request(url, headers={"User-Agent": "monitor-peru"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        return data[0]["commit"]["committer"]["date"]   # ISO UTC
    except Exception as e:
        print(f"[aviso] no se pudo leer fecha del scraper: {e}", file=sys.stderr)
        return None

def fetch_onpe_oficial():
    """Brecha oficial. Requiere curl_cffi (fingerprint Chrome). Devuelve None si falla."""
    try:
        from curl_cffi import requests as cr
        s = cr.Session(impersonate="chrome124")
        s.headers.update({"Referer": "https://resultadosegundavuelta.onpe.gob.pe/"})
        pid = s.get(f"{ONPE}/proceso/proceso-electoral-activo", timeout=30).json()["data"]["idEleccionPrincipal"]
        tot = s.get(f"{ONPE}/resumen-general/totales",
                    params={"idEleccion": pid, "tipoFiltro": "eleccion"}, timeout=30).json()["data"]
        cand = s.get(f"{ONPE}/eleccion-presidencial/participantes-ubicacion-geografica-nombre",
                     params={"idEleccion": pid, "tipoFiltro": "eleccion"}, timeout=30).json()["data"]
        fp = jp = None
        for c in cand:
            blob = json.dumps(c, ensure_ascii=False).upper()
            v = None
            for k in ("votos", "totalVotos", "cantidadVotos", "votosObtenidos"):
                if isinstance(c.get(k), (int, float)): v = int(c[k]); break
            if v is None: continue
            if "FUERZA POPULAR" in blob: fp = v
            elif "JUNTOS POR EL PER" in blob: jp = v
        avance = None
        for k in ("porcentajeActasContabilizadas", "avanceActas", "porcAvance"):
            if isinstance(tot.get(k), (int, float)): avance = float(tot[k]); break
        if fp and jp:
            return {"fp": fp, "jp": jp, "avance": avance, "fuente": "onpe-directo"}
    except Exception as e:
        print(f"[aviso] ONPE directo falló: {e}", file=sys.stderr)
    return None

def phi(z):  # CDF normal estándar
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))

# ---------------------------------------------------------------- pipeline
def main():
    mesas = fetch_tsv("mesas_data.txt")
    votos = fetch_tsv("votos.txt")
    ubi   = fetch_tsv("ubicaciones.txt")
    m = mesas.merge(ubi, left_on="id_ubigeo", right_on="ubigeo", how="left")
    pv = (votos.pivot_table(index="codigo_mesa", columns="partido_id",
                            values="votos", aggfunc="sum").fillna(0)
                .rename(columns={ID_FP: "FP", ID_JP: "JP"}))

    # ---- 1. Brecha oficial (ONPE directo, con respaldo en historial previo)
    of = fetch_onpe_oficial()
    hist_path = DOCS / "historial.json"
    historial = json.loads(hist_path.read_text()) if hist_path.exists() else []
    if of is None and historial:
        prev = historial[-1]
        of = {"fp": prev["fp_oficial"], "jp": prev["jp_oficial"],
              "avance": prev.get("avance"), "fuente": "cache-ultima-corrida"}
    if of is None:
        sys.exit("Sin brecha oficial disponible (ONPE caído y sin historial). Abortando sin cambios.")
    brecha_jp = of["jp"] - of["fp"]            # + = ventaja Sánchez

    # ---- 2. Extranjero pendiente: proyección país por país
    ext   = m[m["ambito"] == "exterior"]
    ext_c = ext[ext["codigo_estado_acta"] == "C"]
    ext_p = ext[ext["codigo_estado_acta"] == "P"]
    pvc = pv.loc[pv.index.isin(ext_c["codigo_mesa"])].join(
        ext_c.set_index("codigo_mesa")[["pais", "continente", "electores_habiles", "votos_validos"]])
    pais = pvc.groupby("pais").agg(n=("FP","count"), fp=("FP","sum"), jp=("JP","sum"),
                                   el=("electores_habiles","sum"), va=("votos_validos","sum"))
    cont = pvc.groupby("continente").agg(fp=("FP","sum"), jp=("JP","sum"),
                                         el=("electores_habiles","sum"), va=("votos_validos","sum"))
    g_fs = pvc["FP"].sum() / max(pvc["FP"].sum() + pvc["JP"].sum(), 1)
    g_vr = pvc["votos_validos"].sum() / max(pvc["electores_habiles"].sum(), 1)

    def tasa(p, c):
        if p in pais.index and pais.loc[p, "n"] >= 3 and (pais.loc[p,"fp"]+pais.loc[p,"jp"]) > 0:
            r = pais.loc[p];  return r["fp"]/(r["fp"]+r["jp"]), r["va"]/max(r["el"],1)
        if c in cont.index and (cont.loc[c,"fp"]+cont.loc[c,"jp"]) > 0:
            r = cont.loc[c];  return r["fp"]/(r["fp"]+r["jp"]), r["va"]/max(r["el"],1)
        return g_fs, g_vr

    ext_rows, ext_neto, ext_validos = [], 0.0, 0.0
    for (cnt, p), g in ext_p.groupby(["continente", "pais"]):
        fs, vr = tasa(p, cnt)
        val = g["electores_habiles"].sum() * vr
        net = val * (2*fs - 1)
        ext_neto += net; ext_validos += val
        ext_rows.append({"pais": p, "mesas": int(len(g)), "fp_share": round(fs, 4),
                         "neto_fp": int(net)})
    ext_rows.sort(key=lambda r: -abs(r["neto_fp"]))

    # ---- 3. Perú pendiente
    per_p = m[(m["ambito"]=="peru") & (m["codigo_estado_acta"]=="P")]
    per_c = m[(m["ambito"]=="peru") & (m["codigo_estado_acta"]=="C")]
    peru_neto, peru_validos = 0.0, 0.0
    for (pr, di), g in per_p.groupby(["provincia", "distrito"]):
        same = per_c[(per_c["distrito"]==di) & (per_c["provincia"]==pr)]
        if len(same) < 3: same = per_c[per_c["provincia"]==pr]
        s = pv.loc[pv.index.isin(same["codigo_mesa"])]
        if s["FP"].sum()+s["JP"].sum() == 0: continue
        fs = s["FP"].sum()/(s["FP"].sum()+s["JP"].sum())
        vr = same["votos_validos"].sum()/max(same["electores_habiles"].sum(),1)
        val = g["electores_habiles"].sum()*vr
        peru_neto += val*(2*fs-1); peru_validos += val

    # ---- 4. Actas en el JEE
    jee_codes = m[m["codigo_estado_acta"]=="E"]["codigo_mesa"]
    pj = pv.loc[pv.index.isin(jee_codes)]
    jee_neto = float(pj["FP"].sum() - pj["JP"].sum())
    jee_fp_actas = int((pj["FP"] > pj["JP"]).sum())
    jee_jp_actas = int((pj["JP"] > pj["FP"]).sum())

    # ---- 5. Modelo de probabilidad
    margen = -brecha_jp + ext_neto + peru_neto + SUPERVIVENCIA_JEE * jee_neto   # + = FP
    sigma = math.sqrt((0.08*(ext_validos+peru_validos))**2     # error de proyección pendiente
                      + (0.25*abs(jee_neto))**2                 # incertidumbre resolución JEE
                      + 20000**2)                               # riesgo de modelo / sesgos sistemáticos
    p_fp = phi(margen / sigma)

    ahora = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    scraper_fecha = scraper_last_update()
    resumen = {
        "actualizado_utc": ahora,
        "fuente_brecha": of["fuente"],
        "scraper_ultima_actualizacion": scraper_fecha,
        "avance_actas_pct": of.get("avance"),
        "fp_oficial": int(of["fp"]), "jp_oficial": int(of["jp"]),
        "brecha_oficial": int(brecha_jp),
        "ext_pendiente": {"mesas": int(len(ext_p)), "validos_est": int(ext_validos),
                          "neto_fp": int(ext_neto), "paises_top": ext_rows[:8]},
        "peru_pendiente": {"mesas": int(len(per_p)), "validos_est": int(peru_validos),
                           "neto_fp": int(peru_neto)},
        "jee": {"actas": int(len(pj)), "fp_actas": jee_fp_actas, "jp_actas": jee_jp_actas,
                "neto_fp": int(jee_neto), "supervivencia_asumida": SUPERVIVENCIA_JEE},
        "margen_proyectado_fp": int(margen),
        "sigma": int(sigma),
        "prob_fujimori": round(p_fp, 4),
        "prob_sanchez": round(1 - p_fp, 4),
    }
    DOCS.mkdir(exist_ok=True)
    (DOCS / "resumen.json").write_text(json.dumps(resumen, ensure_ascii=False, indent=1))

    historial.append({"t": ahora, "prob_fp": round(p_fp, 4),
                      "brecha": int(brecha_jp), "avance": of.get("avance"),
                      "fp_oficial": int(of["fp"]), "jp_oficial": int(of["jp"])})
    hist_path.write_text(json.dumps(historial[-500:], ensure_ascii=False))
    print(f"OK · P(Fujimori)={p_fp:.1%} · margen proyectado FP {margen:+,.0f} ± {sigma:,.0f}")

if __name__ == "__main__":
    main()
