import os
import sqlite3
from pathlib import Path


def caminho():
    pasta = os.environ.get("SGF_CONTROLE") or os.environ.get("GEFIN_CONTROLE")
    base = Path(pasta) if pasta else Path(__file__).resolve().parent / "controle"
    return base / "sgf.db"


def conectar():
    caminho().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(caminho(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS sgf_sync (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    gerado_em TEXT,
    inicio TEXT,
    fim TEXT,
    titulos_inicio TEXT,
    titulos_fim TEXT,
    filial TEXT
);
CREATE TABLE IF NOT EXISTS sgf_linha (
    data TEXT,
    centro TEXT,
    descricao TEXT,
    tipo TEXT,
    status TEXT,
    valor REAL,
    atipico INTEGER,
    departamento TEXT
);
CREATE TABLE IF NOT EXISTS sgf_titulo (
    fornecedor TEXT,
    documento TEXT,
    filial TEXT,
    emissao TEXT,
    vencimento TEXT,
    pagamento TEXT,
    valor REAL,
    pago REAL,
    juros REAL,
    obs TEXT,
    atipico INTEGER
);
CREATE TABLE IF NOT EXISTS sgf_conta (
    nome TEXT,
    saldo REAL
);
CREATE INDEX IF NOT EXISTS idx_sgf_linha_data ON sgf_linha(data);
CREATE INDEX IF NOT EXISTS idx_sgf_titulo_vencimento ON sgf_titulo(vencimento);
"""


def _numero(valor):
    if valor is None or valor == "":
        return None
    try:
        return round(float(valor), 2)
    except (TypeError, ValueError):
        return None


def _texto(valor):
    if valor is None:
        return ""
    return str(valor)


def salvar(payload):
    if not isinstance(payload, dict):
        return
    conn = conectar()
    try:
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM sgf_linha")
        conn.execute("DELETE FROM sgf_titulo")
        conn.execute("DELETE FROM sgf_conta")
        conn.execute("DELETE FROM sgf_sync")
        conn.execute(
            """
            INSERT INTO sgf_sync (id, gerado_em, inicio, fim, titulos_inicio, titulos_fim, filial)
            VALUES (1, ?, ?, ?, ?, ?, ?)
            """,
            (
                _texto(payload.get("geradoEm")),
                _texto(payload.get("inicio")),
                _texto(payload.get("fim")),
                _texto(payload.get("titulosInicio")),
                _texto(payload.get("titulosFim")),
                _texto(payload.get("filial")),
            ),
        )
        conn.executemany(
            """
            INSERT INTO sgf_linha (data, centro, descricao, tipo, status, valor, atipico, departamento)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    _texto(item.get("data")),
                    _texto(item.get("centro")),
                    _texto(item.get("descricao")),
                    _texto(item.get("tipo")),
                    _texto(item.get("status")),
                    _numero(item.get("valor")) or 0,
                    1 if item.get("atipico") else 0,
                    _texto(item.get("departamento")),
                )
                for item in payload.get("linhas") or []
                if isinstance(item, dict) and not item.get("manual")
            ],
        )
        conn.executemany(
            """
            INSERT INTO sgf_titulo
                (fornecedor, documento, filial, emissao, vencimento, pagamento, valor, pago, juros, obs, atipico)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    _texto(item.get("fornecedor")),
                    _texto(item.get("documento")),
                    _texto(item.get("filial")),
                    _texto(item.get("emissao")),
                    _texto(item.get("vencimento")),
                    _texto(item.get("pagamento")),
                    _numero(item.get("valor")),
                    _numero(item.get("pago")),
                    _numero(item.get("juros")),
                    _texto(item.get("obs")),
                    1 if item.get("atipico") else 0,
                )
                for item in payload.get("titulos") or []
                if isinstance(item, dict)
            ],
        )
        conn.executemany(
            "INSERT INTO sgf_conta (nome, saldo) VALUES (?, ?)",
            [
                (_texto(item.get("nome")) or "Conta", _numero(item.get("saldo")) or 0)
                for item in payload.get("contas") or []
                if isinstance(item, dict)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def ler():
    arquivo = caminho()
    if not arquivo.exists():
        return {}
    conn = conectar()
    try:
        conn.executescript(SCHEMA)
        sync = conn.execute("SELECT * FROM sgf_sync WHERE id = 1").fetchone()
        if not sync:
            return {}
        linhas = [
            {
                "data": item["data"],
                "centro": item["centro"],
                "descricao": item["descricao"],
                "tipo": item["tipo"],
                "status": item["status"],
                "valor": item["valor"],
                "atipico": bool(item["atipico"]),
                "departamento": item["departamento"],
            }
            for item in conn.execute("SELECT * FROM sgf_linha ORDER BY data, tipo, descricao")
        ]
        titulos = [
            {
                "fornecedor": item["fornecedor"],
                "documento": item["documento"],
                "filial": item["filial"],
                "emissao": item["emissao"],
                "vencimento": item["vencimento"],
                "pagamento": item["pagamento"],
                "valor": item["valor"],
                "pago": item["pago"],
                "juros": item["juros"],
                "obs": item["obs"],
                "atipico": bool(item["atipico"]),
            }
            for item in conn.execute("SELECT * FROM sgf_titulo")
        ]
        contas = [
            {"nome": item["nome"], "saldo": item["saldo"]}
            for item in conn.execute("SELECT nome, saldo FROM sgf_conta")
        ]
    finally:
        conn.close()
    return {
        "geradoEm": sync["gerado_em"],
        "inicio": sync["inicio"],
        "fim": sync["fim"],
        "titulosInicio": sync["titulos_inicio"],
        "titulosFim": sync["titulos_fim"],
        "filial": sync["filial"],
        "linhas": linhas,
        "titulos": titulos,
        "contas": contas,
    }
