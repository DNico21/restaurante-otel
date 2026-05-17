import logging
import os
import time
import random
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
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
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.trace import SpanKind, StatusCode
from opentelemetry.semconv.trace import SpanAttributes

# ── Configuración OpenTelemetry ────────────────────────────────────────────────

OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
KITCHEN_URL   = os.getenv("KITCHEN_SERVICE_URL", "http://kitchen-service:8001")
SERVICE_NAME  = "order-service"

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

# Métricas estándar
orders_counter = meter.create_counter(
    "restaurant.orders.total",
    description="Total de órdenes recibidas",
    unit="1",
)
orders_failed_counter = meter.create_counter(
    "restaurant.orders.failed",
    description="Total de órdenes fallidas",
    unit="1",
)
order_duration_histogram = meter.create_histogram(
    "restaurant.order.duration",
    description="Duración del procesamiento de órdenes en milisegundos",
    unit="ms",
)
# Métrica custom: valor total de órdenes procesadas
order_value_histogram = meter.create_histogram(
    "restaurant.order.value",
    description="Valor monetario de cada orden en COP",
    unit="COP",
)
active_orders_gauge_value = 0
active_orders_updown = meter.create_up_down_counter(
    "restaurant.orders.active",
    description="Órdenes actualmente en procesamiento",
    unit="1",
)

# Logs correlacionados con traces
LoggingInstrumentor().instrument(set_logging_format=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [trace_id=%(otelTraceID)s span_id=%(otelSpanID)s] %(name)s - %(message)s",
)
logger = logging.getLogger(SERVICE_NAME)

# ── Modelos ────────────────────────────────────────────────────────────────────

MENU = {
    "hamburguesa": 25000,
    "pizza": 32000,
    "ensalada": 18000,
    "pasta": 28000,
    "sopa": 15000,
}

class OrderRequest(BaseModel):
    customer_name: str
    table_number: int
    items: list[str]

class OrderResponse(BaseModel):
    order_id: str
    status: str
    message: str
    total_cop: int
    kitchen_response: dict | None = None

# ── App ────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("order-service iniciando", extra={"service": SERVICE_NAME})
    yield
    logger.info("order-service apagándose")
    tracer_provider.shutdown()
    meter_provider.shutdown()

app = FastAPI(title="Restaurant Order Service", lifespan=lifespan)

FastAPIInstrumentor.instrument_app(app)
HTTPXClientInstrumentor().instrument()

# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_NAME}

@app.post("/orders", response_model=OrderResponse)
async def create_order(order: OrderRequest, request: Request):
    start_time = time.time()
    active_orders_updown.add(1, {"service": SERVICE_NAME})

    with tracer.start_as_current_span(
        "order.create",
        kind=SpanKind.SERVER,
        attributes={
            SpanAttributes.HTTP_METHOD: "POST",
            SpanAttributes.HTTP_ROUTE: "/orders",
            "restaurant.customer_name": order.customer_name,
            "restaurant.table_number": order.table_number,
            "restaurant.items_count": len(order.items),
        },
    ) as span:
        try:
            logger.info(
                "Orden recibida de %s mesa %d con items: %s",
                order.customer_name, order.table_number, order.items,
            )

            # ── Span manual: validación ────────────────────────────────────────
            order_id, total = _validate_order(order, span)

            # ── Span manual: llamada a kitchen-service ─────────────────────────
            kitchen_data = await _call_kitchen_service(order, order_id)

            duration_ms = (time.time() - start_time) * 1000
            orders_counter.add(1, {"status": "success", "table": str(order.table_number)})
            order_duration_histogram.record(duration_ms, {"service": SERVICE_NAME, "status": "success"})
            order_value_histogram.record(total, {"table": str(order.table_number)})

            span.set_attribute("restaurant.order_id", order_id)
            span.set_attribute("restaurant.total_cop", total)
            span.set_status(StatusCode.OK)

            logger.info("Orden %s procesada exitosamente. Total: $%d COP", order_id, total)

            return OrderResponse(
                order_id=order_id,
                status="accepted",
                message=f"Orden aceptada para {order.customer_name}",
                total_cop=total,
                kitchen_response=kitchen_data,
            )

        except HTTPException as exc:
            _record_error(span, exc, order)
            raise
        except Exception as exc:
            _record_error(span, exc, order)
            raise HTTPException(status_code=500, detail=str(exc))
        finally:
            active_orders_updown.add(-1, {"service": SERVICE_NAME})


def _validate_order(order: OrderRequest, span) -> tuple[str, int]:
    """Validación de ítems y cálculo de total — span manual."""
    with tracer.start_as_current_span("order.validate") as validate_span:
        unknown = [i for i in order.items if i not in MENU]
        if unknown:
            validate_span.set_attribute("restaurant.unknown_items", str(unknown))
            validate_span.set_status(StatusCode.ERROR, f"Ítems no disponibles: {unknown}")
            logger.warning("Ítems no disponibles en el menú: %s", unknown)
            raise HTTPException(
                status_code=400,
                detail=f"Ítems no disponibles en el menú: {unknown}",
            )

        total = sum(MENU[i] for i in order.items)
        order_id = f"ORD-{int(time.time() * 1000)}-{random.randint(100, 999)}"

        validate_span.set_attribute("restaurant.order_id", order_id)
        validate_span.set_attribute("restaurant.total_cop", total)
        validate_span.set_status(StatusCode.OK)
        logger.info("Orden %s validada. Total calculado: $%d COP", order_id, total)

        return order_id, total


async def _call_kitchen_service(order: OrderRequest, order_id: str) -> dict:
    """Llamada HTTP a kitchen-service con propagación de contexto automática."""
    with tracer.start_as_current_span(
        "order.call_kitchen",
        kind=SpanKind.CLIENT,
        attributes={
            SpanAttributes.HTTP_METHOD: "POST",
            SpanAttributes.HTTP_URL: f"{KITCHEN_URL}/kitchen/check",
            "restaurant.order_id": order_id,
        },
    ) as kitchen_span:
        async with httpx.AsyncClient(timeout=10.0) as client:
            payload = {
                "order_id": order_id,
                "customer_name": order.customer_name,
                "table_number": order.table_number,
                "items": order.items,
            }
            logger.info("Consultando kitchen-service para orden %s", order_id)
            resp = await client.post(f"{KITCHEN_URL}/kitchen/check", json=payload)
            resp.raise_for_status()
            data = resp.json()

            kitchen_span.set_attribute(SpanAttributes.HTTP_STATUS_CODE, resp.status_code)
            kitchen_span.set_attribute("restaurant.previous_orders", data.get("previous_orders_count", 0))
            kitchen_span.set_status(StatusCode.OK)
            logger.info(
                "kitchen-service respondió para orden %s: %d órdenes previas",
                order_id, data.get("previous_orders_count", 0),
            )
            return data


def _record_error(span, exc: Exception, order: OrderRequest):
    span.record_exception(exc)
    span.set_status(StatusCode.ERROR, str(exc))
    orders_failed_counter.add(1, {"table": str(order.table_number)})
    logger.error("Error procesando orden de %s: %s", order.customer_name, exc)


@app.get("/menu")
async def get_menu():
    with tracer.start_as_current_span("menu.get"):
        return {"menu": MENU, "currency": "COP"}
