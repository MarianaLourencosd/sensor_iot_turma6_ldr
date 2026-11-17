import csv, json, math, random, re, sys, threading, time
from collections import deque
from datetime import datetime
from pathlib import Path

import serial
from serial.tools import list_ports
from flask import Flask, jsonify, send_file

PORTA_SERIAL = 'COM9'      
BAUD_RATE = 9600
LIMITE = 200             
PASTA = Path(__file__).resolve().parent
DATALAKE = PASTA / 'datalake'
SIMULAR = '--simular' in sys.argv

lock = threading.Lock()
log_linhas = deque(maxlen=50)
dados = {"luz": 0, "risco": False, "hora": "--:--:--", "status": "Iniciando...",
         "firebase": "desligado", "modo": "simulacao" if SIMULAR else "arduino",
         "leituras": 0, "inicio": time.time()}


def log(msg):
    linha = f"{time.strftime('%H:%M:%S')}  {msg}"
    print(linha)
    with lock:
        log_linhas.append(linha)

db = None
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    firebase_admin.initialize_app(credentials.Certificate(str(PASTA / 'firebase_key.json')))
    db = firestore.client()
    dados["firebase"] = "ativo"
    log("Firebase conectado.")
except Exception as e:
    log(f"Firebase desligado ({e}). O resto continua funcionando.")


def salvar_firebase(luz, risco):
    if db:
        try:
            db.collection("historico_luz").add({"luz": luz, "risco": risco, "timestamp": firestore.SERVER_TIMESTAMP})
        except Exception as e:
            log(f"Erro no Firebase: {e}")


def salvar_datalake(luz, risco, linha):
    agora = datetime.now()
    dia = agora.strftime('%Y-%m-%d')
    (DATALAKE / 'bruto').mkdir(parents=True, exist_ok=True)
    (DATALAKE / 'tratado').mkdir(parents=True, exist_ok=True)

    with open(DATALAKE / 'bruto' / f'{dia}.jsonl', 'a', encoding='utf-8') as f:
        f.write(json.dumps({"data_hora": agora.isoformat(timespec='seconds'), "linha": linha}) + '\n')

    arquivo = DATALAKE / 'tratado' / f'{dia}.csv'
    novo = not arquivo.exists()
    with open(arquivo, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        if novo:
            w.writerow(['data_hora', 'luz', 'risco'])
        w.writerow([agora.isoformat(timespec='seconds'), luz, int(risco)])


ultima = {"datalake": 0, "firebase": 0}


def registrar_leitura(luz, risco, origem, linha):
    agora = time.time()
    with lock:
        dados.update(luz=luz, risco=risco, hora=time.strftime('%H:%M:%S'),
                     status=f"Ativo ({origem})", leituras=dados["leituras"] + 1)
    if agora - ultima["datalake"] >= 1:        
        salvar_datalake(luz, risco, linha)
        ultima["datalake"] = agora
    if agora - ultima["firebase"] >= 5:        
        threading.Thread(target=salvar_firebase, args=(luz, risco), daemon=True).start()
        ultima["firebase"] = agora


def extrair_leitura(linha):
    achou = re.search(r"(?:Luz|Area B)\s*:\s*(\d+)", linha, re.I) or re.fullmatch(r"\s*(\d+)\s*", linha)
    if not achou:
        return None
    luz = int(achou.group(1))
    risco = "RISCO" in linha.upper() or ("NORMAL" not in linha.upper() and luz > LIMITE)
    return luz, risco


def ler_arduino():
    while True:
        portas = sorted(list_ports.comports(), key=lambda p: p.device != PORTA_SERIAL)
        if not portas:
            dados["status"] = "Nenhuma porta COM encontrada. O Arduino está conectado?"
            time.sleep(3)
            continue
        for p in portas:
            try:
                with serial.Serial(p.device, BAUD_RATE, timeout=1) as ser:
                    time.sleep(2)
                    log(f"Testando {p.device} ({p.description})...")
                    dados["status"] = f"Testando {p.device}..."
                    ultima_valida = time.time()
                    while time.time() - ultima_valida < 6:
                        linha = ser.readline().decode('utf-8', errors='ignore').strip()
                        if not linha:
                            continue
                        log(f"[{p.device}] {linha}")
                        leitura = extrair_leitura(linha)
                        if leitura:
                            ultima_valida = time.time()
                            registrar_leitura(*leitura, p.device, linha)
                    log(f"{p.device} não mandou leituras. Tentando outra porta.")
            except Exception as e:
                dados["status"] = f"Erro na {p.device}: feche o Monitor Serial da Arduino IDE"
                log(f"Erro na {p.device}: {e}")
                time.sleep(1)


def simular():
    log("MODO SIMULAÇÃO: valores gerados pelo Python, sem Arduino.")
    t = 0
    while True:
        luz = int(200 + 160 * math.sin(t) + random.randint(-25, 25))
        risco = luz > LIMITE
        linha = f"Luz: {luz} | " + ("RISCO - LEDs ACESOS" if risco else "NORMAL")
        log(f"[SIMULADOR] {linha}")
        registrar_leitura(luz, risco, "SIMULADOR", linha)
        t += 0.25
        time.sleep(0.5)


def gerar_relatorio():
    dia = datetime.now().strftime('%Y-%m-%d')
    arquivo = DATALAKE / 'tratado' / f'{dia}.csv'
    linhas = list(csv.DictReader(open(arquivo, encoding='utf-8'))) if arquivo.exists() else []
    valores = [int(l['luz']) for l in linhas]
    riscos = sum(int(l['risco']) for l in linhas)
    return {
        "dia": dia,
        "leituras": len(valores),
        "media": round(sum(valores) / len(valores), 1) if valores else 0,
        "minimo": min(valores, default=0),
        "maximo": max(valores, default=0),
        "pct_risco": round(100 * riscos / len(valores), 1) if valores else 0,
        "ultimas": valores[-60:],
        "ligado_ha": int(time.time() - dados["inicio"]),
        "arquivos": sorted(str(a.relative_to(DATALAKE)).replace('\\', '/') for a in DATALAKE.rglob('*.*')) if DATALAKE.exists() else [],
    }


app = Flask(__name__)


@app.route('/')
def pagina():
    return PAGINA


@app.route('/api/dados')
def api_dados():
    with lock:
        return jsonify({**dados, "limite": LIMITE, "log": list(log_linhas)})


@app.route('/api/relatorio')
def api_relatorio():
    return jsonify(gerar_relatorio())


@app.route('/download')
def download():
    arquivo = DATALAKE / 'tratado' / f"{datetime.now():%Y-%m-%d}.csv"
    return send_file(arquivo, as_attachment=True) if arquivo.exists() else ("Sem dados ainda", 404)


PAGINA = r"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="UTF-8"><title>Monitor de Luz</title>
<style>
 *{box-sizing:border-box;margin:0}
 body{font-family:Segoe UI,sans-serif;background:#0f172a;color:#f8fafc;padding:20px}
 .grade{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px;max-width:1100px;margin:auto}
 .card{background:#1e293b;border-radius:16px;padding:20px}
 h1{text-align:center;margin-bottom:16px} h2{font-size:1rem;margin-bottom:12px;color:#94a3b8}
 .valor{font-size:5rem;font-weight:800;text-align:center} .centro{text-align:center}
 .badge{display:inline-block;padding:6px 16px;border-radius:99px;font-weight:700;background:#334155}
 .leds{display:flex;gap:14px;justify-content:center;margin:16px 0}
 .led{width:24px;height:24px;border-radius:50%;background:#3f1d1d} .led.on{background:#ef4444;box-shadow:0 0 14px #ef4444}
 .barra{height:12px;background:#0f172a;border-radius:99px;overflow:hidden;margin-top:12px} .fill{height:100%;width:0}
 .term{background:#020617;height:230px;overflow-y:auto;font-family:Consolas,monospace;font-size:.78rem;padding:10px;border-radius:10px}
 .r{color:#f87171}.n{color:#34d399}.e{color:#fbbf24}
 table{width:100%;font-size:.9rem} td{padding:5px 0} td:last-child{text-align:right;font-weight:700}
 .sim{background:#581c87;color:#e9d5ff;text-align:center;padding:8px;border-radius:10px;max-width:1100px;margin:0 auto 14px;display:none}
 a{color:#38bdf8} .fluxo{line-height:2;font-size:.9rem}
</style></head><body>
<h1>💡 Monitoramento de Luz</h1>
<div class="sim" id="sim">🧪 MODO SIMULAÇÃO: valores gerados pelo Python, não pelo Arduino</div>
<div class="grade">
 <div class="card centro">
  <h2>Leitura ao vivo (sensor LDR)</h2>
  <div class="valor" id="luz">--</div><div>de 1023 • limite <b id="lim">--</b></div>
  <div class="barra"><div class="fill" id="fill"></div></div>
  <div class="leds"><div class="led"></div><div class="led"></div><div class="led"></div></div>
  <span class="badge" id="badge">Aguardando...</span>
  <p style="margin-top:12px;color:#94a3b8">Status: <b id="status">--</b> • Firebase: <b id="fb">--</b></p>
 </div>
 <div class="card"><h2>📟 Serial + Python (ao vivo)</h2><div class="term" id="term"></div></div>
 <div class="card">
  <h2>📊 Relatório do servidor (hoje)</h2>
  <table>
   <tr><td>Leituras gravadas</td><td id="r1">--</td></tr>
   <tr><td>Média de luz</td><td id="r2">--</td></tr>
   <tr><td>Mínimo / Máximo</td><td id="r3">--</td></tr>
   <tr><td>Tempo em RISCO</td><td id="r4">--</td></tr>
   <tr><td>Servidor ligado há</td><td id="r5">--</td></tr>
  </table>
  <svg id="graf" viewBox="0 0 300 80" style="width:100%;height:90px;margin-top:10px"></svg>
  <p><a href="/download">⬇ Baixar dados de hoje (CSV)</a></p>
 </div>
 <div class="card">
  <h2>🗄️ Datalake e 🔗 comunicação entre interfaces</h2>
  <div class="fluxo">
   🔆 <b>Arduino</b> (LDR no A4, LEDs 5-6-7)<br>
   ↓ USB Serial 9600 • texto <code>Luz: 523 | RISCO</code><br>
   🐍 <b>Python</b> lê a porta COM e extrai o valor<br>
   ↓ grava em → 🗄️ <b>datalake</b> (bruto .jsonl + tratado .csv) e 🔥 <b>Firebase</b><br>
   ↓ API HTTP JSON <code>/api/dados</code> e <code>/api/relatorio</code><br>
   🌐 <b>Navegador</b> atualiza a cada 1 segundo
  </div>
  <p style="margin-top:10px;color:#94a3b8">Arquivos no datalake:</p>
  <div id="arqs" style="font-family:Consolas,monospace;font-size:.8rem"></div>
 </div>
</div>
<script>
function atualizar(){
 fetch('/api/dados').then(r=>r.json()).then(d=>{
  const ok=d.hora!=='--:--:--', cor=d.risco?'#ef4444':'#10b981';
  document.getElementById('sim').style.display=d.modo==='simulacao'?'block':'none';
  document.getElementById('lim').innerText=d.limite;
  document.getElementById('status').innerText=d.status;
  document.getElementById('fb').innerText=d.firebase;
  const b=document.getElementById('badge');
  if(ok){
   document.getElementById('luz').innerText=d.luz;
   document.getElementById('luz').style.color=cor;
   document.getElementById('fill').style.width=(d.luz/1023*100)+'%';
   document.getElementById('fill').style.background=cor;
   b.innerText=d.risco?'🔴 VERMELHO - RISCO':'✅ NORMAL'; b.style.color=cor;
  } else { b.innerText='⚠️ '+d.status; b.style.color='#fbbf24'; }
  document.querySelectorAll('.led').forEach(l=>l.classList.toggle('on',ok&&d.risco));
  const t=document.getElementById('term');
  t.innerHTML=d.log.map(l=>{const u=l.toUpperCase();
   const c=u.includes('RISCO')?'r':u.includes('NORMAL')?'n':(u.includes('ERRO')||u.includes('DESLIGADO'))?'e':'';
   return '<div class="'+c+'">'+l.replace(/</g,'&lt;')+'</div>'}).join('');
  t.scrollTop=t.scrollHeight;
 }).catch(()=>document.getElementById('status').innerText='Servidor desligado');
}
function relatorio(){
 fetch('/api/relatorio').then(r=>r.json()).then(r=>{
  document.getElementById('r1').innerText=r.leituras;
  document.getElementById('r2').innerText=r.media;
  document.getElementById('r3').innerText=r.minimo+' / '+r.maximo;
  document.getElementById('r4').innerText=r.pct_risco+'%';
  document.getElementById('r5').innerText=Math.floor(r.ligado_ha/60)+' min '+(r.ligado_ha%60)+' s';
  document.getElementById('arqs').innerHTML=r.arquivos.map(a=>'📄 datalake/'+a).join('<br>')||'(vazio)';
  const v=r.ultimas, n=v.length, y=x=>78-x/1023*76;
  document.getElementById('graf').innerHTML=n<2?'':
   '<line x1="0" x2="300" y1="'+y(200)+'" y2="'+y(200)+'" stroke="#fff" stroke-dasharray="4" opacity=".5"/>'+
   '<polyline fill="none" stroke="#38bdf8" stroke-width="2" points="'+v.map((x,i)=>(i*300/(n-1))+','+y(x)).join(' ')+'"/>';
 });
}
atualizar(); relatorio(); setInterval(atualizar,1000); setInterval(relatorio,3000);
</script></body></html>"""

if __name__ == '__main__':
    threading.Thread(target=simular if SIMULAR else ler_arduino, daemon=True).start()
    log("Abra no navegador: http://127.0.0.1:5000")
    app.run(host='0.0.0.0', port=5000)