"""
Etapa 1 — coleta de dados de tabuademares.com/br/paraiba/joao-pessoa.

Objetivo: gravar em data/raw/ os CSVs que as Etapas 2 e 3 vão consumir.

    mares_<ano>.csv      tábua de marés do ano (4 marés por dia)
    mares_previsao.csv   marés do mês corrente, para cruzar com a previsão
    ondas.csv            altura de onda hora a hora (~7 dias à frente)
    vento.csv            velocidade do vento hora a hora (~7 dias à frente)

Tudo vem renderizado pelo servidor: requests + BeautifulSoup bastam. Os
números já saem convertidos (vírgula -> ponto) e as horas no formato HH:MM.

Rodar com: uv run python src/scrape.py
"""

import re
import time
from datetime import datetime
from pathlib import Path
import pandas as pd
import requests
from bs4 import BeautifulSoup

URL_BASE = "https://tabuademares.com/br/paraiba/joao-pessoa"
URL_ONDAS = f"{URL_BASE}/previsao/ondas"
URL_VENTO = f"{URL_BASE}/previsao/vento"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}
PAUSA = 1.5

DIR_RAW = Path(__file__).resolve().parent.parent / "data" / "raw"

MESES_ABREV = {
    "JAN": 1, "FEV": 2, "MAR": 3, "ABR": 4, "MAI": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SET": 9, "OUT": 10, "NOV": 11, "DEZ": 12,
}

def para_float(texto: str) -> float:
    """Converte "2,3 m" / "21 km/h" em número, trocando a vírgula decimal."""
    numero = re.search(r"-?\d+(?:,\d+)?", texto)
    return float(numero.group().replace(",", ".")) if numero else float("nan")

def baixar_html(url: str, dados_post: dict | None = None) -> str:
    if dados_post is None:
        resp = requests.get(url, headers=HEADERS, timeout=30)
    else:
        resp = requests.post(url, data=dados_post, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    print(f"  {resp.status_code} {len(resp.text):>7} bytes  {url} {dados_post or ''}")
    return resp.text


def parsear_mares(html: str, ano: int, mes: int) -> list[dict]:
    """Extrai da tábua mensal uma linha por evento de maré.

    Estrutura no HTML (tabela `#tabla_mareas`):
      - cada dia é um `<tr onclick="Day('2025-1-1')">`;
      - dentro dele, até 4 `td.tabla_mareas_marea`, um por maré. Em dias com
        só 3 marés a quarta célula vem vazia;
      - o tipo da maré está na classe do ícone: `tabla_mareas_marea_pleamar`
        (alta) ou `tabla_mareas_marea_bajamar` (baixa);
      - o coeficiente fica em `td.tabla_mareas_coeficiente_numero`;
      - a fase da lua é só um ícone: a classe `icon-hsN` traz a idade da lua
        em dias (0 = lua nova, ~15 = lua cheia).
    """
    soup = BeautifulSoup(html, "html.parser")
    linhas = []
    for tr in soup.select("#tabla_mareas tr[onclick]"):
        dia = int(tr.select_one(".tabla_mareas_dia_numero").get_text(strip=True))
        data = f"{ano:04d}-{mes:02d}-{dia:02d}"

        coef = tr.select_one(".tabla_mareas_coeficiente_numero")
        coeficiente = int(next(coef.stripped_strings)) if coef else None

        icone_lua = tr.select_one(".tabla_mareas_luna [class*=icon-hs]")
        idade_lua = None
        if icone_lua:
            m = re.search(r"icon-hs(\d+)", " ".join(icone_lua["class"]))
            idade_lua = int(m.group(1)) if m else None

        nascer = tr.select_one(".tabla_mareas_salida_puesta_sol_salida")
        por = tr.select_one(".tabla_mareas_salida_puesta_sol_puesta")

        for celula in tr.select("td.tabla_mareas_marea"):
            hora = celula.select_one(".tabla_mareas_marea_hora")
            if hora is None or not hora.get_text(strip=True):
                continue  # dia com só 3 marés
            alta = celula.select_one(".tabla_mareas_marea_pleamar") is not None
            altura = celula.select_one(".tabla_mareas_marea_altura_numero")
            linhas.append({
                "data": data,
                "hora": hora.get_text(strip=True).zfill(5),
                "altura_m": para_float(altura.get_text()),
                "tipo": "alta" if alta else "baixa",
                "coeficiente": coeficiente,
                "idade_lua": idade_lua,
                "nascer_sol": nascer.get_text(strip=True).zfill(5) if nascer else None,
                "por_sol": por.get_text(strip=True).zfill(5) if por else None,
            })
    return linhas

def parsear_previsao(html: str, ano: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    linhas = []
    mes_anterior = None
    for ficha in soup.select("div.ficha"):
        dia = int(ficha.select_one(".f_circulo .dia").get_text(strip=True))
        mes = MESES_ABREV[ficha.select_one(".f_circulo .mes").get_text(strip=True).upper()]
        if mes_anterior is not None and mes < mes_anterior:
            ano += 1
        mes_anterior = mes
        data = f"{ano:04d}-{mes:02d}-{dia:02d}"

        for bloco in ficha.select("div.f_temp_horas"):
            hora, direcao = (d.get_text(strip=True) for d in bloco.select("div.f_temp_hora"))
            valor = bloco.select_one(".grafico_temp_barra_relleno").get_text(" ", strip=True)
            linhas.append({
                "data": data,
                "hora": hora.zfill(5),
                "valor": para_float(valor),
                "direcao": direcao,
            })
    return linhas


def coletar_mares_do_ano(ano: int) -> pd.DataFrame:
    """Junta os 12 meses da tábua de marés de `ano` num DataFrame.

    O site troca de mês com um formulário (`#form_calendario`) que faz POST
    do campo `fecha` (AAAA-MM-DD) para a própria URL.
    """
    linhas = []
    for mes in range(1, 13):
        html = baixar_html(URL_BASE, {"fecha": f"{ano:04d}-{mes:02d}-01"})
        linhas += parsear_mares(html, ano, mes)
        time.sleep(PAUSA)
    return pd.DataFrame(linhas)


def main(ano: int = 2025) -> None:
    DIR_RAW.mkdir(parents=True, exist_ok=True)
    hoje = datetime.now()

    print(f"Marés de {ano}:")
    mares_ano = coletar_mares_do_ano(ano)

    print("Marés do mês corrente:")
    mares_previsao = pd.DataFrame(parsear_mares(baixar_html(URL_BASE), hoje.year, hoje.month))
    time.sleep(PAUSA)

    print("Previsão de ondas:")
    ondas = pd.DataFrame(parsear_previsao(baixar_html(URL_ONDAS), hoje.year))
    ondas = ondas.rename(columns={"valor": "altura_onda_m", "direcao": "direcao_onda"})
    time.sleep(PAUSA)

    print("Previsão de vento:")
    vento = pd.DataFrame(parsear_previsao(baixar_html(URL_VENTO), hoje.year))
    vento = vento.rename(columns={"valor": "vento_kmh", "direcao": "direcao_vento"})

    saidas = {
        f"mares_{ano}.csv": mares_ano,
        "mares_previsao.csv": mares_previsao,
        "ondas.csv": ondas,
        "vento.csv": vento,
    }
    print()
    for nome, df in saidas.items():
        df.to_csv(DIR_RAW / nome, index=False)
        print(f"{nome:<20} {len(df):>5} linhas")
if __name__ == "__main__":
    main()
