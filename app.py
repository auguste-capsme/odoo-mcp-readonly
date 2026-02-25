import os
import xmlrpc.client
from fastapi import FastAPI

app = FastAPI()

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USERNAME = os.getenv("ODOO_USERNAME")
ODOO_API_KEY = os.getenv("ODOO_API_KEY")

common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_API_KEY, {})

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

@app.get("/")
def health():
    return {"status": "running"}

@app.get("/stock/{product_id}")
def get_stock(product_id: int):
    result = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_API_KEY,
        "stock.quant",
        "search_read",
        [[["product_id", "=", product_id]]],
        {"fields": ["location_id", "quantity"]}
    )
    return result
