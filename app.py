import html
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

PAINEL = Path(os.environ.get("GEFIN_PAINEL", Path(__file__).with_name("painel_sgf.html")))
DB = Path(os.environ.get("GEFIN_DB", Path(__file__).with_name("usuarios.db")))
CONTROLE = Path(os.environ.get("GEFIN_CONTROLE", Path(__file__).with_name("controle")))
AGUARDAR_SYNC = os.environ.get("GEFIN_AGUARDAR_SYNC") == "1"
LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")
INTERVALO_MIN = 5
INTERVALO_MAX = 24 * 60

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.secret_key = os.environ.get("GEFIN_SECRET") or ""
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("GEFIN_COOKIE_SECURE") == "1",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
)


def preparar():
    DB.parent.mkdir(parents=True, exist_ok=True)
    if not app.secret_key:
        chave = DB.parent / "secret.key"
        if chave.exists():
            app.secret_key = chave.read_text(encoding="utf-8").strip()
        else:
            app.secret_key = secrets.token_hex(32)
            chave.write_text(app.secret_key, encoding="utf-8")
    with conectar() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metas (
                id INTEGER PRIMARY KEY,
                tipo TEXT NOT NULL,
                categoria TEXT NOT NULL,
                valor REAL NOT NULL,
                UNIQUE(tipo, categoria)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metas_origem (
                origem TEXT PRIMARY KEY,
                valor REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS configuracao (
                chave TEXT PRIMARY KEY,
                valor TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY,
                login TEXT NOT NULL UNIQUE,
                nome TEXT NOT NULL,
                senha_hash TEXT NOT NULL,
                admin INTEGER NOT NULL DEFAULT 0,
                ativo INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lancamentos (
                id INTEGER PRIMARY KEY,
                tipo TEXT NOT NULL,
                data TEXT NOT NULL,
                origem TEXT NOT NULL,
                descricao TEXT NOT NULL,
                valor REAL NOT NULL,
                criado_em TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usuario_lojas (
                usuario_id INTEGER NOT NULL,
                origem TEXT NOT NULL,
                PRIMARY KEY (usuario_id, origem)
            )
            """
        )
        total = conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
        if total == 0:
            login = os.environ.get("GEFIN_ADMIN_USER", "").strip().lower()
            senha = os.environ.get("GEFIN_ADMIN_PASS", "")
            if LOGIN_RE.fullmatch(login or "") and len(senha) >= 8:
                conn.execute(
                    "INSERT INTO usuarios (login, nome, senha_hash, admin, ativo) VALUES (?, ?, ?, 1, 1)",
                    (login, "Administrador", generate_password_hash(senha)),
                )
    agenda = agenda_salva()
    publicar_agenda(agenda["intervalo"], agenda["pausado"])
    proteger_banco()


def conectar():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def proteger_banco():
    for caminho in (DB, Path(str(DB) + "-wal"), Path(str(DB) + "-shm"), DB.parent / "secret.key"):
        if caminho.exists():
            os.chmod(caminho, 0o600)


def usuario_por_id(usuario_id):
    with conectar() as conn:
        return conn.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()


def usuario_por_login(login):
    with conectar() as conn:
        return conn.execute("SELECT * FROM usuarios WHERE login = ?", (login,)).fetchone()


def listar_usuarios():
    with conectar() as conn:
        return conn.execute("SELECT * FROM usuarios ORDER BY nome, login").fetchall()


def lojas_do_usuario(usuario_id):
    with conectar() as conn:
        linhas = conn.execute(
            "SELECT origem FROM usuario_lojas WHERE usuario_id = ?",
            (usuario_id,),
        ).fetchall()
    return {codigo_origem(item["origem"]) for item in linhas if codigo_origem(item["origem"])}


def acesso_lojas(usuario):
    if not usuario:
        return set()
    marcas = lojas_do_usuario(usuario["id"])
    return marcas or None


def salvar_lojas(usuario_id, codigos):
    with conectar() as conn:
        conn.execute("DELETE FROM usuario_lojas WHERE usuario_id = ?", (usuario_id,))
        for codigo in sorted(codigos):
            conn.execute(
                "INSERT INTO usuario_lojas (usuario_id, origem) VALUES (?, ?)",
                (usuario_id, codigo),
            )


def texto_lojas(usuario_id):
    codigos = lojas_do_usuario(usuario_id)
    if not codigos:
        return "Todas"
    return ", ".join(nome_origem(codigo) for codigo in sorted(codigos, key=ordem_origem))


def lojas_editor(usuario):
    return acesso_lojas(usuario)


def lojas_escolhidas(editor, atuais=None):
    permitidas = lojas_editor(editor)
    conhecidas = {item["codigo"] for item in origens_disponiveis()} | set(atuais or ())
    escolhidas = set()
    for codigo in request.form.getlist("lojas"):
        normal = codigo_origem(codigo)
        if normal not in conhecidas:
            continue
        if permitidas is not None and normal not in permitidas:
            continue
        escolhidas.add(normal)
    return escolhidas


def lojas_para_editar(editor, marcadas):
    por_codigo = {item["codigo"]: dict(item) for item in origens_disponiveis()}
    for codigo in marcadas:
        por_codigo.setdefault(codigo, {"codigo": codigo, "nome": nome_origem(codigo)})
    permitidas = lojas_editor(editor)
    opcoes = []
    for codigo in sorted(por_codigo, key=ordem_origem):
        if permitidas is not None and codigo not in permitidas:
            continue
        item = por_codigo[codigo]
        item["marcada"] = codigo in marcadas
        opcoes.append(item)
    return opcoes


def ultimo_admin(exceto_id=None):
    with conectar() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM usuarios WHERE admin = 1 AND ativo = 1 AND id != ?",
            (exceto_id or 0,),
        ).fetchone()
        return row[0] == 0


def usuario_atual():
    usuario_id = session.get("uid")
    if not usuario_id:
        return None
    usuario = usuario_por_id(usuario_id)
    if not usuario or not usuario["ativo"]:
        session.clear()
        return None
    return usuario


def garantir_csrf():
    if not session.get("csrf"):
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def tema_atual():
    valor = configuracao_get("tema")
    return valor if valor in {"claro", "escuro"} else "claro"


def montar_menu(usuario, ativo=""):
    if not usuario:
        return ""
    csrf = html.escape(garantir_csrf())
    nome = html.escape(str(usuario["nome"]))

    def link(href, rotulo):
        ligado = ativo == href or (href != "/" and ativo.startswith(href))
        classe = ' class="ativo"' if ligado else ""
        return f'<a href="{href}"{classe}>{html.escape(rotulo)}</a>'

    links = [
        link("/", "Relatório"),
        link("/lancamentos", "Relatório de entradas e despesas manual"),
    ]
    if usuario["admin"]:
        links.extend(
            [
                link("/metas", "Meta de Despesas"),
                link("/usuarios", "Usuários"),
                link("/sincronismo", "Sincronismo"),
                link("/consulta", "Acesso SGF"),
            ]
        )
    links.extend(
        [
            link("/configuracoes", "Configurações"),
            link("/senha", "Senha"),
        ]
    )
    return (
        '<div class="menu-fundo" hidden></div>'
        '<aside class="menu" id="menuLateral">'
        '<p class="menu-marca">SGF</p>'
        f"<nav>{''.join(links)}</nav>"
        '<div class="menu-usuario">'
        f"<strong>{nome}</strong>"
        '<form method="post" action="/sair">'
        f'<input type="hidden" name="csrf" value="{csrf}">'
        '<button type="submit">Sair</button>'
        "</form></div></aside>"
    )


@app.context_processor
def injetar_shell():
    usuario = usuario_atual()
    return {
        "usuario_logado": usuario,
        "tema": tema_atual(),
        "menu_html": montar_menu(usuario, request.path) if usuario else "",
    }


def csrf_ok():
    enviado = request.form.get("csrf", "")
    return secrets.compare_digest(enviado, session.get("csrf", ""))


def gravar_json(caminho, dados):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_suffix(".tmp")
    temporario.write_text(json.dumps(dados), encoding="utf-8")
    os.replace(temporario, caminho)


def ler_json(caminho):
    if not caminho.exists():
        return {}
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dados if isinstance(dados, dict) else {}


def credenciais_sgf():
    dados = ler_json(CONTROLE / "sgf.json")
    usuario = str(dados.get("user") or os.environ.get("SGF_USER", "")).strip()
    return usuario, bool(dados.get("password") or os.environ.get("SGF_PASS"))


def salvar_credenciais_sgf(usuario, senha):
    atual = ler_json(CONTROLE / "sgf.json")
    if not senha:
        senha = atual.get("password") or os.environ.get("SGF_PASS", "")
    if not senha:
        return "Informe a senha do SGF."
    gravar_json(CONTROLE / "sgf.json", {"user": usuario, "password": senha})
    return ""


def intervalo_ambiente():
    try:
        valor = int(os.environ.get("SGF_A_CADA", "30"))
    except ValueError:
        valor = 30
    if INTERVALO_MIN <= valor <= INTERVALO_MAX:
        return valor
    return 30


def configuracao_get(chave):
    with conectar() as conn:
        row = conn.execute("SELECT valor FROM configuracao WHERE chave = ?", (chave,)).fetchone()
    if row is None:
        return None
    return row["valor"]


def configuracao_set(chave, valor):
    with conectar() as conn:
        conn.execute(
            """
            INSERT INTO configuracao (chave, valor) VALUES (?, ?)
            ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor
            """,
            (chave, str(valor)),
        )


def agenda_salva():
    bruto = configuracao_get("sync_intervalo_minutos")
    pausa = configuracao_get("sync_pausado")
    if bruto is None:
        return {"intervalo": intervalo_ambiente(), "pausado": False}
    try:
        intervalo = int(bruto)
    except ValueError:
        intervalo = intervalo_ambiente()
    if intervalo < INTERVALO_MIN or intervalo > INTERVALO_MAX:
        intervalo = intervalo_ambiente()
    return {"intervalo": intervalo, "pausado": str(pausa) == "1"}


def publicar_agenda(intervalo, pausado):
    gravar_json(
        CONTROLE / "agenda.json",
        {"intervaloMinutos": int(intervalo), "pausado": bool(pausado)},
    )


def pedir_sincronizacao(inicio="", fim=""):
    pedido_id = secrets.token_hex(8)
    gravar_json(
        CONTROLE / "pedido.json",
        {
            "id": pedido_id,
            "inicio": inicio,
            "fim": fim,
            "quando": datetime.now().astimezone().isoformat(timespec="seconds"),
        },
    )
    return {"ok": True, "id": pedido_id}


def resumo_sincronismo():
    estado = ler_json(CONTROLE / "estado.json")
    pedido = ler_json(CONTROLE / "pedido.json")
    feito = ler_json(CONTROLE / "feito.json")
    agenda = agenda_salva()
    gerado = str(dados_painel().get("geradoEm") or "")
    return {
        "ok": True,
        "geradoEm": gerado,
        "idade": idade_sync(gerado),
        "emAndamento": bool(estado.get("emAndamento")),
        "ultimoOk": estado.get("ultimoOk") if "ultimoOk" in estado else None,
        "ultimoErroEm": str(estado.get("ultimoErroEm") or ""),
        "proximoEm": str(estado.get("proximoEm") or ""),
        "pausado": agenda["pausado"],
        "intervaloMinutos": agenda["intervalo"],
        "pedidoId": str(pedido.get("id") or ""),
        "feitoId": str(feito.get("id") or ""),
        "feitoOk": feito.get("ok") if "ok" in feito else None,
    }


def dados_painel():
    if not PAINEL.exists():
        return {}
    texto = PAINEL.read_text(encoding="utf-8")
    marca = "const DADOS = "
    inicio = texto.find(marca)
    if inicio < 0:
        return {}
    try:
        dados, _ = json.JSONDecoder().raw_decode(texto[inicio + len(marca) :])
    except json.JSONDecodeError:
        return {}
    return dados if isinstance(dados, dict) else {}


def logo_painel():
    caminho = Path(__file__).resolve().parent / "marca.png"
    if not caminho.exists():
        return ""
    import base64

    return "data:image/png;base64," + base64.b64encode(caminho.read_bytes()).decode("ascii")


def montar_painel(dados):
    template = (Path(__file__).resolve().parent / "templates" / "painel.html").read_text(encoding="utf-8")
    return template.replace("__DADOS__", json.dumps(dados, ensure_ascii=False)).replace("__LOGO__", logo_painel())


def idade_sync(iso):
    texto = str(iso or "").strip()
    if not texto:
        return "sem sincronização"
    try:
        quando = datetime.fromisoformat(texto)
    except ValueError:
        return "sem sincronização"
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=FORTALEZA)
    agora = datetime.now(FORTALEZA)
    minutos = max(0, int((agora - quando).total_seconds())) // 60
    if minutos < 1:
        return "há menos de 1 min"
    if minutos < 60:
        return f"há {minutos} min"
    horas = minutos // 60
    if horas < 24:
        return f"há {horas} h"
    return f"há {horas // 24} d"


def periodo_formulario(inicio, fim):
    if not inicio and not fim:
        hoje = datetime.now()
        return hoje.replace(day=1).strftime("%Y-%m-%d"), hoje.strftime("%Y-%m-%d")
    try:
        ini = datetime.strptime(inicio, "%Y-%m-%d")
        final = datetime.strptime(fim, "%Y-%m-%d")
    except ValueError:
        return None
    if final < ini or (final - ini).days > 400:
        return None
    return inicio, fim


def bloco_atualizar(_csrf, gerado_em):
    return (
        '<div style="position:sticky;top:46px;z-index:19;display:flex;gap:10px;align-items:center;'
        'flex-wrap:wrap;padding:8px 16px;background:#f8fafc;border-bottom:1px solid #d7e0ec;'
        'font:14px/1.3 system-ui,Segoe UI,sans-serif">'
        '<button id="atualizar" type="button" style="border:1px solid #1d4ed8;background:#1d4ed8;color:#fff;'
        'border-radius:8px;padding:6px 12px;cursor:pointer;font-weight:700">Atualizar</button>'
        f'<span id="idadeSync">{html.escape(idade_sync(gerado_em))}</span>'
        '<span id="avisoSync"></span></div>'
    )


def script_sincronismo(csrf, gerado_em):
    return (
        "<script>(function(){"
        f"var gerado={json.dumps(gerado_em)};"
        f"var csrf={json.dumps(csrf)};"
        "var aviso=document.getElementById('avisoSync');"
        "var botao=document.getElementById('atualizar');"
        "var espera=null;"
        "function mostrar(texto){if(aviso)aviso.textContent=texto;}"
        "function idade(iso){if(!iso)return 'sem sincronização';"
        "var ms=Date.now()-new Date(String(iso).replace(' ','T')).getTime();"
        "if(!isFinite(ms))return 'sem sincronização';"
        "var minutos=Math.floor(Math.max(0,ms)/60000);"
        "if(minutos<1)return 'há menos de 1 min';"
        "if(minutos<60)return 'há '+minutos+' min';"
        "var horas=Math.floor(minutos/60);"
        "if(horas<24)return 'há '+horas+' h';"
        "return 'há '+Math.floor(horas/24)+' d';}"
        "function pintar(){var el=document.getElementById('idadeSync');if(el)el.textContent=idade(gerado);}"
        "pintar();setInterval(pintar,15000);"
        "function estado(){return fetch('/sync/status').then(function(resposta){return resposta.json();});}"
        "function aplicar(st){if(!st||!st.ok||(botao&&botao.disabled))return;"
        "if(st.emAndamento){mostrar('Sincronizando em segundo plano. A tela continua com o último sincronismo.');return;}"
        "if(st.geradoEm&&gerado&&st.geradoEm!==gerado){mostrar('Há um sincronismo novo. Recarregue a página para vê-lo.');return;}"
        "if(st.ultimoOk===false)mostrar('A última sincronização falhou. A tela continua com o último sincronismo concluído.');}"
        "estado().then(aplicar).catch(function(){});"
        "setInterval(function(){estado().then(aplicar).catch(function(){});},15000);"
        "if(!botao)return;"
        "botao.onclick=function(){"
        "if(espera)clearInterval(espera);"
        "botao.disabled=true;botao.textContent='Sincronizando...';"
        "mostrar('Sincronizando em segundo plano. A tela continua com o último sincronismo.');"
        "var corpo=new URLSearchParams();corpo.set('csrf',csrf);"
        "var pedido='';var comeco=Date.now();"
        "fetch('/atualizar',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:corpo.toString()})"
        ".then(function(resposta){return resposta.json();})"
        ".then(function(dados){"
        "if(!dados||!dados.ok){botao.disabled=false;botao.textContent='Atualizar';"
        "mostrar('Não foi possível sincronizar. A tela continua com o último sincronismo.');return;}"
        "pedido=dados.id||'';"
        "espera=setInterval(function(){"
        "estado().then(function(st){"
        "if(!st)return;"
        "if(st.geradoEm&&st.geradoEm!==gerado&&!st.emAndamento){clearInterval(espera);location.reload();return;}"
        "if(pedido&&st.feitoId===pedido&&st.feitoOk===false&&!st.emAndamento){"
        "clearInterval(espera);botao.disabled=false;botao.textContent='Atualizar';"
        "mostrar('Não foi possível sincronizar. A tela continua com o último sincronismo.');return;}"
        "if(Date.now()-comeco>180000){clearInterval(espera);botao.disabled=false;botao.textContent='Atualizar';"
        "mostrar('A sincronização continua em segundo plano. A tela segue com o último sincronismo.');}"
        "}).catch(function(){});},2000);})"
        ".catch(function(){botao.disabled=false;botao.textContent='Atualizar';"
        "mostrar('Não foi possível sincronizar. A tela continua com o último sincronismo.');});"
        "};})();</script>"
    )


def listar_metas():
    with conectar() as conn:
        linhas = conn.execute("SELECT tipo, categoria, valor FROM metas ORDER BY tipo, categoria").fetchall()
    return [{"tipo": item["tipo"], "categoria": item["categoria"], "valor": item["valor"]} for item in linhas]


def listar_metas_admin():
    with conectar() as conn:
        return conn.execute("SELECT * FROM metas ORDER BY tipo, categoria").fetchall()


def categorias_painel():
    dados = dados_painel()
    vistas = []
    chaves = set()
    for item in dados.get("linhas") or []:
        if item.get("atipico"):
            continue
        tipo = item.get("tipo")
        descricao = str(item.get("descricao") or "").strip()
        chave = (tipo, descricao)
        if tipo in {"receber", "pagar"} and descricao and chave not in chaves:
            chaves.add(chave)
            vistas.append({"tipo": tipo, "descricao": descricao})
    vistas.sort(key=lambda item: (item["tipo"], item["descricao"]))
    return vistas


CODIGO_ORIGEM = re.compile(r"^[0-9A-Za-z]{0,12}$")


FORTALEZA = timezone(timedelta(hours=-3))


def codigo_origem(codigo):
    texto = str(codigo or "").strip()
    if texto.isdigit():
        return str(int(texto))
    return texto


def nome_origem(codigo):
    normal = codigo_origem(codigo)
    if normal == "100":
        return "ADM | Central"
    if normal == "1":
        return "Matriz"
    if not normal:
        return "Sem filial"
    return f"Filial {normal}"


def texto_fortaleza(iso):
    texto = str(iso or "").strip()
    if not texto:
        return ""
    try:
        quando = datetime.fromisoformat(texto)
    except ValueError:
        return texto.replace("T", " ")
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=FORTALEZA)
    return quando.astimezone(FORTALEZA).strftime("%d/%m/%Y %H:%M") + " (−03:00, Fortaleza)"


def ordem_origem(codigo):
    if str(codigo).isdigit():
        return (0, int(codigo))
    return (1, str(codigo))


def decimal_br(valor):
    texto = f"{float(valor):,.2f}"
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")


def codigos_no_painel():
    codigos = set()
    for item in dados_painel().get("titulos") or []:
        codigo = str(item.get("filial") or "").strip()
        if CODIGO_ORIGEM.fullmatch(codigo):
            codigos.add(codigo)
    return codigos


def metas_origem_salvas():
    with conectar() as conn:
        linhas = conn.execute("SELECT origem, valor FROM metas_origem").fetchall()
    return {item["origem"]: item["valor"] for item in linhas}


def percentual_aviso():
    with conectar() as conn:
        row = conn.execute("SELECT valor FROM configuracao WHERE chave = ?", ("meta_percentual",)).fetchone()
    if not row:
        return None
    try:
        valor = float(str(row["valor"]).replace(",", "."))
    except ValueError:
        return None
    if valor < 1 or valor > 100:
        return None
    return valor


def texto_percentual(valor):
    if valor is None:
        return ""
    numero = float(valor)
    if numero.is_integer():
        return str(int(numero))
    return f"{numero:.1f}".replace(".", ",")


def filtrar_acesso(dados, permitidas):
    if permitidas is None:
        return dados
    base = dict(dados or {})
    titulos = []
    for item in base.get("titulos") or []:
        if isinstance(item, dict) and codigo_origem(item.get("filial")) in permitidas:
            titulos.append(item)
    linhas = []
    for item in base.get("linhas") or []:
        if not isinstance(item, dict) or not item.get("manual"):
            continue
        if codigo_origem(item.get("origem")) in permitidas:
            linhas.append(item)
    base["titulos"] = titulos
    base["linhas"] = linhas
    base["contas"] = []
    base["acessoLojas"] = sorted(permitidas, key=ordem_origem)
    return base


def linhas_formulario(textos=None, permitidas=None):
    salvas = metas_origem_salvas()
    presentes = codigos_no_painel()
    codigos = set(presentes) | set(salvas)
    if textos is not None:
        codigos |= set(textos)
    linhas = []
    for codigo in sorted(codigos, key=ordem_origem):
        if not CODIGO_ORIGEM.fullmatch(codigo):
            continue
        if textos is not None and codigo in textos:
            texto = textos[codigo]
        elif codigo in salvas:
            texto = decimal_br(salvas[codigo])
        else:
            texto = ""
        linhas.append(
            {
                "codigo": codigo,
                "nome": nome_origem(codigo),
                "texto": texto,
                "no_painel": codigo in presentes,
            }
        )
    if permitidas is not None:
        linhas = [item for item in linhas if codigo_origem(item["codigo"]) in permitidas]
    return linhas


def payload_metas_origem(permitidas=None):
    salvas = metas_origem_salvas()
    origens = sorted(salvas, key=ordem_origem)
    if permitidas is not None:
        origens = [origem for origem in origens if codigo_origem(origem) in permitidas]
    return {
        "percentual": percentual_aviso(),
        "itens": [{"origem": origem, "valor": salvas[origem]} for origem in origens],
    }


def ler_percentual_form(texto):
    bruto = (texto or "").strip().replace("%", "").replace(" ", "")
    if not bruto:
        return None, "Informe o percentual de aviso."
    if "," in bruto:
        bruto = bruto.replace(".", "").replace(",", ".")
    try:
        valor = round(float(bruto), 1)
    except ValueError:
        return None, "Informe o percentual de aviso entre 1 e 100."
    if valor < 1 or valor > 100:
        return None, "Informe o percentual de aviso entre 1 e 100."
    return valor, ""


def ler_meta_form(texto):
    if not (texto or "").strip():
        return None, ""
    valor = valor_br(texto)
    if valor is None:
        return None, "Informe a meta com um valor maior que zero ou deixe o campo em branco."
    return valor, ""


def valor_br(texto):
    texto = (texto or "").strip().replace("R$", "").strip()
    if not texto:
        return None
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        valor = round(float(texto), 2)
    except ValueError:
        return None
    if valor <= 0:
        return None
    return valor


def moeda_br(valor):
    texto = f"{float(valor):,.2f}"
    return "R$ " + texto.replace(",", "X").replace(".", ",").replace("X", ".")


def listar_lancamentos():
    with conectar() as conn:
        return conn.execute("SELECT * FROM lancamentos ORDER BY data DESC, id DESC").fetchall()


def origens_disponiveis():
    codigos = {"1", "100"}
    codigos |= {codigo_origem(item) for item in codigos_no_painel()}
    with conectar() as conn:
        for row in conn.execute("SELECT origem FROM lancamentos").fetchall():
            codigos.add(codigo_origem(row["origem"]))
    codigos.discard("")
    return [
        {"codigo": codigo, "nome": nome_origem(codigo)}
        for codigo in sorted(codigos, key=ordem_origem)
        if CODIGO_ORIGEM.fullmatch(codigo)
    ]


def incorporar_lancamentos(dados):
    base = dict(dados or {})
    linhas = list(base.get("linhas") or [])
    titulos = list(base.get("titulos") or [])
    for item in listar_lancamentos():
        origem = codigo_origem(item["origem"])
        nome = nome_origem(origem)
        descricao = f"{nome} · {item['descricao']}"
        valor = round(float(item["valor"]), 2)
        manual = {
            "data": item["data"],
            "centro": "manual",
            "descricao": descricao,
            "tipo": "receber" if item["tipo"] == "entrada" else "pagar",
            "status": "baixado",
            "valor": valor,
            "atipico": False,
            "manual": True,
            "origem": origem,
        }
        linhas.append(manual)
        if item["tipo"] == "despesa":
            titulos.append(
                {
                    "fornecedor": item["descricao"],
                    "documento": f"Manual {item['id']}",
                    "filial": origem,
                    "emissao": item["data"],
                    "vencimento": item["data"],
                    "pagamento": item["data"],
                    "valor": valor,
                    "pago": valor,
                    "juros": 0,
                    "obs": "Lançamento manual",
                    "atipico": False,
                    "manual": True,
                }
            )
    base["linhas"] = linhas
    base["titulos"] = titulos
    return base


def serie_lancamentos(lancamentos):
    nomes = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
    mapa = {}
    for item in lancamentos:
        chave = str(item["data"])[:7]
        grupo = mapa.setdefault(chave, {"entrada": 0.0, "despesa": 0.0})
        if item["tipo"] in grupo:
            grupo[item["tipo"]] += float(item["valor"])
    maior = 1.0
    for grupo in mapa.values():
        maior = max(maior, grupo["entrada"], grupo["despesa"])
    serie = []
    for chave in sorted(mapa):
        ano, mes = chave.split("-")
        grupo = mapa[chave]
        serie.append(
            {
                "rotulo": f"{nomes[int(mes) - 1]}/{ano[2:]}",
                "entrada": round(grupo["entrada"], 2),
                "despesa": round(grupo["despesa"], 2),
                "altura_entrada": round(grupo["entrada"] / maior * 100, 1),
                "altura_despesa": round(grupo["despesa"] / maior * 100, 1),
            }
        )
    return serie


def exigir_login():
    usuario = usuario_atual()
    if not usuario:
        return None, redirect("/login")
    return usuario, None


def senha_valida(senha, confirmar):
    if len(senha) < 8:
        return "A senha precisa ter pelo menos 8 caracteres."
    if senha != confirmar:
        return "A confirmação da senha não confere."
    return ""


def login_valido(login, proprio_id=None):
    if not LOGIN_RE.fullmatch(login):
        return "O usuário deve ter de 2 a 40 caracteres: letras minúsculas, números, ponto, _ ou -."
    existente = usuario_por_login(login)
    if existente and existente["id"] != proprio_id:
        return "Esse usuário já existe."
    return ""


@app.after_request
def sem_cache(resposta):
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


@app.get("/login")
def login_form():
    if usuario_atual():
        return redirect("/")
    with conectar() as conn:
        vazio = conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0
    erro = ""
    if vazio:
        erro = "O primeiro administrador ainda não foi criado. Defina GEFIN_ADMIN_USER e GEFIN_ADMIN_PASS no ambiente e reinicie."
    return render_template("login.html", erro=erro, login="", csrf=garantir_csrf())


@app.post("/login")
def login_post():
    if not csrf_ok():
        return redirect("/login")
    login = request.form.get("login", "").strip().lower()
    senha = request.form.get("senha", "")
    usuario = usuario_por_login(login)
    if not usuario or not usuario["ativo"] or not check_password_hash(usuario["senha_hash"], senha):
        return render_template(
            "login.html",
            erro="Usuário ou senha incorretos.",
            login=login,
            csrf=garantir_csrf(),
        )
    session.clear()
    session.permanent = True
    session["uid"] = usuario["id"]
    session["csrf"] = secrets.token_urlsafe(32)
    return redirect("/")


@app.post("/sair")
def sair():
    if csrf_ok():
        session.clear()
    return redirect("/login")


@app.get("/")
def painel():
    usuario, resposta = exigir_login()
    if resposta:
        return resposta
    bruto = dados_painel()
    dados = filtrar_acesso(incorporar_lancamentos(bruto), acesso_lojas(usuario))
    if bruto or dados.get("linhas") or dados.get("titulos"):
        conteudo = montar_painel(dados)
    elif not PAINEL.exists():
        conteudo = "<!DOCTYPE html><html lang='pt-BR'><body><p>O relatório ainda está sendo gerado. Use Atualizar para consultar o SGF.</p></body></html>"
    else:
        conteudo = PAINEL.read_text(encoding="utf-8")
    csrf = garantir_csrf()
    tem_menu = "__MENU__" in conteudo
    barra = (
        '<div style="position:sticky;top:0;z-index:20;display:flex;justify-content:space-between;'
        'gap:12px;align-items:center;padding:10px 16px;background:#102033;color:#fff;'
        'font:14px/1.3 system-ui,Segoe UI,sans-serif">'
        f"<span>{html.escape(usuario['nome'])}</span><span style=\"display:flex;gap:14px;align-items:center\">"
        + (
            '<a style="color:#fff" href="/usuarios">Usuários</a>'
            '<a style="color:#fff" href="/metas">Meta de Despesas</a>'
            '<a style="color:#fff" href="/sincronismo">Sincronismo</a>'
            '<a style="color:#fff" href="/consulta">Acesso SGF</a>'
            if usuario["admin"]
            else ""
        )
        + '<a style="color:#fff" href="/senha">Senha</a>'
        + '<form method="post" action="/sair" style="margin:0">'
        + f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
        + '<button style="border:1px solid #fff;background:transparent;color:#fff;border-radius:8px;padding:4px 10px;cursor:pointer" type="submit">Sair</button>'
        + "</form></span></div>"
    )
    if 'id="atualizar"' not in conteudo:
        barra += bloco_atualizar(csrf, str(dados_painel().get("geradoEm") or ""))
    conteudo = conteudo.replace("__METAS_ORIGEM__", json.dumps(payload_metas_origem(acesso_lojas(usuario)), ensure_ascii=False), 1)
    conteudo = conteudo.replace("__METAS__", json.dumps(listar_metas(), ensure_ascii=False), 1)
    conteudo = conteudo.replace('"__CSRF__"', json.dumps(csrf), 1)
    conteudo = conteudo.replace("__TEMA__", html.escape(tema_atual()), 1)
    conteudo = conteudo.replace("__MENU__", montar_menu(usuario, request.path), 1)
    if not tem_menu:
        if "<body>" in conteudo:
            conteudo = conteudo.replace("<body>", "<body>" + barra, 1)
        else:
            conteudo = barra + conteudo
    script = script_sincronismo(csrf, str(dados_painel().get("geradoEm") or ""))
    if "</body>" in conteudo:
        conteudo = conteudo.replace("</body>", script + "</body>", 1)
    else:
        conteudo += script
    return conteudo


@app.post("/atualizar")
def atualizar():
    _usuario, resposta = exigir_login()
    if resposta:
        return resposta
    if not csrf_ok():
        return jsonify({"ok": False}), 403
    if not AGUARDAR_SYNC:
        return jsonify({"ok": False})
    resultado = pedir_sincronizacao()
    return jsonify({"ok": True, "id": resultado["id"]})


@app.get("/sync/status")
def sync_status():
    if not usuario_atual():
        return jsonify({"ok": False}), 401
    return jsonify(resumo_sincronismo())


@app.get("/saidas")
def saidas():
    usuario, resposta = exigir_login()
    if resposta:
        return resposta
    inicio = request.args.get("inicio", "")
    fim = request.args.get("fim", "")
    periodo = periodo_formulario(inicio, fim)
    if not periodo or not inicio or not fim:
        return jsonify({"ok": False, "titulos": []}), 400
    visiveis = filtrar_acesso(incorporar_lancamentos(dados_painel()), acesso_lojas(usuario))
    titulos = []
    for item in visiveis.get("titulos") or []:
        if not isinstance(item, dict):
            continue
        vencimento = str(item.get("vencimento") or "")
        if periodo[0] <= vencimento <= periodo[1]:
            titulos.append(item)
    return jsonify({"ok": True, "titulos": titulos})


@app.route("/lancamentos", methods=["GET", "POST"])
def lancamentos():
    usuario, resposta = exigir_login()
    if resposta:
        return resposta
    erro = ""
    if request.method == "POST":
        if not csrf_ok():
            return redirect("/lancamentos")
        tipo = request.form.get("tipo", "")
        data = request.form.get("data", "")
        origem = codigo_origem(request.form.get("origem", ""))
        descricao = " ".join(request.form.get("descricao", "").split())
        valor = valor_br(request.form.get("valor", ""))
        permitidas = acesso_lojas(usuario)
        codigos = {item["codigo"] for item in origens_disponiveis()}
        if permitidas is not None:
            codigos &= permitidas
        if tipo not in {"entrada", "despesa"}:
            erro = "Escolha entrada ou despesa."
        elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", data or ""):
            erro = "Informe a data do lançamento."
        elif origem not in codigos:
            erro = "Informe a loja de origem."
        elif not descricao or len(descricao) > 120:
            erro = "Informe a descrição, com até 120 caracteres."
        elif valor is None:
            erro = "Informe um valor maior que zero."
        else:
            with conectar() as conn:
                conn.execute(
                    "INSERT INTO lancamentos (tipo, data, origem, descricao, valor, criado_em) VALUES (?, ?, ?, ?, ?, ?)",
                    (tipo, data, origem, descricao, valor, datetime.now(FORTALEZA).isoformat(timespec="seconds")),
                )
            session["aviso"] = "Lançamento manual salvo. Ele entra nos relatórios gerais."
            return redirect("/lancamentos")
    aviso = session.pop("aviso", "")
    permitidas = acesso_lojas(usuario)
    itens = [
        item
        for item in listar_lancamentos()
        if permitidas is None or codigo_origem(item["origem"]) in permitidas
    ]
    origens = origens_disponiveis()
    if permitidas is not None:
        origens = [item for item in origens if item["codigo"] in permitidas]
    return render_template(
        "lancamentos.html",
        erro=erro,
        aviso=aviso,
        itens=itens,
        origens=origens,
        serie=serie_lancamentos(itens),
        csrf=garantir_csrf(),
        nome_origem=nome_origem,
        moeda_br=moeda_br,
    )


@app.post("/lancamentos/<int:lancamento_id>/excluir")
def lancamentos_excluir(lancamento_id):
    usuario, resposta = exigir_login()
    if resposta:
        return resposta
    if csrf_ok():
        permitidas = acesso_lojas(usuario)
        with conectar() as conn:
            row = conn.execute("SELECT origem FROM lancamentos WHERE id = ?", (lancamento_id,)).fetchone()
            if row and (permitidas is None or codigo_origem(row["origem"]) in permitidas):
                conn.execute("DELETE FROM lancamentos WHERE id = ?", (lancamento_id,))
                session["aviso"] = "Lançamento manual excluído."
    return redirect("/lancamentos")


@app.route("/configuracoes", methods=["GET", "POST"])
def configuracoes():
    usuario, resposta = exigir_login()
    if resposta:
        return resposta
    erro = ""
    if request.method == "POST":
        if not csrf_ok():
            return redirect("/configuracoes")
        tema = request.form.get("tema", "")
        if tema not in {"claro", "escuro"}:
            erro = "Escolha o modo claro ou o modo escuro."
        else:
            configuracao_set("tema", tema)
            session["aviso"] = "Aparência salva."
            return redirect("/configuracoes")
    return render_template(
        "configuracoes.html",
        erro=erro,
        aviso=session.pop("aviso", ""),
        tema_escolhido=tema_atual(),
        csrf=garantir_csrf(),
        usuario_nome=usuario["nome"],
    )


@app.get("/usuarios")
def usuarios():
    usuario, resposta = exigir_login()
    if resposta:
        return resposta
    if not usuario["admin"]:
        return redirect("/")
    aviso = session.pop("aviso", "")
    usuarios = listar_usuarios()
    return render_template(
        "usuarios.html",
        usuarios=usuarios,
        acessos={item["id"]: texto_lojas(item["id"]) for item in usuarios},
        aviso=aviso,
        csrf=garantir_csrf(),
    )


@app.get("/usuarios/novo")
def usuario_novo():
    usuario, resposta = exigir_login()
    if resposta:
        return resposta
    if not usuario["admin"]:
        return redirect("/")
    return render_template(
        "usuario.html",
        novo=True,
        erro="",
        aviso="",
        nome="",
        login="",
        perfil="consulta",
        ativo=True,
        lojas=lojas_para_editar(usuario, set()),
        csrf=garantir_csrf(),
    )


@app.post("/usuarios/novo")
def usuario_novo_post():
    usuario, resposta = exigir_login()
    if resposta:
        return resposta
    if not usuario["admin"] or not csrf_ok():
        return redirect("/")
    nome = request.form.get("nome", "").strip()
    login = request.form.get("login", "").strip().lower()
    perfil = request.form.get("perfil", "consulta")
    senha = request.form.get("senha", "")
    confirmar = request.form.get("confirmar", "")
    escolhidas = lojas_escolhidas(usuario)
    erro = ""
    if not nome:
        erro = "Informe o nome."
    else:
        erro = login_valido(login) or senha_valida(senha, confirmar)
    if not erro and not escolhidas:
        erro = "Selecione ao menos uma loja."
    if erro:
        return render_template(
            "usuario.html",
            novo=True,
            erro=erro,
            aviso="",
            nome=nome,
            login=login,
            perfil=perfil,
            ativo=True,
            lojas=lojas_para_editar(usuario, escolhidas),
            csrf=garantir_csrf(),
        )
    with conectar() as conn:
        cursor = conn.execute(
            "INSERT INTO usuarios (login, nome, senha_hash, admin, ativo) VALUES (?, ?, ?, ?, 1)",
            (login, nome, generate_password_hash(senha), 1 if perfil == "admin" else 0),
        )
        novo_id = cursor.lastrowid
    salvar_lojas(novo_id, escolhidas)
    session["aviso"] = "Usuário cadastrado."
    return redirect("/usuarios")


@app.get("/usuarios/<int:usuario_id>")
def usuario_editar(usuario_id):
    usuario, resposta = exigir_login()
    if resposta:
        return resposta
    if not usuario["admin"]:
        return redirect("/")
    alvo = usuario_por_id(usuario_id)
    if not alvo:
        return redirect("/usuarios")
    return render_template(
        "usuario.html",
        novo=False,
        erro="",
        aviso=session.pop("aviso", ""),
        nome=alvo["nome"],
        login=alvo["login"],
        perfil="admin" if alvo["admin"] else "consulta",
        ativo=bool(alvo["ativo"]),
        lojas=lojas_para_editar(usuario, lojas_do_usuario(alvo["id"])),
        csrf=garantir_csrf(),
    )


@app.post("/usuarios/<int:usuario_id>")
def usuario_editar_post(usuario_id):
    usuario, resposta = exigir_login()
    if resposta:
        return resposta
    if not usuario["admin"] or not csrf_ok():
        return redirect("/")
    alvo = usuario_por_id(usuario_id)
    if not alvo:
        return redirect("/usuarios")
    nome = request.form.get("nome", "").strip()
    login = request.form.get("login", "").strip().lower()
    perfil = request.form.get("perfil", "consulta")
    ativo = request.form.get("ativo") == "on"
    senha = request.form.get("senha", "")
    confirmar = request.form.get("confirmar", "")
    atuais = lojas_do_usuario(alvo["id"])
    escolhidas = lojas_escolhidas(usuario, atuais)
    admin = perfil == "admin"
    erro = ""
    if not nome:
        erro = "Informe o nome."
    else:
        erro = login_valido(login, alvo["id"])
    if not erro and (senha or confirmar):
        erro = senha_valida(senha, confirmar)
    if not erro and alvo["admin"] and alvo["ativo"] and (not admin or not ativo) and ultimo_admin(alvo["id"]):
        erro = "É preciso manter pelo menos um administrador ativo."
    if not erro and not escolhidas and not (atuais - lojas_editor(usuario)):
        erro = "Selecione ao menos uma loja."
    if erro:
        return render_template(
            "usuario.html",
            novo=False,
            erro=erro,
            aviso="",
            nome=nome,
            login=login,
            perfil=perfil,
            ativo=ativo,
            lojas=lojas_para_editar(usuario, escolhidas or atuais),
            csrf=garantir_csrf(),
        )
    with conectar() as conn:
        conn.execute(
            "UPDATE usuarios SET login = ?, nome = ?, admin = ?, ativo = ? WHERE id = ?",
            (login, nome, 1 if admin else 0, 1 if ativo else 0, alvo["id"]),
        )
        if senha:
            conn.execute(
                "UPDATE usuarios SET senha_hash = ? WHERE id = ?",
                (generate_password_hash(senha), alvo["id"]),
            )
    editor = lojas_editor(usuario)
    finais = escolhidas if editor is None else (escolhidas & editor) | (atuais - editor)
    salvar_lojas(alvo["id"], finais)
    session["aviso"] = "Usuário atualizado."
    return redirect(f"/usuarios/{alvo['id']}")


@app.get("/consulta")
def consulta_form():
    usuario, resposta = exigir_login()
    if resposta:
        return resposta
    if not usuario["admin"]:
        return redirect("/")
    usuario_sgf, _senha_definida = credenciais_sgf()
    return render_template(
        "consulta.html",
        erro="",
        aviso=session.pop("aviso", ""),
        usuario_sgf=usuario_sgf,
        csrf=garantir_csrf(),
    )


@app.post("/consulta")
def consulta_post():
    usuario, resposta = exigir_login()
    if resposta:
        return resposta
    if not usuario["admin"] or not csrf_ok():
        return redirect("/")
    usuario_sgf = request.form.get("usuario", "").strip()
    senha = request.form.get("senha", "")
    confirmar = request.form.get("confirmar", "")
    erro = ""
    if not usuario_sgf or len(usuario_sgf) > 40 or any(c in usuario_sgf for c in "\r\n\t"):
        erro = "Informe o usuário do SGF."
    elif senha or confirmar:
        if senha != confirmar:
            erro = "A confirmação da senha não confere."
        elif not senha.strip():
            erro = "Informe a senha do SGF."
    if not erro:
        erro = salvar_credenciais_sgf(usuario_sgf, senha)
    if erro:
        return render_template(
            "consulta.html",
            erro=erro,
            aviso="",
            usuario_sgf=usuario_sgf,
            csrf=garantir_csrf(),
        )
    session["aviso"] = "Acesso da consulta atualizado. A próxima abertura do relatório usa esse login."
    return redirect("/consulta")


def ler_intervalo_form(texto):
    texto = str(texto or "").strip()
    if not texto.isdigit():
        return None, "Informe o intervalo em minutos."
    valor = int(texto)
    if valor < INTERVALO_MIN or valor > INTERVALO_MAX:
        return None, f"O intervalo fica entre {INTERVALO_MIN} e {INTERVALO_MAX} minutos."
    return valor, ""


@app.get("/sincronismo")
def sincronismo_form():
    usuario, resposta = exigir_login()
    if resposta:
        return resposta
    if not usuario["admin"]:
        return redirect("/")
    resumo = resumo_sincronismo()
    return render_template(
        "sincronismo.html",
        erro="",
        aviso=session.pop("aviso", ""),
        intervalo=resumo["intervaloMinutos"],
        pausado=resumo["pausado"],
        idade=resumo["idade"],
        gerado_em=texto_fortaleza(resumo["geradoEm"]),
        em_andamento=resumo["emAndamento"],
        ultimo_ok=resumo["ultimoOk"],
        ultimo_erro=texto_fortaleza(resumo["ultimoErroEm"]),
        proximo=texto_fortaleza(resumo["proximoEm"]),
        csrf=garantir_csrf(),
    )


@app.post("/sincronismo")
def sincronismo_post():
    usuario, resposta = exigir_login()
    if resposta:
        return resposta
    if not usuario["admin"] or not csrf_ok():
        return redirect("/")
    intervalo, erro = ler_intervalo_form(request.form.get("intervalo", ""))
    pausado = request.form.get("pausado") == "on"
    resumo = resumo_sincronismo()
    if erro:
        return render_template(
            "sincronismo.html",
            erro=erro,
            aviso="",
            intervalo=request.form.get("intervalo", ""),
            pausado=pausado,
            idade=resumo["idade"],
            gerado_em=texto_fortaleza(resumo["geradoEm"]),
            em_andamento=resumo["emAndamento"],
            ultimo_ok=resumo["ultimoOk"],
            ultimo_erro=texto_fortaleza(resumo["ultimoErroEm"]),
            proximo=texto_fortaleza(resumo["proximoEm"]),
            csrf=garantir_csrf(),
        )
    configuracao_set("sync_intervalo_minutos", intervalo)
    configuracao_set("sync_pausado", "1" if pausado else "0")
    publicar_agenda(intervalo, pausado)
    session["aviso"] = "Agendamento salvo. O coletor usa o novo intervalo na próxima volta."
    return redirect("/sincronismo")


@app.get("/metas")
def metas_form():
    usuario, resposta = exigir_login()
    if resposta:
        return resposta
    if not usuario["admin"]:
        return redirect("/")
    return render_template(
        "metas.html",
        erro="",
        aviso=session.pop("aviso", ""),
        linhas=linhas_formulario(permitidas=acesso_lojas(usuario)),
        percentual=texto_percentual(percentual_aviso()),
        csrf=garantir_csrf(),
    )


@app.post("/metas")
def metas_post():
    usuario, resposta = exigir_login()
    if resposta:
        return resposta
    if not usuario["admin"] or not csrf_ok():
        return redirect("/")
    percentual, erro = ler_percentual_form(request.form.get("percentual", ""))
    origens = request.form.getlist("origem")
    valores = request.form.getlist("valor")
    textos = {}
    pares = []
    if len(origens) != len(valores):
        erro = erro or "Não foi possível ler as metas."
    else:
        vistos = set()
        permitidas = acesso_lojas(usuario)
        conhecidas = {codigo_origem(codigo) for codigo in codigos_no_painel()} | {
            codigo_origem(codigo) for codigo in metas_origem_salvas()
        }
        for codigo, texto in zip(origens, valores):
            codigo = codigo_origem(codigo.strip())
            textos[codigo] = texto
            if codigo in vistos:
                erro = erro or "Origem repetida."
                continue
            vistos.add(codigo)
            if not CODIGO_ORIGEM.fullmatch(codigo) or codigo not in conhecidas:
                erro = erro or "Origem desconhecida."
                continue
            if permitidas is not None and codigo not in permitidas:
                erro = erro or "Origem desconhecida."
                continue
            valor, falha = ler_meta_form(texto)
            if falha:
                erro = erro or falha
                continue
            pares.append((codigo, valor))
    if erro:
        return render_template(
            "metas.html",
            erro=erro,
            aviso="",
            linhas=linhas_formulario(textos, acesso_lojas(usuario)),
            percentual=request.form.get("percentual", ""),
            csrf=garantir_csrf(),
        )
    with conectar() as conn:
        conn.execute(
            """
            INSERT INTO configuracao (chave, valor) VALUES ('meta_percentual', ?)
            ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor
            """,
            (str(percentual),),
        )
        for codigo, valor in pares:
            if valor is None:
                conn.execute("DELETE FROM metas_origem WHERE origem = ?", (codigo,))
            else:
                conn.execute(
                    """
                    INSERT INTO metas_origem (origem, valor) VALUES (?, ?)
                    ON CONFLICT(origem) DO UPDATE SET valor = excluded.valor
                    """,
                    (codigo, valor),
                )
    session["aviso"] = "Meta de despesas salva."
    return redirect("/metas")


@app.post("/metas/<int:meta_id>/excluir")
def metas_excluir(meta_id):
    usuario, resposta = exigir_login()
    if resposta:
        return resposta
    if not usuario["admin"] or not csrf_ok():
        return redirect("/")
    with conectar() as conn:
        conn.execute("DELETE FROM metas WHERE id = ?", (meta_id,))
    session["aviso"] = "Meta excluída."
    return redirect("/metas")


@app.get("/senha")
def senha_form():
    usuario, resposta = exigir_login()
    if resposta:
        return resposta
    return render_template(
        "senha.html",
        erro="",
        aviso=session.pop("aviso", ""),
        admin=bool(usuario["admin"]),
        csrf=garantir_csrf(),
    )


@app.post("/senha")
def senha_post():
    usuario, resposta = exigir_login()
    if resposta:
        return resposta
    if not csrf_ok():
        return redirect("/senha")
    atual = request.form.get("atual", "")
    senha = request.form.get("senha", "")
    confirmar = request.form.get("confirmar", "")
    erro = ""
    if not check_password_hash(usuario["senha_hash"], atual):
        erro = "A senha atual não confere."
    else:
        erro = senha_valida(senha, confirmar)
    if erro:
        return render_template(
            "senha.html",
            erro=erro,
            aviso="",
            admin=bool(usuario["admin"]),
            csrf=garantir_csrf(),
        )
    with conectar() as conn:
        conn.execute(
            "UPDATE usuarios SET senha_hash = ? WHERE id = ?",
            (generate_password_hash(senha), usuario["id"]),
        )
    session["aviso"] = "Senha alterada."
    return redirect("/senha")


def pasta_backup():
    texto = os.environ.get("GEFIN_BACKUP_DIR", "").strip()
    if not texto:
        return None
    pasta = Path(texto)
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def limpar_backups_antigos(pasta, dias):
    limite = datetime.now(FORTALEZA).date() - timedelta(days=dias)
    for caminho in pasta.iterdir():
        nome = caminho.name
        if not (nome.startswith("sgf-usuarios-") or nome.startswith("sgf-painel-")):
            continue
        achado = re.search(r"(\d{4}-\d{2}-\d{2})", nome)
        if not achado:
            continue
        try:
            dia = datetime.strptime(achado.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if dia < limite:
            caminho.unlink()


def gravar_backup():
    pasta = pasta_backup()
    if pasta is None:
        return None
    dia = datetime.now(FORTALEZA).date().isoformat()
    destino = pasta / f"sgf-usuarios-{dia}.db"
    if not (destino.exists() and destino.stat().st_size > 0):
        temporario = pasta / f".sgf-usuarios-{dia}.db.tmp"
        if temporario.exists():
            temporario.unlink()
        origem = sqlite3.connect(DB, timeout=30)
        copia = sqlite3.connect(temporario)
        try:
            origem.backup(copia)
        finally:
            copia.close()
            origem.close()
        os.chmod(temporario, 0o600)
        temporario.replace(destino)
    painel_destino = pasta / f"sgf-painel-{dia}.html"
    if PAINEL.exists() and not (painel_destino.exists() and painel_destino.stat().st_size > 0):
        shutil.copyfile(PAINEL, painel_destino)
        os.chmod(painel_destino, 0o600)
    limpar_backups_antigos(pasta, 30)
    return destino


def segundos_ate_backup():
    agora = datetime.now(FORTALEZA)
    alvo = agora.replace(hour=3, minute=0, second=0, microsecond=0)
    if alvo <= agora:
        alvo += timedelta(days=1)
    return max(60, (alvo - agora).total_seconds())


def ciclo_backup():
    while True:
        try:
            gravar_backup()
        except Exception:
            logging.getLogger("sgf").exception("Falha no backup diário do SGF")
            time.sleep(15 * 60)
            continue
        time.sleep(segundos_ate_backup())


def iniciar_backup():
    if not os.environ.get("GEFIN_BACKUP_DIR", "").strip():
        return
    if getattr(iniciar_backup, "ativo", False):
        return
    iniciar_backup.ativo = True
    threading.Thread(target=ciclo_backup, name="backup-sgf", daemon=True).start()


preparar()
iniciar_backup()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("GEFIN_PORT", "8096")))
