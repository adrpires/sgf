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
  input[type="date"] { border:1px solid var(--line); border-radius:8px; padding:6px 8px; font:inherit; }
  .tabela { overflow:auto; max-height:540px; }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); font-size:13px; white-space:nowrap; }
  th { color:var(--muted); font-weight:600; position:sticky; top:0; background:#fff; }
  td.num, th.num { text-align:right; }
  tr.atipico td { background:#fff7e8; }
  .note { background:#fff7e8; border:1px solid #f0d7a2; border-radius:10px; padding:10px 12px; margin-top:12px; }
  @media (max-width:800px) { .cards { grid-template-columns:1fr 1fr; } }
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
  </div>
  <div class="cards">
    <div class="card"><span>Entradas</span><strong class="rec" id="receber"></strong></div>
    <div class="card"><span>Saídas</span><strong class="pay" id="pagar"></strong></div>
    <div class="card"><span>Saldo</span><strong id="saldo"></strong></div>
    <div class="card"><span>Saídas em aberto</span><strong class="pay" id="abertas"></strong></div>
  </div>
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
    <h2>Entradas <em>Recebida e em aberto no mesmo dia</em></h2>
    <div class="tabela"><table>
      <thead><tr><th>Data</th><th>Descrição</th><th class="num">Recebida</th><th class="num">Em aberto</th><th class="num">Total</th></tr></thead>
      <tbody id="entradas"></tbody>
    </table></div>
  </section>
  <section class="panel">
    <h2 id="tituloSaidas">Saídas</h2>
    <div class="tabela"><table id="tabelaSaidas"></table></div>
  </section>
  <section class="panel">
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

function render() {
  const inicio = document.getElementById('inicio').value;
  const fim = document.getElementById('fim').value;
  const entradas = linhasDoPeriodo('receber', inicio, fim);
  const saidasDiarias = linhasDoPeriodo('pagar', inicio, fim);
  const saidasTitulos = titulos.filter(item => dentro(item.vencimento, inicio, fim));
  const usarTitulos = saidasTitulos.length > 0;
  const totalEntradas = soma(entradas, 'valor');
  const totalSaidas = usarTitulos ? soma(saidasTitulos, 'valor') : soma(saidasDiarias, 'valor');
  const abertas = usarTitulos ? saidasTitulos.filter(item => !item.pagamento) : saidasDiarias.filter(item => item.status === 'pendente');
  document.getElementById('receber').textContent = moeda.format(totalEntradas);
  document.getElementById('pagar').textContent = moeda.format(totalSaidas);
  const saldo = totalEntradas - totalSaidas;
  const saldoEl = document.getElementById('saldo');
  saldoEl.textContent = moeda.format(saldo);
  saldoEl.className = saldo >= 0 ? 'rec' : 'pay';
  document.getElementById('abertas').textContent = moeda.format(soma(abertas, 'valor'));
  renderGrafico(fim);

  document.getElementById('entradas').innerHTML = agrupar(entradas).map(item =>
    '<tr><td>' + br(item.data) + '</td><td>' + esc(item.descricao) + '</td><td class="num rec">' + moeda.format(item.baixado) + '</td><td class="num">' + moeda.format(item.aberto) + '</td><td class="num">' + moeda.format(item.baixado + item.aberto) + '</td></tr>'
  ).join('') || '<tr><td colspan="5">Nenhuma entrada nesse período.</td></tr>';

  const titulo = document.getElementById('tituloSaidas');
  const tabela = document.getElementById('tabelaSaidas');
  if (usarTitulos) {
    titulo.innerHTML = 'Saídas <em>Títulos por vencimento</em>';
    tabela.innerHTML = '<thead><tr><th>Fornecedor</th><th>Documento</th><th>Filial</th><th>Emissão</th><th>Vencimento</th><th class="num">Valor</th><th class="num">Pago</th><th class="num">Juros</th><th>Pagamento</th><th>Observação</th></tr></thead><tbody>'
      + saidasTitulos.map(item =>
        '<tr class="' + (item.atipico ? 'atipico' : '') + '"><td>' + esc(item.fornecedor) + '</td><td>' + esc(item.documento) + '</td><td>' + esc(item.filial) + '</td><td>' + br(item.emissao) + '</td><td>' + br(item.vencimento) + '</td><td class="num">' + moedaOuVazio(item.valor) + '</td><td class="num">' + (item.atipico ? 'ilegível' : moedaOuVazio(item.pago)) + '</td><td class="num">' + (item.atipico ? '' : moedaOuVazio(item.juros)) + '</td><td>' + (item.pagamento ? br(item.pagamento) : 'Em aberto') + '</td><td>' + esc(item.obs) + '</td></tr>'
      ).join('') + '</tbody>';
  } else {
    titulo.innerHTML = 'Saídas <em>Paga e em aberto no mesmo dia</em>';
    tabela.innerHTML = '<thead><tr><th>Data</th><th>Descrição</th><th class="num">Paga</th><th class="num">Em aberto</th><th class="num">Total</th></tr></thead><tbody>'
      + (agrupar(saidasDiarias).map(item =>
        '<tr><td>' + br(item.data) + '</td><td>' + esc(item.descricao) + '</td><td class="num pay">' + moeda.format(item.baixado) + '</td><td class="num">' + moeda.format(item.aberto) + '</td><td class="num">' + moeda.format(item.baixado + item.aberto) + '</td></tr>'
      ).join('') || '<tr><td colspan="5">Nenhuma saída nesse período.</td></tr>') + '</tbody>';
  }

  document.getElementById('contas').innerHTML = (DADOS.contas || []).map(conta =>
    '<tr><td>' + esc(conta.nome) + '</td><td class="num">' + moeda.format(conta.saldo) + '</td></tr>'
  ).join('');

  document.getElementById('meta').textContent = (DADOS.filial || '') + ' · atualizado em ' + String(DADOS.geradoEm || '').replace('T', ' ');
  const contasFora = (DADOS.contas || []).filter(conta => Math.abs(conta.saldo) >= 1000000).map(conta => conta.nome);
  let nota = 'O SGF separa cada dia em duas partes: o que já foi baixado e o que continua em aberto. A tabela junta as duas na mesma linha.';
  if (usarTitulos) {
    const cobertura = br(vencimentos[0]) + ' a ' + br(vencimentos[vencimentos.length - 1]);
    nota += ' As saídas deste período saem do relatório de títulos, com vencimento de ' + cobertura + '.';
  } else {
    nota += ' As saídas são o total diário de despesas. O nome do fornecedor aparece quando o relatório de títulos está no servidor.';
  }
  if (contasFora.length) nota += ' Saldo fora de escala: ' + contasFora.join(', ') + '.';
  document.getElementById('nota').textContent = nota;
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


def logo_data():
    caminho = Path(__file__).resolve().parent / "marca.png"
    if not caminho.exists():
        return ""
    import base64

    return "data:image/png;base64," + base64.b64encode(caminho.read_bytes()).decode("ascii")


def gravar(payload, filial):
    payload["filial"] = filial
    payload["titulos"] = carregar_titulos()
    html = HTML.replace("__DADOS__", json.dumps(payload, ensure_ascii=False)).replace("__LOGO__", logo_data())
    arquivo = destino()
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    arquivo.write_text(html, encoding="utf-8")
    print(f"painel {arquivo} linhas={len(payload['linhas'])} titulos={len(payload['titulos'])} contas={len(payload['contas'])}")


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


def ler_pedido():
    arquivo = controle_dir() / "pedido.json"
    if not arquivo.exists():
        return {}
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dados if isinstance(dados, dict) else {}


def ciclo(base, user, password, estado):
    if estado.get("user") != user or estado.get("password") != password:
        estado.pop("token", None)
    estado["user"] = user
    estado["password"] = password
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
    estado = {}
    ultimo_id = ""
    proximo = 0.0
    while True:
        pedido = ler_pedido()
        pedido_id = str(pedido.get("id") or "")
        agora = time.time()
        por_pedido = bool(pedido_id) and pedido_id != ultimo_id
        por_relogio = args.a_cada > 0 and agora >= proximo
        if not (por_pedido or por_relogio or proximo == 0):
            if args.a_cada <= 0:
                break
            time.sleep(0.4)
            continue
        ok = False
        try:
            user, password = credenciais()
            ciclo(base, user, password, estado)
            ok = True
        except Exception as exc:
            print("falha", exc)
            estado.clear()
            if args.a_cada <= 0 and not por_pedido:
                raise
        if pedido_id:
            gravar_json(controle_dir() / "feito.json", {"id": pedido_id, "ok": ok})
            ultimo_id = pedido_id
        proximo = time.time() + (args.a_cada * 60 if args.a_cada > 0 else 10**12)
        if args.a_cada <= 0 and not por_pedido:
            break


if __name__ == "__main__":
    main()
