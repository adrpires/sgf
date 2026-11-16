import argparse
import sqlite3
import json
import os
import re
import time

import sgf_base
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


DATA_CAIXA = re.compile(r"^\d{2}/\d{2}/\d{4}$")
HORA_CAIXA = re.compile(r"^\d{2}:\d{2}:\d{2}$")


def iso_caixa(token):
    if not token or not DATA_CAIXA.fullmatch(token):
        return None
    dia, mes, ano = token.split("/")
    return f"{ano}-{mes}-{dia}"


def colunas_caixa(linha):
    colunas = [parte.strip() for parte in linha.split("\t")]
    while colunas and not colunas[0]:
        colunas.pop(0)
    while colunas and not colunas[-1]:
        colunas.pop()
    return colunas


def parse_caixa(texto):
    filial = ""
    filial_nome = ""
    historico = ""
    movimentos = []
    for bruta in texto.splitlines():
        linha = bruta.replace("\xa0", " ").strip()
        if not linha or linha.startswith("Página") or linha.startswith("Pagina") or linha.startswith("GRUPO"):
            continue
        if linha.startswith("Total"):
            continue
        if linha.startswith("Filial:"):
            colunas = colunas_caixa(linha)
            filial = colunas[1] if len(colunas) > 1 else ""
            filial_nome = " ".join(colunas[2:]).strip()
            historico = ""
            continue
        if linha.startswith("Histórico:") or linha.startswith("Historico:"):
            colunas = colunas_caixa(linha)
            historico = " ".join(colunas[2:]).strip() if len(colunas) > 2 else ""
            continue
        if linha.startswith("Observação:") or linha.startswith("Observacao:"):
            if movimentos:
                movimentos[-1]["obs"] = re.sub(r"\s+", " ", linha.split(":", 1)[-1]).strip()
            continue
        colunas = colunas_caixa(linha)
        if len(colunas) < 6 or not iso_caixa(colunas[0]) or not HORA_CAIXA.fullmatch(colunas[1]):
            continue
        tipo = colunas[3].upper()
        movimentos.append(
            {
                "filial": filial,
                "filialNome": filial_nome,
                "historico": historico,
                "data": iso_caixa(colunas[0]),
                "hora": colunas[1],
                "valor": dinheiro(colunas[2]),
                "tipo": "saida" if tipo == "S" else "entrada" if tipo == "E" else tipo,
                "moeda": colunas[4],
                "operador": " ".join(colunas[5:]).strip(),
                "obs": "",
            }
        )
    return movimentos


FORMAS_VENDA = ("dinheiro", "chequeVista", "chequePre", "crediario", "convenio", "cartao")

EXTRAIR_VENDAS = r"""() => {
    const direto = tabela => [...tabela.querySelectorAll(':scope > tr, :scope > thead > tr, :scope > tbody > tr')];
    const numero = texto => {
        const limpo = String(texto || '').trim();
        if (!/^-?\d{1,3}(?:\.\d{3})*(?:,\d+)?$/.test(limpo)) return null;
        return Number(limpo.replace(/\./g, '').replace(',', '.'));
    };
    const numerosDa = tr => {
        const folhas = [];
        const visitar = td => {
            const tabelas = [...td.children].filter(el => el.tagName === 'TABLE');
            if (!tabelas.length) {
                folhas.push(td.innerText.replace(/\s+/g, ' ').trim());
                return;
            }
            tabelas.forEach(tabela => direto(tabela).forEach(linha => [...linha.cells].forEach(visitar)));
        };
        [...tr.cells].forEach(visitar);
        const nums = [];
        let moeda = false;
        folhas.forEach(folha => {
            if (nums.length >= 7) return;
            const valor = numero(folha);
            if (valor === null) return;
            if (folha.includes(',')) moeda = true;
            nums.push(valor);
        });
        if (nums.length < 7) return null;
        const soma = nums.slice(0, 6).reduce((total, valor) => total + valor, 0);
        if (Math.abs(soma - nums[6]) > 0.05) return null;
        return {numeros: nums.slice(0, 7), moeda};
    };
    const eventos = [];
    [...document.querySelectorAll('table')].filter(item => direto(item).length > 8).forEach(tabela => {
        direto(tabela).forEach(tr => {
            const texto = tr.innerText.replace(/\s+/g, ' ').trim();
            const filial = texto.match(/^FILIAL:\s*(\d+)\s*(.*)$/);
            if (filial && tr.cells.length <= 4) {
                eventos.push({tipo: 'filial', filial: filial[1], nome: (filial[2] || '').trim()});
                return;
            }
            if (/^\d{2}\/\d{2}\/\d{4}$/.test(texto) && tr.cells.length <= 4) {
                eventos.push({tipo: 'data', data: texto});
                return;
            }
            if (/^TOTAL/.test(texto)) {
                eventos.push({tipo: 'total'});
                return;
            }
            const valores = numerosDa(tr);
            if (valores) eventos.push(Object.assign({tipo: 'valores'}, valores));
        });
    });
    return eventos;
}"""


def montar_vendas(eventos):
    if not isinstance(eventos, list):
        return []
    filial = ""
    nome = ""
    data = ""
    balde = []
    dias = []

    def fechar():
        nonlocal data, balde
        if not data or not balde:
            balde = []
            return
        inteiros = [item for item in balde if isinstance(item, dict) and not item.get("moeda")]
        moedas = [item for item in balde if isinstance(item, dict) and item.get("moeda")]
        balde = []
        if not inteiros or len(moedas) < 3:
            return
        qtd = inteiros[0].get("numeros") or []
        bruto = moedas[0].get("numeros") or []
        desconto = moedas[1].get("numeros") or []
        liquido = moedas[2].get("numeros") or []
        if len(qtd) < 7 or len(bruto) < 7 or len(desconto) < 7 or len(liquido) < 7:
            return
        dias.append(
            {
                "filial": filial,
                "filialNome": nome,
                "data": data,
                "qtd": int(round(float(qtd[6]))),
                "bruto": round(float(bruto[6]), 2),
                "desconto": round(float(desconto[6]), 2),
                "liquido": round(float(liquido[6]), 2),
                "formas": {
                    chave: round(float(liquido[indice]), 2)
                    for indice, chave in enumerate(FORMAS_VENDA)
                },
            }
        )

    for evento in eventos:
        if not isinstance(evento, dict):
            continue
        tipo = evento.get("tipo")
        if tipo == "filial":
            fechar()
            filial = str(evento.get("filial") or "").strip()
            nome = str(evento.get("nome") or "").strip()
            data = ""
        elif tipo == "data":
            fechar()
            partes = str(evento.get("data") or "").split("/")
            data = f"{partes[2]}-{partes[1]}-{partes[0]}" if len(partes) == 3 else ""
        elif tipo == "total":
            fechar()
            data = ""
        elif tipo == "valores" and data:
            balde.append(evento)
    fechar()
    return dias


def inicio_historico(hoje=None):
    hoje = hoje or hoje_fortaleza()
    ano = hoje.year if hoje.month >= 7 else hoje.year - 1
    return hoje.replace(year=ano, month=7, day=1)


def meses_no_intervalo(inicio, fim):
    cursor = inicio
    while cursor <= fim:
        if cursor.month == 12:
            proximo = cursor.replace(year=cursor.year + 1, month=1, day=1)
        else:
            proximo = cursor.replace(month=cursor.month + 1, day=1)
        yield cursor, min(fim, proximo - timedelta(days=1))
        cursor = proximo


def mesclar_intervalo(anteriores, novos, inicio, fim, tipo=None):
    def permanece(item):
        if not isinstance(item, dict):
            return False
        data = str(item.get("data") or "")
        if data < inicio or data > fim:
            return True
        if tipo and item.get("tipo") != tipo:
            return True
        return False

    return [item for item in anteriores if permanece(item)] + [
        item for item in novos if isinstance(item, dict)
    ]


def ampliar_cobertura(antigo_inicio, antigo_fim, inicio, fim):
    coberto_de = str(antigo_inicio or inicio)
    coberto_ate = str(antigo_fim or fim)
    return min(coberto_de, inicio), max(coberto_ate, fim)


def gravar_recorte(caminho, chave, novos, inicio, fim, tipo="todos", identificador="sync", marcar=False):
    anterior = ler_json(caminho)
    if not isinstance(anterior, dict):
        anterior = {}
    lista = anterior.get(chave) if isinstance(anterior.get(chave), list) else []
    filtro = {"entradas": "entrada", "saidas": "saida"}.get(tipo)
    cob_ini, cob_fim = ampliar_cobertura(anterior.get("inicio"), anterior.get("fim"), inicio, fim)
    meses = [item for item in (anterior.get("meses") or []) if isinstance(item, str)]
    if marcar and inicio[:7] and inicio[:7] not in meses:
        meses.append(inicio[:7])
        meses.sort()
    payload = {
        "id": identificador,
        "ok": True,
        "inicio": cob_ini,
        "fim": cob_fim,
        "geradoEm": agora_fortaleza(),
        "meses": meses,
        chave: mesclar_intervalo(lista, novos, inicio, fim, filtro),
    }
    if chave == "movimentos":
        payload["tipo"] = "todos"
    gravar_json(caminho, payload)
    return payload


def completar_historico(sessao):
    hoje = hoje_fortaleza()
    mes_atual = hoje.strftime("%Y-%m")
    for inicio, fim in meses_no_intervalo(inicio_historico(hoje), hoje):
        mes = inicio.strftime("%Y-%m")
        atual = mes == mes_atual
        caixa = ler_json(controle_dir() / "caixa-feito.json")
        if atual or mes not in set(caixa.get("meses") or []):
            try:
                movimentos = sessao.consultar_caixa(inicio.isoformat(), fim.isoformat(), "todos")
                gravar_recorte(
                    controle_dir() / "caixa-feito.json",
                    "movimentos",
                    movimentos,
                    inicio.isoformat(),
                    fim.isoformat(),
                    marcar=True,
                )
                print(f"caixa {inicio.isoformat()} {fim.isoformat()} movimentos={len(movimentos)}")
            except Exception as exc:
                print("falha caixa", type(exc).__name__, inicio.isoformat())
        vendas = ler_json(controle_dir() / "vendas-feito.json")
        if atual or mes not in set(vendas.get("meses") or []):
            try:
                dias = sessao.consultar_vendas(inicio.isoformat(), fim.isoformat())
                gravar_recorte(
                    controle_dir() / "vendas-feito.json",
                    "vendas",
                    dias,
                    inicio.isoformat(),
                    fim.isoformat(),
                    marcar=True,
                )
                print(f"vendas {inicio.isoformat()} {fim.isoformat()} dias={len(dias)}")
            except Exception as exc:
                print("falha vendas", type(exc).__name__, inicio.isoformat())


def parse_relatorio(texto):
    fornecedor = ""
    tipo_despesa = ""
    titulos = []
    for bruta in texto.splitlines():
        linha = bruta.replace("\xa0", " ").strip()
        if not linha or linha.startswith("Total") or linha.startswith("Página") or linha.startswith("Pagina"):
            continue
        if linha.startswith("Tipo Despesa:") or linha.startswith("Tipo de Despesa:"):
            nome = re.sub(r"\s+", " ", linha.split(":", 1)[1]).strip()
            partes = nome.split()
            if len(partes) > 1 and partes[0].isdigit():
                nome = " ".join(partes[1:])
            if nome and not nome.upper().startswith("CONSIDERAR"):
                tipo_despesa = nome
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
                "tipoDespesa": tipo_despesa,
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
    try:
        sgf_base.salvar(payload)
    except sqlite3.Error as exc:
        print("falha base", type(exc).__name__)
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

    def _relatorio_classe(self, inicio, fim, selecao, classe):
        ini = datetime.strptime(inicio, "%Y-%m-%d")
        final = datetime.strptime(fim, "%Y-%m-%d")
        if final < ini or (final - ini).days > 400:
            raise ValueError("periodo invalido")
        self.page.goto(self.base + "/Rel_0010.pod", wait_until="domcontentloaded", timeout=40000)
        self.page.wait_for_selector("#runReport", timeout=20000)
        self.page.evaluate(
            """([ini, fim, selecao]) => {
                document.getElementById('dat_inicio').value = ini;
                document.getElementById('dat_fim').value = fim;
                document.getElementById('selecao1_3').checked = true;
                document.getElementById(selecao).checked = true;
                document.getElementById('selecao3_1').checked = true;
                document.getElementById('sel_filtroper_1').checked = true;
                document.getElementById('saida3').checked = true;
            }""",
            [ini.strftime("%d/%m/%Y"), final.strftime("%d/%m/%Y"), selecao],
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
        titulos = parse_relatorio(texto)
        for item in titulos:
            item["classe"] = classe
            if classe == "despesa" and not item.get("tipoDespesa"):
                item["tipoDespesa"] = item.get("fornecedor") or ""
        return titulos

    def consultar(self, inicio, fim):
        fornecedores = self._relatorio_classe(inicio, fim, "selecao2_1", "fornecedor")
        despesas = self._relatorio_classe(inicio, fim, "selecao2_2", "despesa")
        return fornecedores + despesas

    def consultar_caixa(self, inicio, fim, tipo="todos"):
        ini = datetime.strptime(inicio, "%Y-%m-%d")
        final = datetime.strptime(fim, "%Y-%m-%d")
        if final < ini or (final - ini).days > 31:
            raise ValueError("periodo invalido")
        selecao = {"entradas": "selecao1_0", "saidas": "selecao1_1"}.get(tipo, "selecao1_2")
        self.page.goto(self.base + "/Rel_0047.pod", wait_until="domcontentloaded", timeout=40000)
        self.page.wait_for_selector("#runReport", timeout=20000)
        self.page.evaluate(
            """([ini, fim, selecao]) => {
                document.getElementById(selecao).checked = true;
                document.getElementById('selecao2_0').checked = true;
                document.getElementById('dat_abeini').value = ini;
                document.getElementById('dat_abefim').value = fim;
                document.getElementById('agrup_fil_2').checked = true;
                document.getElementById('saidaRelatorio3').checked = true;
            }""",
            [ini.strftime("%d/%m/%Y"), final.strftime("%d/%m/%Y"), selecao],
        )
        with self.page.expect_popup(timeout=150000) as espera:
            self.page.click("#runReport")
        popup = espera.value
        popup.wait_for_load_state("domcontentloaded", timeout=150000)
        popup.wait_for_function(
            "() => document.body && (document.body.innerText.includes('Movimentos Caixa') || document.body.innerText.includes('Total Geral'))",
            timeout=150000,
        )
        texto = popup.evaluate("() => { const t = document.querySelector('table'); return t ? t.innerText : ''; }")
        popup.close()
        return parse_caixa(texto)

    def consultar_vendas(self, inicio, fim):
        ini = datetime.strptime(inicio, "%Y-%m-%d")
        final = datetime.strptime(fim, "%Y-%m-%d")
        if final < ini or (final - ini).days > 31:
            raise ValueError("periodo invalido")
        self.page.goto(self.base + "/Rel_0056.pod", wait_until="domcontentloaded", timeout=40000)
        self.page.wait_for_selector("#runReport", timeout=20000)
        self.page.evaluate(
            """([ini, fim]) => {
                document.getElementById('dat_inicio').value = ini;
                document.getElementById('dat_fim').value = fim;
                document.getElementById('selI_0').checked = true;
                document.getElementById('agrup_fil_2').checked = true;
                document.getElementById('saida_3').checked = true;
                const total = document.getElementById('chk_som_total');
                if (total) total.checked = false;
            }""",
            [ini.strftime("%d/%m/%Y"), final.strftime("%d/%m/%Y")],
        )
        with self.page.expect_popup(timeout=180000) as espera:
            self.page.click("#runReport")
        popup = espera.value
        popup.wait_for_load_state("domcontentloaded", timeout=180000)
        popup.wait_for_function(
            "() => document.body && (document.body.innerText.includes('FILIAL:') || document.body.innerText.includes('Nenhum relat'))",
            timeout=180000,
        )
        eventos = popup.evaluate(EXTRAIR_VENDAS)
        popup.close()
        return montar_vendas(eventos)


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
    try:
        completar_historico(sessao)
    except Exception as exc:
        print("falha historico", type(exc).__name__)


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


def ler_caixa():
    return ler_json(controle_dir() / "caixa.json")


def atender_caixa(sessao, ultimo_id):
    pedido = ler_caixa()
    pedido_id = str(pedido.get("id") or "")
    if not pedido_id or pedido_id == ultimo_id:
        return ultimo_id
    movimentos = []
    ok = False
    try:
        user, password = credenciais()
        sessao.garantir(user, password)
        movimentos = sessao.consultar_caixa(
            str(pedido.get("inicio") or ""),
            str(pedido.get("fim") or ""),
            str(pedido.get("tipo") or "todos"),
        )
        ok = True
        print(f"caixa {pedido.get('inicio')} {pedido.get('fim')} movimentos={len(movimentos)}")
    except Exception as exc:
        print("falha caixa", type(exc).__name__)
        sessao.fechar()
    anterior = ler_json(controle_dir() / "caixa-feito.json")
    if not ok and anterior.get("ok"):
        gravar_json(
            controle_dir() / "caixa-feito.json",
            {
                "id": pedido_id,
                "ok": False,
                "inicio": anterior.get("inicio") or "",
                "fim": anterior.get("fim") or "",
                "tipo": anterior.get("tipo") or "todos",
                "geradoEm": anterior.get("geradoEm") or "",
                "meses": anterior.get("meses") or [],
                "movimentos": anterior.get("movimentos") or [],
            },
        )
    else:
        gravar_recorte(
            controle_dir() / "caixa-feito.json",
            "movimentos",
            movimentos,
            str(pedido.get("inicio") or ""),
            str(pedido.get("fim") or ""),
            str(pedido.get("tipo") or "todos"),
            pedido_id,
        )
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
    ultimo_caixa = str(ler_json(controle_dir() / "caixa-feito.json").get("id") or "")
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
        ultimo_caixa = atender_caixa(sessao, ultimo_caixa)
        time.sleep(0.4)


def servir_agenda(sessao, base, padrao):
    feito = ler_json(controle_dir() / "feito.json")
    ultimo_id = str(feito.get("id") or "")
    ultimo_relatorio = ""
    ultimo_caixa = str(ler_json(controle_dir() / "caixa-feito.json").get("id") or "")
    ultimo_fim = destino().stat().st_mtime if painel_valido() else 0.0
    if ultimo_fim:
        print("painel existente; proxima consulta segue a agenda")
    try:
        user, password = credenciais()
        sessao.garantir(user, password)
        completar_historico(sessao)
    except Exception as exc:
        print("falha historico", type(exc).__name__)
    while True:
        ultimo_caixa = atender_caixa(sessao, ultimo_caixa)
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
