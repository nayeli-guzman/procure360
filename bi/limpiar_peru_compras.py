"""
Procure360 BI · Limpieza de órdenes de compra de Perú Compras (OCDS, OCP Data Registry)

Uso:
    python3 limpiar_peru_compras.py RUTA_A_LA_CARPETA_CON_LOS_CSV

Genera, en la misma carpeta donde ejecutes el script:
    procure360_bi.xlsx -> las tres tablas en un solo Excel (esto es lo que subes a Power BI)
    ordenes.csv        -> una fila por orden de compra
    proveedores.csv    -> una fila por proveedor, con ranking, % acumulado y clase ABC
    entidades.csv      -> una fila por entidad compradora, con su gasto total

Si algo falla, el script imprime las columnas que encontró en cada archivo.
Copia ese texto y envíaselo a Claude.
"""

from __future__ import annotations  # para que funcione también con Python 3.9 (el que trae macOS)

import sys
from pathlib import Path

import pandas as pd


def leer(carpeta: Path, nombre: str, obligatorio: bool = True) -> pd.DataFrame | None:
    ruta = carpeta / f"{nombre}.csv"
    if not ruta.exists():
        if obligatorio:
            sys.exit(f"[ERROR] No encontré {ruta.name} en {carpeta}. Revisa que descomprimiste bien el archivo.")
        return None
    df = pd.read_csv(ruta, dtype=str, low_memory=False)
    print(f"  {ruta.name}: {len(df):,} filas · columnas: {', '.join(df.columns)}")
    return df


def primera_columna(df: pd.DataFrame, candidatas: list[str]) -> str | None:
    for c in candidatas:
        if c in df.columns and df[c].notna().any():
            return c
    return None


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Uso: python3 limpiar_peru_compras.py RUTA_A_LA_CARPETA_CON_LOS_CSV")
    carpeta = Path(sys.argv[1]).expanduser()
    print(f"Leyendo archivos de {carpeta} …")

    main_df = leer(carpeta, "main")
    contracts = leer(carpeta, "contracts", obligatorio=False)
    awards = leer(carpeta, "awards", obligatorio=False)
    suppliers = leer(carpeta, "awards_suppliers", obligatorio=False)
    parties = leer(carpeta, "parties", obligatorio=False)

    # ---------- Base: una fila por orden (ocid) ----------
    base = pd.DataFrame({"ocid": main_df["ocid"]}).drop_duplicates()

    # Descripción y catálogo / acuerdo marco (lo que exista en main)
    col_desc = primera_columna(main_df, ["tender_title", "tender_description", "title"])
    col_cat = primera_columna(main_df, [
        "tender_procurementMethodDetails", "tender_mainProcurementCategory",
        "tender_additionalProcurementCategories", "tender_procurementMethod",
    ])
    extra = main_df[["ocid"] + [c for c in [col_desc, col_cat] if c]].drop_duplicates("ocid")
    extra = extra.rename(columns={col_desc: "descripcion"} if col_desc else {})
    if col_cat:
        extra = extra.rename(columns={col_cat: "catalogo"})
    base = base.merge(extra, on="ocid", how="left")

    # Si main no trae catálogo, usamos el título de la adjudicación (awards.title)
    if "catalogo" not in base.columns and awards is not None and "title" in awards.columns:
        tit = awards[["main_ocid", "title"]].dropna().drop_duplicates("main_ocid")
        base = base.merge(tit.rename(columns={"main_ocid": "ocid", "title": "catalogo"}), on="ocid", how="left")
        print("  (catálogo tomado de awards.title)")

    # Muestra ejemplos de las columnas de texto, para revisar que el catálogo tenga sentido
    print("\nEjemplos de columnas descriptivas:")
    for c in [c for c in main_df.columns if any(k in c.lower() for k in ["title", "description", "category", "method", "details"])]:
        print(f"  main.{c}: {main_df[c].dropna().head(3).tolist()}")
    if awards is not None and "title" in awards.columns:
        print(f"  awards.title: {awards['title'].dropna().head(3).tolist()}")

    # Entidad compradora: buyer_name en main, o la parte con rol "buyer" en parties
    col_buyer = primera_columna(main_df, ["buyer_name"])
    if col_buyer:
        base = base.merge(main_df[["ocid", col_buyer]].drop_duplicates("ocid").rename(columns={col_buyer: "entidad"}),
                          on="ocid", how="left")
    elif parties is not None and "roles" in parties.columns:
        compradores = parties[parties["roles"].fillna("").str.contains("buyer", case=False)]
        compradores = compradores[["main_ocid", "name"]].drop_duplicates("main_ocid")
        base = base.merge(compradores.rename(columns={"main_ocid": "ocid", "name": "entidad"}), on="ocid", how="left")
    else:
        base["entidad"] = None

    # Proveedor: primer proveedor adjudicado de cada orden
    if suppliers is not None:
        col_ruc = primera_columna(suppliers, ["id", "identifier_id", "identifier"])
        cols = ["main_ocid", "name"] + ([col_ruc] if col_ruc else [])
        prov = suppliers[cols].drop_duplicates("main_ocid")
        prov = prov.rename(columns={"main_ocid": "ocid", "name": "proveedor"})
        if col_ruc:
            prov = prov.rename(columns={col_ruc: "ruc_proveedor"})
        base = base.merge(prov, on="ocid", how="left")
    else:
        base["proveedor"] = None

    # Monto, moneda, fecha y estado: preferimos contracts; si no hay, awards
    origen = contracts if contracts is not None and "value_amount" in contracts.columns else awards
    if origen is None:
        sys.exit("[ERROR] No encontré montos ni en contracts.csv ni en awards.csv.")
    col_fecha = primera_columna(origen, ["dateSigned", "date", "period_startDate"])
    col_moneda = primera_columna(origen, ["value_currency"])
    col_estado = primera_columna(origen, ["status"])
    o = origen[["main_ocid", "value_amount"] + [c for c in [col_fecha, col_moneda, col_estado] if c]].copy()
    o["value_amount"] = pd.to_numeric(o["value_amount"], errors="coerce")
    agg = {"value_amount": "sum"}
    for c in [col_fecha, col_moneda, col_estado]:
        if c:
            agg[c] = "first"
    o = o.groupby("main_ocid", as_index=False).agg(agg)
    o = o.rename(columns={"main_ocid": "ocid", "value_amount": "monto"})
    if col_fecha:
        o = o.rename(columns={col_fecha: "fecha"})
    if col_moneda:
        o = o.rename(columns={col_moneda: "moneda"})
    if col_estado:
        o = o.rename(columns={col_estado: "estado"})
    base = base.merge(o, on="ocid", how="inner")

    # Fecha de respaldo desde main si hace falta
    if "fecha" not in base.columns or base["fecha"].isna().all():
        if "date" in main_df.columns:
            base = base.drop(columns=["fecha"], errors="ignore").merge(
                main_df[["ocid", "date"]].drop_duplicates("ocid").rename(columns={"date": "fecha"}), on="ocid", how="left")

    # ---------- Limpieza ----------
    antes = len(base)
    base["fecha"] = pd.to_datetime(base["fecha"], errors="coerce", utc=True).dt.tz_convert(None).dt.date
    base = base[base["monto"].notna() & (base["monto"] > 0) & base["fecha"].notna()]
    if "moneda" in base.columns:
        base = base[base["moneda"].fillna("PEN").str.upper() == "PEN"]
    for c in ["entidad", "proveedor", "catalogo", "descripcion"]:
        if c in base.columns:
            base[c] = base[c].fillna("(sin dato)").astype(str).str.strip().str.upper()
    base["fecha"] = pd.to_datetime(base["fecha"])
    base["anio"] = base["fecha"].dt.year
    base["mes"] = base["fecha"].dt.to_period("M").astype(str)  # p. ej. 2023-04
    print(f"\nÓrdenes válidas: {len(base):,} de {antes:,} (se quitaron montos vacíos o en cero, fechas vacías y otras monedas)")

    # ---------- Clasificación ABC de proveedores (Pareto) ----------
    prov = (base.groupby("proveedor", as_index=False)
                .agg(monto_total=("monto", "sum"), n_ordenes=("ocid", "count"), n_entidades=("entidad", "nunique"))
                .sort_values("monto_total", ascending=False))
    prov["ranking"] = range(1, len(prov) + 1)
    prov["pct_gasto"] = prov["monto_total"] / prov["monto_total"].sum()
    prov["pct_acumulado"] = prov["pct_gasto"].cumsum()
    prov["clase_abc"] = pd.cut(prov["pct_acumulado"], bins=[-1, 0.80, 0.95, 1.01], labels=["A", "B", "C"]).astype(str)
    # El proveedor que cruza el 80 % también cuenta como A
    base = base.merge(prov[["proveedor", "clase_abc", "ranking"]], on="proveedor", how="left")

    ent = (base.groupby("entidad", as_index=False)
               .agg(monto_total=("monto", "sum"), n_ordenes=("ocid", "count"), n_proveedores=("proveedor", "nunique"))
               .sort_values("monto_total", ascending=False))

    # ---------- Exportar ----------
    columnas = [c for c in ["ocid", "fecha", "anio", "mes", "entidad", "proveedor", "ruc_proveedor", "catalogo",
                            "descripcion", "monto", "estado", "clase_abc", "ranking"] if c in base.columns]
    base[columnas].to_csv("ordenes.csv", index=False, encoding="utf-8-sig")
    prov.to_csv("proveedores.csv", index=False, encoding="utf-8-sig")
    ent.to_csv("entidades.csv", index=False, encoding="utf-8-sig")

    # Un solo Excel con las tres tablas (una hoja cada una): es lo que subes a Power BI
    excel_ok = False
    try:
        with pd.ExcelWriter("procure360_bi.xlsx", engine="openpyxl") as xl:
            base[columnas].to_excel(xl, sheet_name="ordenes", index=False)
            prov.to_excel(xl, sheet_name="proveedores", index=False)
            ent.to_excel(xl, sheet_name="entidades", index=False)
        excel_ok = True
        print("Se creó procure360_bi.xlsx (hojas: ordenes, proveedores, entidades)")
    except ModuleNotFoundError:
        print("[AVISO] Falta openpyxl para crear el Excel. Instálalo con: python3 -m pip install openpyxl")

    # ---------- Resumen para tus hallazgos ----------
    total = base["monto"].sum()
    n_a = (prov["clase_abc"] == "A").sum()
    print("\n===== RESUMEN (cópialo, te sirve para el paso 6) =====")
    print(f"Periodo: {base['fecha'].min():%Y-%m-%d} a {base['fecha'].max():%Y-%m-%d}")
    print(f"Gasto total: S/ {total:,.0f}")
    print(f"Órdenes: {len(base):,} · Proveedores: {len(prov):,} · Entidades: {len(ent):,}")
    print(f"Ticket promedio por orden: S/ {total / len(base):,.0f}")
    print(f"Proveedores clase A (80 % del gasto): {n_a:,} de {len(prov):,} ({n_a / len(prov):.1%})")
    print(f"Top 10 proveedores concentran: {prov.head(10)['pct_gasto'].sum():.1%} del gasto")
    if "catalogo" in base.columns:
        top_cat = base.groupby("catalogo")["monto"].sum().sort_values(ascending=False).head(5)
        print("Top 5 catálogos / categorías por gasto:")
        for k, v in top_cat.items():
            print(f"   - {k[:70]}: S/ {v:,.0f} ({v / total:.1%})")
    mes_pico = base.groupby("mes")["monto"].sum().idxmax()
    print(f"Mes con más gasto: {mes_pico}")
    if excel_ok:
        print("\nListo: se crearon procure360_bi.xlsx, ordenes.csv, proveedores.csv y entidades.csv")
    else:
        print("\nSe crearon ordenes.csv, proveedores.csv y entidades.csv, pero FALTA el Excel: instala openpyxl y vuelve a correr el script.")


if __name__ == "__main__":
    main()