import argparse
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE_DEFAULT = "http://45.160.125.114:4647/sgfpod1"
ATIPICO = 1_000_000
FORTALEZA = timezone(timedelta(hours=-3))


def agora_fortaleza():
    return datetime.now(FORTALEZA).isoformat(timespec="seconds")


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
        "geradoEm": agora_fortaleza(),
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
<title>Gestão Financeira</title>
<style>
  :root { --bg:#f3f6fb; --card:#fff; --ink:#102033; --muted:#5c6b80; --line:#d7e0ec; --pay:#dc2626; --rec:#059669; --accent:#1d4ed8; --chart-grid:#e2e9f2; }
  * { box-sizing:border-box; }
  body { margin:0; font:15px/1.45 system-ui, Segoe UI, sans-serif; background:var(--bg); color:var(--ink); }
  main { max-width:1100px; margin:0 auto; padding:24px; }
  .brand { display:flex; align-items:center; gap:14px; margin-bottom:16px; }
  .brand-logo-wrap { position:relative; flex-shrink:0; line-height:0; }
  .brand-logo { width:52px; height:52px; border-radius:50%; object-fit:cover; background:#fff; padding:3px; border:2px solid rgba(29,78,216,.25); box-shadow:0 4px 16px rgba(29,78,216,.28); display:block; }
  .brand-logo-ring { position:absolute; inset:-7px; border-radius:50%; border:2px dashed rgba(29,78,216,.45); animation:spinRing 14s linear infinite; }
  @keyframes spinRing { to { transform:rotate(360deg); } }
  h1 { font-size:22px; margin:0; }
  .sub { color:var(--muted); }
  .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:16px; }
  button { border:1px solid var(--line); background:#fff; border-radius:8px; padding:6px 10px; cursor:pointer; }
  .cards { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:16px; padding:14px; box-shadow:0 8px 24px rgba(16,32,51,.04); }
  .card span { display:block; color:var(--muted); font-size:12px; }
  .card strong { font-size:20px; }
  .pay { color:var(--pay); } .rec { color:var(--rec); }
  .panel { background:#fff; border:1px solid var(--line); border-radius:16px; padding:16px; margin-top:18px; }
  .panel h2 { margin:0 0 12px; font-size:16px; display:flex; justify-content:space-between; gap:8px; }
  .panel h2 em { font-style:normal; color:var(--muted); font-size:12px; font-weight:500; }
  .chart-wrap { position:relative; height:260px; padding:8px 4px 28px 48px; }
  .chart-y-axis { position:absolute; left:0; top:8px; bottom:28px; width:44px; display:flex; flex-direction:column; justify-content:space-between; font-size:11px; color:var(--muted); text-align:right; }
  .chart-grid-lines { position:absolute; left:48px; right:4px; top:8px; bottom:28px; display:flex; flex-direction:column; justify-content:space-between; pointer-events:none; }
  .chart-grid-lines div { border-top:1px dashed var(--chart-grid); height:0; }
  .chart-bars { position:relative; height:100%; display:flex; align-items:flex-end; justify-content:space-around; gap:8px; z-index:1; }
  .bar-group { flex:1; max-width:70px; display:flex; flex-direction:column; align-items:center; height:100%; justify-content:flex-end; gap:6px; }
  .bar-pair { display:flex; align-items:flex-end; gap:4px; height:100%; width:100%; justify-content:center; }
  .bar { width:45%; max-width:22px; border-radius:6px 6px 0 0; }
  .bar-pagar { background:linear-gradient(180deg, var(--pay), #fca5a5); }
  .bar-receber { background:linear-gradient(180deg, var(--rec), #6ee7b7); }
  .bar-label { font-size:11px; font-weight:600; color:var(--muted); text-transform:uppercase; }
  .chart-legend { display:flex; gap:16px; justify-content:center; font-size:13px; color:var(--muted); }
  .legend-dot { width:10px; height:10px; border-radius:3px; display:inline-block; margin-right:4px; }
  .legend-dot.pagar { background:var(--pay); } .legend-dot.receber { background:var(--rec); }
  label { color:var(--muted); font-size:13px; }
  input[type="date"], select { border:1px solid var(--line); border-radius:8px; padding:6px 8px; font:inherit; background:#fff; max-width:100%; }
  select { min-width:260px; }
  .sessao { margin-top:16px; border:1px solid var(--line); border-left:4px solid var(--pay); border-radius:12px; padding:0; background:#fff; }
  .sessao summary { cursor:pointer; display:flex; justify-content:space-between; gap:8px; flex-wrap:wrap; align-items:center; padding:12px; font-weight:700; font-size:16px; }
  .sessao summary::-webkit-details-marker { display:none; }
  .sessao summary::before { content:"▸"; color:var(--muted); margin-right:8px; }
  .sessao[open] summary::before { content:"▾"; }
  .sessao summary em { font-style:normal; color:var(--muted); font-size:12px; font-weight:600; }
  .sessao .tabela { margin:0 12px 12px; }
  .check-inline { display:flex; align-items:center; gap:6px; }
  .check-inline input { width:auto; }
  .chart-scroll { overflow:auto; max-width:100%; }
  .chart-scroll svg { display:block; }
  .loja-bloco { margin-top:16px; }
  .loja-bloco h3 { margin:0 0 6px; font-size:15px; }
  .origem-nota { color:var(--muted); font-size:13px; margin:8px 0 0; }
  .origem-nota:empty { display:none; }
  button#atualizar { background:var(--accent); color:#fff; border-color:var(--accent); font-weight:700; }
  button#atualizar:disabled { opacity:.65; cursor:wait; }
  .idade-sync { color:var(--muted); font-size:13px; font-weight:600; }
  .aviso-sync { color:#b42318; font-weight:700; margin:-8px 0 12px; }
  .aviso-sync:empty { display:none; }
  .totalizador { display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; margin:8px 0 0; padding:10px 12px; background:#f8fafc; border:1px solid var(--line); border-radius:10px; font-weight:700; }
  tfoot td { font-weight:700; background:#f8fafc; }
  .bar-origem { width:36px; max-width:36px; background:linear-gradient(180deg, var(--pay), #fca5a5); }
  .bar-valor { font-size:10px; color:var(--muted); text-align:center; }
  .tabela { overflow:auto; max-height:540px; }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); font-size:13px; white-space:nowrap; }
  th { color:var(--muted); font-weight:600; position:sticky; top:0; background:#fff; }
  td.num, th.num { text-align:right; }
  tr.atipico td { background:#fff7e8; }
  .note { background:#fff7e8; border:1px solid #f0d7a2; border-radius:10px; padding:10px 12px; margin-top:12px; }
  .alerta { border-radius:12px; padding:10px 12px; margin:0 0 8px; font-weight:700; }
  .alerta-verde { background:#e7f6ee; border:1px solid #059669; color:#067647; }
  .alerta-vermelho { background:#fde8e6; border:1px solid #dc2626; color:#b42318; }
  #alertas:empty { display:none; }
  .charts-row { display:flex; gap:12px; align-items:stretch; }
  .charts-row > .panel { flex:1 1 0; min-width:0; }
  .charts-row .panel h2 { flex-wrap:wrap; }
  .chart-line { width:100%; height:240px; }
  .chart-line svg { width:100%; height:100%; display:block; }
  .chart-legend { flex-wrap:wrap; }
  .serie-alerta { font-weight:800; }
  #avisosMeta:empty { display:none; }
  @media (max-width:800px) {
    .cards { grid-template-columns:1fr 1fr; }
    .charts-row { flex-direction:column; }
    .charts-row > .panel { flex-basis:auto; width:100%; }
  }
</style>
</head>
<body>
<main>
  <div class="brand">
    <span class="brand-logo-wrap"><img class="brand-logo" src="__LOGO__" alt="Gestão Financeira"><span class="brand-logo-ring"></span></span>
    <div>
      <h1>Gestão Financeira</h1>
      <div class="sub" id="meta"></div>
    </div>
  </div>
  <div class="row">
    <label>De <input id="inicio" type="date"></label>
    <label>Até <input id="fim" type="date"></label>
    <button id="buscar" type="button">Buscar</button>
    <button id="atualizar" type="button">Atualizar</button>
    <span id="idadeSync" class="idade-sync"></span>
  </div>
  <p id="avisoSync" class="aviso-sync"></p>
  <div id="alertas"></div>
  <div class="cards">
    <div class="card"><span>Entradas</span><strong class="rec" id="receber"></strong></div>
    <div class="card"><span>Saídas</span><strong class="pay" id="pagar"></strong></div>
    <div class="card"><span>Saldo</span><strong id="saldo"></strong></div>
    <div class="card"><span>Saídas em aberto</span><strong class="pay" id="abertas"></strong></div>
  </div>
  <div class="charts-row">
  <section class="panel">
    <h2>Comparativo dos últimos 6 meses <em>Entradas e saídas</em></h2>
    <div class="chart-wrap">
      <div class="chart-y-axis" id="chartYAxis"></div>
      <div class="chart-grid-lines"><div></div><div></div><div></div><div></div><div></div></div>
      <div class="chart-bars" id="chartBars"></div>
    </div>
    <div class="chart-legend">
      <span><span class="legend-dot pagar"></span>Saídas</span>
      <span><span class="legend-dot receber"></span>Entradas</span>
    </div>
  </section>
  <section class="panel">
    <h2>Meta mensal por origem <em id="metaLegenda">Percentual da meta</em></h2>
    <div id="avisosMeta"></div>
    <div class="chart-line" id="metaChart"></div>
    <div class="chart-legend" id="metaLegend"></div>
  </section>
  </div>
  <section class="panel">
    <h2>Saídas por origem <em id="origemLegenda">Total do período</em></h2>
    <div class="chart-wrap">
      <div class="chart-y-axis" id="origemY"></div>
      <div class="chart-grid-lines"><div></div><div></div><div></div><div></div><div></div></div>
      <div class="chart-bars" id="origemBars"></div>
    </div>
    <p id="origemNota" class="origem-nota"></p>
  </section>
  <section class="panel">
    <h2>Fornecedores por loja <em id="fornecedorLojaLegenda">Uma coluna por fornecedor em cada local</em></h2>
    <div id="graficosLoja"></div>
  </section>
  <section class="panel">
    <h2>Fornecedores <em>Soma de todas as lojas</em></h2>
    <div id="graficoFornecedores"></div>
  </section>
  <section class="panel">
    <h2>Pendentes por mês <em>Títulos em aberto, pelo mês de vencimento</em></h2>
    <div id="graficoPendentes"></div>
  </section>
  <section class="panel">
    <h2>Entradas <em>Receitas da classificação financeira do SGF, somadas por dia. Esse relatório não separa por loja.</em></h2>
    <div class="tabela"><table>
      <thead><tr><th>Data</th><th>Descrição</th><th class="num">Recebida</th><th class="num">Em aberto</th><th class="num">Total</th></tr></thead>
      <tbody id="entradas"></tbody>
    </table></div>
  </section>
  <section class="panel">
    <h2 id="tituloSaidas">Saídas</h2>
    <div class="row">
      <label>Descrição / fornecedor <select id="filtroFornecedor"><option value="">Todos</option></select></label>
      <label class="check-inline"><input id="soPendentes" type="checkbox"> Só pendentes</label>
    </div>
    <div class="totalizador" id="totalSaidas"></div>
    <div id="sessoesSaidas"></div>
  </section>
  <section class="panel">
    <h2>Saldos das contas</h2>
    <div class="tabela"><table><thead><tr><th>Conta</th><th class="num">Saldo</th></tr></thead><tbody id="contas"></tbody></table></div>
  </section>
  <div class="note" id="nota"></div>
</main>
<script>
const DADOS = __DADOS__;
const METAS = __METAS__;
const METAS_ORIGEM = __METAS_ORIGEM__;
const moeda = new Intl.NumberFormat('pt-BR', {style:'currency', currency:'BRL'});
const titulosEmbutidos = DADOS.titulos || [];
const coberturaInicio = DADOS.titulosInicio || '';
const coberturaFim = DADOS.titulosFim || '';
const meses = ['jan','fev','mar','abr','mai','jun','jul','ago','set','out','nov','dez'];

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
function agrupar(lista) {
  const mapa = new Map();
  lista.forEach(item => {
    const chave = item.data + '\\n' + item.descricao;
    if (!mapa.has(chave)) mapa.set(chave, {data:item.data, descricao:item.descricao, baixado:0, aberto:0});
    const grupo = mapa.get(chave);
    if (item.status === 'pendente') grupo.aberto += Number(item.valor) || 0;
    else grupo.baixado += Number(item.valor) || 0;
  });
  return Array.from(mapa.values());
}
function linhasDoPeriodo(tipo, inicio, fim) {
  return (DADOS.linhas || []).filter(item => item.tipo === tipo && !item.atipico && dentro(item.data, inicio, fim));
}
function renderGrafico(fim) {
  const base = new Date(fim + 'T12:00:00');
  const serie = [];
  for (let i = 5; i >= 0; i--) serie.push(new Date(base.getFullYear(), base.getMonth() - i, 1));
  const dados = serie.map(data => {
    const chave = data.getFullYear() + '-' + String(data.getMonth() + 1).padStart(2, '0');
    const doMes = (DADOS.linhas || []).filter(item => !item.atipico && String(item.data || '').startsWith(chave));
    return {
      label: meses[data.getMonth()] + '/' + String(data.getFullYear()).slice(2),
      pagar: soma(doMes.filter(item => item.tipo === 'pagar'), 'valor'),
      receber: soma(doMes.filter(item => item.tipo === 'receber'), 'valor'),
      atual: chave === fim.slice(0, 7)
    };
  });
  const maximo = Math.max(...dados.flatMap(item => [item.pagar, item.receber]), 1);
  const escala = Math.ceil(maximo / 1000) * 1000 || 1000;
  let eixo = '';
  for (let i = 4; i >= 0; i--) {
    const valor = (escala / 4) * i;
    eixo += '<div>' + (valor >= 1000 ? (valor / 1000).toFixed(valor % 1000 === 0 ? 0 : 1) + 'k' : valor.toFixed(0)) + '</div>';
  }
  document.getElementById('chartYAxis').innerHTML = eixo;
  document.getElementById('chartBars').innerHTML = dados.map(item => {
    const alturaPagar = item.pagar === 0 ? 0 : Math.max(2, (item.pagar / escala) * 100);
    const alturaReceber = item.receber === 0 ? 0 : Math.max(2, (item.receber / escala) * 100);
    return '<div class="bar-group"><div class="bar-pair"><div class="bar bar-pagar" style="height:' + alturaPagar + '%" title="Saídas: ' + moeda.format(item.pagar) + '"></div><div class="bar bar-receber" style="height:' + alturaReceber + '%" title="Entradas: ' + moeda.format(item.receber) + '"></div></div><div class="bar-label" style="' + (item.atual ? 'color:var(--accent);font-weight:800;' : '') + '">' + item.label + '</div></div>';
  }).join('');
}

function codigoOrigem(codigo) {
  const texto = String(codigo ?? '').trim();
  if (/^\\d+$/.test(texto)) return String(parseInt(texto, 10));
  return texto;
}
function nomeOrigem(codigo) {
  const normal = codigoOrigem(codigo);
  if (normal === '100') return 'ADM | Central';
  if (normal === '1') return 'Matriz';
  if (!normal) return 'Sem filial';
  return 'Filial ' + normal;
}
function ordemOrigem(codigo) {
  const normal = codigoOrigem(codigo);
  if (normal === '1') return 0;
  if (normal === '100') return 1;
  const numero = parseInt(normal, 10);
  return Number.isFinite(numero) ? numero + 1 : 9999;
}
function totaisDe(lista) {
  return {
    quantidade: lista.length,
    valor: soma(lista, 'valor'),
    pago: soma(lista.filter(item => !item.atipico), 'pago'),
    aberto: soma(lista.filter(item => !item.pagamento), 'valor')
  };
}
function textoTotais(prefixo, totais) {
  return esc(prefixo) + ' · ' + totais.quantidade + (totais.quantidade === 1 ? ' título' : ' títulos')
    + ' · Valor ' + moeda.format(totais.valor)
    + ' · Pago ' + moeda.format(totais.pago)
    + ' · Em aberto ' + moeda.format(totais.aberto);
}
function renderGraficoOrigem(lista, legenda) {
  document.getElementById('origemLegenda').textContent = legenda;
  const grupos = new Map();
  lista.forEach(item => {
    const codigo = codigoOrigem(item.filial);
    grupos.set(codigo, (grupos.get(codigo) || 0) + (Number(item.valor) || 0));
  });
  const dados = [...grupos.entries()].sort((a, b) => ordemOrigem(a[0]) - ordemOrigem(b[0]));
  if (!dados.length) {
    document.getElementById('origemY').innerHTML = '';
    document.getElementById('origemBars').innerHTML = '<div class="sub">Sem saídas para este filtro.</div>';
    return;
  }
  const maximo = Math.max(...dados.map(item => item[1]), 1);
  const escala = Math.ceil(maximo / 1000) * 1000 || 1000;
  let eixo = '';
  for (let i = 4; i >= 0; i--) {
    const valor = (escala / 4) * i;
    eixo += '<div>' + (valor >= 1000 ? (valor / 1000).toFixed(valor % 1000 === 0 ? 0 : 1) + 'k' : valor.toFixed(0)) + '</div>';
  }
  document.getElementById('origemY').innerHTML = eixo;
  document.getElementById('origemBars').innerHTML = dados.map(item => {
    const altura = item[1] === 0 ? 0 : Math.max(2, (item[1] / escala) * 100);
    return '<div class="bar-group"><div class="bar-pair"><div class="bar bar-origem" style="height:' + altura + '%" title="' + esc(nomeOrigem(item[0])) + ': ' + moeda.format(item[1]) + '"></div></div><div class="bar-label">' + esc(nomeOrigem(item[0])) + '<br>' + moeda.format(item[1]) + '</div></div>';
  }).join('');
}
function notaOrigem(lista) {
  const nota = document.getElementById('origemNota');
  if (!nota) return;
  const codigos = new Set(lista.map(item => codigoOrigem(item.filial)));
  const todos = new Set(titulosEmbutidos.map(item => codigoOrigem(item.filial)));
  if (!todos.has('1')) {
    nota.textContent = 'A matriz é a filial 1 no contas a pagar. Esse código não veio no último sincronismo, por isso o gráfico não tem a coluna da matriz.';
    return;
  }
  if (!codigos.has('1')) {
    nota.textContent = 'A matriz tem títulos no sincronismo, fora do período ou do filtro desta tela.';
    return;
  }
  nota.textContent = '';
}
function gruposFornecedor(lista) {
  const mapa = new Map();
  lista.forEach(item => {
    const nome = item.fornecedor || '(Sem fornecedor)';
    mapa.set(nome, (mapa.get(nome) || 0) + (Number(item.valor) || 0));
  });
  return [...mapa.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'pt'));
}
function escalaMil(maximo) {
  return Math.ceil(Math.max(maximo, 1) / 1000) * 1000 || 1000;
}
function textoEixo(valor) {
  if (valor >= 1000) return (valor / 1000).toFixed(valor % 1000 === 0 ? 0 : 1) + 'k';
  return valor.toFixed(0);
}
function svgColunas(dados, girar) {
  if (!dados.length) return '<p>Nenhum valor neste recorte.</p>';
  const maior = dados.reduce((maximo, item) => Math.max(maximo, String(item[0]).length), 0);
  const larguraBarra = 28;
  const passo = girar ? Math.max(64, Math.min(120, 22 + maior * 3.4)) : 72;
  const esquerda = 52;
  const topo = 16;
  const alturaPlot = 150;
  const alturaRotulo = girar ? Math.max(120, Math.min(220, 28 + maior * 5.5)) : 40;
  const altura = topo + alturaPlot + 8 + alturaRotulo;
  const largura = Math.max(esquerda + dados.length * passo + 24, 280);
  const escala = escalaMil(Math.max(...dados.map(item => item[1])));
  let svg = '<svg width="' + largura + '" height="' + altura + '" role="img">';
  for (let i = 0; i <= 4; i++) {
    const valor = (escala / 4) * i;
    const y = topo + alturaPlot - (valor / escala) * alturaPlot;
    svg += '<line x1="' + esquerda + '" y1="' + y.toFixed(1) + '" x2="' + (largura - 8) + '" y2="' + y.toFixed(1) + '" stroke="#e2e9f2" stroke-dasharray="4 4"></line>';
    svg += '<text x="' + (esquerda - 6) + '" y="' + (y + 3).toFixed(1) + '" text-anchor="end" fill="#5c6b80" font-size="10">' + textoEixo(valor) + '</text>';
  }
  dados.forEach((item, indice) => {
    const x = esquerda + indice * passo + (passo - larguraBarra) / 2;
    const alturaBarra = item[1] === 0 ? 0 : Math.max(2, (item[1] / escala) * alturaPlot);
    const y = topo + alturaPlot - alturaBarra;
    const centro = x + larguraBarra / 2;
    svg += '<rect x="' + x.toFixed(1) + '" y="' + y.toFixed(1) + '" width="' + larguraBarra + '" height="' + alturaBarra.toFixed(1) + '" rx="4" fill="#dc2626"><title>' + esc(item[0]) + ' · ' + moeda.format(item[1]) + '</title></rect>';
    if (alturaBarra > 16) {
      svg += '<text x="' + centro.toFixed(1) + '" y="' + (y - 4).toFixed(1) + '" text-anchor="middle" fill="#5c6b80" font-size="10">' + textoEixo(item[1]) + '</text>';
    }
    const ty = topo + alturaPlot + 14;
    if (girar) {
      svg += '<text transform="rotate(45 ' + centro.toFixed(1) + ' ' + ty + ')" x="' + centro.toFixed(1) + '" y="' + ty + '" text-anchor="start" fill="#102033" font-size="11"><title>' + esc(item[0]) + '</title>' + esc(item[0]) + '</text>';
    } else {
      svg += '<text x="' + centro.toFixed(1) + '" y="' + (ty + 4) + '" text-anchor="middle" fill="#102033" font-size="11">' + esc(item[0]) + '</text>';
    }
  });
  svg += '</svg>';
  return '<div class="chart-scroll">' + svg + '</div>';
}
function renderFornecedores(lista) {
  const lojas = document.getElementById('graficosLoja');
  const geral = document.getElementById('graficoFornecedores');
  const legenda = document.getElementById('fornecedorLojaLegenda');
  if (!lojas || !geral) return;
  const codigos = [...new Set(lista.map(item => codigoOrigem(item.filial)))].sort((a, b) => ordemOrigem(a) - ordemOrigem(b));
  lojas.innerHTML = codigos.map(codigo => {
    const linhas = lista.filter(item => codigoOrigem(item.filial) === codigo);
    return '<section class="loja-bloco"><h3>' + esc(nomeOrigem(codigo)) + '</h3>' + svgColunas(gruposFornecedor(linhas), true) + '</section>';
  }).join('') || '<p>Nenhuma saída neste recorte.</p>';
  geral.innerHTML = svgColunas(gruposFornecedor(lista), true);
  if (legenda) legenda.textContent = codigos.length ? codigos.length + (codigos.length === 1 ? ' local' : ' locais') : 'Nenhum local neste recorte';
}
function mesesDoRecorte(inicio, fim, chaves) {
  const lista = [...chaves].filter(Boolean).sort();
  if (!lista.length) return [];
  const de = (inicio || lista[0]).slice(0, 7);
  const ate = (fim || lista[lista.length - 1]).slice(0, 7);
  const cursor = new Date(de + '-01T12:00:00');
  const limite = new Date(ate + '-01T12:00:00');
  const saida = [];
  while (cursor <= limite && saida.length < 36) {
    const chave = chaveMes(cursor);
    saida.push({chave: chave, label: meses[cursor.getMonth()] + '/' + String(cursor.getFullYear()).slice(2)});
    cursor.setMonth(cursor.getMonth() + 1);
  }
  return saida;
}
function renderPendentesMes(lista, inicio, fim) {
  const caixa = document.getElementById('graficoPendentes');
  if (!caixa) return;
  const abertos = lista.filter(item => !item.pagamento && !item.atipico);
  const mapa = new Map();
  abertos.forEach(item => {
    const chave = String(item.vencimento || '').slice(0, 7);
    if (!chave) return;
    mapa.set(chave, (mapa.get(chave) || 0) + (Number(item.valor) || 0));
  });
  const serie = mesesDoRecorte(inicio, fim, mapa.keys());
  if (!serie.length) {
    caixa.innerHTML = '<p>Nenhum título em aberto neste período.</p>';
    return;
  }
  caixa.innerHTML = svgColunas(serie.map(mes => [mes.label, mapa.get(mes.chave) || 0]), false);
}

function chaveMes(data) {
  return data.getFullYear() + '-' + String(data.getMonth() + 1).padStart(2, '0');
}
function serieMeses(fim) {
  const base = new Date(fim + 'T12:00:00');
  const lista = [];
  for (let i = 5; i >= 0; i--) {
    const data = new Date(base.getFullYear(), base.getMonth() - i, 1);
    lista.push({chave: chaveMes(data), label: meses[data.getMonth()] + '/' + String(data.getFullYear()).slice(2)});
  }
  return lista;
}
function fimDoMes(chave) {
  const partes = chave.split('-');
  const dia = new Date(Number(partes[0]), Number(partes[1]), 0).getDate();
  return chave + '-' + String(dia).padStart(2, '0');
}
function mesCoberto(chave) {
  if (!coberturaInicio || !coberturaFim) return false;
  return fimDoMes(chave) >= coberturaInicio && (chave + '-01') <= coberturaFim;
}
function textoPct(valor) {
  const arred = Math.round(valor * 10) / 10;
  return (Number.isInteger(arred) ? String(arred) : arred.toFixed(1).replace('.', ',')) + '%';
}
function renderGraficoMetas(fim) {
  const caixa = document.getElementById('metaChart');
  const avisos = document.getElementById('avisosMeta');
  const legenda = document.getElementById('metaLegend');
  const titulo = document.getElementById('metaLegenda');
  if (!caixa || !avisos || !legenda) return;
  const config = METAS_ORIGEM || {percentual: null, itens: []};
  const itens = (config.itens || []).filter(item => Number(item.valor) > 0);
  const limite = Number(config.percentual);
  const limiteOk = Number.isFinite(limite) && limite >= 1 && limite <= 100;
  if (!itens.length) {
    caixa.innerHTML = '<p class="sub">Nenhuma meta mensal definida.</p>';
    avisos.innerHTML = '';
    legenda.innerHTML = '';
    if (titulo) titulo.textContent = 'Percentual da meta';
    return;
  }
  const serie = serieMeses(fim || coberturaFim || chaveMes(new Date()));
  const hoje = chaveMes(new Date());
  const neutras = ['#94a3b8', '#64748b', '#475569', '#cbd5e1', '#334155'];
  const alertasCor = ['#dc2626', '#c2410c', '#7c3aed', '#be185d', '#b45309'];
  const series = itens.map(item => {
    const codigo = String(item.origem ?? '');
    const meta = Number(item.valor);
    const pontos = serie.map(mes => {
      if (!mesCoberto(mes.chave)) return {chave: mes.chave, label: mes.label, nulo: true};
      const realizado = soma(titulosEmbutidos.filter(titulo => codigoOrigem(titulo.filial) === codigoOrigem(codigo) && String(titulo.vencimento || '').startsWith(mes.chave)), 'valor');
      return {chave: mes.chave, label: mes.label, nulo: false, pct: (realizado / meta) * 100, realizado: realizado};
    });
    const atual = pontos.find(ponto => ponto.chave === hoje && !ponto.nulo) || null;
    return {codigo: codigo, nome: nomeOrigem(codigo), meta: meta, pontos: pontos, atual: atual, estourou: !!(limiteOk && atual && atual.pct >= limite)};
  }).sort((a, b) => ordemOrigem(a.codigo) - ordemOrigem(b.codigo));
  const valores = series.flatMap(item => item.pontos.filter(ponto => !ponto.nulo).map(ponto => ponto.pct));
  const topo = Math.max(100, limiteOk ? limite : 0, ...valores, 1);
  const escala = Math.ceil(topo / 10) * 10;
  const largura = 320;
  const altura = 200;
  const esquerda = 36;
  const direita = 8;
  const topoPx = 16;
  const basePx = 28;
  const plotW = largura - esquerda - direita;
  const plotH = altura - topoPx - basePx;
  const xDe = indice => serie.length === 1 ? esquerda + plotW / 2 : esquerda + (plotW * indice) / (serie.length - 1);
  const yDe = pct => topoPx + plotH - (pct / escala) * plotH;
  let svg = '<svg viewBox="0 0 ' + largura + ' ' + altura + '" role="img" aria-label="Percentual da meta mensal por origem">';
  for (let i = 0; i <= 4; i++) {
    const valor = (escala / 4) * i;
    const y = yDe(valor);
    svg += '<line x1="' + esquerda + '" y1="' + y.toFixed(1) + '" x2="' + (largura - direita) + '" y2="' + y.toFixed(1) + '" stroke="#e2e9f2"></line>';
    svg += '<text x="' + (esquerda - 4) + '" y="' + (y + 3).toFixed(1) + '" text-anchor="end" fill="#5c6b80" font-size="10">' + Math.round(valor) + '%</text>';
  }
  if (limiteOk) {
    const yLimite = yDe(limite);
    svg += '<line class="limite-meta" x1="' + esquerda + '" y1="' + yLimite.toFixed(1) + '" x2="' + (largura - direita) + '" y2="' + yLimite.toFixed(1) + '" stroke="#64748b" stroke-dasharray="5 4" stroke-width="1.5"></line>';
  }
  let indiceNeutra = 0;
  let indiceAlerta = 0;
  series.forEach(item => {
    const cor = item.estourou ? alertasCor[indiceAlerta++ % alertasCor.length] : neutras[indiceNeutra++ % neutras.length];
    item.cor = cor;
    let caminho = '';
    let mover = true;
    item.pontos.forEach((ponto, indice) => {
      if (ponto.nulo) { mover = true; return; }
      const x = xDe(indice);
      const y = yDe(ponto.pct);
      caminho += (mover ? 'M' : 'L') + x.toFixed(1) + ' ' + y.toFixed(1) + ' ';
      mover = false;
    });
    svg += '<g class="' + (item.estourou ? 'serie-alerta' : 'serie-neutra') + '" data-origem="' + esc(item.codigo) + '" data-alerta="' + (item.estourou ? '1' : '0') + '">';
    if (caminho) svg += '<path d="' + caminho.trim() + '" fill="none" stroke="' + cor + '" stroke-width="' + (item.estourou ? '3' : '1.75') + '" stroke-linejoin="round" stroke-linecap="round"></path>';
    item.pontos.forEach((ponto, indice) => {
      if (ponto.nulo) return;
      const x = xDe(indice);
      const y = yDe(ponto.pct);
      const alertaPonto = item.estourou && ponto.chave === hoje;
      svg += '<circle class="' + (alertaPonto ? 'ponto-alerta' : 'ponto-neutro') + '" cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="' + (alertaPonto ? '5.5' : '3') + '" fill="' + cor + '"' + (alertaPonto ? ' stroke="#fff" stroke-width="2"' : '') + '><title>' + esc(item.nome) + ' · ' + esc(ponto.label) + ' · ' + moeda.format(ponto.realizado) + ' · ' + textoPct(ponto.pct) + '</title></circle>';
      if (alertaPonto) svg += '<text x="' + x.toFixed(1) + '" y="' + Math.max(12, y - 8).toFixed(1) + '" text-anchor="middle" fill="' + cor + '" font-size="11" font-weight="700">' + textoPct(ponto.pct) + '</text>';
    });
    svg += '</g>';
  });
  serie.forEach((mes, indice) => {
    svg += '<text x="' + xDe(indice).toFixed(1) + '" y="' + (altura - 8) + '" text-anchor="middle" font-size="10" fill="' + (mes.chave === hoje ? '#1d4ed8' : '#5c6b80') + '">' + esc(mes.label) + '</text>';
  });
  svg += '</svg>';
  caixa.innerHTML = svg;
  avisos.innerHTML = series.filter(item => item.estourou).map(item =>
    '<div class="alerta alerta-vermelho" data-alerta="1" data-origem="' + esc(item.codigo) + '">' + esc(item.nome) + ' atingiu ' + textoPct(item.atual.pct) + ' da meta neste mês.</div>'
  ).join('');
  legenda.innerHTML = series.map(item => {
    const atual = item.pontos.find(ponto => ponto.chave === hoje && !ponto.nulo);
    return '<span class="' + (item.estourou ? 'serie-alerta' : 'serie-neutra') + '" data-origem="' + esc(item.codigo) + '" data-alerta="' + (item.estourou ? '1' : '0') + '"><span class="legend-dot" style="background:' + item.cor + '"></span>' + esc(item.nome) + (atual ? ' · ' + textoPct(atual.pct) : '') + '</span>';
  }).join('');
  if (titulo) {
    let texto = limiteOk ? 'Aviso em ' + textoPct(limite) : 'Defina o percentual de aviso';
    if (serie.some(mes => !mesCoberto(mes.chave))) texto += ' · sem ponto fora do sincronismo';
    titulo.textContent = texto;
  }
}

function renderAlertas(inicio, fim) {
  const caixa = document.getElementById('alertas');
  if (!caixa) return;
  caixa.innerHTML = (METAS || []).map(meta => {
    const total = soma((DADOS.linhas || []).filter(item => !item.atipico && item.tipo === meta.tipo && item.descricao === meta.categoria && dentro(item.data, inicio, fim)), 'valor');
    if (total < Number(meta.valor)) return '';
    const receita = meta.tipo === 'receber';
    return '<div class="alerta ' + (receita ? 'alerta-verde' : 'alerta-vermelho') + '">'
      + (receita ? 'Meta de receita atingida' : 'Meta de despesa atingida')
      + ': ' + esc(meta.categoria) + ' · ' + moeda.format(total) + ' de ' + moeda.format(Number(meta.valor)) + '</div>';
  }).join('');
}

function render() {
  const inicio = document.getElementById('inicio').value;
  const fim = document.getElementById('fim').value;
  renderAlertas(inicio, fim);
  const titulos = titulosEmbutidos;
  const entradas = linhasDoPeriodo('receber', inicio, fim);
  const soPendentes = document.getElementById('soPendentes');
  if (soPendentes && !soPendentes.dataset.pronto) {
    soPendentes.dataset.pronto = '1';
    soPendentes.onchange = render;
  }
  const pendentes = !!(soPendentes && soPendentes.checked);
  const saidasPeriodo = titulos.filter(item => dentro(item.vencimento, inicio, fim) && (!pendentes || !item.pagamento));
  const select = document.getElementById('filtroFornecedor');
  const selecionadoAntes = select.value;
  const nomes = [...new Set(saidasPeriodo.map(item => item.fornecedor || ''))].sort((a, b) => a.localeCompare(b, 'pt'));
  const selecionado = nomes.includes(selecionadoAntes) ? selecionadoAntes : '';
  select.onchange = null;
  select.innerHTML = '<option value="">Todos</option>' + nomes.map(nome =>
    '<option value="' + esc(nome) + '">' + esc(nome || '(Sem descrição)') + '</option>'
  ).join('');
  select.value = selecionado;
  select.onchange = render;
  const saidasTitulos = selecionado ? saidasPeriodo.filter(item => (item.fornecedor || '') === selecionado) : saidasPeriodo;
  const totalEntradas = soma(entradas, 'valor');
  const totais = totaisDe(saidasTitulos);
  const totalSaidas = totais.valor;
  document.getElementById('receber').textContent = moeda.format(totalEntradas);
  document.getElementById('pagar').textContent = moeda.format(totalSaidas);
  const saldo = totalEntradas - totalSaidas;
  const saldoEl = document.getElementById('saldo');
  saldoEl.textContent = moeda.format(saldo);
  saldoEl.className = saldo >= 0 ? 'rec' : 'pay';
  document.getElementById('abertas').textContent = moeda.format(totais.aberto);
  renderGrafico(fim);
  renderGraficoMetas(fim);
  renderGraficoOrigem(saidasTitulos, (pendentes ? 'Somente em aberto' : (selecionado ? selecionado : 'Total do período')));
  notaOrigem(saidasPeriodo);
  renderFornecedores(saidasTitulos);
  renderPendentesMes(saidasPeriodo, inicio, fim);

  document.getElementById('entradas').innerHTML = agrupar(entradas).map(item =>
    '<tr><td>' + br(item.data) + '</td><td>' + esc(item.descricao) + '</td><td class="num rec">' + moeda.format(item.baixado) + '</td><td class="num">' + moeda.format(item.aberto) + '</td><td class="num">' + moeda.format(item.baixado + item.aberto) + '</td></tr>'
  ).join('') || '<tr><td colspan="5">Nenhuma entrada nesse período.</td></tr>';

  const titulo = document.getElementById('tituloSaidas');
  const sessoes = document.getElementById('sessoesSaidas');
  const caixaTotal = document.getElementById('totalSaidas');
  titulo.innerHTML = 'Saídas <em>' + (selecionado ? esc(selecionado) : 'Uma subseção por origem') + '</em>';
  caixaTotal.innerHTML = textoTotais('Total', totais);
  const origens = [...new Set(saidasTitulos.map(item => codigoOrigem(item.filial)))].sort((a, b) => ordemOrigem(a) - ordemOrigem(b));
  sessoes.innerHTML = origens.map(codigo => {
    const linhas = saidasTitulos.filter(item => codigoOrigem(item.filial) === codigo);
    const parcial = totaisDe(linhas);
    return '<details class="sessao"><summary><span>' + esc(nomeOrigem(codigo)) + '</span><em>Total ' + moeda.format(parcial.valor) + ' · Em aberto ' + moeda.format(parcial.aberto) + '</em></summary>'
      + '<div class="tabela"><table><thead><tr><th>Descrição</th><th>Documento</th><th>Emissão</th><th>Vencimento</th><th class="num">Valor</th><th class="num">Pago</th><th class="num">Juros</th><th>Pagamento</th><th>Observação</th></tr></thead><tbody>'
      + linhas.map(item =>
        '<tr class="' + (item.atipico ? 'atipico' : '') + '"><td>' + esc(item.fornecedor) + '</td><td>' + esc(item.documento) + '</td><td>' + br(item.emissao) + '</td><td>' + br(item.vencimento) + '</td><td class="num">' + moedaOuVazio(item.valor) + '</td><td class="num">' + (item.atipico ? 'ilegível' : moedaOuVazio(item.pago)) + '</td><td class="num">' + (item.atipico ? '' : moedaOuVazio(item.juros)) + '</td><td>' + (item.pagamento ? br(item.pagamento) : 'Em aberto') + '</td><td>' + esc(item.obs) + '</td></tr>'
      ).join('')
      + '</tbody><tfoot><tr><td colspan="4">Subtotal · ' + parcial.quantidade + (parcial.quantidade === 1 ? ' título' : ' títulos') + '</td><td class="num">' + moeda.format(parcial.valor) + '</td><td class="num">' + moeda.format(parcial.pago) + '</td><td></td><td class="num">' + moeda.format(parcial.aberto) + '</td><td></td></tr></tfoot></table></div></details>';
  }).join('') || '<p>Nenhuma saída nesse período.</p>';

  document.getElementById('contas').innerHTML = (DADOS.contas || []).map(conta =>
    '<tr><td>' + esc(conta.nome) + '</td><td class="num">' + moeda.format(conta.saldo) + '</td></tr>'
  ).join('');

  document.getElementById('meta').textContent = (DADOS.filial || '') + ' · atualizado em ' + textoQuando(DADOS.geradoEm || '');
  const contasFora = (DADOS.contas || []).filter(conta => Math.abs(conta.saldo) >= 1000000).map(conta => conta.nome);
  let nota = 'As entradas vêm da classificação financeira do SGF: cada movimento com tipo receita entra na soma do dia, sem divisão por loja.';
  nota += ' As saídas vêm do contas a pagar. Cada filial e a matriz ficam fechadas até serem abertas. Só pendentes esconde o que já foi pago e mantém os meses em aberto.';
  nota += ' Atualizar pede um sincronismo em segundo plano; a tela continua no último sincronismo até ele terminar.';
  if (coberturaInicio && coberturaFim && (inicio < coberturaInicio || fim > coberturaFim)) {
    nota += ' O último sincronismo cobre ' + br(coberturaInicio) + ' a ' + br(coberturaFim) + '.';
  }
  if (contasFora.length) nota += ' Saldo fora de escala: ' + contasFora.join(', ') + '.';
  document.getElementById('nota').textContent = nota;
}

function isoLocal(data) {
  const mes = String(data.getMonth() + 1).padStart(2, '0');
  const dia = String(data.getDate()).padStart(2, '0');
  return data.getFullYear() + '-' + mes + '-' + dia;
}
function textoQuando(iso) {
  if (!iso) return '';
  const data = new Date(String(iso).replace(' ', 'T'));
  if (!Number.isFinite(data.getTime())) return String(iso).replace('T', ' ');
  const texto = new Intl.DateTimeFormat('pt-BR', {
    timeZone: 'America/Fortaleza',
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23'
  }).format(data);
  return texto + ' (−03:00, Fortaleza)';
}
function textoIdade(iso) {
  if (!iso) return 'sem sincronização';
  const ms = Date.now() - new Date(String(iso).replace(' ', 'T')).getTime();
  if (!Number.isFinite(ms)) return 'sem sincronização';
  const minutos = Math.floor(Math.max(0, ms) / 60000);
  if (minutos < 1) return 'há menos de 1 min';
  if (minutos < 60) return 'há ' + minutos + ' min';
  const horas = Math.floor(minutos / 60);
  if (horas < 24) return 'há ' + horas + ' h';
  return 'há ' + Math.floor(horas / 24) + ' d';
}
function pintarIdade() {
  const el = document.getElementById('idadeSync');
  if (el) el.textContent = textoIdade(DADOS.geradoEm || '');
}
pintarIdade();
setInterval(pintarIdade, 15000);

const inicioEl = document.getElementById('inicio');
const fimEl = document.getElementById('fim');
const agora = new Date();
if (coberturaInicio && coberturaFim) {
  inicioEl.value = coberturaInicio;
  fimEl.value = coberturaFim;
} else {
  inicioEl.value = isoLocal(new Date(agora.getFullYear(), agora.getMonth(), 1));
  fimEl.value = isoLocal(agora);
}
document.getElementById('buscar').onclick = render;
inicioEl.onchange = render;
fimEl.onchange = render;
render();
</script>
</body>
</html>
"""


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
    hoje = date.today()
    padrao = hoje.replace(day=1).isoformat(), hoje.isoformat()
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
