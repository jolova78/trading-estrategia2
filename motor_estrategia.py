"""
ESTRATEGIA 2 - MOTOR PAPER
Versión operativa para GitHub

Reglas auditadas:
- Entrada:
    Close > EMA200
    EMA50 > EMA200
    Close > máximo de los 20 cierres anteriores
- Salida:
    Close < EMA50
- Señal calculada al cierre
- Ejecución PAPER en la apertura siguiente
- Compra: Open * 1.001
- Venta: Open * 0.999
- Máximo 5 posiciones
- Objetivo: 20% del capital por posición
"""

import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import Adjustment, DataFeed


# ============================================================
# CONFIGURACIÓN
# ============================================================

ACTIVOS = [
    "AAPL",
    "MSFT",
    "GOOGL",
    "AMZN",
    "NVDA",
    "SPY",
    "JPM",
    "JNJ",
    "XOM",
]

CAPITAL_INICIAL = 10000.00

MAX_POSICIONES = 5

PESO_POSICION = 0.20

CAPITAL_POR_POSICION = CAPITAL_INICIAL * PESO_POSICION

SLIPPAGE_COMPRA = 1.001
SLIPPAGE_VENTA = 0.999

ARCHIVO_ESTADO = "estado_paper.json"


# ============================================================
# UTILIDADES
# ============================================================

def ahora_utc():
    return datetime.now(timezone.utc).isoformat()


def cargar_estado():
    if not os.path.exists(ARCHIVO_ESTADO):
        raise FileNotFoundError(
            f"No existe {ARCHIVO_ESTADO}"
        )

    with open(
        ARCHIVO_ESTADO,
        "r",
        encoding="utf-8"
    ) as archivo:
        return json.load(archivo)


def guardar_estado(estado):
    temporal = ARCHIVO_ESTADO + ".tmp"

    with open(
        temporal,
        "w",
        encoding="utf-8"
    ) as archivo:
        json.dump(
            estado,
            archivo,
            indent=2,
            ensure_ascii=False
        )

    os.replace(
        temporal,
        ARCHIVO_ESTADO
    )


def crear_cliente():
    api_key = os.environ.get(
        "ALPACA_API_KEY"
    )

    secret_key = os.environ.get(
        "ALPACA_SECRET_KEY"
    )

    if not api_key or not secret_key:
        raise RuntimeError(
            "Faltan ALPACA_API_KEY "
            "o ALPACA_SECRET_KEY"
        )

    return StockHistoricalDataClient(
        api_key,
        secret_key
    )


# ============================================================
# DESCARGAR HISTÓRICOS
# ============================================================

def descargar_datos(cliente):

    inicio = pd.Timestamp(
        "2024-01-01",
        tz="UTC"
    ).to_pydatetime()

    solicitud = StockBarsRequest(
        symbol_or_symbols=ACTIVOS,
        timeframe=TimeFrame.Day,
        start=inicio,
        adjustment=Adjustment.ALL,
        feed=DataFeed.IEX,
    )

    barras = cliente.get_stock_bars(
        solicitud
    ).df

    if barras.empty:
        raise RuntimeError(
            "Alpaca no devolvió datos."
        )

    datos = {}

    for ticker in ACTIVOS:

        try:
            z = barras.xs(
                ticker,
                level="symbol"
            ).copy()

        except KeyError:
            print(
                f"AVISO: sin datos para {ticker}"
            )
            continue

        z = (
            z
            .reset_index()
            .sort_values("timestamp")
            .reset_index(drop=True)
        )

        datos[ticker] = z

    return datos


# ============================================================
# INDICADORES Y SEÑALES
# ============================================================

def calcular_senales(df):

    z = df.copy()

    z["EMA50"] = (
        z["close"]
        .ewm(
            span=50,
            adjust=False
        )
        .mean()
    )

    z["EMA200"] = (
        z["close"]
        .ewm(
            span=200,
            adjust=False
        )
        .mean()
    )

    # Máximo de los 20 cierres ANTERIORES.
    # shift(1) evita utilizar el cierre actual
    # dentro de su propio máximo.
    z["Max20"] = (
        z["close"]
        .shift(1)
        .rolling(20)
        .max()
    )

    z["entrada"] = (
        (z["close"] > z["EMA200"])
        & (z["EMA50"] > z["EMA200"])
        & (z["close"] > z["Max20"])
    )

    z["salida"] = (
        z["close"] < z["EMA50"]
    )

    return z


def preparar_datos(datos):

    preparados = {}

    for ticker, df in datos.items():

        z = calcular_senales(df)

        z = z.dropna(
            subset=[
                "EMA50",
                "EMA200",
                "Max20",
            ]
        )

        if z.empty:
            continue

        preparados[ticker] = z

    return preparados


# ============================================================
# POSICIONES
# ============================================================

def mapa_posiciones(estado):

    return {
        p["activo"]: p
        for p in estado.get(
            "posiciones",
            []
        )
    }


def capital_estimado(
    estado,
    datos
):

    total = float(
        estado.get(
            "efectivo",
            0
        )
    )

    for posicion in estado.get(
        "posiciones",
        []
    ):

        ticker = posicion["activo"]

        if ticker not in datos:
            continue

        ultimo = datos[ticker].iloc[-1]

        precio = float(
            ultimo["close"]
        )

        acciones = float(
            posicion["acciones"]
        )

        total += (
            precio * acciones
        )

    return total


# ============================================================
# SEÑALES DEL ÚLTIMO CIERRE COMÚN
# ============================================================

def fecha_comun(datos):

    fechas = [
        z["timestamp"].max()
        for z in datos.values()
    ]

    if not fechas:
        raise RuntimeError(
            "No existen fechas disponibles."
        )

    return min(fechas)


def obtener_senales(
    datos,
    fecha_senal
):

    senales = {}

    for ticker in ACTIVOS:

        if ticker not in datos:
            continue

        z = datos[ticker]

        fila = z[
            z["timestamp"]
            == fecha_senal
        ]

        if fila.empty:
            continue

        fila = fila.iloc[0]

        senales[ticker] = {
            "close": float(
                fila["close"]
            ),
            "entrada": bool(
                fila["entrada"]
            ),
            "salida": bool(
                fila["salida"]
            ),
        }

    return senales


# ============================================================
# GENERAR ÓRDENES
# ============================================================

def generar_ordenes(
    estado,
    senales
):

    posiciones = mapa_posiciones(
        estado
    )

    ordenes = []

    # --------------------------------------------------------
    # 1. VENTAS
    # --------------------------------------------------------

    ventas = set()

    for ticker in posiciones:

        senal = senales.get(
            ticker
        )

        if not senal:
            continue

        if senal["salida"]:

            ordenes.append({
                "activo": ticker,
                "accion": "VENDER",
                "motivo": "Señal de salida",
                "ejecucion": "Próxima apertura",
            })

            ventas.add(
                ticker
            )

    mantenidas = (
        set(posiciones.keys())
        - ventas
    )

    # --------------------------------------------------------
    # 2. COMPRAS
    # --------------------------------------------------------

    for ticker in ACTIVOS:

        if len(mantenidas) >= MAX_POSICIONES:
            break

        senal = senales.get(
            ticker
        )

        if not senal:
            continue

        if not senal["entrada"]:
            continue

        if ticker in mantenidas:
            continue

        # Si ya está en cartera pero se venderá
        # en la misma apertura, no recompramos
        # inmediatamente.
        if ticker in ventas:
            continue

        ordenes.append({
            "activo": ticker,
            "accion": "COMPRAR",
            "motivo": "Señal de entrada",
            "ejecucion": "Próxima apertura",
        })

        mantenidas.add(
            ticker
        )

    return ordenes


# ============================================================
# HISTORIAL
# ============================================================

def registrar_evento(
    estado,
    tipo,
    detalle
):

    historial = estado.setdefault(
        "historial",
        []
    )

    historial.append({
        "fecha": ahora_utc(),
        "tipo": tipo,
        "detalle": detalle,
    })


# ============================================================
# MOTOR
# ============================================================

def ejecutar_ventas(
    estado,
    datos
):

    ordenes = estado.get(
        "ordenes_pendientes",
        []
    )

    posiciones = estado.get(
        "posiciones",
        []
    )

    nuevas_posiciones = []

    efectivo = float(
        estado.get(
            "efectivo",
            0
        )
    )

    for posicion in posiciones:

        ticker = posicion["activo"]

        vender = any(
            o["accion"] == "VENDER"
            and o["activo"] == ticker
            for o in ordenes
        )

        if not vender:
            nuevas_posiciones.append(
                posicion
            )
            continue

        if ticker not in datos:
            nuevas_posiciones.append(
                posicion
            )
            continue

        apertura = float(
            datos[ticker]
            .iloc[-1]["open"]
        )

        precio = round(
            apertura *
            SLIPPAGE_VENTA,
            2
        )

        acciones = float(
            posicion["acciones"]
        )

        importe = round(
            acciones * precio,
            2
        )

        efectivo += importe

        registrar_evento(
            estado,
            "VENTA",
            {
                "activo": ticker,
                "acciones": acciones,
                "precio": precio,
                "importe": importe
            }
        )

    estado["efectivo"] = round(
        efectivo,
        2
    )

    estado["posiciones"] = (
        nuevas_posiciones
    )

    return estado

def ejecutar_compras(
    estado,
    datos
):

    ordenes = estado.get(
        "ordenes_pendientes",
        []
    )

    posiciones = estado.get(
        "posiciones",
        []
    )

    efectivo = float(
        estado.get(
            "efectivo",
            0
        )
    )

    for orden in ordenes:

        if orden["accion"] != "COMPRAR":
            continue

        ticker = orden["activo"]

        if ticker not in datos:
            continue

        apertura = float(
            datos[ticker]
            .iloc[-1]["open"]
        )

        precio = round(
            apertura *
            SLIPPAGE_COMPRA,
            2
        )

        capital_por_posicion = (
            estado["efectivo"]
            + sum(
                p["acciones"] * datos[p["activo"]].iloc[-1]["close"]
                for p in estado["posiciones"]
                if p["activo"] in datos
            )
        ) * PESO_POSICION

        monto = min(
            capital_por_posicion,
            efectivo
        )
        if monto <= 0:
            continue

        acciones = round(
            monto / precio,
            6
        )

        importe = round(
            acciones * precio,
            2
        )

        efectivo -= importe

        posiciones.append(
            {
                "activo": ticker,
                "acciones": acciones,
                "precio_compra": precio,
                "fecha_compra": estado.get(
                    "fecha_actual"
                )
            }
        )

        registrar_evento(
            estado,
            "COMPRA",
            {
                "activo": ticker,
                "acciones": acciones,
                "precio": precio,
                "importe": importe
            }
        )

    estado["efectivo"] = round(
        efectivo,
        2
    )

    estado["posiciones"] = posiciones

    return estado

def ejecutar_ordenes(
    estado,
    datos
):

    ordenes = estado.get(
        "ordenes_pendientes",
        []
    )

    if not ordenes:
        return estado

    estado = ejecutar_ventas(
        estado,
        datos
    )

    estado = ejecutar_compras(
        estado,
        datos
    )

    estado["ordenes_ejecutadas"] = list(
        ordenes
    )

    estado["ordenes_pendientes"] = []

    estado["ultima_ejecucion"] = (
        estado.get("fecha_actual")
    )

    return estado
    
def ejecutar():

    print(
        "========================================"
    )

    print(
        "ESTRATEGIA 2 - MOTOR PAPER"
    )

    print(
        "========================================"
    )

    estado = cargar_estado()

    if estado.get("modo") != "PAPER":
        raise RuntimeError(
            "El motor solo puede ejecutarse "
            "en modo PAPER."
        )

    cliente = crear_cliente()

    datos_crudos = descargar_datos(
        cliente
    )

    datos = preparar_datos(
        datos_crudos
    )

    fecha_senal = fecha_comun(
        datos
    )

    senales = obtener_senales(
        datos,
        fecha_senal
    )


    # Ejecutar órdenes pendientes de la sesión anterior
    estado = ejecutar_ordenes(
        estado,
        datos
    )

    estado["ultima_ejecucion_ordenes"] = ahora_utc()
    
    estado["capital_estimado"] = round(
        capital_estimado(
            estado,
            datos
        ),
        2
        )

    # Generar las órdenes para la próxima apertura
    ordenes = generar_ordenes(
        estado,
        senales
    )

    estado["ordenes_generadas"] = len(ordenes)
    
    capital = estado["capital_estimado"]

    estado[
        "max_posiciones"
    ] = MAX_POSICIONES

    estado[
        "peso_posicion"
    ] = PESO_POSICION

    estado[
        "ordenes_pendientes"
    ] = ordenes

    estado[
        "ultima_senal"
    ] = str(
        fecha_senal
    )

    estado[
        "ultima_actualizacion_motor"
    ] = ahora_utc()

    estado[
        "version_motor"
    ] = "2.1.0"
    
    estado[
        "reglas_motor"
    ] = {
        "entrada": (
            "Close > EMA200; "
            "EMA50 > EMA200; "
            "Close > Max20 anterior"
        ),
        "salida": (
            "Close < EMA50"
        ),
        "ejecucion": (
            "Próxima apertura"
        ),
        "slippage_compra": (
            SLIPPAGE_COMPRA
        ),
        "slippage_venta": (
            SLIPPAGE_VENTA
        ),
    }

    registrar_evento(
        estado,
        "GENERACION_SENALES",
        {
            "fecha_senal": str(
                fecha_senal
            ),
            "ordenes": ordenes,
        }
    )

    guardar_estado(
        estado
    )

    print()

    print(
        "Fecha de señal:",
        fecha_senal
    )

    print()

    print(
        "=== SEÑALES ==="
    )

    for ticker in ACTIVOS:

        if ticker not in senales:
            continue

        s = senales[ticker]

        print(
            ticker,
            "| Close:",
            round(
                s["close"],
                2
            ),
            "| Entrada:",
            s["entrada"],
            "| Salida:",
            s["salida"],
        )

    print()

    print(
        "=== ÓRDENES PRÓXIMA APERTURA ==="
    )

    if not ordenes:

        print(
            "SIN ÓRDENES"
        )

    else:

        for orden in ordenes:

            print(
                orden["accion"],
                orden["activo"],
                "-",
                orden["motivo"],
            )

    print()

    print(
        "Capital estimado:",
        round(
            capital,
            2
        )
    )

    print()

    print(
        "MOTOR EJECUTADO CORRECTAMENTE"
    )


if __name__ == "__main__":

    try:
        ejecutar()

    except Exception as error:

        print(
            "ERROR DEL MOTOR:",
            str(error)
        )

        sys.exit(1)
