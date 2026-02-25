import os
import xmlrpc.client
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query

app = FastAPI()

# ---- Env ----
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USERNAME = os.getenv("ODOO_USERNAME")
ODOO_API_KEY = os.getenv("ODOO_API_KEY")

if not all([ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_API_KEY]):
    raise RuntimeError("Missing one of ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_API_KEY")

# ---- Odoo RPC ----
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_API_KEY, {})
if not uid:
    raise RuntimeError("Odoo authentication failed (check DB/username/API key).")

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")


# ---------------------------
# Helpers
# ---------------------------
def _score(query: str, text: Optional[str]) -> float:
    """Return a fuzzy score in [0, 1]."""
    q = (query or "").strip().lower()
    t = (text or "").strip().lower() if text else ""
    if not q or not t:
        return 0.0
    bonus = 0.15 if q in t else 0.0
    sim = SequenceMatcher(None, q, t).ratio()
    return min(1.0, sim + bonus)


def _search_templates(q: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Search product.template candidates using tolerant domain and score them.
    Note: default_code/barcode may or may not exist on template depending on config.
    We try anyway; if field doesn't exist, Odoo will ignore it in fields list? (it may error in some setups).
    """
    # Domain: name ilike OR default_code ilike OR barcode ilike
    domain = ["|", "|",
              ["name", "ilike", q],
              ["default_code", "ilike", q],
              ["barcode", "ilike", q]]

    # Some databases don't have default_code/barcode on template; to be robust,
    # we fetch only fields that exist by using a safe list.
    fields = ["id", "name", "default_code", "barcode"]

    try:
        templates = models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY,
            "product.template", "search_read",
            [domain],
            {"fields": fields, "limit": limit}
        )
    except Exception:
        # Fallback: only id + name if custom fields cause issues
        templates = models.execute_kw(
            ODOO_DB, uid, ODOO_API_KEY,
            "product.template", "search_read",
            [[["name", "ilike", q]]],
            {"fields": ["id", "name"], "limit": limit}
        )

    scored: List[Dict[str, Any]] = []
    for t in templates:
        s_name = _score(q, t.get("name"))
        s_code = _score(q, t.get("default_code"))
        s_bar = _score(q, t.get("barcode"))
        s = max(s_name, s_code, s_bar)
        t["score"] = round(float(s), 4)
        scored.append(t)

    scored.sort(key=lambda x: x.get("score", 0), reverse=True)
    return scored


def _template_to_variant_id(template_id: int) -> Optional[int]:
    """Return a product.product id for a given product.template id."""
    variants = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "product.product", "search_read",
        [[["product_tmpl_id", "=", template_id]]],
        {"fields": ["id"], "limit": 1}
    )
    if not variants:
        return None
    return int(variants[0]["id"])


def _get_quants(product_id: int) -> List[Dict[str, Any]]:
    """Return stock.quant rows for a product.product id."""
    return models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "stock.quant", "search_read",
        [[["product_id", "=", product_id]]],
        {"fields": ["location_id", "quantity"]}
    )


def _confidence(candidates: List[Dict[str, Any]]) -> bool:
    """
    Decide if the top candidate is confident enough to auto-select.
    You can adjust thresholds later.
    """
    if not candidates:
        return False
    best = candidates[0]
    best_score = float(best.get("score", 0.0))
    second_score = float(candidates[1].get("score", 0.0)) if len(candidates) > 1 else 0.0

    # Confident if:
    # - score is high enough AND
    # - either single candidate OR it is clearly better than #2
    return (best_score >= 0.72) and (len(candidates) == 1 or (best_score - second_score) >= 0.10)


# ---------------------------
# Routes
# ---------------------------
@app.get("/")
def health():
    return {"status": "running"}


@app.get("/stock/{template_id}")
def get_stock(template_id: int):
    """
    Get stock.quant by product.template ID (we resolve to the underlying product.product).
    """
    product_id = _template_to_variant_id(template_id)
    if not product_id:
        raise HTTPException(status_code=404, detail="No variant found for this template_id")
    return _get_quants(product_id)


@app.get("/resolve_product")
def resolve_product(q: str = Query(..., min_length=2), limit: int = 5):
    """
    Fuzzy search product templates by a query (e.g., "noisette").
    Returns candidates with scores.
    """
    results = _search_templates(q, limit=limit)
    return {"query": q, "results": results}


@app.get("/stock_query")
def stock_query(q: str = Query(..., min_length=2), limit: int = 5):
    """
    Fuzzy resolve + get stock.
    - If confident: returns selected + stock, needs_confirmation=false
    - If ambiguous: returns top_candidates + stock of best guess? (we DON'T fetch stock unless confident)
    """
    candidates = _search_templates(q, limit=limit)
    if not candidates:
        return {"query": q, "error": "Product not found", "needs_confirmation": True, "top_candidates": []}

    is_confident = _confidence(candidates)
    best = candidates[0]

    if not is_confident:
        # Ambiguous: don't guess stock, ask confirmation
        return {
            "query": q,
            "needs_confirmation": True,
            "selected": None,
            "top_candidates": candidates[: min(5, len(candidates))]
        }

    template_id = int(best["id"])
    product_id = _template_to_variant_id(template_id)
    if not product_id:
        return {
            "query": q,
            "needs_confirmation": True,
            "error": "Variant not found",
            "selected": best,
            "top_candidates": candidates[: min(5, len(candidates))]
        }

    quants = _get_quants(product_id)
    return {
        "query": q,
        "needs_confirmation": False,
        "selected": best,
        "stock": quants
    }
