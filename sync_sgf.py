import argparse
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE_DEFAULT = "http://45.160.125.114:4647/sgfpod1"
ATIPICO = 1_000_000


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
    hoje = date.today()
    inicio = (hoje.replace(day=1) - timedelta(days=180)).replace(day=1)
    fim = hoje + timedelta(days=200)
    classes = pull(
        session,
        base,
        "/rest/integracao/financeiro/obter-classificacao-financeira-v1",
        {"dataInicial": "2020-01-01", "dataFinal": fim.isoformat()},
    )
    por_centro = {}
    for item in classes:
        por_centro[str(item.get("centroCusto"))] = item
    movimentos = pull(
        session,
        base,
        "/rest/integracao/financeiro/obter-resultado-financeiro-v1",
        {"dataInicial": inicio.isoformat(), "dataFinal": fim.isoformat()},
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
        "geradoEm": datetime.now().isoformat(timespec="seconds"),
        "inicio": inicio.isoformat(),
        "fim": fim.isoformat(),
        "contas": [
            {
                "nome": item.get("nome") or "Conta",
                "saldo": round(float(item.get("saldo") or 0), 2),
            }
            for item in contas
        ],
        "linhas": linhas,
    }


HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Painel SGF</title>
<style>
  :root { --bg:#f3f6fb; --card:#fff; --ink:#102033; --muted:#5c6b80; --line:#d7e0ec; --pay:#b42318; --rec:#067647; --accent:#1d4ed8; }
  * { box-sizing:border-box; }
  body { margin:0; font:15px/1.45 system-ui, Segoe UI, sans-serif; background:var(--bg); color:var(--ink); }
  main { max-width:1100px; margin:0 auto; padding:24px; }
  h1 { font-size:22px; margin:0 0 4px; }
  .sub { color:var(--muted); margin-bottom:16px; }
  .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:16px; }
  button { border:1px solid var(--line); background:#fff; border-radius:8px; padding:6px 10px; cursor:pointer; }
  .cards { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px; }
  .card span { display:block; color:var(--muted); font-size:12px; }
  .card strong { font-size:20px; }
  .pay { color:var(--pay); } .rec { color:var(--rec); }
  section { margin-top:18px; }
  label { color:var(--muted); font-size:13px; }
  input[type="date"] { border:1px solid var(--line); border-radius:8px; padding:6px 8px; font:inherit; }
  .tabela { overflow:auto; max-height:540px; background:#fff; border:1px solid var(--line); border-radius:12px; }
  table { width:100%; border-collapse:collapse; background:#fff; }
  th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); font-size:13px; white-space:nowrap; }
  th { color:var(--muted); font-weight:600; position:sticky; top:0; background:#fff; }
  td.num, th.num { text-align:right; }
  tr.atipico td { background:#fff7e8; }
  .bars { display:flex; gap:10px; align-items:flex-end; height:180px; background:#fff; border:1px solid var(--line); border-radius:12px; padding:12px; }
  .bar { flex:1; display:flex; gap:3px; align-items:flex-end; height:140px; }
  .bar i { display:block; width:50%; border-radius:4px 4px 0 0; }
  .bar i.p { background:#f3b4ae; } .bar i.r { background:#9ad7b8; }
  .bar em { display:block; text-align:center; font-style:normal; color:var(--muted); font-size:11px; margin-top:4px; }
  .note { background:#fff7e8; border:1px solid #f0d7a2; border-radius:10px; padding:10px 12px; margin-top:12px; }
  @media (max-width:800px) { .cards { grid-template-columns:1fr 1fr; } }
</style>
</head>
<body>
<main>
  <h1>Painel financeiro SGF</h1>
  <div class="sub" id="meta"></div>
  <div class="row">
    <label>De <input id="inicio" type="date"></label>
    <label>Até <input id="fim" type="date"></label>
    <button id="buscar" type="button">Buscar</button>
  </div>
  <div class="cards">
    <div class="card"><span>Entradas</span><strong class="rec" id="receber"></strong></div>
    <div class="card"><span>Saídas</span><strong class="pay" id="pagar"></strong></div>
    <div class="card"><span>Saldo</span><strong id="saldo"></strong></div>
    <div class="card"><span>Saídas em aberto</span><strong class="pay" id="abertas"></strong></div>
  </div>
  <section>
    <h2>Entradas</h2>
    <div class="tabela"><table>
      <thead><tr><th>Data</th><th>Descrição</th><th>Situação</th><th class="num">Valor</th></tr></thead>
      <tbody id="entradas"></tbody>
    </table></div>
  </section>
  <section>
    <h2>Saídas</h2>
    <div class="tabela"><table>
      <thead><tr><th>Fornecedor</th><th>Documento</th><th>Filial</th><th>Emissão</th><th>Vencimento</th><th class="num">Valor</th><th class="num">Pago</th><th class="num">Juros</th><th>Pagamento</th><th>Observação</th></tr></thead>
      <tbody id="saidas"></tbody>
    </table></div>
  </section>
  <section>
    <h2>Saldos das contas</h2>
    <div class="tabela"><table><thead><tr><th>Conta</th><th class="num">Saldo</th></tr></thead><tbody id="contas"></tbody></table></div>
  </section>
  <div class="note" id="nota"></div>
</main>
<script>
const DADOS = __DADOS__;
const moeda = new Intl.NumberFormat('pt-BR', {style:'currency', currency:'BRL'});
const titulos = DADOS.titulos || [];
const vencimentos = titulos.map(item => item.vencimento).filter(Boolean).sort();

function esc(valor) {
  return String(valor ?? '').replace(/[&<>"]/g, caractere => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[caractere]));
}
function br(data) {
  if (!data) return '';
  const partes = data.split('-');
  return partes[2] + '/' + partes[1] + '/' + partes[0];
}
function dentro(data, inicio, fim) { return !!data && data >= inicio && data <= fim; }
function soma(lista, campo) { return lista.reduce((total, item) => total + (Number(item[campo]) || 0), 0); }
function moedaOuVazio(valor) { return valor === null || valor === undefined || valor === '' ? '' : moeda.format(valor); }

function render() {
  const inicio = document.getElementById('inicio').value;
  const fim = document.getElementById('fim').value;
  const entradas = (DADOS.linhas || []).filter(item => item.tipo === 'receber' && !item.atipico && dentro(item.data, inicio, fim));
  const saidas = titulos.filter(item => dentro(item.vencimento, inicio, fim));
  const totalEntradas = soma(entradas, 'valor');
  const totalSaidas = soma(saidas, 'valor');
  const abertas = saidas.filter(item => !item.pagamento);
  document.getElementById('receber').textContent = moeda.format(totalEntradas);
  document.getElementById('pagar').textContent = moeda.format(totalSaidas);
  const saldo = totalEntradas - totalSaidas;
  const saldoEl = document.getElementById('saldo');
  saldoEl.textContent = moeda.format(saldo);
  saldoEl.className = saldo >= 0 ? 'rec' : 'pay';
  document.getElementById('abertas').textContent = moeda.format(soma(abertas, 'valor'));

  document.getElementById('entradas').innerHTML = entradas.map(item =>
    '<tr><td>' + br(item.data) + '</td><td>' + esc(item.descricao) + '</td><td>' + (item.status === 'pendente' ? 'Em aberto' : 'Recebida') + '</td><td class="num">' + moeda.format(item.valor) + '</td></tr>'
  ).join('') || '<tr><td colspan="4">Nenhuma entrada nesse período.</td></tr>';

  document.getElementById('saidas').innerHTML = saidas.map(item =>
    '<tr class="' + (item.atipico ? 'atipico' : '') + '"><td>' + esc(item.fornecedor) + '</td><td>' + esc(item.documento) + '</td><td>' + esc(item.filial) + '</td><td>' + br(item.emissao) + '</td><td>' + br(item.vencimento) + '</td><td class="num">' + moedaOuVazio(item.valor) + '</td><td class="num">' + (item.atipico ? 'ilegível' : moedaOuVazio(item.pago)) + '</td><td class="num">' + (item.atipico ? '' : moedaOuVazio(item.juros)) + '</td><td>' + (item.pagamento ? br(item.pagamento) : 'Em aberto') + '</td><td>' + esc(item.obs) + '</td></tr>'
  ).join('') || '<tr><td colspan="10">Nenhuma saída com vencimento nesse período.</td></tr>';

  document.getElementById('contas').innerHTML = (DADOS.contas || []).map(conta =>
    '<tr><td>' + esc(conta.nome) + '</td><td class="num">' + moeda.format(conta.saldo) + '</td></tr>'
  ).join('');

  const cobertura = vencimentos.length ? br(vencimentos[0]) + ' a ' + br(vencimentos[vencimentos.length - 1]) : 'nenhum relatório importado';
  document.getElementById('meta').textContent = (DADOS.filial || '') + ' · atualizado em ' + String(DADOS.geradoEm || '').replace('T', ' ');
  const contasFora = (DADOS.contas || []).filter(conta => Math.abs(conta.saldo) >= 1000000).map(conta => conta.nome);
  document.getElementById('nota').textContent = 'Entradas são o movimento diário de receita. Saídas são os títulos do relatório, filtrados pelo vencimento. Títulos carregados: ' + cobertura + '.'
    + (contasFora.length ? ' Saldo fora de escala: ' + contasFora.join(', ') + '.' : '');
}

function isoLocal(data) {
  const mes = String(data.getMonth() + 1).padStart(2, '0');
  const dia = String(data.getDate()).padStart(2, '0');
  return data.getFullYear() + '-' + mes + '-' + dia;
}
const inicioEl = document.getElementById('inicio');
const fimEl = document.getElementById('fim');
const agora = new Date();
inicioEl.value = isoLocal(new Date(agora.getFullYear(), agora.getMonth(), 1));
fimEl.value = isoLocal(agora);
document.getElementById('buscar').onclick = render;
inicioEl.onchange = render;
fimEl.onchange = render;
render();
</script>
</body>
</html>
"""


def carregar_titulos():
    caminho = Path(__file__).resolve().parent / "apoio" / "titulos.json"
    if not caminho.exists():
        return []
    return json.loads(caminho.read_text(encoding="utf-8"))


def gravar(payload, filial):
    payload["filial"] = filial
    payload["titulos"] = carregar_titulos()
    html = HTML.replace("__DADOS__", json.dumps(payload, ensure_ascii=False))
    arquivo = destino()
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    arquivo.write_text(html, encoding="utf-8")
    print(f"painel {arquivo} linhas={len(payload['linhas'])} titulos={len(payload['titulos'])} contas={len(payload['contas'])}")


def ciclo(base, user, password, estado):
    token = estado.get("token")
    exp = estado.get("exp")
    if not token or exp - datetime.now(timezone.utc) < timedelta(hours=2):
        token, filial = login(base, user, password)
        estado["token"] = token
        estado["exp"] = token_exp(token)
        estado["filial"] = filial
        print("sessao ate", estado["exp"].isoformat())
    payload = coletar(base, estado["token"])
    gravar(payload, estado["filial"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--a-cada", type=int, default=0, help="Minutos entre consultas. 0 roda uma vez.")
    args = parser.parse_args()
    base = os.environ.get("SGF_BASE", BASE_DEFAULT).rstrip("/")
    user = os.environ["SGF_USER"]
    password = os.environ["SGF_PASS"]
    estado = {}
    while True:
        try:
            ciclo(base, user, password, estado)
        except Exception as exc:
            print("falha", exc)
            estado.clear()
            if args.a_cada <= 0:
                raise
        if args.a_cada <= 0:
            break
        time.sleep(args.a_cada * 60)


if __name__ == "__main__":
    main()
