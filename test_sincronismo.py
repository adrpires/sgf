import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from datetime import date, timedelta
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="sgf-sync-"))
os.environ["GEFIN_DB"] = str(RAIZ / "usuarios.db")
os.environ["GEFIN_CONTROLE"] = str(RAIZ / "controle")
os.environ["GEFIN_PAINEL"] = str(RAIZ / "painel_sgf.html")
os.environ["GEFIN_SECRET"] = "segredo-de-teste"
os.environ["GEFIN_AGUARDAR_SYNC"] = "1"
os.environ["GEFIN_ADMIN_USER"] = "admin"
os.environ["GEFIN_ADMIN_PASS"] = "senha1234"
os.environ["SGF_A_CADA"] = "30"
os.environ["SGF_CONTROLE"] = os.environ["GEFIN_CONTROLE"]
os.environ["SGF_OUT"] = os.environ["GEFIN_PAINEL"]

if "playwright" not in sys.modules:
    playwright = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: None
    sys.modules["playwright"] = playwright
    sys.modules["playwright.sync_api"] = sync_api

import app as aplicacao
import sgf_base
import sync_sgf
from werkzeug.security import generate_password_hash

PAINEL = """<!DOCTYPE html>
<html lang="pt-BR"><body>
<button id="atualizar" type="button">Atualizar</button>
<input id="inicio" value="2026-11-01">
<input id="fim" value="2026-11-07">
<span id="idadeSync"></span>
<p id="avisoSync"></p>
<script>const DADOS = {"geradoEm":"2026-11-07T10:00:00-03:00","titulos":[],"linhas":[],"contas":[],"filial":"Teste"};</script>
</body></html>
"""
CONTROLE = Path(os.environ["GEFIN_CONTROLE"])
PAINEL_CAMINHO = Path(os.environ["GEFIN_PAINEL"])


def csrf_de(html):
    achado = re.search(r'name="csrf" value="([^"]+)"', html)
    if not achado:
        raise AssertionError("csrf ausente")
    return achado.group(1)


class Sincronismo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if aplicacao.usuario_por_login("consulta") is None:
            with aplicacao.conectar() as conn:
                conn.execute(
                    "INSERT INTO usuarios (login, nome, senha_hash, admin, ativo) VALUES (?, ?, ?, 0, 1)",
                    ("consulta", "Consulta", generate_password_hash("senha1234")),
                )

    def setUp(self):
        CONTROLE.mkdir(parents=True, exist_ok=True)
        for nome in ("pedido.json", "feito.json", "estado.json", "sgf.db", "sgf.db-wal", "sgf.db-shm"):
            caminho = CONTROLE / nome
            if caminho.exists():
                caminho.unlink()
        sync_sgf._estado_assinatura = None
        aplicacao.configuracao_set("sync_intervalo_minutos", "30")
        aplicacao.configuracao_set("sync_pausado", "0")
        aplicacao.publicar_agenda(30, False)
        PAINEL_CAMINHO.write_text(PAINEL, encoding="utf-8")
        os.utime(PAINEL_CAMINHO, None)
        self.assertTrue(sync_sgf.painel_valido())

    def entrar(self, login="admin", senha="senha1234"):
        cliente = aplicacao.app.test_client()
        pagina = cliente.get("/login")
        token = csrf_de(pagina.get_data(as_text=True))
        resposta = cliente.post(
            "/login",
            data={"csrf": token, "login": login, "senha": senha},
            follow_redirects=True,
        )
        return cliente, resposta

    def test_login_e_refresh_usam_o_ultimo_painel(self):
        antes = PAINEL_CAMINHO.stat().st_mtime_ns
        cliente, resposta = self.entrar()
        html = resposta.get_data(as_text=True)
        self.assertEqual(resposta.status_code, 200)
        self.assertIn("Administrador", html)
        self.assertIn("/sync/status", html)
        self.assertLess(html.find("botao.onclick"), html.find("fetch('/atualizar'"))
        self.assertEqual(PAINEL_CAMINHO.stat().st_mtime_ns, antes)
        self.assertFalse((CONTROLE / "pedido.json").exists())
        anonimo = aplicacao.app.test_client()
        pagina_login = anonimo.get("/login")
        self.assertIn("Ao entrar, o painel abre com o último sincronismo.", pagina_login.get_data(as_text=True))
        de_novo = cliente.get("/")
        self.assertEqual(de_novo.status_code, 200)
        self.assertEqual(PAINEL_CAMINHO.stat().st_mtime_ns, antes)
        self.assertFalse((CONTROLE / "pedido.json").exists())
        estado = cliente.get("/sync/status")
        self.assertEqual(estado.status_code, 200)
        self.assertEqual(estado.get_json()["geradoEm"], "2026-11-07T10:00:00-03:00")
        self.assertFalse((CONTROLE / "pedido.json").exists())

    def test_atualizar_so_enfileira(self):
        cliente, resposta = self.entrar()
        token = csrf_de(resposta.get_data(as_text=True))
        antes = PAINEL_CAMINHO.stat().st_mtime_ns
        inicio = time.perf_counter()
        pedido = cliente.post(
            "/atualizar",
            data={"csrf": token, "inicio": "2026-11-01", "fim": "2026-11-07"},
        )
        duracao = time.perf_counter() - inicio
        self.assertLess(duracao, 1)
        corpo = pedido.get_json()
        self.assertTrue(corpo["ok"])
        self.assertTrue(corpo["id"])
        gravado = json.loads((CONTROLE / "pedido.json").read_text(encoding="utf-8"))
        self.assertEqual(gravado["id"], corpo["id"])
        self.assertEqual(gravado["inicio"], "")
        self.assertEqual(gravado["fim"], "")
        self.assertEqual(PAINEL_CAMINHO.stat().st_mtime_ns, antes)

    def test_admin_grava_agenda_e_consulta_nao_altera(self):
        cliente, resposta = self.entrar()
        token = csrf_de(resposta.get_data(as_text=True))
        pagina = cliente.get("/sincronismo")
        self.assertEqual(pagina.status_code, 200)
        self.assertIn("Sincronismo", pagina.get_data(as_text=True))
        salvo = cliente.post(
            "/sincronismo",
            data={"csrf": token, "intervalo": "15", "pausado": "on"},
            follow_redirects=True,
        )
        self.assertIn("Agendamento salvo", salvo.get_data(as_text=True))
        agenda = json.loads((CONTROLE / "agenda.json").read_text(encoding="utf-8"))
        self.assertEqual(agenda, {"intervaloMinutos": 15, "pausado": True})
        self.assertEqual(sync_sgf.ler_agenda(30), (15, True))
        invalido = cliente.post("/sincronismo", data={"csrf": token, "intervalo": "2"})
        self.assertIn("entre 5 e 1440", invalido.get_data(as_text=True))
        self.assertEqual(json.loads((CONTROLE / "agenda.json").read_text(encoding="utf-8"))["intervaloMinutos"], 15)
        consulta, _ = self.entrar("consulta")
        bloqueio = consulta.get("/sincronismo")
        self.assertEqual(bloqueio.status_code, 302)
        self.assertEqual(bloqueio.headers["Location"], "/")

    def test_painel_recente_nao_consulta_de_novo(self):
        ultimo, _fim, motivo = sync_sgf.passo_agenda(
            None, "http://exemplo", 30, "", PAINEL_CAMINHO.stat().st_mtime
        )
        self.assertEqual(motivo, "")
        self.assertEqual(ultimo, "")
        self.assertFalse((CONTROLE / "pedido.json").exists())
        estado = json.loads((CONTROLE / "estado.json").read_text(encoding="utf-8"))
        self.assertFalse(estado["emAndamento"])
        self.assertTrue(estado["proximoEm"])

    def test_pedido_manual_roda_com_agenda_pausada(self):
        aplicacao.publicar_agenda(30, True)
        sync_sgf.gravar_json(
            CONTROLE / "pedido.json",
            {"id": "abc", "inicio": "2026-11-01", "fim": "2026-11-07"},
        )
        chamadas = []

        def falso(_sessao, _base, inicio="", fim=""):
            chamadas.append((inicio, fim))
            return True

        original = sync_sgf.executar_ciclo
        sync_sgf.executar_ciclo = falso
        try:
            ultimo, _fim, motivo = sync_sgf.passo_agenda(
                None, "http://exemplo", 30, "", PAINEL_CAMINHO.stat().st_mtime
            )
        finally:
            sync_sgf.executar_ciclo = original
        self.assertEqual(motivo, "pedido")
        self.assertEqual(ultimo, "abc")
        self.assertEqual(chamadas, [("2026-11-01", "2026-11-07")])
        feito = json.loads((CONTROLE / "feito.json").read_text(encoding="utf-8"))
        self.assertEqual(feito, {"id": "abc", "ok": True})
        estado = json.loads((CONTROLE / "estado.json").read_text(encoding="utf-8"))
        self.assertFalse(estado["emAndamento"])
        self.assertTrue(estado["ultimoOk"])

    def test_sem_painel_a_agenda_dispara_um_ciclo(self):
        PAINEL_CAMINHO.unlink()
        chamadas = []

        def falso(_sessao, _base, inicio="", fim=""):
            chamadas.append((inicio, fim))
            return False

        original = sync_sgf.executar_ciclo
        sync_sgf.executar_ciclo = falso
        try:
            _ultimo, _fim, motivo = sync_sgf.passo_agenda(None, "http://exemplo", 30, "", 0)
        finally:
            sync_sgf.executar_ciclo = original
        self.assertEqual(motivo, "relogio")
        self.assertEqual(chamadas, [("", "")])
        estado = json.loads((CONTROLE / "estado.json").read_text(encoding="utf-8"))
        self.assertFalse(estado["emAndamento"])
        self.assertFalse(estado["ultimoOk"])
        self.assertFalse(PAINEL_CAMINHO.exists())

    def test_gravar_troca_o_arquivo_inteiro(self):
        sync_sgf.gravar(
            {"linhas": [], "contas": [], "geradoEm": "2026-11-07T12:00:00-03:00", "titulos": []},
            "Filial",
        )
        texto = PAINEL_CAMINHO.read_text(encoding="utf-8")
        self.assertIn("const DADOS = ", texto)
        self.assertIn("2026-11-07T12:00:00-03:00", texto)
        self.assertFalse(Path(str(PAINEL_CAMINHO) + ".tmp").exists())
        self.assertTrue(sync_sgf.painel_valido())

    def test_fuso_de_fortaleza_e_nome_da_matriz(self):
        self.assertTrue(sync_sgf.agora_fortaleza().endswith("-03:00"))
        self.assertEqual(aplicacao.nome_origem("1"), "Matriz")
        self.assertEqual(aplicacao.nome_origem("01"), "Matriz")
        self.assertEqual(aplicacao.nome_origem("2"), "Filial 2")
        self.assertEqual(aplicacao.nome_origem("100"), "ADM | Central")
        self.assertIn("19:16", aplicacao.texto_fortaleza("2026-11-07T22:16:07+00:00"))
        self.assertIn("Fortaleza", aplicacao.texto_fortaleza("2026-11-07T22:16:07+00:00"))
        self.assertIn("America/Fortaleza", sync_sgf.HTML)
        self.assertIn("Fornecedores por loja", sync_sgf.HTML)

    def test_tela_nova_aparece_com_o_html_antigo(self):
        cliente, resposta = self.entrar()
        html = resposta.get_data(as_text=True)
        self.assertIn("Fornecedores por loja", html)
        self.assertIn('id="graficosLoja"', html)
        self.assertIn('id="graficoPendentes"', html)
        self.assertIn('id="soPendentes"', html)
        self.assertIn("<details class=\"sessao\">", html)
        self.assertIn("2026-11-07T10:00:00-03:00", html)
        self.assertNotIn("__DADOS__", html)
        self.assertNotIn("Fornecedores por loja", PAINEL_CAMINHO.read_text(encoding="utf-8"))
        antes = PAINEL_CAMINHO.stat().st_mtime_ns
        cliente.get("/")
        self.assertEqual(PAINEL_CAMINHO.stat().st_mtime_ns, antes)

    def test_contas_a_pagar_cobre_a_mesma_janela_das_entradas(self):
        inicio, fim = sync_sgf.janela_financeira(date(2026, 11, 7))
        self.assertEqual(inicio, date(2026, 5, 1))
        self.assertEqual(fim, date(2026, 11, 7))
        self.assertLessEqual(inicio, date(2026, 10, 1))
        self.assertGreaterEqual(fim, date(2026, 10, 31))
        longo_inicio, longo_fim = sync_sgf.janela_financeira(date(2027, 1, 31))
        self.assertEqual(longo_fim, date(2027, 1, 31))
        self.assertLessEqual((longo_fim - longo_inicio).days, 400)
        original = sync_sgf.janela_financeira
        sync_sgf.janela_financeira = lambda hoje=None: (date(2026, 5, 1), date(2027, 5, 26))
        try:
            self.assertEqual(sync_sgf.periodo_consulta("", ""), ("2026-05-01", "2027-05-26"))
            self.assertEqual(sync_sgf.periodo_consulta("2026-10-01", "2026-10-31"), ("2026-10-01", "2026-10-31"))
        finally:
            sync_sgf.janela_financeira = original
        self.assertIn("inicioEl.value = vigente.slice(0, 8) + '01'", sync_sgf.HTML)
        self.assertNotIn("inicioEl.onchange = function() { limitarAoVigente(); render(); }", sync_sgf.HTML)
        self.assertIn("addEventListener('blur', aplicarPeriodo)", sync_sgf.HTML)
        self.assertIn("/painel/recorte", sync_sgf.HTML)
        self.assertIn("fimDoGrafico", sync_sgf.HTML)
        self.assertIn("cerca de seis meses atrás", sync_sgf.HTML)
        self.assertIn("Sistemas de Gestão Financeira", sync_sgf.HTML)
        self.assertIn("Meta de Despesas", sync_sgf.HTML)
        self.assertIn('stroke="transparent" stroke-width="14"', sync_sgf.HTML)

    def test_lancamento_manual_entra_no_relatorio_geral(self):
        cliente, resposta = self.entrar()
        token = csrf_de(resposta.get_data(as_text=True))
        self.assertIn(">Dashboard<", resposta.get_data(as_text=True))
        self.assertIn("menu-usuario", resposta.get_data(as_text=True))
        self.assertLess(
            resposta.get_data(as_text=True).find("Administrador"),
            resposta.get_data(as_text=True).find(">Sair<"),
        )
        self.assertIn("menu-logo", resposta.get_data(as_text=True))
        self.assertIn("Relatório de entradas e despesas manual", resposta.get_data(as_text=True))
        self.assertIn('class="hamburguer"', resposta.get_data(as_text=True))
        self.assertIn("Administrador", resposta.get_data(as_text=True))
        salvo = cliente.post(
            "/lancamentos",
            data={
                "csrf": token,
                "tipo": "entrada",
                "data": "2026-10-15",
                "origem": "1",
                "descricao": "Aporte da matriz",
                "valor": "1500,00",
            },
            follow_redirects=True,
        )
        self.assertIn("Lançamento manual salvo", salvo.get_data(as_text=True))
        self.assertIn("Aporte da matriz", salvo.get_data(as_text=True))
        despesa = cliente.post(
            "/lancamentos",
            data={
                "csrf": token,
                "tipo": "despesa",
                "data": "2026-10-16",
                "origem": "100",
                "descricao": "Conserto",
                "valor": "80,00",
            },
            follow_redirects=True,
        )
        self.assertEqual(despesa.status_code, 200)
        pagina = cliente.get("/")
        html = pagina.get_data(as_text=True)
        self.assertIn("Matriz · Aporte da matriz", html)
        self.assertIn("ADM | Central · Conserto", html)
        self.assertIn('"manual": true', html)
        tema = cliente.post(
            "/configuracoes",
            data={"csrf": token, "tema": "escuro"},
            follow_redirects=True,
        )
        self.assertIn("Aparência salva", tema.get_data(as_text=True))
        self.assertIn('data-tema="escuro"', tema.get_data(as_text=True))
        self.assertIn('data-tema="escuro"', cliente.get("/").get_data(as_text=True))
        self.assertIn("Desenvolvido pela APSI Telecom", sync_sgf.HTML)
        self.assertIn("hojeFortaleza", sync_sgf.HTML)
        self.assertIn("alternarAberto", sync_sgf.HTML)
        self.assertIn("serieComparativo", sync_sgf.HTML)
        self.assertLess(sync_sgf.HTML.find(">Entradas <"), sync_sgf.HTML.find("Saídas por origem"))
        self.assertLess(sync_sgf.HTML.find('id="tituloSaidas"'), sync_sgf.HTML.find("Fornecedores por loja"))

    def test_usuario_ve_somente_as_lojas_marcadas(self):
        painel = {
            "geradoEm": "2026-11-07T10:00:00-03:00",
            "filial": "Teste",
            "contas": [{"nome": "Conta secreta", "saldo": 10}],
            "linhas": [
                {"data": "2026-11-02", "descricao": "Receita geral", "tipo": "receber", "valor": 50, "atipico": False}
            ],
            "titulos": [
                {
                    "fornecedor": "Fornecedor Matriz",
                    "documento": "1",
                    "filial": "1",
                    "emissao": "2026-11-02",
                    "vencimento": "2026-11-02",
                    "pagamento": "2026-11-02",
                    "valor": 10,
                    "pago": 10,
                    "juros": 0,
                    "obs": "",
                    "atipico": False,
                },
                {
                    "fornecedor": "Fornecedor Filial",
                    "documento": "2",
                    "filial": "2",
                    "emissao": "2026-11-02",
                    "vencimento": "2026-11-02",
                    "pagamento": "",
                    "valor": 20,
                    "pago": 0,
                    "juros": 0,
                    "obs": "",
                    "atipico": False,
                },
            ],
        }
        PAINEL_CAMINHO.write_text(
            "<!DOCTYPE html><html><body><script>const DADOS = "
            + json.dumps(painel)
            + ";</script></body></html>",
            encoding="utf-8",
        )
        admin, _ = self.entrar()
        ficha = admin.get("/usuarios/novo")
        html = ficha.get_data(as_text=True)
        token = csrf_de(html)
        self.assertIn("Filial 2", html)
        vazio = admin.post(
            "/usuarios/novo",
            data={
                "csrf": token,
                "nome": "Loja",
                "login": "loja",
                "perfil": "consulta",
                "senha": "senha1234",
                "confirmar": "senha1234",
            },
        )
        self.assertIn("Selecione ao menos uma loja", vazio.get_data(as_text=True))
        criado = admin.post(
            "/usuarios/novo",
            data={
                "csrf": token,
                "nome": "Loja",
                "login": "loja",
                "perfil": "consulta",
                "senha": "senha1234",
                "confirmar": "senha1234",
                "lojas": "1",
            },
            follow_redirects=True,
        )
        self.assertIn("Matriz", criado.get_data(as_text=True))
        admin.post(
            "/lancamentos",
            data={
                "csrf": token,
                "tipo": "despesa",
                "data": "2026-11-03",
                "origem": "2",
                "descricao": "Gasto da filial",
                "valor": "15,00",
            },
        )
        cliente, _ = self.entrar("loja", "senha1234")
        pagina = cliente.get("/").get_data(as_text=True)
        self.assertIn("Fornecedor Matriz", pagina)
        self.assertNotIn("Fornecedor Filial", pagina)
        self.assertNotIn("Receita geral", pagina)
        self.assertNotIn("Conta secreta", pagina)
        self.assertNotIn("Gasto da filial", pagina)
        self.assertIn('"acessoLojas": ["1"]', pagina)
        saidas = cliente.get("/saidas?inicio=2026-11-01&fim=2026-11-07")
        nomes = [item["fornecedor"] for item in saidas.get_json()["titulos"]]
        self.assertEqual(nomes, ["Fornecedor Matriz"])
        bloqueado = cliente.post(
            "/lancamentos",
            data={
                "csrf": csrf_de(cliente.get("/lancamentos").get_data(as_text=True)),
                "tipo": "despesa",
                "data": "2026-11-04",
                "origem": "2",
                "descricao": "Fora da loja",
                "valor": "9,00",
            },
        )
        self.assertIn("Informe a loja de origem", bloqueado.get_data(as_text=True))
        self.assertNotIn("Fora da loja", cliente.get("/lancamentos").get_data(as_text=True))
        completo = admin.get("/").get_data(as_text=True)
        self.assertIn("Fornecedor Filial", completo)
        self.assertIn("Receita geral", completo)
        self.assertIn("Conta secreta", completo)
        self.assertNotIn('"acessoLojas"', completo)
        todas = admin.post(
            "/usuarios/novo",
            data={
                "csrf": token,
                "nome": "Geral",
                "login": "geral",
                "perfil": "admin",
                "senha": "senha1234",
                "confirmar": "senha1234",
                "lojas": ["1", "2", "100"],
            },
            follow_redirects=True,
        )
        self.assertIn("Usuário cadastrado", todas.get_data(as_text=True))
        amplo, _ = self.entrar("geral", "senha1234")
        visao = amplo.get("/").get_data(as_text=True)
        self.assertIn("Receita geral", visao)
        self.assertIn("Fornecedor Filial", visao)
        self.assertIn("Conta secreta", visao)
        self.assertNotIn('"acessoLojas"', visao)

    def test_backup_diario_fica_fora_e_preserva_outros_arquivos(self):
        pasta = RAIZ / "backup-teste"
        os.environ["GEFIN_BACKUP_DIR"] = str(pasta)
        try:
            destino = aplicacao.gravar_backup()
            self.assertTrue(destino.is_file())
            self.assertGreater(destino.stat().st_size, 0)
            self.assertEqual(oct(destino.stat().st_mode & 0o777), "0o600")
            with sqlite3.connect(destino) as conn:
                total = conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
            self.assertGreaterEqual(total, 1)
            self.assertEqual(aplicacao.gravar_backup(), destino)
            self.assertTrue((pasta / f"sgf-painel-{destino.name[13:23]}.html").is_file())
            antigo = pasta / "sgf-usuarios-2020-01-01.db"
            antigo.write_text("velho", encoding="utf-8")
            alheio = pasta / "outro-sistema.db"
            alheio.write_text("nao-mexer", encoding="utf-8")
            aplicacao.gravar_backup()
            self.assertFalse(antigo.exists())
            self.assertEqual(alheio.read_text(encoding="utf-8"), "nao-mexer")
            self.assertTrue(destino.exists())
        finally:
            os.environ.pop("GEFIN_BACKUP_DIR", None)

    def test_regras_da_agenda(self):
        agora = 1_000_000
        self.assertEqual(sync_sgf.deve_sincronizar(agora, agora - 10, 30, False, "", ""), "")
        self.assertEqual(sync_sgf.deve_sincronizar(agora, agora - 1800, 30, False, "", ""), "relogio")
        self.assertEqual(sync_sgf.deve_sincronizar(agora, agora - 1800, 30, True, "", ""), "")
        self.assertEqual(sync_sgf.deve_sincronizar(agora, agora, 30, True, "abc", ""), "pedido")
        self.assertEqual(sync_sgf.deve_sincronizar(agora, 0, 30, False, "", ""), "relogio")
        self.assertEqual(sync_sgf.deve_sincronizar(agora, agora, 30, False, "abc", "abc"), "")

    def test_parse_caixa_separa_loja_historico_e_tipo(self):
        texto = (
            "Filial:\t2\tLOJA CENTRO\t\n"
            "Histórico:\t1\tSANGRIA\t\n"
            "\t12/11/2026\t14:18:11\t100,00\tS\tDINHEIRO\tANA\t\n"
            "\tObservação:\tMONTANTE\t\n"
            "\t12/11/2026\t09:00:00\t50,00\tE\tDINHEIRO\tANA\t\n"
            "\tObservação:\tSUPRIMENTO\t\n"
            "Total Histórico:\t-50,00\t\n"
            "Página 1 de\t2\t\n"
            "Filial:\t3\tLOJA NORTE\t\n"
            "Histórico:\t5\tFECHAMENTO\t\n"
            "\t13/11/2026\t18:00:00\t10,50\tS\tCARTAO\tBIA\t\n"
            "\tObservação:\t\t\n"
        )
        movimentos = sync_sgf.parse_caixa(texto)
        self.assertEqual(len(movimentos), 3)
        self.assertEqual(movimentos[0]["filial"], "2")
        self.assertEqual(movimentos[0]["filialNome"], "LOJA CENTRO")
        self.assertEqual(movimentos[0]["historico"], "SANGRIA")
        self.assertEqual(movimentos[0]["tipo"], "saida")
        self.assertEqual(movimentos[0]["obs"], "MONTANTE")
        self.assertEqual(movimentos[0]["valor"], 100.0)
        self.assertEqual(movimentos[1]["tipo"], "entrada")
        self.assertEqual(movimentos[2]["filialNome"], "LOJA NORTE")
        self.assertEqual(movimentos[2]["obs"], "")

    def test_relatorio_guarda_o_tipo_de_despesa_do_sgf(self):
        colunas = [""] * 17
        colunas[0] = "1-1"
        colunas[2] = "2"
        colunas[4] = "01/11/26"
        colunas[5] = "10/11/26"
        colunas[10] = "100,00"
        texto = (
            "Tipo Despesa:\tCONSIDERAR: TODOS\n"
            "Tipo Despesa:\t4\tALUGUEL\n"
            "Fornecedor:\t9\tIMOBILIARIA\n"
            + "\t".join(colunas)
            + "\n"
        )
        itens = sync_sgf.parse_relatorio(texto)
        self.assertEqual(len(itens), 1)
        self.assertEqual(itens[0]["tipoDespesa"], "ALUGUEL")
        self.assertEqual(itens[0]["fornecedor"], "9 IMOBILIARIA")
        fonte = Path(sync_sgf.__file__).read_text(encoding="utf-8")
        self.assertIn('"selecao2_1", "fornecedor"', fonte)
        self.assertIn('"selecao2_2", "despesa"', fonte)

    def test_movimento_de_caixa_entra_no_menu_e_enfileira(self):
        cliente, resposta = self.entrar()
        self.assertIn("Movimento de caixa", resposta.get_data(as_text=True))
        pagina = cliente.get("/caixa")
        html = pagina.get_data(as_text=True)
        self.assertEqual(pagina.status_code, 200)
        self.assertNotIn("Consultar no SGF", html)
        self.assertIn('id="graficoCaixa"', html)
        self.assertIn("outro.open = false", html)
        self.assertIn('class="card recortes"', html)
        token = csrf_de(html)
        pedido = cliente.post(
            "/caixa",
            data={"csrf": token, "inicio": "2026-11-01", "fim": "2026-11-07", "tipo": "saidas"},
            follow_redirects=True,
        )
        self.assertIn("Consulta enviada ao SGF.", pedido.get_data(as_text=True))
        gravado = json.loads((CONTROLE / "caixa.json").read_text(encoding="utf-8"))
        self.assertEqual(gravado["inicio"], "2026-11-01")
        self.assertEqual(gravado["fim"], "2026-11-07")
        self.assertEqual(gravado["tipo"], "saidas")
        self.assertIn("concluindo um movimento de caixa", pedido.get_data(as_text=True))
        longo = cliente.post(
            "/caixa",
            data={"csrf": csrf_de(pedido.get_data(as_text=True)), "inicio": "2026-10-01", "fim": "2026-11-07", "tipo": "todos"},
        )
        self.assertIn("no máximo 31 dias", longo.get_data(as_text=True))

    def test_movimento_de_caixa_esconde_loja_nao_marcada(self):
        (CONTROLE / "caixa-feito.json").write_text(
            json.dumps(
                {
                    "id": "feito",
                    "ok": True,
                    "inicio": "2026-11-12",
                    "fim": "2026-11-13",
                    "tipo": "todos",
                    "geradoEm": "2026-11-13T10:00:00-03:00",
                    "movimentos": [
                        {"filial": "2", "filialNome": "LOJA CENTRO", "historico": "SANGRIA", "data": "2026-11-12", "hora": "14:18:11", "valor": 100, "tipo": "saida", "moeda": "DINHEIRO", "operador": "ANA", "obs": ""},
                        {"filial": "3", "filialNome": "LOJA NORTE", "historico": "SANGRIA", "data": "2026-11-12", "hora": "15:00:00", "valor": 40, "tipo": "saida", "moeda": "DINHEIRO", "operador": "BIA", "obs": ""},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with aplicacao.conectar() as conn:
            usuario = conn.execute("SELECT id FROM usuarios WHERE login = ?", ("consulta",)).fetchone()
            conn.execute("DELETE FROM usuario_lojas WHERE usuario_id = ?", (usuario["id"],))
            conn.execute(
                "INSERT INTO usuario_lojas (usuario_id, origem) VALUES (?, ?)",
                (usuario["id"], "3"),
            )
        try:
            cliente, _resposta = self.entrar("consulta")
            html = cliente.get("/caixa").get_data(as_text=True)
            self.assertIn("LOJA NORTE", html)
            self.assertNotIn("LOJA CENTRO", html)
        finally:
            with aplicacao.conectar() as conn:
                conn.execute("DELETE FROM usuario_lojas WHERE usuario_id = ?", (usuario["id"],))

    def test_sincronismo_grava_na_base_e_o_periodo_le_dela(self):
        sync_sgf.gravar(
            {
                "geradoEm": "2026-11-13T10:00:00-03:00",
                "inicio": "2026-05-01",
                "fim": "2026-11-13",
                "titulosInicio": "2026-05-01",
                "titulosFim": "2026-11-13",
                "linhas": [
                    {"data": "2026-11-02", "descricao": "Receita do mes", "tipo": "receber", "status": "baixado", "valor": 10, "atipico": False},
                    {"data": "2026-05-02", "descricao": "Receita antiga", "tipo": "receber", "status": "baixado", "valor": 99, "atipico": False},
                ],
                "titulos": [
                    {"fornecedor": "Atual", "documento": "1-1", "filial": "2", "emissao": "2026-11-01", "vencimento": "2026-11-10", "pagamento": "", "valor": 5, "pago": None, "juros": 0, "obs": "", "atipico": False},
                    {"fornecedor": "Antigo", "documento": "1-2", "filial": "2", "emissao": "2026-05-01", "vencimento": "2026-05-10", "pagamento": "", "valor": 7, "pago": None, "juros": 0, "obs": "", "atipico": False},
                ],
                "contas": [{"nome": "Caixa", "saldo": 3}],
            },
            "Filial",
        )
        guardado = sgf_base.ler()
        self.assertEqual(guardado["geradoEm"], "2026-11-13T10:00:00-03:00")
        self.assertEqual(len(guardado["titulos"]), 2)
        cliente, _resposta = self.entrar()
        recorte = cliente.get("/painel/recorte?inicio=2026-11-01&fim=2026-11-13")
        corpo = recorte.get_json()
        self.assertTrue(corpo["ok"])
        self.assertEqual([item["descricao"] for item in corpo["linhas"]], ["Receita do mes"])
        self.assertEqual([item["fornecedor"] for item in corpo["titulos"]], ["Atual"])
        self.assertEqual(corpo["porOrigemMes"], [{"origem": "2", "mes": "2026-11", "valor": 5.0}])
        self.assertIn("2", corpo["filiais"])

    def test_cards_em_linha_e_meta_por_tipo_de_despesa(self):
        dados = {
            "geradoEm": "2026-11-13T10:00:00-03:00",
            "titulos": [
                {
                    "fornecedor": "Companhia de energia",
                    "documento": "1",
                    "filial": "2",
                    "emissao": "2026-11-02",
                    "vencimento": "2026-11-10",
                    "pagamento": "",
                    "valor": 80,
                    "pago": 0,
                    "juros": 0,
                    "obs": "",
                    "atipico": False,
                    "classe": "despesa",
                    "tipoDespesa": "Energia",
                }
            ],
            "linhas": [],
            "contas": [],
            "filial": "Teste",
        }
        PAINEL_CAMINHO.write_text(
            "<!DOCTYPE html><html><body><script>const DADOS = " + json.dumps(dados) + ";</script></body></html>",
            encoding="utf-8",
        )
        cliente, _resposta = self.entrar()
        pagina = cliente.get("/metas")
        html = pagina.get_data(as_text=True)
        self.assertIn("Energia", html)
        self.assertNotIn("Fornecedor em cada tipo", html)
        token = csrf_de(html)
        recusado = cliente.post(
            "/metas/despesas",
            data={"csrf": token, "nome": "Aluguel inventado", "valor": "10,00"},
            follow_redirects=True,
        )
        self.assertIn("Despesa desconhecida", recusado.get_data(as_text=True))
        pagina = cliente.get("/metas")
        token = csrf_de(pagina.get_data(as_text=True))
        salvo = cliente.post(
            "/metas/despesas",
            data={"csrf": token, "nome": "Energia", "valor": "1.000,00"},
            follow_redirects=True,
        )
        self.assertIn("Meta de despesa salva", salvo.get_data(as_text=True))
        painel = cliente.get("/").get_data(as_text=True)
        self.assertIn("grid-template-columns:repeat(4, minmax(0, 1fr))", painel)
        self.assertIn(".cards { grid-template-columns:1fr; }", painel)
        self.assertIn("Tipo Fornecedores", painel)
        self.assertIn("Tipo Despesa", painel)
        self.assertIn('id="conciliacaoTipos"', painel)
        self.assertIn("Energia", painel)
        self.assertIn('"nome": "Energia"', painel)
        self.assertNotIn("__TIPOS_DESPESA__", painel)
        self.assertNotIn("__MAPA_TIPO__", painel)
        self.assertIn('data-pagina="dashboard"', painel)
        self.assertIn('id="resumoBars"', painel)
        self.assertIn('id="graficoEntradas"', painel)
        self.assertIn('id="tiposBars"', painel)
        self.assertIn('id="concLojaBars"', painel)
        self.assertIn('id="graficoConciliacaoTipo"', painel)
        self.assertIn('id="graficoSaldos"', painel)
        self.assertIn('main[data-pagina="dashboard"] .so-detalhe { display:none; }', painel)
        detalhe = cliente.get("/detalhes")
        self.assertEqual(detalhe.status_code, 200)
        pagina = detalhe.get_data(as_text=True)
        self.assertIn('data-pagina="detalhes"', pagina)
        self.assertIn(">Detalhes<", pagina)
        self.assertIn('id="sessoesSaidas"', pagina)
        self.assertIn('id="contas"', pagina)


if __name__ == "__main__":
    unittest.main()
