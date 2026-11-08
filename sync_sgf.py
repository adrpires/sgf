import argparse
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE_DEFAULT = "http://45.160.125.114:4647/sgfpod1"
ATIPICO = 1_000_000
FORTALEZA = timezone(timedelta(hours=-3))


def agora_fortaleza():
    return datetime.now(FORTALEZA).isoformat(timespec="seconds")


def hoje_fortaleza():
    return datetime.now(FORTALEZA).date()


def janela_financeira(hoje=None):
    hoje = hoje or hoje_fortaleza()
    inicio = (hoje.replace(day=1) - timedelta(days=180)).replace(day=1)
    return inicio, hoje


def destino():
    configurado = os.environ.get("SGF_OUT")
    if configurado:
        return Path(configurado)
    return Path(__file__).with_name("painel_sgf.html")


def abrir_navegador(playwright):
    args = ["--disable-dev-shm-usage"]
    if os.environ.get("SGF_NO_SANDBOX") == "1":
        args.append("--no-sandbox")
    opcoes = {"headless": True, "args": args}
    canal = os.environ.get("SGF_BROWSER")
    if canal:
        opcoes["channel"] = canal
    return playwright.chromium.launch(**opcoes)


def login(base, user, password):
    with sync_playwright() as p:
        browser = abrir_navegador(p)
        page = browser.new_page()
        page.goto(base + "/Login.pod", wait_until="domcontentloaded", timeout=40000)
        page.wait_for_function(
            "() => document.querySelector('#id_cod_filial') && document.querySelector('#id_cod_filial').value !== ''",
            timeout=20000,
        )
        filial = page.locator("#nom_filial").input_value()
        page.fill("#id_cod_usuario", user)
        page.fill("#nom_senha", password)
        page.click("#login")
        page.wait_for_url("**/Menu.pod", timeout=20000)
        token = page.evaluate("localStorage.getItem('token_integracao')")
        browser.close()
    if not token:
        raise RuntimeError("Login sem token de integração.")
    return token, filial


def token_exp(token):
    import base64

    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    data = json.loads(base64.urlsafe_b64decode(payload))
    return datetime.fromtimestamp(data["exp"], timezone.utc)


def pull(session, base, path, params):
    rows = []
    offset = 0
    while offset <= 20000:
        query = dict(params)
        query["primeiroRegistro"] = offset
        query["quantidadeRegistros"] = 200
        response = session.get(base + path, params=query, timeout=60)
        response.raise_for_status()
        batch = response.json()
        if not isinstance(batch, list) or not batch:
            break
        rows.extend(batch)
        if len(batch) < 200:
            break
        offset += 200
    return rows


def coletar(base, token):
    session = requests.Session()
    session.headers["Authorization"] = "Bearer " + token
    inicio_data, fim_data = janela_financeira()
    inicio = inicio_data.isoformat()
    fim = fim_data.isoformat()
    classes = pull(
        session,
        base,
        "/rest/integracao/financeiro/obter-classificacao-financeira-v1",
        {"dataInicial": "2020-01-01", "dataFinal": fim},
    )
    por_centro = {}
    for item in classes:
        por_centro[str(item.get("centroCusto"))] = item
    movimentos = pull(
        session,
        base,
        "/rest/integracao/financeiro/obter-resultado-financeiro-v1",
        {"dataInicial": inicio, "dataFinal": fim},
    )
    contas = pull(
        session,
        base,
        "/rest/integracao/financeiro/conta/obter-todos-v1",
        {},
    )
    linhas = []
    for row in movimentos:
        centro = str(row.get("centroCusto"))
        classe = por_centro.get(centro, {})
        tipo_classe = classe.get("tipoClassificacao")
        if tipo_classe == "R":
            tipo = "receber"
        elif tipo_classe == "D":
            tipo = "pagar"
        else:
            tipo = "pagar" if float(row.get("valorBruto") or 0) < 0 else "receber"
        valor = abs(float(row.get("valorBruto") or 0))
        linhas.append(
            {
                "data": row.get("data"),
                "centro": centro,
                "descricao": classe.get("nomeDescricao") or f"Centro {centro}",
                "tipo": tipo,
                "status": "pendente" if row.get("baixadoAberto") == "A" else "baixado",
                "valor": round(valor, 2),
                "atipico": valor >= ATIPICO,
                "departamento": row.get("codigoDepartamento"),
            }
        )
    linhas.sort(key=lambda item: (item["data"] or "", item["tipo"], item["descricao"]))
    return {
        "geradoEm": agora_fortaleza(),
        "inicio": inicio,
        "fim": fim,
        "contas": [
            {
                "nome": item.get("nome") or "Conta",
                "saldo": round(float(item.get("saldo") or 0), 2),
            }
            for item in contas
        ],
        "linhas": linhas,
    }


HTML = (Path(__file__).resolve().parent / "templates" / "painel.html").read_text(encoding="utf-8")


DOC = re.compile(r"^(?:GF)?\d[\w./-]*-\d+$")
DATA_CURTA = re.compile(r"^\d{2}/\d{2}/\d{2}$")
DINHEIRO = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2}$|^-?\d+,\d{2}$")


def dinheiro(token):
    if not token or not DINHEIRO.fullmatch(token):
        return None
    return float(token.replace(".", "").replace(",", "."))


def iso_curto(token):
    if not token or not DATA_CURTA.fullmatch(token):
        return None
    dia, mes, ano = token.split("/")
    return f"20{ano}-{mes}-{dia}"


def parse_relatorio(texto):
    fornecedor = ""
    titulos = []
    for bruta in texto.splitlines():
        linha = bruta.replace("\xa0", " ").strip()
        if not linha or linha.startswith("Total") or linha.startswith("Página") or linha.startswith("Pagina"):
            continue
        if linha.startswith("Fornecedor:"):
            nome = re.sub(r"\s+", " ", linha.split(":", 1)[1]).strip()
            if nome and not nome.upper().startswith("CONSIDERAR"):
                fornecedor = nome
            continue
        if linha.startswith("Obs"):
            if titulos:
                titulos[-1]["obs"] = re.sub(r"\s+", " ", linha.split(":", 1)[-1]).strip()
            continue
        colunas = [parte.strip() for parte in linha.split("\t")]
        if not colunas or not DOC.match(colunas[0]):
            continue

        def coluna(indice):
            return colunas[indice] if indice < len(colunas) else ""

        pago = dinheiro(coluna(14))
        juros = dinheiro(coluna(16))
        atipico = any(
            (dinheiro(token) is not None and abs(dinheiro(token)) >= ATIPICO)
            or (dinheiro(token) is None and len(re.sub(r"\D", "", token)) >= 8)
            for token in (coluna(14), coluna(16))
            if token
        )
        titulos.append(
            {
                "fornecedor": fornecedor,
                "documento": coluna(0),
                "filial": coluna(2),
                "emissao": iso_curto(coluna(4)),
                "vencimento": iso_curto(coluna(5)),
                "pagamento": iso_curto(coluna(13)),
                "valor": dinheiro(coluna(10)),
                "pago": None if atipico else pago,
                "juros": None if atipico else (0 if juros is None else juros),
                "obs": "",
                "atipico": atipico,
            }
        )
    return titulos


def logo_data():
    caminho = Path(__file__).resolve().parent / "marca.png"
    if not caminho.exists():
        return ""
    import base64

    return "data:image/png;base64," + base64.b64encode(caminho.read_bytes()).decode("ascii")


def gravar(payload, filial):
    payload["filial"] = filial
    payload.setdefault("titulos", [])
    html = HTML.replace("__DADOS__", json.dumps(payload, ensure_ascii=False)).replace("__LOGO__", logo_data())
    arquivo = destino()
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    temporario = arquivo.with_name(arquivo.name + ".tmp")
    temporario.write_text(html, encoding="utf-8")
    os.replace(temporario, arquivo)
    print(f"painel {arquivo} linhas={len(payload['linhas'])} contas={len(payload['contas'])}")


def controle_dir():
    configurado = os.environ.get("SGF_CONTROLE")
    if configurado:
        return Path(configurado)
    return Path(__file__).resolve().parent / "controle"


def gravar_json(caminho, dados):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_suffix(".tmp")
    temporario.write_text(json.dumps(dados), encoding="utf-8")
    os.replace(temporario, caminho)


def credenciais():
    dados = {}
    arquivo = controle_dir() / "sgf.json"
    if arquivo.exists():
        try:
            dados = json.loads(arquivo.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            dados = {}
    if not isinstance(dados, dict):
        dados = {}
    usuario = str(dados.get("user") or os.environ.get("SGF_USER", "")).strip()
    senha = str(dados.get("password") or os.environ.get("SGF_PASS", ""))
    if not usuario or not senha:
        raise RuntimeError("Acesso do SGF não configurado.")
    return usuario, senha


def ler_json(caminho):
    if not caminho.exists():
        return {}
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dados if isinstance(dados, dict) else {}


def ler_pedido():
    return ler_json(controle_dir() / "pedido.json")


def painel_valido(caminho=None):
    arquivo = caminho or destino()
    if not arquivo.exists():
        return False
    try:
        if arquivo.stat().st_size < 200:
            return False
        with arquivo.open(encoding="utf-8", errors="ignore") as arquivo_lido:
            trecho = arquivo_lido.read(512_000)
    except OSError:
        return False
    return "const DADOS = " in trecho


def limitar_intervalo(valor, padrao):
    fallback = padrao if isinstance(padrao, int) and 5 <= padrao <= 1440 else 30
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        return fallback
    if numero < 5 or numero > 1440:
        return fallback
    return numero


def ler_agenda(padrao):
    dados = ler_json(controle_dir() / "agenda.json")
    if "intervaloMinutos" in dados:
        intervalo = limitar_intervalo(dados.get("intervaloMinutos"), padrao if padrao > 0 else 30)
    elif padrao > 0:
        intervalo = limitar_intervalo(padrao, padrao)
    else:
        intervalo = 0
    return intervalo, dados.get("pausado") is True


def deve_sincronizar(agora, ultimo_fim, intervalo, pausado, pedido_id, ultimo_pedido_id):
    if pedido_id and pedido_id != ultimo_pedido_id:
        return "pedido"
    if pausado or intervalo <= 0:
        return ""
    if not ultimo_fim or agora >= float(ultimo_fim) + intervalo * 60:
        return "relogio"
    return ""


def iso_local(epoch):
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch, FORTALEZA).isoformat(timespec="seconds")


_estado_assinatura = None


def atualizar_estado(campos):
    global _estado_assinatura
    caminho = controle_dir() / "estado.json"
    atual = ler_json(caminho)
    atual.update(campos)
    assinatura = json.dumps(atual, sort_keys=True, ensure_ascii=False)
    if caminho.exists() and assinatura == _estado_assinatura:
        return
    _estado_assinatura = assinatura
    gravar_json(caminho, atual)


def executar_com_estado(sessao, base, inicio, fim, intervalo, pausado):
    agora_iso = agora_fortaleza()
    atualizar_estado(
        {
            "emAndamento": True,
            "pausado": pausado,
            "intervaloMinutos": intervalo,
            "iniciadoEm": agora_iso,
        }
    )
    ok = executar_ciclo(sessao, base, inicio, fim)
    terminou = time.time()
    terminou_iso = agora_fortaleza()
    campos = {
        "emAndamento": False,
        "pausado": pausado,
        "intervaloMinutos": intervalo,
        "terminadoEm": terminou_iso,
        "ultimoOk": ok,
    }
    if ok:
        campos["ultimoSucessoEm"] = terminou_iso
        campos["ultimoErroEm"] = ""
        campos["ultimoErro"] = ""
    else:
        campos["ultimoErroEm"] = terminou_iso
        campos["ultimoErro"] = "falha"
    if not pausado and intervalo > 0:
        campos["proximoEm"] = iso_local(terminou + intervalo * 60)
    else:
        campos["proximoEm"] = ""
    atualizar_estado(campos)
    return ok, terminou


def passo_agenda(sessao, base, padrao, ultimo_id, ultimo_fim):
    intervalo, pausado = ler_agenda(padrao)
    pedido = ler_pedido()
    pedido_id = str(pedido.get("id") or "")
    motivo = deve_sincronizar(time.time(), ultimo_fim, intervalo, pausado, pedido_id, ultimo_id)
    if not motivo:
        proximo = ""
        if ultimo_fim and not pausado and intervalo > 0:
            proximo = iso_local(float(ultimo_fim) + intervalo * 60)
        atualizar_estado(
            {
                "emAndamento": False,
                "pausado": pausado,
                "intervaloMinutos": intervalo,
                "proximoEm": proximo,
            }
        )
        return ultimo_id, ultimo_fim, ""
    inicio = fim = ""
    if motivo == "pedido":
        inicio = str(pedido.get("inicio") or "")
        fim = str(pedido.get("fim") or "")
    ok, terminou = executar_com_estado(sessao, base, inicio, fim, intervalo, pausado)
    if motivo == "pedido":
        gravar_json(controle_dir() / "feito.json", {"id": pedido_id, "ok": ok})
        ultimo_id = pedido_id
    print("sincronismo", motivo, "ok" if ok else "falha")
    return ultimo_id, terminou, motivo


class Sessao:
    def __init__(self, base):
        self.base = base
        self.user = ""
        self.password = ""
        self.token = ""
        self.exp = None
        self.filial = ""
        self.playwright = None
        self.browser = None
        self.page = None

    def fechar(self):
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
        if self.playwright:
            try:
                self.playwright.stop()
            except Exception:
                pass
        self.browser = None
        self.playwright = None
        self.page = None
        self.token = ""
        self.exp = None

    def pagina_viva(self):
        try:
            return self.page is not None and not self.page.is_closed()
        except Exception:
            return False

    def garantir(self, user, password):
        if self.user != user or self.password != password or not self.pagina_viva():
            self.fechar()
        self.user = user
        self.password = password
        if self.token and self.exp and self.exp - datetime.now(timezone.utc) >= timedelta(hours=2) and self.pagina_viva():
            return
        self.fechar()
        self.user = user
        self.password = password
        self.playwright = sync_playwright().start()
        self.browser = abrir_navegador(self.playwright)
        self.page = self.browser.new_page()
        self.page.goto(self.base + "/Login.pod", wait_until="domcontentloaded", timeout=40000)
        self.page.wait_for_function(
            "() => document.querySelector('#id_cod_filial') && document.querySelector('#id_cod_filial').value !== ''",
            timeout=20000,
        )
        self.filial = self.page.locator("#nom_filial").input_value()
        self.page.fill("#id_cod_usuario", user)
        self.page.fill("#nom_senha", password)
        self.page.click("#login")
        self.page.wait_for_url("**/Menu.pod", timeout=20000)
        token = self.page.evaluate("localStorage.getItem('token_integracao')")
        if not token:
            self.fechar()
            raise RuntimeError("Login sem token de integração.")
        self.token = token
        self.exp = token_exp(token)
        print("sessao ate", self.exp.isoformat())

    def consultar(self, inicio, fim):
        ini = datetime.strptime(inicio, "%Y-%m-%d")
        final = datetime.strptime(fim, "%Y-%m-%d")
        if final < ini or (final - ini).days > 400:
            raise ValueError("periodo invalido")
        self.page.goto(self.base + "/Rel_0010.pod", wait_until="domcontentloaded", timeout=40000)
        self.page.wait_for_selector("#runReport", timeout=20000)
        self.page.evaluate(
            """([ini, fim]) => {
                document.getElementById('dat_inicio').value = ini;
                document.getElementById('dat_fim').value = fim;
                document.getElementById('selecao1_3').checked = true;
                document.getElementById('selecao2_3').checked = true;
                document.getElementById('selecao3_1').checked = true;
                document.getElementById('sel_filtroper_1').checked = true;
                document.getElementById('saida3').checked = true;
            }""",
            [ini.strftime("%d/%m/%Y"), final.strftime("%d/%m/%Y")],
        )
        with self.page.expect_popup(timeout=150000) as espera:
            self.page.click("#runReport")
        popup = espera.value
        popup.wait_for_load_state("domcontentloaded", timeout=150000)
        popup.wait_for_function(
            "() => document.body && (document.body.innerText.includes('Fornecedor') || document.body.innerText.includes('Total Geral'))",
            timeout=150000,
        )
        texto = popup.evaluate("() => { const t = document.querySelector('table'); return t ? t.innerText : ''; }")
        popup.close()
        return parse_relatorio(texto)


def periodo_consulta(inicio, fim):
    padrao_inicio, padrao_fim = janela_financeira()
    padrao = padrao_inicio.isoformat(), padrao_fim.isoformat()
    if not inicio or not fim:
        return padrao
    try:
        ini = datetime.strptime(inicio, "%Y-%m-%d").date()
        final = datetime.strptime(fim, "%Y-%m-%d").date()
    except ValueError:
        return padrao
    if final < ini or (final - ini).days > 400:
        return padrao
    return ini.isoformat(), final.isoformat()


def ciclo(base, user, password, sessao, inicio="", fim=""):
    sessao.garantir(user, password)
    payload = coletar(base, sessao.token)
    inicio, fim = periodo_consulta(inicio, fim)
    payload["titulos"] = sessao.consultar(inicio, fim)
    payload["titulosInicio"] = inicio
    payload["titulosFim"] = fim
    payload["geradoEm"] = agora_fortaleza()
    gravar(payload, sessao.filial)


def ler_relatorio():
    arquivo = controle_dir() / "relatorio.json"
    if not arquivo.exists():
        return {}
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dados if isinstance(dados, dict) else {}


def atender_relatorio(sessao, ultimo_id):
    pedido = ler_relatorio()
    pedido_id = str(pedido.get("id") or "")
    if not pedido_id or pedido_id == ultimo_id:
        return ultimo_id
    titulos = []
    ok = False
    try:
        user, password = credenciais()
        sessao.garantir(user, password)
        titulos = sessao.consultar(str(pedido.get("inicio") or ""), str(pedido.get("fim") or ""))
        ok = True
        print(f"relatorio {pedido.get('inicio')} {pedido.get('fim')} titulos={len(titulos)}")
    except Exception as exc:
        print("falha relatorio", type(exc).__name__)
        sessao.fechar()
    gravar_json(controle_dir() / "relatorio-feito.json", {"id": pedido_id, "ok": ok, "titulos": titulos})
    return pedido_id


def executar_ciclo(sessao, base, inicio="", fim=""):
    try:
        user, password = credenciais()
        ciclo(base, user, password, sessao, inicio, fim)
        return True
    except Exception as exc:
        print("falha", type(exc).__name__)
        sessao.fechar()
        return False


def servir_sob_demanda(sessao, base):
    feito = ler_json(controle_dir() / "feito.json")
    ultimo_id = str(feito.get("id") or "")
    while True:
        pedido = ler_pedido()
        pedido_id = str(pedido.get("id") or "")
        if pedido_id and pedido_id != ultimo_id:
            ok, _terminou = executar_com_estado(
                sessao,
                base,
                str(pedido.get("inicio") or ""),
                str(pedido.get("fim") or ""),
                0,
                True,
            )
            gravar_json(controle_dir() / "feito.json", {"id": pedido_id, "ok": ok})
            ultimo_id = pedido_id
            print("sincronismo pedido", "ok" if ok else "falha")
        else:
            atualizar_estado(
                {"emAndamento": False, "pausado": True, "intervaloMinutos": 0, "proximoEm": ""}
            )
        time.sleep(0.4)


def servir_agenda(sessao, base, padrao):
    feito = ler_json(controle_dir() / "feito.json")
    ultimo_id = str(feito.get("id") or "")
    ultimo_relatorio = ""
    ultimo_fim = destino().stat().st_mtime if painel_valido() else 0.0
    if ultimo_fim:
        print("painel existente; proxima consulta segue a agenda")
    while True:
        ultimo_relatorio = atender_relatorio(sessao, ultimo_relatorio)
        ultimo_id, ultimo_fim, _motivo = passo_agenda(sessao, base, padrao, ultimo_id, ultimo_fim)
        time.sleep(0.4)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--a-cada", type=int, default=0, help="Minutos entre consultas. 0 roda uma vez.")
    parser.add_argument("--sob-demanda", action="store_true", help="Só sincroniza quando o painel pede.")
    args = parser.parse_args()
    base = os.environ.get("SGF_BASE", BASE_DEFAULT).rstrip("/")
    sessao = Sessao(base)
    if args.sob_demanda:
        servir_sob_demanda(sessao, base)
        return
    if args.a_cada <= 0:
        ok, _terminou = executar_com_estado(sessao, base, "", "", 0, False)
        if not ok:
            raise RuntimeError("falha na consulta")
        return
    servir_agenda(sessao, base, args.a_cada)


if __name__ == "__main__":
    main()
