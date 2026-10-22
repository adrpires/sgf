import json
import re
from pathlib import Path

import fitz

PASTA = Path("apoio")
DOC = re.compile(r"^(?:GF)?\d[\w./-]*-\d+$")
DATA = re.compile(r"^\d{2}/\d{2}/\d{2}$")
DINHEIRO = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2}$|^-?\d+,\d{2}$")
BANDAS = (
    ("boleto", 14),
    ("juros", 56),
    ("desconto_pagamento", 116),
    ("pago", 181),
    ("pagamento", 230),
    ("antecipado", 289),
    ("pagar", 392),
    ("desconto", 453),
    ("devolucao", 507),
    ("duplicata", 560),
    ("banco", 650),
    ("vencimento", 712),
    ("emissao", 756),
    ("filial", 903),
    ("documento", 922),
)


def dinheiro(token):
    if not token or not DINHEIRO.fullmatch(token):
        return None
    return float(token.replace(".", "").replace(",", "."))


def iso(token):
    if not token or not DATA.fullmatch(token):
        return None
    dia, mes, ano = token.split("/")
    return f"20{ano}-{mes}-{dia}"


def banda(y):
    nome, centro = min(BANDAS, key=lambda item: abs(item[1] - y))
    if abs(centro - y) > 22:
        return None
    return nome


def clusters(words):
    xs = sorted({round(w[0], 1) for w in words if w[0] > 40})
    grupos = []
    for x in xs:
        if grupos and x - grupos[-1][1] <= 3.5:
            grupos[-1][1] = x
        else:
            grupos.append([x, x])
    colunas = []
    for inicio, fim in grupos:
        itens = [w for w in words if inicio - 0.2 <= round(w[0], 1) <= fim + 0.2]
        if itens:
            colunas.append(itens)
    return colunas


def texto(itens):
    return " ".join(w[4] for w in sorted(itens, key=lambda w: -w[1]))


def nome_fornecedor(itens):
    partes = [
        w[4]
        for w in sorted(itens, key=lambda w: -w[1])
        if w[4] not in {"Fornecedor:", "Total", "Geral:"} and w[1] > 740
    ]
    return " ".join(partes).strip()


def titulo_de(itens, fornecedor):
    campos = {}
    for w in itens:
        nome = banda(w[1])
        if nome:
            campos.setdefault(nome, []).append(w[4])
    documento = next((t for t in campos.get("documento", []) if DOC.match(t)), "")
    if not documento:
        return None
    pagar = next((dinheiro(t) for t in campos.get("pagar", []) if dinheiro(t) is not None), None)
    pago = next((dinheiro(t) for t in campos.get("pago", []) if dinheiro(t) is not None), None)
    suspeitos = campos.get("pago", []) + campos.get("juros", [])
    atipico = any(
        (dinheiro(token) is not None and dinheiro(token) >= 1_000_000)
        or (dinheiro(token) is None and len(re.sub(r"\D", "", token)) >= 8)
        for token in suspeitos
    )
    filial = next((t for t in campos.get("filial", []) if re.fullmatch(r"\d{1,3}", t)), "")
    return {
        "fornecedor": fornecedor,
        "documento": documento,
        "filial": filial,
        "emissao": iso(next(iter(campos.get("emissao", [])), None)),
        "vencimento": iso(next(iter(campos.get("vencimento", [])), None)),
        "pagamento": iso(next(iter(campos.get("pagamento", [])), None)),
        "valor": pagar,
        "pago": None if atipico else pago,
        "desconto": next((dinheiro(t) for t in campos.get("desconto", []) if dinheiro(t) is not None), 0),
        "juros": None if atipico else next((dinheiro(t) for t in campos.get("juros", []) if dinheiro(t) is not None), 0),
        "banco": " ".join(t for t in campos.get("banco", []) if not DINHEIRO.fullmatch(t) and not DATA.fullmatch(t)).strip(),
        "boleto": " ".join(campos.get("boleto", [])).strip(),
        "obs": "",
        "atipico": atipico,
    }


def ler_pdf(caminho):
    doc = fitz.open(caminho)
    primeira = doc[0].get_text()
    if "I:TODAS" not in primeira and "Seleção I:TODAS" not in primeira and "I:TODAS" not in primeira.replace("ç", "c"):
        cabeca = primeira.upper()
        if "CONTAS A PAGAR" in cabeca and "DUPLICATAS EM ABERTO" in cabeca:
            return []
    titulos = []
    fornecedor = ""
    for page in doc:
        words = page.get_text("words")
        for itens in clusters(words):
            tokens = {w[4] for w in itens}
            if "Fornecedor:" in tokens and "Total" not in tokens:
                nome = nome_fornecedor(itens)
                if nome:
                    fornecedor = nome
                continue
            if "Total" in tokens or "Página" in tokens or "Pagina" in tokens:
                continue
            if any(token.startswith("Obs") for token in tokens):
                if titulos:
                    titulos[-1]["obs"] = texto(itens).replace("Obs.:", "").strip()
                continue
            item = titulo_de(itens, fornecedor)
            if item:
                titulos.append(item)
    return titulos


def main():
    titulos = []
    for pdf in sorted(PASTA.glob("*.pdf")):
        lote = ler_pdf(pdf)
        print(pdf.name, len(lote))
        titulos.extend(lote)
    pagar = sum(item["valor"] or 0 for item in titulos)
    pago = sum(item["pago"] or 0 for item in titulos if not item["atipico"])
    print("titulos", len(titulos), "pagar", round(pagar, 2), "pago", round(pago, 2))
    print("atipicos", sum(1 for item in titulos if item["atipico"]))
    print("sem vencimento", sum(1 for item in titulos if not item["vencimento"]))
    print("sem valor", sum(1 for item in titulos if item["valor"] is None))
    print("com obs", sum(1 for item in titulos if item["obs"]))
    print("exemplo", json.dumps(titulos[0], ensure_ascii=False))
    nestle = next(item for item in titulos if item["documento"] == "1020442/01-0")
    print("nestle", json.dumps(nestle, ensure_ascii=False))
    destino = PASTA / "titulos.json"
    destino.write_text(json.dumps(titulos, ensure_ascii=False), encoding="utf-8")
    print("gravou", destino, "bytes", destino.stat().st_size)


if __name__ == "__main__":
    main()
