import logging
import os
import sqlite3
import time
import random
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.trace import SpanKind, StatusCode
from opentelemetry.semconv.trace import SpanAttributes

# ── Configuración OpenTelemetry ────────────────────────────────────────────────

OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
SERVICE_NAME  = "kitchen-service"
DB_PATH       = "/data/kitchen.db"

resource = Resource.create({
    "service.name": SERVICE_NAME,
    "service.version": "1.0.0",
    "deployment.environment": "development",
    "service.namespace": "restaurante",
})

# Traces
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True))
)
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer(SERVICE_NAME)

# Metrics
metric_reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint=OTEL_ENDPOINT, insecure=True),
    export_interval_millis=5000,
)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter(SERVICE_NAME)

# Métricas kitchen
db_queries_counter = meter.create_counter(
    "restaurant.kitchen.db_queries",
    description="Total de consultas a la base de datos",
    unit="1",
)
db_query_duration = meter.create_histogram(
    "restaurant.kitchen.db_query_duration",
    description="Duración de consultas a la base de datos",
    unit="ms",
)
# Métrica custom: platos más populares por conteo
dish_popularity_counter = meter.create_counter(
    "restaurant.kitchen.dish_requests",
    description="Conteo de solicitudes por plato (métrica custom)",
    unit="1",
)
kitchen_errors_counter = meter.create_counter(
    "restaurant.kitchen.errors",
    description="Errores en el kitchen-service",
    unit="1",
)

# Logs + Traces correlacionados
LoggingInstrumentor().instrument(set_logging_format=True)
SQLite3Instrumentor().instrument()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [trace_id=%(otelTraceID)s span_id=%(otelSpanID)s] %(name)s - %(message)s",
)
logger = logging.getLogger(SERVICE_NAME)

# ── Base de datos ──────────────────────────────────────────────────────────────

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id    TEXT NOT NULL,
            customer    TEXT NOT NULL,
            table_num   INTEGER NOT NULL,
            items       TEXT NOT NULL,
            created_at  REAL NOT NULL
        )
    """)
    # Seed con historial previo de ejemplo
    cursor.execute("SELECT COUNT(*) FROM orders")
    if cursor.fetchone()[0] == 0:
        seed_data = [
            ("ORD-SEED-001", "Carlos Pérez",   3, "hamburguesa,sopa",  time.time() - 3600),
            ("ORD-SEED-002", "María López",    5, "pizza,ensalada",    time.time() - 7200),
            ("ORD-SEED-003", "Juan Rodríguez", 1, "pasta",             time.time() - 1800),
            ("ORD-SEED-004", "Ana García",     2, "hamburguesa,pizza", time.time() - 900),
        ]
        cursor.executemany(
            "INSERT INTO orders (order_id, customer, table_num, items, created_at) VALUES (?,?,?,?,?)",
            seed_data,
        )
        conn.commit()
        logger.info("Base de datos inicializada con %d registros seed", len(seed_data))
    conn.close()

# ── Modelos ────────────────────────────────────────────────────────────────────

class KitchenCheckRequest(BaseModel):
    order_id: str
    customer_name: str
    table_number: int
    items: list[str]

class KitchenCheckResponse(BaseModel):
    order_id: str
    previous_orders_count: int
    customer_is_returning: bool
    estimated_prep_minutes: int
    popular_items: list[str]
    message: str

# ── App ────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("kitchen-service iniciando, DB en %s", DB_PATH)
    yield
    logger.info("kitchen-service apagándose")
    tracer_provider.shutdown()
    meter_provider.shutdown()

app = FastAPI(title="Restaurant Kitchen Service", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app)

# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_NAME}

@app.post("/kitchen/check", response_model=KitchenCheckResponse)
async def kitchen_check(req: KitchenCheckRequest):
    with tracer.start_as_current_span(
        "kitchen.check",
        kind=SpanKind.SERVER,
        attributes={
            SpanAttributes.HTTP_METHOD: "POST",
            SpanAttributes.HTTP_ROUTE: "/kitchen/check",
            "restaurant.order_id": req.order_id,
            "restaurant.customer": req.customer_name,
            "restaurant.table_number": req.table_number,
        },
    ) as span:
        logger.info(
            "kitchen-service procesando orden %s para %s (mesa %d)",
            req.order_id, req.customer_name, req.table_number,
        )

        # Métrica custom: contar cada plato solicitado
        for item in req.items:
            dish_popularity_counter.add(1, {"dish": item})

        try:
            # ── Span: consulta historial del cliente ───────────────────────────
            prev_count = _query_customer_history(req.customer_name, span)

            # ── Span: consulta platos populares ───────────────────────────────
            popular = _query_popular_items(span)

            # ── Span: guardar la nueva orden ───────────────────────────────────
            _save_order(req, span)

            # Tiempo de preparación simulado según historial
            base_time = 10 + len(req.items) * 3
            # Si es cliente frecuente, cocina más rápido
            prep_time = max(5, base_time - (2 if prev_count > 2 else 0))

            is_returning = prev_count > 0
            span.set_attribute("restaurant.previous_orders_count", prev_count)
            span.set_attribute("restaurant.is_returning_customer", is_returning)
            span.set_attribute("restaurant.estimated_prep_minutes", prep_time)
            span.set_status(StatusCode.OK)

            logger.info(
                "Orden %s: cliente %s tiene %d órdenes previas. Prep: %d min",
                req.order_id, req.customer_name, prev_count, prep_time,
            )

            return KitchenCheckResponse(
                order_id=req.order_id,
                previous_orders_count=prev_count,
                customer_is_returning=is_returning,
                estimated_prep_minutes=prep_time,
                popular_items=popular,
                message=f"Orden registrada en cocina. Tiempo estimado: {prep_time} minutos.",
            )

        except Exception as exc:
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            kitchen_errors_counter.add(1, {"operation": "kitchen.check"})
            logger.error("Error en kitchen.check para orden %s: %s", req.order_id, exc)
            raise HTTPException(status_code=500, detail=str(exc))


def _query_customer_history(customer_name: str, parent_span) -> int:
    """Consulta cuántas órdenes previas tiene el cliente — span manual."""
    with tracer.start_as_current_span(
        "kitchen.db.query_history",
        attributes={
            "db.system": "sqlite",
            "db.operation": "SELECT",
            "db.statement": "SELECT COUNT(*) FROM orders WHERE customer = ?",
            "restaurant.customer": customer_name,
        },
    ) as db_span:
        t0 = time.time()
        conn = sqlite3.connect(DB_PATH)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM orders WHERE customer = ?", (customer_name,))
            count = cursor.fetchone()[0]
            duration_ms = (time.time() - t0) * 1000
            db_queries_counter.add(1, {"operation": "query_history"})
            db_query_duration.record(duration_ms, {"operation": "query_history"})
            db_span.set_attribute("db.result_count", count)
            db_span.set_status(StatusCode.OK)
            logger.info("Historial de '%s': %d órdenes previas", customer_name, count)
            return count
        except Exception as exc:
            db_span.record_exception(exc)
            db_span.set_status(StatusCode.ERROR, str(exc))
            raise
        finally:
            conn.close()


def _query_popular_items(parent_span) -> list[str]:
    """Consulta los platos más pedidos — span manual."""
    with tracer.start_as_current_span(
        "kitchen.db.query_popular",
        attributes={
            "db.system": "sqlite",
            "db.operation": "SELECT",
            "db.statement": "Aggregate items frequency from orders",
        },
    ) as db_span:
        t0 = time.time()
        conn = sqlite3.connect(DB_PATH)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT items FROM orders ORDER BY created_at DESC LIMIT 20")
            rows = cursor.fetchall()
            freq: dict[str, int] = {}
            for (items_str,) in rows:
                for item in items_str.split(","):
                    item = item.strip()
                    freq[item] = freq.get(item, 0) + 1

            popular = sorted(freq, key=freq.get, reverse=True)[:3]
            duration_ms = (time.time() - t0) * 1000
            db_queries_counter.add(1, {"operation": "query_popular"})
            db_query_duration.record(duration_ms, {"operation": "query_popular"})
            db_span.set_attribute("db.popular_items", str(popular))
            db_span.set_status(StatusCode.OK)
            return popular
        except Exception as exc:
            db_span.record_exception(exc)
            db_span.set_status(StatusCode.ERROR, str(exc))
            raise
        finally:
            conn.close()


def _save_order(req: KitchenCheckRequest, parent_span):
    """Guarda la nueva orden en la base de datos — span manual."""
    with tracer.start_as_current_span(
        "kitchen.db.save_order",
        attributes={
            "db.system": "sqlite",
            "db.operation": "INSERT",
            "db.statement": "INSERT INTO orders ...",
            "restaurant.order_id": req.order_id,
        },
    ) as db_span:
        t0 = time.time()
        conn = sqlite3.connect(DB_PATH)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO orders (order_id, customer, table_num, items, created_at) VALUES (?,?,?,?,?)",
                (req.order_id, req.customer_name, req.table_number, ",".join(req.items), time.time()),
            )
            conn.commit()
            duration_ms = (time.time() - t0) * 1000
            db_queries_counter.add(1, {"operation": "save_order"})
            db_query_duration.record(duration_ms, {"operation": "save_order"})
            db_span.set_status(StatusCode.OK)
            logger.info("Orden %s guardada en DB", req.order_id)
        except Exception as exc:
            db_span.record_exception(exc)
            db_span.set_status(StatusCode.ERROR, str(exc))
            raise
        finally:
            conn.close()


@app.get("/kitchen/stats")
async def kitchen_stats():
    """Endpoint de estadísticas para demostrar métricas adicionales."""
    with tracer.start_as_current_span("kitchen.stats"):
        conn = sqlite3.connect(DB_PATH)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM orders")
            total = cursor.fetchone()[0]
            cursor.execute("SELECT customer, COUNT(*) as cnt FROM orders GROUP BY customer ORDER BY cnt DESC LIMIT 5")
            top_customers = [{"customer": r[0], "orders": r[1]} for r in cursor.fetchall()]
            return {"total_orders": total, "top_customers": top_customers}
        finally:
            conn.close()
