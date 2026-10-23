import html
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

PAINEL = Path(os.environ.get("GEFIN_PAINEL", Path(__file__).with_name("painel_sgf.html")))
DB = Path(os.environ.get("GEFIN_DB", Path(__file__).with_name("usuarios.db")))
CONTROLE = Path(os.environ.get("GEFIN_CONTROLE", Path(__file__).with_name("controle")))
AGUARDAR_SYNC = os.environ.get("GEFIN_AGUARDAR_SYNC") == "1"
SYNC_TIMEOUT = int(os.environ.get("GEFIN_SYNC_TIMEOUT", "150"))
LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")

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
        total = conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
        if total == 0:
            login = os.environ.get("GEFIN_ADMIN_USER", "").strip().lower()
            senha = os.environ.get("GEFIN_ADMIN_PASS", "")
            if LOGIN_RE.fullmatch(login or "") and len(senha) >= 8:
                conn.execute(
                    "INSERT INTO usuarios (login, nome, senha_hash, admin, ativo) VALUES (?, ?, ?, 1, 1)",
                    (login, "Administrador", generate_password_hash(senha)),
                )


def conectar():
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def usuario_por_id(usuario_id):
    with conectar() as conn:
        return conn.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()


def usuario_por_login(login):
    with conectar() as conn:
        return conn.execute("SELECT * FROM usuarios WHERE login = ?", (login,)).fetchone()


def listar_usuarios():
    with conectar() as conn:
        return conn.execute("SELECT * FROM usuarios ORDER BY nome, login").fetchall()


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


def pedir_sincronizacao(inicio="", fim=""):
    if not AGUARDAR_SYNC:
        return {"ok": True}
    antes = PAINEL.stat().st_mtime_ns if PAINEL.exists() else 0
    pedido_id = secrets.token_hex(8)
    gravar_json(CONTROLE / "pedido.json", {"id": pedido_id, "inicio": inicio, "fim": fim})
    limite = time.time() + SYNC_TIMEOUT
    feito_caminho = CONTROLE / "feito.json"
    while time.time() < limite:
        feito = ler_json(feito_caminho)
        if feito.get("id") == pedido_id:
            return feito
        if PAINEL.exists() and PAINEL.stat().st_mtime_ns > antes and feito.get("ok"):
            return feito
        time.sleep(0.4)
    return {"ok": False}


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


def idade_sync(iso):
    texto = str(iso or "").strip()
    if not texto:
        return "sem sincronização"
    try:
        quando = datetime.fromisoformat(texto)
    except ValueError:
        return "sem sincronização"
    agora = datetime.now(quando.tzinfo) if quando.tzinfo else datetime.now()
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


def bloco_atualizar(csrf, gerado_em):
    return (
        '<div style="position:sticky;top:46px;z-index:19;display:flex;gap:10px;align-items:center;'
        'flex-wrap:wrap;padding:8px 16px;background:#f8fafc;border-bottom:1px solid #d7e0ec;'
        'font:14px/1.3 system-ui,Segoe UI,sans-serif">'
        '<button id="atualizar" type="button" style="border:1px solid #1d4ed8;background:#1d4ed8;color:#fff;'
        'border-radius:8px;padding:6px 12px;cursor:pointer;font-weight:700">Atualizar</button>'
        f'<span id="idadeSync" data-quando="{html.escape(gerado_em, quote=True)}">{html.escape(idade_sync(gerado_em))}</span>'
        '<span id="avisoSync"></span></div>'
        "<script>(function(){"
        f"var quando={json.dumps(gerado_em)};"
        "function textoIdade(iso){if(!iso)return 'sem sincronização';"
        "var ms=Date.now()-new Date(String(iso).replace(' ','T')).getTime();"
        "if(!isFinite(ms))return 'sem sincronização';"
        "var minutos=Math.floor(Math.max(0,ms)/60000);"
        "if(minutos<1)return 'há menos de 1 min';"
        "if(minutos<60)return 'há '+minutos+' min';"
        "var horas=Math.floor(minutos/60);"
        "if(horas<24)return 'há '+horas+' h';"
        "return 'há '+Math.floor(horas/24)+' d';}"
        "var el=document.getElementById('idadeSync');"
        "function pintar(){if(el)el.textContent=textoIdade(quando);}"
        "pintar();setInterval(pintar,15000);"
        "var botao=document.getElementById('atualizar');"
        "if(!botao)return;"
        "botao.addEventListener('click',function(){"
        "botao.disabled=true;botao.textContent='Sincronizando...';"
        "var aviso=document.getElementById('avisoSync');if(aviso)aviso.textContent='';"
        "var corpo=new URLSearchParams();"
        f"corpo.set('csrf',{json.dumps(csrf)});"
        "var inicio=document.getElementById('inicio');var fim=document.getElementById('fim');"
        "if(inicio&&fim){corpo.set('inicio',inicio.value);corpo.set('fim',fim.value);}"
        "fetch('/atualizar',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:corpo.toString()})"
        ".then(function(resposta){return resposta.json();})"
        ".then(function(dados){if(dados&&dados.ok){location.reload();return;}"
        "botao.disabled=false;botao.textContent='Atualizar';"
        "if(aviso)aviso.textContent='Não foi possível sincronizar. A tela continua com a última consulta concluída.';})"
        ".catch(function(){botao.disabled=false;botao.textContent='Atualizar';"
        "if(aviso)aviso.textContent='Não foi possível sincronizar. A tela continua com a última consulta concluída.';});"
        "});})();</script>"
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


def nome_origem(codigo):
    if codigo == "100":
        return "ADM | Central"
    if not codigo:
        return "Sem filial"
    return f"Filial {codigo}"


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


def linhas_formulario(textos=None):
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
    return linhas


def payload_metas_origem():
    salvas = metas_origem_salvas()
    return {
        "percentual": percentual_aviso(),
        "itens": [{"origem": origem, "valor": salvas[origem]} for origem in sorted(salvas, key=ordem_origem)],
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
    if not PAINEL.exists():
        conteudo = "<!DOCTYPE html><html lang='pt-BR'><body><p>O relatório ainda está sendo gerado. Use Atualizar para consultar o SGF.</p></body></html>"
    else:
        conteudo = PAINEL.read_text(encoding="utf-8")
    csrf = garantir_csrf()
    barra = (
        '<div style="position:sticky;top:0;z-index:20;display:flex;justify-content:space-between;'
        'gap:12px;align-items:center;padding:10px 16px;background:#102033;color:#fff;'
        'font:14px/1.3 system-ui,Segoe UI,sans-serif">'
        f"<span>{html.escape(usuario['nome'])}</span><span style=\"display:flex;gap:14px;align-items:center\">"
        + ('<a style="color:#fff" href="/usuarios">Usuários</a><a style="color:#fff" href="/metas">Metas</a><a style="color:#fff" href="/consulta">Acesso SGF</a>' if usuario["admin"] else "")
        + '<a style="color:#fff" href="/senha">Senha</a>'
        + '<form method="post" action="/sair" style="margin:0">'
        + f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
        + '<button style="border:1px solid #fff;background:transparent;color:#fff;border-radius:8px;padding:4px 10px;cursor:pointer" type="submit">Sair</button>'
        + "</form></span></div>"
    )
    if 'id="atualizar"' not in conteudo:
        barra += bloco_atualizar(csrf, str(dados_painel().get("geradoEm") or ""))
    conteudo = conteudo.replace("__METAS_ORIGEM__", json.dumps(payload_metas_origem(), ensure_ascii=False), 1)
    conteudo = conteudo.replace("__METAS__", json.dumps(listar_metas(), ensure_ascii=False), 1)
    conteudo = conteudo.replace('"__CSRF__"', json.dumps(csrf), 1)
    if "<body>" in conteudo:
        conteudo = conteudo.replace("<body>", "<body>" + barra, 1)
    else:
        conteudo = barra + conteudo
    return conteudo


@app.post("/atualizar")
def atualizar():
    _usuario, resposta = exigir_login()
    if resposta:
        return resposta
    if not csrf_ok():
        return jsonify({"ok": False}), 403
    periodo = periodo_formulario(request.form.get("inicio", ""), request.form.get("fim", ""))
    if not periodo:
        return jsonify({"ok": False}), 400
    if not AGUARDAR_SYNC:
        return jsonify({"ok": False})
    resultado = pedir_sincronizacao(periodo[0], periodo[1])
    return jsonify({"ok": bool(resultado.get("ok"))})


@app.get("/saidas")
def saidas():
    _usuario, resposta = exigir_login()
    if resposta:
        return resposta
    inicio = request.args.get("inicio", "")
    fim = request.args.get("fim", "")
    periodo = periodo_formulario(inicio, fim)
    if not periodo or not inicio or not fim:
        return jsonify({"ok": False, "titulos": []}), 400
    titulos = []
    for item in dados_painel().get("titulos") or []:
        if not isinstance(item, dict):
            continue
        vencimento = str(item.get("vencimento") or "")
        if periodo[0] <= vencimento <= periodo[1]:
            titulos.append(item)
    return jsonify({"ok": True, "titulos": titulos})


@app.get("/usuarios")
def usuarios():
    usuario, resposta = exigir_login()
    if resposta:
        return resposta
    if not usuario["admin"]:
        return redirect("/")
    aviso = session.pop("aviso", "")
    return render_template("usuarios.html", usuarios=listar_usuarios(), aviso=aviso, csrf=garantir_csrf())


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
    erro = ""
    if not nome:
        erro = "Informe o nome."
    else:
        erro = login_valido(login) or senha_valida(senha, confirmar)
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
            csrf=garantir_csrf(),
        )
    with conectar() as conn:
        conn.execute(
            "INSERT INTO usuarios (login, nome, senha_hash, admin, ativo) VALUES (?, ?, ?, ?, 1)",
            (login, nome, generate_password_hash(senha), 1 if perfil == "admin" else 0),
        )
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
        linhas=linhas_formulario(),
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
        conhecidas = codigos_no_painel() | set(metas_origem_salvas())
        for codigo, texto in zip(origens, valores):
            codigo = codigo.strip()
            textos[codigo] = texto
            if codigo in vistos:
                erro = erro or "Origem repetida."
                continue
            vistos.add(codigo)
            if not CODIGO_ORIGEM.fullmatch(codigo) or codigo not in conhecidas:
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
            linhas=linhas_formulario(textos),
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
    session["aviso"] = "Metas salvas."
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


preparar()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("GEFIN_PORT", "8096")))
