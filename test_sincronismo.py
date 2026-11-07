import json
import os
import re
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
        for nome in ("pedido.json", "feito.json", "estado.json"):
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
        self.assertEqual(fim, date(2026, 11, 7) + timedelta(days=200))
        self.assertLessEqual((fim - inicio).days, 400)
        self.assertLessEqual(inicio, date(2026, 10, 1))
        self.assertGreaterEqual(fim, date(2026, 10, 31))
        longo_inicio, longo_fim = sync_sgf.janela_financeira(date(2027, 1, 31))
        self.assertLessEqual((longo_fim - longo_inicio).days, 400)
        original = sync_sgf.janela_financeira
        sync_sgf.janela_financeira = lambda hoje=None: (date(2026, 5, 1), date(2027, 5, 26))
        try:
            self.assertEqual(sync_sgf.periodo_consulta("", ""), ("2026-05-01", "2027-05-26"))
            self.assertEqual(sync_sgf.periodo_consulta("2026-10-01", "2026-10-31"), ("2026-10-01", "2026-10-31"))
        finally:
            sync_sgf.janela_financeira = original
        self.assertIn("fimDoGrafico", sync_sgf.HTML)
        self.assertIn("cerca de seis meses atrás", sync_sgf.HTML)

    def test_regras_da_agenda(self):
        agora = 1_000_000
        self.assertEqual(sync_sgf.deve_sincronizar(agora, agora - 10, 30, False, "", ""), "")
        self.assertEqual(sync_sgf.deve_sincronizar(agora, agora - 1800, 30, False, "", ""), "relogio")
        self.assertEqual(sync_sgf.deve_sincronizar(agora, agora - 1800, 30, True, "", ""), "")
        self.assertEqual(sync_sgf.deve_sincronizar(agora, agora, 30, True, "abc", ""), "pedido")
        self.assertEqual(sync_sgf.deve_sincronizar(agora, 0, 30, False, "", ""), "relogio")
        self.assertEqual(sync_sgf.deve_sincronizar(agora, agora, 30, False, "abc", "abc"), "")


if __name__ == "__main__":
    unittest.main()
