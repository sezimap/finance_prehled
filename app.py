
import io
import json
import re
import sys
from datetime import datetime

import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt

st.set_page_config(page_title="Finance přehled", layout="wide")

st.title("📒 Osobní finance – CSV ➜ přehledy")
st.caption("Nahraj výpis z účtu, roztřiď platby do kategorií a zobraz si přehledy. UI v češtině, žádná instalace databáze.")

# ---------- Pomocné funkce ----------

def try_read_csv(file, sep_guess=";", encodings=("utf-8", "windows-1250", "cp1250", "iso-8859-2")):
    last_err = None
    for enc in encodings:
        try:
            return pd.read_csv(file, encoding=enc, sep=sep_guess, quotechar='"')
        except Exception as e:
            last_err = e
            continue
    raise last_err

def to_float(x):
    if pd.isna(x):
        return None
    s = str(x).strip()
    # odstranit mezery tisícovek, nahradit čárku za tečku
    s = s.replace(" ", "").replace("\xa0", "").replace(",", ".")
    try:
        return float(s)
    except:
        return None

def parse_date(s):
    if pd.isna(s):
        return None
    # podpora formátů typu 31.10.2025, 2025-10-31 apod.
    s = str(s).strip()
    for dayfirst in (True, False):
        try:
            return pd.to_datetime(s, dayfirst=dayfirst, errors="raise")
        except:
            pass
    return pd.NaT

def ensure_columns(df, mapping):
    # vytvoř standardizované názvy
    out = pd.DataFrame(index=df.index)
    out["datum"] = df[mapping["date"]].map(parse_date)
    out["castka"] = df[mapping["amount"]].map(to_float)
    out["popis"] = df[mapping["desc"]].astype(str)
    if mapping.get("category"):
        out["kategorie"] = df[mapping["category"]].astype(str)
    else:
        out["kategorie"] = "Nezařazeno"
    # Pokud máme směr (příjem/výdaj), pokusíme se nastavit znaménko
    if mapping.get("direction"):
        dir_col = df[mapping["direction"]].astype(str).str.lower()
        mask_income = dir_col.str.contains("příchozí|prichozi|credit|incoming")
        mask_exp = dir_col.str.contains("odchozí|odchozi|debit|outgoing")
        out.loc[mask_income & out["castka"].notna(), "castka"] = out.loc[mask_income & out["castka"].notna(), "castka"].abs()
        out.loc[mask_exp & out["castka"].notna(), "castka"] = -out.loc[mask_exp & out["castka"].notna(), "castka"].abs()

    return out

def apply_rules(df, rules):
    # rules = list of dicts: {"name": "Potraviny", "keywords": ["albert","lidl"], "regex": ""}
    if not rules:
        return df
    cats = df["kategorie"].copy()
    text = (df["popis"].astype(str)).str.lower()
    for rule in rules:
        cat = rule.get("name", "Nezařazeno")
        # match keywords
        kws = [k.strip().lower() for k in rule.get("keywords", []) if k.strip()]
        rx = rule.get("regex", "").strip()
        mask = pd.Series(False, index=df.index)
        if kws:
            for k in kws:
                mask = mask | text.str.contains(re.escape(k), na=False)
        if rx:
            try:
                mask = mask | text.str.contains(rx, regex=True, na=False, flags=re.IGNORECASE)
            except:
                pass
        cats = cats.mask(mask, cat)
    df = df.copy()
    df["kategorie"] = cats
    return df

def monthly_summary(df):
    dd = df.copy()
    dd["mesic"] = dd["datum"].dt.to_period("M").dt.to_timestamp()
    agg = dd.groupby("mesic")["castka"].sum().sort_index()
    inc = dd[dd["castka"] > 0].groupby("mesic")["castka"].sum()
    exp = -dd[dd["castka"] < 0].groupby("mesic")["castka"].sum()
    res = pd.concat([inc.rename("Příjmy"), exp.rename("Výdaje"), agg.rename("Saldo")], axis=1).fillna(0.0)
    return res

def category_summary(df):
    exp = df[df["castka"] < 0]
    return (-exp.groupby("kategorie")["castka"].sum().sort_values(ascending=False)).rename("Výdaje CZK")

# ---------- Sidebar: nahrání a mapování ----------

st.sidebar.header("1) Nahrát CSV")
file = st.sidebar.file_uploader("Vyber CSV výpis", type=["csv"])

sep = st.sidebar.selectbox("Oddělovač sloupců", [";", ",", "\\t (tab)"], index=0)
sep_val = {";": ";", ",": ",", "\\t (tab)": "\t"}[sep]

if file:
    try:
        df_raw = try_read_csv(file, sep_guess=sep_val)
    except Exception as e:
        st.error(f"Soubor se nepodařilo načíst: {e}")
        st.stop()

    st.sidebar.header("2) Namapuj sloupce")
    cols = list(df_raw.columns)

    def pick(label, default_candidates):
        for cand in default_candidates:
            for c in cols:
                if cand.lower() in c.lower():
                    return c
        return cols[0] if cols else None

    date_col = st.sidebar.selectbox("Sloupec s datem", options=cols, index=cols.index(pick("date", ["datum", "date", "zaúčtová", "provedení"])) if cols else 0)
    amount_col = st.sidebar.selectbox("Sloupec s částkou", options=cols, index=cols.index(pick("amount", ["částka", "castka", "amount"])) if cols else 0)
    desc_col = st.sidebar.selectbox("Sloupec s popisem", options=cols, index=cols.index(pick("desc", ["popis", "název", "obchodní místo", "description"])) if cols else 0)
    direction_col = st.sidebar.selectbox("Sloupec se směrem (příchozí/odchozí) – volitelné", options=["(žádný)"] + cols, index=0)
    category_col = st.sidebar.selectbox("Sloupec s kategorií – volitelné", options=["(žádný)"] + cols, index=0)

    mapping = {
        "date": date_col,
        "amount": amount_col,
        "desc": desc_col,
        "direction": None if direction_col == "(žádný)" else direction_col,
        "category": None if category_col == "(žádný)" else category_col,
    }

    data = ensure_columns(df_raw, mapping)

    # ---------- Pravidla kategorií ----------
    st.sidebar.header("3) Pravidla kategorií (automatické)")
    if "category_rules" not in st.session_state:
        st.session_state.category_rules = []

    with st.sidebar.expander("Pravidla (klíčová slova / regex)", expanded=False):
        st.write("Každé pravidlo: název kategorie + klíčová slova (oddělená čárkou). Volitelně regulární výraz.")
        cat_name = st.text_input("Název kategorie", value="Potraviny")
        keywords = st.text_input("Klíčová slova (např. albert, lidl)")
        regex = st.text_input("Regulární výraz (volitelné)")
        cols_btn = st.columns(2)
        with cols_btn[0]:
            if st.button("➕ Přidat pravidlo"):
                st.session_state.category_rules.append({
                    "name": cat_name.strip() or "Nezařazeno",
                    "keywords": [k.strip() for k in keywords.split(",")] if keywords.strip() else [],
                    "regex": regex.strip(),
                })
        with cols_btn[1]:
            if st.button("🧹 Vymazat všechna pravidla"):
                st.session_state.category_rules = []

        if st.session_state.category_rules:
            st.json(st.session_state.category_rules)

    data = apply_rules(data, st.session_state.category_rules)

    # ---------- Hlavní část: tabulka a editace ----------
    st.subheader("📄 Transakce")
    st.caption("Tip: Můžeš upravovat sloupec *kategorie* ručně. Změny se projeví v grafech níže.")
    data_edit = st.data_editor(
        data,
        column_config={
            "datum": st.column_config.DateColumn("Datum", format="DD.MM.YYYY"),
            "castka": st.column_config.NumberColumn("Částka (CZK)", step=1.0),
            "kategorie": st.column_config.TextColumn("Kategorie"),
            "popis": st.column_config.TextColumn("Popis"),
        },
        disabled=["datum", "castka", "popis"],
        hide_index=True,
        use_container_width=True,
        num_rows="dynamic",
    )

    # ---------- Přehledy ----------
    col1, col2, col3 = st.columns(3)
    total_income = data_edit.loc[data_edit["castka"] > 0, "castka"].sum()
    total_exp = -data_edit.loc[data_edit["castka"] < 0, "castka"].sum()
    balance = total_income - total_exp
    col1.metric("Příjmy (CZK)", f"{total_income:,.0f}".replace(",", " "))
    col2.metric("Výdaje (CZK)", f"{total_exp:,.0f}".replace(",", " "))
    col3.metric("Saldo (CZK)", f"{balance:,.0f}".replace(",", " "))

    st.subheader("📊 Grafy")

    # a) Koláč výdajů podle kategorií
    cats = category_summary(data_edit)
    if len(cats) > 0 and cats.sum() > 0:
        fig1 = plt.figure(figsize=(6, 6))
        plt.pie(cats.values, labels=cats.index, autopct="%1.1f%%", startangle=90)
        plt.title("Podíl výdajů podle kategorií")
        st.pyplot(fig1)
    else:
        st.info("Žádná data pro výdaje.")

    # b) Trend po měsících
    m = monthly_summary(data_edit)
    if not m.empty:
        fig2 = plt.figure(figsize=(8, 4))
        plt.plot(m.index, m["Příjmy"], marker="o", label="Příjmy")
        plt.plot(m.index, m["Výdaje"], marker="o", label="Výdaje")
        plt.plot(m.index, m["Saldo"], marker="o", label="Saldo")
        plt.title("Vývoj po měsících")
        plt.legend()
        plt.xticks(rotation=30)
        st.pyplot(fig2)
    else:
        st.info("Žádná data pro časový přehled.")

    # ---------- Export ----------
    st.subheader("⬇️ Export upravených dat a pravidel")
    buf = io.StringIO()
    data_edit.to_csv(buf, index=False)
    st.download_button("Stáhnout CSV (upravené)", data=buf.getvalue(), file_name="vypis_upraveny.csv", mime="text/csv")

    rules_json = json.dumps(st.session_state.category_rules, ensure_ascii=False, indent=2)
    st.download_button("Stáhnout pravidla (JSON)", data=rules_json, file_name="pravidla_kategorii.json", mime="application/json")

    st.caption("Pozn.: Pravidla se ukládají pouze v této relaci, proto si je stáhni jako JSON a příště je můžeš načíst (tato verze zatím pouze exportuje).")

else:
    st.info("⬅️ Nahraj prosím CSV výpis (soubor .csv) a v postranním panelu nastav mapování sloupců.")
