"""
Script de prueba para generar tráfico y demostrar todas las funcionalidades
de OpenTelemetry en la plataforma de restaurante.

Uso:
    python test_requests.py

Responde las preguntas del ejercicio generando:
  - Trazas distribuidas con el mismo TraceId
  - Métricas HTTP (RPS, latencia, errores)
  - Logs correlacionados
  - Errores intencionados para ver spans fallidos
"""

import httpx
import time
import json
import random

BASE_URL = "http://localhost:8000"

VALID_ORDERS = [
    {"customer_name": "Carlos Pérez",    "table_number": 3, "items": ["hamburguesa", "sopa"]},
    {"customer_name": "María López",     "table_number": 5, "items": ["pizza", "ensalada"]},
    {"customer_name": "Juan Rodríguez",  "table_number": 1, "items": ["pasta"]},
    {"customer_name": "Ana García",      "table_number": 2, "items": ["hamburguesa", "pizza"]},
    {"customer_name": "Pedro Martínez",  "table_number": 7, "items": ["ensalada", "sopa", "pasta"]},
]

INVALID_ORDER = {
    "customer_name": "Cliente Error",
    "table_number": 99,
    "items": ["sushi", "tacos"],  # No están en el menú → 400
}

def print_result(label: str, resp: httpx.Response):
    status = resp.status_code
    icon = "✓" if status < 400 else "✗"
    print(f"  {icon} [{status}] {label}")
    if status < 400:
        data = resp.json()
        if "order_id" in data:
            print(f"       order_id={data['order_id']}  total=${data.get('total_cop', 0):,} COP")
            kitchen = data.get("kitchen_response", {})
            if kitchen:
                print(f"       previas={kitchen.get('previous_orders_count')}  prep={kitchen.get('estimated_prep_minutes')}min")
    else:
        print(f"       ERROR: {resp.text[:120]}")

def main():
    print("\n=== Test del Restaurante - OpenTelemetry ===\n")

    # 1. Verificar health
    print("1. Health checks:")
    for svc, url in [("order-service", "http://localhost:8000/health"),
                     ("kitchen-service", "http://localhost:8001/health")]:
        try:
            r = httpx.get(url, timeout=5)
            print(f"   ✓ {svc}: {r.json()}")
        except Exception as e:
            print(f"   ✗ {svc}: {e}")

    # 2. Consultar menú
    print("\n2. Menú disponible:")
    r = httpx.get(f"{BASE_URL}/menu")
    menu = r.json()
    for item, price in menu["menu"].items():
        print(f"   - {item}: ${price:,} COP")

    # 3. Órdenes exitosas (genera trazas end-to-end)
    print("\n3. Creando órdenes exitosas:")
    for order in VALID_ORDERS:
        r = httpx.post(f"{BASE_URL}/orders", json=order, timeout=10)
        print_result(f"{order['customer_name']} (mesa {order['table_number']})", r)
        time.sleep(0.5)

    # 4. Orden inválida (genera span con error)
    print("\n4. Orden inválida (debe fallar con 400):")
    r = httpx.post(f"{BASE_URL}/orders", json=INVALID_ORDER, timeout=10)
    print_result("Orden con ítems inexistentes", r)

    # 5. Carga de tráfico para generar métricas RPS y percentiles
    print("\n5. Generando carga de tráfico (20 requests)...")
    success = 0
    errors = 0
    for i in range(20):
        order = random.choice(VALID_ORDERS).copy()
        order["customer_name"] += f" #{i}"
        try:
            r = httpx.post(f"{BASE_URL}/orders", json=order, timeout=10)
            if r.status_code == 200:
                success += 1
            else:
                errors += 1
        except Exception:
            errors += 1
        time.sleep(0.2)
    print(f"   Exitosos: {success}  Errores: {errors}")

    # 6. Estadísticas de cocina
    print("\n6. Estadísticas de cocina:")
    r = httpx.get("http://localhost:8001/kitchen/stats", timeout=10)
    stats = r.json()
    print(f"   Total órdenes en DB: {stats['total_orders']}")
    print("   Top clientes:")
    for c in stats["top_customers"]:
        print(f"     - {c['customer']}: {c['orders']} órdenes")

    print("\n=== Listo! Revisa los dashboards: ===")
    print("  Jaeger:     http://localhost:16686")
    print("  Grafana:    http://localhost:3000  (admin/admin)")
    print("  Prometheus: http://localhost:9090")
    print("  OTel zPages: http://localhost:55679/debug/tracez\n")

if __name__ == "__main__":
    main()
