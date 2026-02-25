import os
import json
import re
import unicodedata
import xmlrpc.client
from typing import Any, Dict, List, Optional, Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Odoo Readonly Gateway", version="1.0.0")

# -----------------------
# Env
# -----------------------
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USERNAME = os.getenv("ODOO_USERNAME")
ODOO_API_KEY = os.getenv("ODOO_API_KEY")

if not all([ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_API_KEY]):
    raise RuntimeError("Missing one of ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_API_KEY")

# -----------------------
# Odoo XML-RPC clients
# -----------------------
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_API_KEY, {})
if not uid:
    raise RuntimeError("Odoo authentication failed (check DB/username/API key).")

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

# -----------------------
# Read-only enforcement
# -----------------------
ALLOWED_METHODS = {"search_read", "read", "fields_get", "name_search"}

def _odoo_call(model: str, method: str, args: List[Any], kwargs: Dict[str, Any]) -> Any:
    if method not in ALLOWED_METHODS:
        raise HTTPException(status_code=403, detail=f"Method not allowed: {method}")
    try:
        return models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, model, method, args, kwargs)
    except xmlrpc.client.Fault as e:
        raise HTTPException(status_code=400, detail=f"Odoo Fault: {e.faultString}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

# -----------------------
# Helpers
# -----------------------
def _normalize(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\s+", " ", s)
    return s

def _parse_domain(domain: Union[str, List[Any], None]) -> List[Any]:
    """
    domain can be:
    - a JSON string: '[["name","ilike","noisette"]]'
    - a python-like list passed by actions as structured JSON
    - None -> []
    """
    if domain is None:
        return []
    if isinstance(domain, list):
        return domain
    if isinstance(domain, str):
        d = domain.strip()
        if not d:
            return []
        try:
            return json.loads(d)
        except Exception:
            raise HTTPException(status_code=400, detail="domain must be JSON (stringified) or a JSON list.")
    raise HTTPException(status_code=400, detail="domain must be JSON (string) or list.")

def _parse_fields(fields: Union[str, List[str], None]) -> List[str]:
    """
    fields can be:
    - comma-separated string: "id,name"
    - JSON list ["id","name"]
    - None -> []
    """
    if fields is None:
        return []
    if isinstance(fields, list):
        return fields
    if isinstance(fields, str):
        f = fields.strip()
        if not f:
            return []
        # try json list first
        if f.startswith("["):
            try:
                return json.loads(f)
            except Exception:
                pass
        return [x.strip() for x in f.split(",") if x.strip()]
    raise HTTPException(status_code=400, detail="fields must be a comma-separated string or JSON list.")

def _token_or_domain(field: str, q: str) -> List[Any]:
    """
    Build OR domain for tokens: (field ilike tok1) OR (field ilike tok2) OR ...
    """
    qn = _normalize(q)
    tokens = [t for t in qn.split(" ") if len(t) >= 2]
    if not tokens:
        tokens = [qn]

    if len(tokens) == 1:
        return [[field, "ilike", tokens[0]]]

    # OR chain: ["|","|", cond1, cond2, cond3]
    dom: List[Any] = ["|"] * (len(tokens) - 1)
    for tok in tokens:
        dom.append([field, "ilike", tok])
    return dom

# -----------------------
# Request/Response models
# -----------------------
class SearchReadReq(BaseModel):
    model: str = Field(..., description="Odoo model name, e.g. product.template")
    domain: Union[str, List[Any], None] = Field(default=None, description="JSON domain list or JSON string")
    fields: Union[str, List[str], None] = Field(default=None, description="Comma string or JSON list")
    limit: int = 80
    offset: int = 0
    order: Optional[str] = None
    context: Optional[Dict[str, Any]] = None

class ReadReq(BaseModel):
    model: str
    ids: List[int]
    fields: Union[str, List[str], None] = None
    context: Optional[Dict[str, Any]] = None

class FieldsGetReq(BaseModel):
    model: str
    attributes: Optional[List[str]] = None
    context: Optional[Dict[str, Any]] = None

class NameSearchReq(BaseModel):
    model: str
    name: str
    domain: Union[str, List[Any], None] = None
    limit: int = 10
    context: Optional[Dict[str, Any]] = None

class ProductSearchReq(BaseModel):
    q: str
    limit: int = 10

# -----------------------
# Endpoints
# -----------------------
@app.get("/")
def health():
    return {"status": "running"}

@app.post("/odoo/search_read")
def odoo_search_read(req: SearchReadReq):
    domain = _parse_domain(req.domain)
    fields = _parse_fields(req.fields)
    kwargs: Dict[str, Any] = {
        "fields": fields,
        "limit": req.limit,
        "offset": req.offset,
    }
    if req.order:
        kwargs["order"] = req.order
    if req.context:
        kwargs["context"] = req.context
    return _odoo_call(req.model, "search_read", [domain], kwargs)

@app.post("/odoo/read")
def odoo_read(req: ReadReq):
    fields = _parse_fields(req.fields)
    kwargs: Dict[str, Any] = {"fields": fields}
    if req.context:
        kwargs["context"] = req.context
    return _odoo_call(req.model, "read", [req.ids], kwargs)

@app.post("/odoo/fields_get")
def odoo_fields_get(req: FieldsGetReq):
    kwargs: Dict[str, Any] = {}
    if req.attributes:
        kwargs["attributes"] = req.attributes
    if req.context:
        kwargs["context"] = req.context
    return _odoo_call(req.model, "fields_get", [], kwargs)

@app.post("/odoo/name_search")
def odoo_name_search(req: NameSearchReq):
    domain = _parse_domain(req.domain)
    kwargs: Dict[str, Any] = {"limit": req.limit}
    if req.context:
        kwargs["context"] = req.context
    return _odoo_call(req.model, "name_search", [req.name, domain], kwargs)

# ✅ Résout ton problème "café noisette" / "noisette"
# -> recherche tokenisée + sans accents + retourne candidats (id+name)
@app.post("/odoo/find_product_templates")
def find_product_templates(req: ProductSearchReq):
    # On cherche par tokens sur le champ name (tolerant)
    domain = _token_or_domain("name", req.q)

    # Récup minimal
    results = _odoo_call(
        "product.template",
        "search_read",
        [domain],
        {"fields": ["id", "name"], "limit": req.limit}
    )

    # Petit bonus : on trie côté serveur pour mettre les meilleurs en haut (sans IA, juste heuristique)
    qn = _normalize(req.q)
    def rank(x: Dict[str, Any]) -> int:
        name = _normalize(x.get("name", ""))
        # score simple: nb de tokens présents
        tokens = [t for t in qn.split(" ") if t]
        return sum(1 for t in tokens if t in name)

    results.sort(key=rank, reverse=True)
    return {"query": req.q, "results": results}
