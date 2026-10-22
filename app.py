import html
import os
import re
import secrets
import sqlite3
from pathlib import Path

from flask import Flask, redirect, render_template, request, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

PAINEL = Path(os.environ.get("GEFIN_PAINEL", Path(__file__).with_name("painel_sgf.html")))
DB = Path(os.environ.get("GEFIN_DB", Path(__file__).with_name("usuarios.db")))
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
        conteudo = "<!DOCTYPE html><html lang='pt-BR'><body><p>O relatório ainda está sendo gerado.</p></body></html>"
    else:
        conteudo = PAINEL.read_text(encoding="utf-8")
    barra = (
        '<div style="position:sticky;top:0;z-index:20;display:flex;justify-content:space-between;'
        'gap:12px;align-items:center;padding:10px 16px;background:#102033;color:#fff;'
        'font:14px/1.3 system-ui,Segoe UI,sans-serif">'
        f"<span>{html.escape(usuario['nome'])}</span><span style=\"display:flex;gap:14px;align-items:center\">"
        + ('<a style="color:#fff" href="/usuarios">Usuários</a>' if usuario["admin"] else "")
        + '<a style="color:#fff" href="/senha">Senha</a>'
        + '<form method="post" action="/sair" style="margin:0">'
        + f'<input type="hidden" name="csrf" value="{html.escape(garantir_csrf())}">'
        + '<button style="border:1px solid #fff;background:transparent;color:#fff;border-radius:8px;padding:4px 10px;cursor:pointer" type="submit">Sair</button>'
        + "</form></span></div>"
    )
    if "<body>" in conteudo:
        conteudo = conteudo.replace("<body>", "<body>" + barra, 1)
    else:
        conteudo = barra + conteudo
    return conteudo


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
