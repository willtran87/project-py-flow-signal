"""Scan-only example: no optional dependencies are needed to analyze this file."""

import asyncio
import logging

import requests

logger = logging.getLogger(__name__)


def fetch_price(sku):
    return requests.get(f"https://example.invalid/prices/{sku}", timeout=2).json()


def submit_order(sku):
    try:
        return fetch_price(sku)
    except Exception:
        # Whether this is an acceptable miss, degradation, or failure needs context.
        return None


async def send_receipt(order):
    raise RuntimeError("Example background failure")


async def checkout(order):
    asyncio.create_task(send_receipt(order))
    return submit_order(order["sku"])
