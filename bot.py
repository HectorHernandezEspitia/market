"""Bot que busca acciones, ETF y cripto, los califica y envía un resumen a Telegram."""
import math, os, time
import requests
import yfinance as yf
from yfinance import EquityQuery as EQ, ETFQuery as FQ

# ---------- AJUSTES ----------
CAIDA_TOLERADA = 0.20   # caídas mayores a 20 % en un año restan puntos
TOP = 5                 # cuántos mostrar por categoría
CANDIDATAS = 60         # cuántos revisar por categoría (más = más lento y más bloqueos)

REGLAS = {  # (dato, valor de 0 puntos, valor máximo, peso)
 "consolidada": [("roe",.05,.25,25),("margen",0,.20,20),("per",35,10,20),("deuda",200,30,15),("r12",-.2,.3,20)],
 "crecimiento": [("ventas",0,.4,40),("margen_bruto",.2,.7,20),("margen",-.2,.15,20),("r12",-.3,.5,20)],
 "precio":      [("r3",-.1,.2,30),("r6",-.15,.3,30),("r12",-.2,.4,40)],
}

def num(x):
    return float(x) if isinstance(x,(int,float)) and math.isfinite(x) else None

def puntuar(d, tipo):
    total = usado = 0
    for clave, malo, bueno, peso in REGLAS[tipo]:
        v = d.get(clave)
        if v is None: continue
        s = 0 if (clave=="per" and v<=0) else min(max((v-malo)/(bueno-malo),0),1)
        total += s*peso; usado += peso
    if usado < 0.6*sum(r[3] for r in REGLAS[tipo]) or d.get("caida") is None:
        return None
    castigo = max(abs(d["caida"])-CAIDA_TOLERADA, 0)*50
    return round(max(100*total/usado - castigo, 0))

# ---------- BÚSQUEDA AUTOMÁTICA ----------
def buscar(consulta, orden, n):
    r = yf.screen(consulta, sortField=orden, sortAsc=False, size=n)
    return [q["symbol"] for q in r.get("quotes", [])]

def universo_consolidadas():
    return buscar(EQ("and",[EQ("eq",["region","us"]), EQ("gt",["intradaymarketcap",10_000_000_000]),
                            EQ("gt",["avgdailyvol3m",1_000_000])]), "intradaymarketcap", CANDIDATAS)

def universo_crecimiento():
    return buscar(EQ("and",[EQ("eq",["region","us"]), EQ("btwn",["intradaymarketcap",300_000_000,10_000_000_000]),
                            EQ("gt",["quarterlyrevenuegrowth.quarterly",20]), EQ("gt",["avgdailyvol3m",500_000])]),
                  "avgdailyvol3m", CANDIDATAS)

def universo_colombia():
    return [s for s in buscar(EQ("eq",["region","co"]), "intradaymarketcap", 250) if s.endswith(".CL")]

def universo_etf():
    return buscar(FQ("and",[FQ("eq",["region","us"]), FQ("gt",["fundnetassets",1_000_000_000]),
                            FQ("gt",["avgdailyvol3m",500_000])]), "fundnetassets", CANDIDATAS)

# ---------- DATOS ----------
def historia(t):
    h = t.history(period="1y", auto_adjust=True)
    p = h["Close"].dropna() if "Close" in h else []
    if len(p) < 200: raise ValueError("sin un año de historia")
    cambio = lambda dias: float(p.iloc[-1]/p.iloc[-dias-1]-1)
    return {"r3":cambio(63), "r6":cambio(126), "r12":float(p.iloc[-1]/p.iloc[0]-1),
            "dia":cambio(1), "caida":float((p/p.cummax()-1).min())}

APALANCADOS = ("2x","3x","-1x","ultra","leverag","inverse","bull ","bear ","daily","yieldmax")

def analizar(simbolos, tipo, solo_colombia=False, es_etf=False):
    filas, fallos = [], 0
    for s in simbolos:
        try:
            t = yf.Ticker(s)
            info = t.info or {}
            if solo_colombia and info.get("country") != "Colombia": continue
            nombre = info.get("shortName") or s
            if es_etf and any(x in (info.get("longName") or nombre).lower() for x in APALANCADOS): continue
            d = historia(t)
            if not es_etf:
                deuda = num(info.get("debtToEquity"))
                d.update(roe=num(info.get("returnOnEquity")), margen=num(info.get("profitMargins")),
                         per=num(info.get("trailingPE")), ventas=num(info.get("revenueGrowth")),
                         margen_bruto=num(info.get("grossMargins")),
                         deuda=deuda if deuda is not None and deuda >= 0 else None)
                if info.get("sector") == "Financial Services":
                    d["deuda"] = d["margen_bruto"] = None
            p = puntuar(d, tipo)
            if p is not None: filas.append(dict(d, simbolo=s, nombre=nombre, puntaje=p))
        except Exception as e:
            fallos += 1; print("Fallo", s, e)
        time.sleep(0.5)
    print(f"{len(filas)} calificados, {fallos} fallos")
    return sorted(filas, key=lambda f: -f["puntaje"])

ESTABLES = {"usdt","usdc","dai","usds","usde","fdusd","tusd","pyusd","usd1","usdd",
            "wbtc","weth","steth","wsteth","weeth","wbeth","cbbtc","xaut","paxg"}

def analizar_cripto():
    for intento in range(3):
        r = requests.get("https://api.coingecko.com/api/v3/coins/markets", timeout=30,
            params={"vs_currency":"usd","order":"market_cap_desc","per_page":50,"page":1,
                    "price_change_percentage":"24h,30d,200d,1y"})
        if r.status_code == 200: break
        time.sleep(60)
    r.raise_for_status()
    filas = []
    for c in r.json():
        s = c["symbol"].lower()
        v = [c.get(f"price_change_percentage_{k}_in_currency") for k in ("24h","30d","200d","1y")]
        if s in ESTABLES or None in v: continue
        if abs(c["current_price"]-1) < 0.03 and abs(v[3]) < 5: continue  # otras monedas estables
        dia, r30, r200, r1a = (x/100 for x in v)
        # Sin historia diaria: la "caída" es la peor entre el último mes, 200 días y año
        p = puntuar({"r3":r30,"r6":r200,"r12":r1a,"caida":min(r30,r200,r1a,0)}, "precio")
        filas.append({"simbolo":s.upper(), "nombre":c["name"], "puntaje":p, "dia":dia, "r12":r1a})
    return sorted(filas, key=lambda f: -f["puntaje"])

# ---------- MENSAJE ----------
def seccion(titulo, funcion):
    try:
        filas = funcion()
        if not filas: return f"{titulo}\nSin datos hoy."
        lineas = []
        for f in filas[:TOP]:
            alerta = " ⚠️ se movió fuerte hoy" if abs(f["dia"]) >= 0.05 else ""
            lineas.append(f"• {f['simbolo']} ({f['nombre'][:28]}): {f['puntaje']}/100 | "
                          f"hoy {f['dia']:+.1%} | año {f['r12']:+.0%}{alerta}")
        return titulo + "\n" + "\n".join(lineas)
    except Exception as e:
        print(titulo, e)
        return f"{titulo}\nNo se pudo consultar hoy."

def enviar(texto):
    token, chat = os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id":chat, "text":texto[:4000]}, timeout=30)
    if r.status_code != 200:  # no imprimir la URL: contiene el token
        raise SystemExit(f"Telegram rechazó el mensaje: {r.status_code} {r.text}")

def main():
    partes = ["📊 Candidatos para INVESTIGAR (no son órdenes de compra)",
        seccion("🏛️ Consolidadas EE. UU.", lambda: analizar(universo_consolidadas(), "consolidada")),
        seccion("🇨🇴 Colombia", lambda: analizar(universo_colombia(), "consolidada", solo_colombia=True)),
        seccion("🌱 En crecimiento (riesgo alto)", lambda: analizar(universo_crecimiento(), "crecimiento")),
        seccion("🧺 ETF (solo por precio)", lambda: analizar(universo_etf(), "precio", es_etf=True)),
        seccion("🪙 Cripto (riesgo muy alto, solo por precio)", analizar_cripto),
        "El puntaje mira datos pasados; no predice el futuro. Fuentes: Yahoo Finance y CoinGecko."]
    enviar("\n\n".join(partes))

if __name__ == "__main__":
    main()
