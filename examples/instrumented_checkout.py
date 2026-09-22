"""Scan-only example showing existing signals alongside candidate improvements."""

import logging

import requests
from opentelemetry import trace

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


def fetch_price(sku):
    return requests.get(f"https://example.invalid/prices/{sku}", timeout=2).json()


def fetch_stock(sku):
    with tracer.start_as_current_span("fetch-stock"):
        return requests.get(f"https://example.invalid/stock/{sku}", timeout=2).json()


def checkout(sku):
    logger.info("Checkout started", extra={"sku": sku})
    try:
        price = fetch_price(sku)
    except requests.Timeout:
        # Whether this fallback is acceptable or degraded needs business context.
        price = {"amount": 0, "estimated": True}
    stock = fetch_stock(sku)
    return {"price": price, "stock": stock}
