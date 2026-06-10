# -*- coding: utf-8 -*-
"""
Monitor Electoral Perú 2026 — Segunda Vuelta
Robot de análisis: descarga datos del scraper de oscarzamora,
consulta la ONPE directamente para la cifra oficial,
proyecta las mesas pendientes por geografía y genera resumen.json

Corre automáticamente vía GitHub Actions cada 30 minutos.
"""
import json
import io
import sys
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests as std_requests

RAW = "https://raw.githubusercontent.com/oscarzamora/onpe-scraper-2026-2/main/output/"
ONPE_BASE = "https://resultadosegundavuelta.onpe.gob.pe"

KEIKO = "FUERZA POPULAR"
SANCHEZ = "JUNTOS POR EL PERÚ"

LIMA_TZ = timezone(timedelta(hours=-5))


def descargar(nombre):
    """Descarga un archivo TSV del repo del scraper."""
    r = std_requests.get(RAW + nombre, timeout=60)
    r.raise_for_status()
    return pd.read_csv(io.StringIO(r.text), sep="\t", dtype=str)


def consultar_onpe_oficial():
    """Consulta directa a la API de ONPE (requiere fingerprinting de Chrome).
    Devuelve dict con totales oficiales o None si falla."""
    try:
        from curl_cffi import requests as cf
        # 1. Proceso activo
        r = cf.get(f"{ONPE_BASE}/presentacion-backend/proceso/proceso-electoral-activo",
                   impersonate="chrome124", timeout=30)
        id_eleccion = r.json()["data"]["idEleccion"]

        # 2. Candidatos — totales nacionales
        r = cf.get(f"{ONPE_BASE}/presentacion-backend/candidatos/filtro-tipo-eleccion",
                   params={"idEleccion": id_eleccion, "tipoFiltro": "eleccion"},
                   impersonate="chrome124", timeout=30)
        cand = r.json().get("data")

        # 3. Totales — actas / participación
        r = cf.get(f"{ONPE_BASE}/presentacion-backend/totales/filtro-tipo-eleccion",
                   params={"idEleccion": id_eleccion, "tipoFiltro": "eleccion"},
                   impersonate="chrome124", timeout=30)
        tot = r.json().get("data")
        return {"candidatos": cand, "totales": tot, "id_eleccion": id_eleccion}
    except Exception as e:
        print(f"[AVISO] Consulta directa a ONPE falló ({e}). Continuamos con datos del scraper.",
              file=sys.stderr)
        return None


def main():
    print("Descargando datos del scraper...")
    mesas = descargar("mesas_data.txt")
    votos = descargar("votos.txt")
    agrup = descargar("agrupaciones.txt")
    ub = descargar("ubicaciones.txt")

    # Tipos numéricos
    for c in ["electores_habiles", "votos_emitidos", "votos_validos", "total_asistentes"]:
        mesas[c] = pd.to_numeric(mesas[c], errors="coerce").fillna(0)
    mesas["participacion_ciudadana"] = pd.to_numeric(mesas["participacion_ciudadana"], errors="coerce")
    votos["votos"] = pd.to_numeric(votos["votos"], errors="coerce").fillna(0)
    votos["partido_id"] = votos["partido_id"].astype(str)
    agrup["partido_id"] = agrup["partido_id"].astype(str)

    id_k = agrup.loc[agrup["nombre"] == KEIKO, "partido_id"].iloc[0]
    id_s = agrup.loc[agrup["nombre"] == SANCHEZ, "partido_id"].iloc[0]

    # ── Conteo del scraper (mesa por mesa) ────────────────────────────────
    contab = mesas[mesas["codigo_estado_acta"] == "C"]
    pend = mesas[mesas["codigo_estado_acta"] != "C"]

    v = votos.merge(contab[["codigo_mesa", "id_ubigeo", "id_ambito_geografico"]],
                    on="codigo_mesa", how="inner")
    tot_k = int(v.loc[v["partido_id"] == id_k, "votos"].sum())
    tot_s = int(v.loc[v["partido_id"] == id_s, "votos"].sum())
    validos = tot_k + tot_s

    # ── Proyección de mesas pendientes por geografía ──────────────────────
    # Para cada mesa pendiente: estimamos votos usando el patrón de las mesas
    # YA contabilizadas de su mismo ubigeo (distrito/ciudad). Si el ubigeo no
    # tiene mesas contabilizadas, usamos el nivel departamento/país, y si no,
    # el promedio de su ámbito (Perú / Exterior).
    piv = v.pivot_table(index="codigo_mesa", columns="partido_id",
                        values="votos", aggfunc="sum", fill_value=0).reset_index()
    piv = piv.rename(columns={id_k: "vk", id_s: "vs"})
    if "vk" not in piv: piv["vk"] = 0
    if "vs" not in piv: piv["vs"] = 0
    piv = piv.merge(contab[["codigo_mesa", "id_ubigeo", "id_ambito_geografico",
                            "electores_habiles"]], on="codigo_mesa")

    # tasas por ubigeo: (votos partido / electores hábiles) — captura participación y preferencia
    def tasas(df, keys):
        g = df.groupby(keys).agg(vk=("vk", "sum"), vs=("vs", "sum"),
                                 eh=("electores_habiles", "sum")).reset_index()
        g["rk"] = g["vk"] / g["eh"].clip(lower=1)
        g["rs"] = g["vs"] / g["eh"].clip(lower=1)
        return g

    t_ubigeo = tasas(piv, ["id_ubigeo"])
    # nivel superior: prefijo de departamento (2 primeros dígitos del ubigeo)
    piv["dep"] = piv["id_ubigeo"].str[:2]
    t_dep = tasas(piv, ["dep"])
    t_amb = tasas(piv, ["id_ambito_geografico"])

    p = pend[["codigo_mesa", "id_ubigeo", "id_ambito_geografico",
              "electores_habiles", "codigo_estado_acta"]].copy()
    p["dep"] = p["id_ubigeo"].str[:2]
    p = p.merge(t_ubigeo[["id_ubigeo", "rk", "rs"]], on="id_ubigeo", how="left")
    p = p.merge(t_dep[["dep", "rk", "rs"]], on="dep", how="left", suffixes=("", "_dep"))
    p = p.merge(t_amb[["id_ambito_geografico", "rk", "rs"]],
                on="id_ambito_geografico", how="left", suffixes=("", "_amb"))
    p["rk"] = p["rk"].fillna(p["rk_dep"]).fillna(p["rk_amb"]).fillna(0)
    p["rs"] = p["rs"].fillna(p["rs_dep"]).fillna(p["rs_amb"]).fillna(0)
    p["proj_k"] = p["rk"] * p["electores_habiles"]
    p["proj_s"] = p["rs"] * p["electores_habiles"]

    proj_k = float(p["proj_k"].sum())
    proj_s = float(p["proj_s"].sum())

    pend_peru = p[p["id_ambito_geografico"] == "1"]
    pend_ext = p[p["id_ambito_geografico"] == "2"]
    jee = pend[pend["codigo_estado_acta"] == "E"]

    # ── Cifra oficial ONPE (directa) ──────────────────────────────────────
    oficial = consultar_onpe_oficial()
    oficial_resumen = None
    if oficial and oficial.get("candidatos"):
        try:
            filas = []
            for c in oficial["candidatos"]:
                filas.append({
                    "agrupacion": c.get("nombreAgrupacionPolitica") or c.get("nombre"),
                    "votos": c.get("totalVotosValidos") or c.get("votos"),
                    "pct": c.get("porcentajeVotosValidos") or c.get("porcentaje"),
                })
            t = oficial.get("totales") or {}
            oficial_resumen = {
                "candidatos": filas,
                "actas_contabilizadas_pct": t.get("actasContabilizadas"),
                "participacion": t.get("participacionCiudadana"),
            }
        except Exception as e:
            print(f"[AVISO] Formato inesperado de ONPE: {e}", file=sys.stderr)

    # ── Desglose exterior por continente ──────────────────────────────────
    ub_ext = ub[ub["ambito"] == "exterior"][["ubigeo", "continente", "pais"]]
    pe = pend_ext.merge(ub_ext, left_on="id_ubigeo", right_on="ubigeo", how="left")
    ext_por_continente = (pe.groupby("continente")
                          .agg(mesas=("codigo_mesa", "count"),
                               electores=("electores_habiles", "sum"),
                               proj_k=("proj_k", "sum"),
                               proj_s=("proj_s", "sum"))
                          .reset_index().to_dict(orient="records"))

    total_proj_k = tot_k + proj_k
    total_proj_s = tot_s + proj_s
    tp = total_proj_k + total_proj_s

    resumen = {
        "actualizado": datetime.now(LIMA_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "actualizado_utc": datetime.now(timezone.utc).isoformat(),
        "fuente_scraper": "oscarzamora/onpe-scraper-2026-2",
        "escrutinio": {
            "mesas_total_dataset": int(len(mesas)),
            "mesas_contabilizadas": int(len(contab)),
            "mesas_pendientes": int(len(pend)),
            "pendientes_peru": int(len(pend_peru)),
            "pendientes_exterior": int(len(pend_ext)),
            "camino_jee": int(len(jee)),
            "electores_en_pendientes": int(pend["electores_habiles"].sum()),
        },
        "conteo": {
            "keiko": {"nombre": KEIKO, "votos": tot_k,
                      "pct": round(100 * tot_k / max(validos, 1), 2)},
            "sanchez": {"nombre": SANCHEZ, "votos": tot_s,
                        "pct": round(100 * tot_s / max(validos, 1), 2)},
            "diferencia": abs(tot_k - tot_s),
            "lidera": KEIKO if tot_k > tot_s else SANCHEZ,
        },
        "proyeccion": {
            "pendientes_keiko": round(proj_k),
            "pendientes_sanchez": round(proj_s),
            "final_keiko": {"votos": round(total_proj_k),
                            "pct": round(100 * total_proj_k / max(tp, 1), 2)},
            "final_sanchez": {"votos": round(total_proj_s),
                              "pct": round(100 * total_proj_s / max(tp, 1), 2)},
            "diferencia_proyectada": round(abs(total_proj_k - total_proj_s)),
            "ganador_proyectado": KEIKO if total_proj_k > total_proj_s else SANCHEZ,
            "metodo": "Tasa votos/electores de mesas contabilizadas del mismo "
                      "ubigeo (fallback: departamento → ámbito), aplicada a "
                      "electores hábiles de mesas pendientes.",
        },
        "exterior_pendiente_por_continente": ext_por_continente,
        "onpe_oficial": oficial_resumen,
    }

    with open("resumen.json", "w", encoding="utf-8") as f:
        json.dump(resumen, f, ensure_ascii=False, indent=1)

    print(json.dumps(resumen["conteo"], ensure_ascii=False, indent=2))
    print(json.dumps(resumen["proyeccion"], ensure_ascii=False, indent=2))
    print("OK → resumen.json generado")


if __name__ == "__main__":
    main()
