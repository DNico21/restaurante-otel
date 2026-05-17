# Restaurante - OpenTelemetry en Microservicios

Implementación completa del taller SRE usando OpenTelemetry en una plataforma de restaurante.

## Arquitectura

```
Cliente HTTP
     |
     v
order-service (puerto 8000)
     | → Valida ítems del menú (span manual)
     | → Calcula total en COP (span manual)
     | → Propaga contexto W3C TraceContext
     v
kitchen-service (puerto 8001)
     | → Consulta historial del cliente en SQLite (span manual)
     | → Consulta platos populares (span manual)
     | → Guarda la orden (span manual)
     v
[Respuesta con traceId compartido]

Ambos servicios → OTel Collector (asíncrono, batch)
                       |
            ┌──────────┴──────────┐
            v                     v
          Jaeger               Prometheus
       (trazas)                (métricas)
                                  v
                               Grafana
                             (dashboards)
```

## Inicio rápido

```bash
# Levantar todo el stack
docker compose up --build -d

# Esperar ~30s a que inicien todos los servicios
# Generar tráfico de prueba
pip install httpx
python test_requests.py
```

## Servicios y puertos

| Servicio          | URL                           | Descripción                     |
|-------------------|-------------------------------|---------------------------------|
| order-service     | http://localhost:8000         | API principal de órdenes        |
| kitchen-service   | http://localhost:8001         | Servicio de cocina con SQLite   |
| Jaeger UI         | http://localhost:16686        | Trazas distribuidas             |
| Grafana           | http://localhost:3000         | Dashboards (admin/admin)        |
| Prometheus        | http://localhost:9090         | Métricas                        |
| OTel zPages       | http://localhost:55679        | Debug del collector             |

## Objetivos del ejercicio cumplidos

### Instrumentación automática
- `FastAPIInstrumentor` en ambos servicios (HTTP spans automáticos)
- `HTTPXClientInstrumentor` en order-service (cliente HTTP instrumentado)
- `SQLite3Instrumentor` en kitchen-service (DB spans automáticos)

### Instrumentación manual
- `order.create` → span raíz en order-service
- `order.validate` → span de validación de ítems del menú
- `order.call_kitchen` → span de llamada HTTP a kitchen-service
- `kitchen.check` → span raíz en kitchen-service
- `kitchen.db.query_history` → span de consulta historial cliente
- `kitchen.db.query_popular` → span de consulta platos populares
- `kitchen.db.save_order` → span de escritura en DB

### Propagación de contexto
- Propagación automática W3C TraceContext via `HTTPXClientInstrumentor`
- **El mismo `traceId` aparece en Jaeger para ambos servicios**

### Correlación logs-traces
- `LoggingInstrumentor` inyecta `trace_id` y `span_id` en cada log
- Formato: `[trace_id=xxx span_id=yyy]` visible en Docker logs

### Métricas HTTP
- `restaurant.orders.total` — contador de órdenes por status/mesa
- `restaurant.orders.failed` — órdenes fallidas
- `restaurant.order.duration` — histograma de latencia en ms
- `restaurant.kitchen.db_queries` — consultas DB por operación
- `restaurant.kitchen.db_query_duration` — latencia de DB

### Métricas custom (requeridas por el taller)
1. **`restaurant.order.value`** — histograma del valor monetario de cada orden en COP
   - Permite calcular p95 del ticket promedio por mesa
2. **`restaurant.kitchen.dish_requests`** — contador de platos solicitados con label `dish`
   - Permite ver los platos más populares en tiempo real

### Exportación OTLP
- Ambos servicios exportan vía **gRPC OTLP** al collector
- Collector usa `BatchSpanProcessor` (asíncrono, no bloquea el request)

### Collector asíncrono
- `batch` processor: `timeout=1s`, `send_batch_size=1024`
- `memory_limiter` processor: límite 512 MiB (buena práctica SRE)
- Pipelines separados para traces, metrics y logs

### Manejo de errores y spans
- `span.record_exception(exc)` en todos los catch
- `span.set_status(StatusCode.ERROR, ...)` para marcar spans fallidos
- Orden inválida (ítems fuera del menú) genera span con error visible en Jaeger

## Respuestas a las preguntas del taller

| Pregunta | Dónde verlo |
|----------|-------------|
| ¿Dónde inició el request? | Jaeger → primer span `order.create` en order-service |
| ¿Cuál servicio tardó más? | Jaeger → comparar duración de spans por servicio |
| ¿Cuál span falló? | Jaeger → spans rojos con `StatusCode.ERROR` |
| ¿Qué operación generó latencia? | Jaeger → waterfall view de spans DB |
| ¿Cuántos requests por segundo? | Grafana → panel "RPS" con `rate(orders_total[1m])` |
| ¿Cuál es el percentil p95? | Grafana → panel "Latencia p95" con `histogram_quantile(0.95,...)` |
| ¿Cuál servicio consume más memoria? | Grafana → panel "Memoria del OTel Collector" |
| ¿Cuál endpoint tiene más errores? | Grafana → panel "Tasa de Error %" + Jaeger filtrado por `error=true` |

## Endpoints disponibles

```bash
# Crear orden
POST http://localhost:8000/orders
{
  "customer_name": "Juan",
  "table_number": 5,
  "items": ["hamburguesa", "pizza"]
}

# Ver menú
GET http://localhost:8000/menu

# Health check order-service
GET http://localhost:8000/health

# Health check kitchen-service
GET http://localhost:8001/health

# Estadísticas de cocina
GET http://localhost:8001/kitchen/stats
```

## Menú disponible

| Plato       | Precio COP |
|-------------|------------|
| hamburguesa | 25.000     |
| pizza       | 32.000     |
| ensalada    | 18.000     |
| pasta       | 28.000     |
| sopa        | 15.000     |
