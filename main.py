from fastapi import FastAPI, Request
from pydantic import BaseModel
from typing import List, Optional
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
import httpx
import base64
import os
import psycopg2
from psycopg2.extras import RealDictCursor
import uuid
import json
import math

app = FastAPI(title="Drip'd API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============ CONFIG ============
CASHFREE_APP_ID = os.environ.get("CASHFREE_APP_ID", "")

GOOGLE_MAPS_KEY = os.environ.get("GOOGLE_MAPS_KEY", "")
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "")
FIREBASE_APP_ID = os.environ.get("FIREBASE_APP_ID", "")
FIREBASE_AUTH_DOMAIN = os.environ.get("FIREBASE_AUTH_DOMAIN", "")
FIREBASE_MESSAGING_SENDER_ID = os.environ.get("FIREBASE_MESSAGING_SENDER_ID", "")
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")
FIREBASE_STORAGE_BUCKET = os.environ.get("FIREBASE_STORAGE_BUCKET", "")

@app.get("/config/maps-key")
def get_maps_key():
    return {"key": GOOGLE_MAPS_KEY}

@app.get("/config/firebase")
def get_firebase_config():
    return {
        "apiKey": FIREBASE_API_KEY,
        "appId": FIREBASE_APP_ID,
        "authDomain": FIREBASE_AUTH_DOMAIN,
        "messagingSenderId": FIREBASE_MESSAGING_SENDER_ID,
        "projectId": FIREBASE_PROJECT_ID,
        "storageBucket": FIREBASE_STORAGE_BUCKET,
    }

@app.get("/location/reverse")
async def reverse_location(lat: float, lng: float):
    try:
        return await reverse_geocode_label(lat, lng)
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/location/search")
async def search_location(q: str):
    query = (q or "").strip()
    if len(query) < 3:
        return {"success": False, "error": "Enter at least 3 characters"}
    try:
        results = []
        if GOOGLE_MAPS_KEY:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    "https://maps.googleapis.com/maps/api/geocode/json",
                    params={"address": query, "key": GOOGLE_MAPS_KEY, "region": "in"},
                    timeout=10
                )
            data = r.json()
            if data.get("status") not in ("OK", "ZERO_RESULTS"):
                return {"success": False, "error": data.get("error_message") or data.get("status", "Google Maps error")}
            for item in data.get("results", [])[:6]:
                loc = item.get("geometry", {}).get("location", {})
                if loc.get("lat") is None or loc.get("lng") is None:
                    continue
                results.append({
                    "label": item.get("formatted_address", query),
                    "address": item.get("formatted_address", query),
                    "lat": loc["lat"],
                    "lng": loc["lng"]
                })
        else:
            async with httpx.AsyncClient(headers={"User-Agent": "Dripd/1.0 support@getdripd.in"}) as client:
                r = await client.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={"q": query, "format": "json", "addressdetails": 1, "limit": 6, "countrycodes": "in"},
                    timeout=10
                )
            for item in r.json():
                results.append({
                    "label": short_location_label(item.get("address", {}), item.get("display_name", query)),
                    "address": item.get("display_name", query),
                    "lat": float(item["lat"]),
                    "lng": float(item["lon"])
                })
        return {"success": True, "results": results}
    except Exception as e:
        return {"success": False, "error": str(e)}


CASHFREE_SECRET_KEY = os.environ.get("CASHFREE_SECRET_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ============ DATABASE ============
def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

# ============ MODELS ============
class CustomerRegister(BaseModel):
    name: str
    phone: str
    address: Optional[str] = None

class CustomerLogin(BaseModel):
    phone: str

class StoreRegister(BaseModel):
    name: str
    owner_name: str
    phone: str
    area: str
    categories: List[str]
    opening_time: str = "10:00"
    closing_time: str = "22:00"
    latitude: Optional[float] = None
    longitude: Optional[float] = None

class StoreLogin(BaseModel):
    phone: str
    otp: Optional[str] = None

class AddProduct(BaseModel):
    store_id: str
    name: str
    price: int
    sizes: List[str]
    category: str
    photo: Optional[str] = None

class UpdatePhoto(BaseModel):
    photo: str

class Order(BaseModel):
    customer_name: str
    customer_phone: str
    customer_address: str = 'Address not provided'
    store_id: str
    product_id: str
    size: str = 'M'
    total_amount: float
    payment_id: Optional[str] = None
    items: Optional[list] = None

    @property
    def total_amount_int(self):
        return int(self.total_amount)

class OrderStatusUpdate(BaseModel):
    order_id: Optional[str] = None
    status: str
    updated_by: Optional[str] = 'system'
    updated_by_id: Optional[str] = None
    note: Optional[str] = None

class RatingRequest(BaseModel):
    order_id: str
    product_rating: float
    store_rating: float
    review: Optional[str] = None

class PartnerRegister(BaseModel):
    name: str
    phone: str
    area: str
    vehicle_type: str
    upi_id: str
    account_name: str

class PartnerLogin(BaseModel):
    phone: str

class StorePaymentDetails(BaseModel):
    store_id: str
    payment_method: str
    bank_name: Optional[str] = None
    account_number: Optional[str] = None
    ifsc_code: Optional[str] = None
    account_holder_name: Optional[str] = None
    upi_id: Optional[str] = None

class PartnerPaymentDetails(BaseModel):
    partner_phone: str
    upi_id: str
    account_name: Optional[str] = None

class PayoutRequest(BaseModel):
    requester_type: str
    requester_id: str
    requester_name: str
    amount: int
    payment_method: str
    payment_details: dict

class TryBuyComplete(BaseModel):
    kept_items: list
    returned_items: list
    refund_amount: float
    final_amount: float
    partner_id: Optional[str] = None

# ============ RAZORPAY ============
async def process_cashfree_refund(payment_id: str, amount: int, notes: str):
    try:
        credentials = base64.b64encode(f"{CASHFREE_APP_ID}:{CASHFREE_SECRET_KEY}".encode()).decode()
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://api.cashfree.com/pg/orders/{payment_id}/refunds",
                headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/json"},
                json={"amount": amount * 100, "notes": {"reason": notes}}
            )
            data = response.json()
            return {"success": True, "refund_id": data.get("id"), "amount": amount}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ============ HELPERS ============
def short_location_label(address: dict, fallback: str = "Location found") -> str:
    if not address:
        return fallback
    area = (
        address.get("suburb") or address.get("neighbourhood") or
        address.get("quarter") or address.get("city_district") or
        address.get("village")
    )
    city = address.get("city") or address.get("town") or address.get("municipality") or address.get("county")
    state = address.get("state")
    parts = []
    for part in (area, city, state):
        if part and part not in parts:
            parts.append(part)
    return ", ".join(parts[:3]) or fallback

def full_location_address(address: dict, fallback: str = "Location found") -> str:
    if not address:
        return fallback
    keys = ("house_number", "road", "suburb", "neighbourhood", "quarter", "city", "town", "state", "postcode")
    parts = []
    for key in keys:
        part = address.get(key)
        if part and part not in parts:
            parts.append(part)
    return ", ".join(parts) or fallback

async def reverse_geocode_label(lat: float, lng: float) -> dict:
    if GOOGLE_MAPS_KEY:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"latlng": f"{lat},{lng}", "key": GOOGLE_MAPS_KEY},
                timeout=10
            )
        data = r.json()
        if data.get("status") == "OK" and data.get("results"):
            priority = {
                "street_address": 0,
                "premise": 1,
                "subpremise": 2,
                "establishment": 3,
                "point_of_interest": 4,
                "route": 5,
                "neighborhood": 6,
                "sublocality": 7,
                "locality": 8,
            }
            def score_result(item):
                types = item.get("types", [])
                return min([priority.get(t, 99) for t in types] or [99])
            result = sorted(data["results"], key=score_result)[0]
            comps = result.get("address_components", [])
            def pick(*types):
                for comp in comps:
                    if any(t in comp.get("types", []) for t in types):
                        return comp.get("long_name")
                return None
            area = pick("sublocality_level_1", "sublocality", "neighborhood")
            city = pick("locality", "administrative_area_level_3")
            state = pick("administrative_area_level_1")
            label = ", ".join([p for p in (area, city, state) if p])
            formatted = result.get("formatted_address", "")
            return {
                "success": True,
                "label": label or formatted or "Location found",
                "address": formatted or label or "Location found"
            }
    async with httpx.AsyncClient(headers={"User-Agent": "Dripd/1.0 support@getdripd.in"}) as client:
        r = await client.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lng, "format": "json", "addressdetails": 1},
            timeout=10
        )
    data = r.json()
    address = data.get("address", {})
    display = data.get("display_name", "")
    return {
        "success": True,
        "label": short_location_label(address, display or "Location found"),
        "address": display or full_location_address(address),
        "raw": data
    }

def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6371
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2 +
        math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
        math.sin(dlng / 2) ** 2
    )
    return radius * 2 * math.asin(math.sqrt(a))

async def get_road_distances(lat: float, lng: float, stores: list) -> dict:
    if not GOOGLE_MAPS_KEY:
        return {}
    distances = {}
    async with httpx.AsyncClient() as client:
        for start in range(0, len(stores), 25):
            batch = stores[start:start + 25]
            destinations = []
            ids = []
            for store in batch:
                if store.get("latitude") is None or store.get("longitude") is None:
                    continue
                destinations.append(f"{float(store['latitude'])},{float(store['longitude'])}")
                ids.append(store["id"])
            if not destinations:
                continue
            r = await client.get(
                "https://maps.googleapis.com/maps/api/distancematrix/json",
                params={
                    "origins": f"{lat},{lng}",
                    "destinations": "|".join(destinations),
                    "mode": "driving",
                    "units": "metric",
                    "key": GOOGLE_MAPS_KEY
                },
                timeout=12
            )
            data = r.json()
            elements = (data.get("rows") or [{}])[0].get("elements") or []
            for store_id, element in zip(ids, elements):
                if element.get("status") != "OK":
                    continue
                distances[store_id] = {
                    "distance_km": element["distance"]["value"] / 1000,
                    "distance_text": element["distance"].get("text", ""),
                    "duration_text": element["duration"].get("text", ""),
                    "duration_seconds": element["duration"].get("value", 0)
                }
    return distances

async def enrich_stores_with_distance(stores: list, lat: float = None, lng: float = None, radius_km: float = 25.0) -> list:
    result = []
    road_distances = {}
    if lat is not None and lng is not None:
        road_distances = await get_road_distances(lat, lng, stores)
    for s in stores:
        store = dict(s)
        if lat is not None and lng is not None and store.get("latitude") is not None and store.get("longitude") is not None:
            road = road_distances.get(store.get("id"))
            if road:
                dist = road["distance_km"]
                if radius_km and dist > radius_km:
                    continue
                store["distance_km"] = round(dist, 1)
                store["distance_text"] = road["distance_text"] or f"{store['distance_km']} km"
                store["delivery_time"] = road["duration_text"] or store.get("delivery_time") or "~30 min"
                store["distance_source"] = "road"
            else:
                dist = haversine_km(lat, lng, float(store["latitude"]), float(store["longitude"]))
                if radius_km and dist > radius_km:
                    continue
                store["distance_km"] = round(dist, 1)
                mins = int((dist / 20) * 60) + 10
                store["distance_text"] = f"{store['distance_km']} km"
                store["delivery_time"] = f"~{mins} min"
                store["distance_source"] = "straight_line"
        else:
            store["delivery_time"] = store.get("delivery_time") or "~30 min"
        result.append(store)
    if lat is not None and lng is not None:
        result.sort(key=lambda store: store.get("distance_km", 99999))
    return result

def create_invoice(cur, order, order_id: str, store_name: str, product_name: str, product_price: int):
    invoice_number = f"INV-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    cur.execute("""
        INSERT INTO invoices (id, invoice_number, order_id, customer_name, customer_phone,
            customer_address, store_name, store_id, product_name, product_price, size,
            delivery_fee, platform_fee, try_and_buy_fee, total_amount, payment_id, is_try_and_buy)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        str(uuid.uuid4()), invoice_number, order_id,
        order.customer_name, order.customer_phone, order.customer_address,
        store_name, order.store_id, product_name, product_price, order.size,
        40, 5, 0, order.total_amount, order.payment_id, False
    ))
    return invoice_number

def create_commission(cur, order_id: str, store_id: str, order_amount: int):
    commission_rate = 0.20
    commission_amount = int(order_amount * commission_rate)
    store_payout = order_amount - commission_amount
    cur.execute("""
        INSERT INTO commissions (id, order_id, store_id, order_amount, commission_rate,
            commission_amount, store_payout, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
        ON CONFLICT (order_id) DO NOTHING
    """, (str(uuid.uuid4()), order_id, store_id, order_amount,
          commission_rate, commission_amount, store_payout))
    return commission_amount, store_payout

# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def home():
    return {"app": "Drip'd", "version": "1.0", "message": "Fashion delivered in 60 minutes!"}

# ============ OTP ============
@app.post("/otp/send")
async def send_otp(request: Request):
    return {"success": False, "error": "OTP is handled by Firebase Phone Auth on the client."}

@app.post("/otp/verify")
async def verify_otp(request: Request):
    return {"success": False, "error": "OTP is verified by Firebase Phone Auth on the client."}

# ============ CUSTOMERS ============
@app.post("/customers/register")
def register_customer(customer: CustomerRegister):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM customers WHERE phone = %s", (customer.phone,))
        existing = cur.fetchone()
        if existing:
            conn.close()
            return {"success": True, "customer": dict(existing), "already_registered": True}
        customer_id = f"c_{uuid.uuid4().hex[:8]}"
        cur.execute("""
            INSERT INTO customers (id, name, phone, address)
            VALUES (%s, %s, %s, %s)
        """, (customer_id, customer.name, customer.phone, customer.address))
        conn.commit()
        cur.execute("SELECT * FROM customers WHERE id = %s", (customer_id,))
        new_customer = cur.fetchone()
        conn.close()
        return {"success": True, "customer": dict(new_customer)}
    except Exception as e:
        return {"error": str(e)}

@app.get("/customers/{phone}")
def get_customer(phone: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM customers WHERE phone = %s", (phone,))
        customer = cur.fetchone()
        conn.close()
        if not customer:
            return {"exists": False}
        return {"exists": True, "customer": dict(customer)}
    except Exception as e:
        return {"error": str(e)}

@app.patch("/customers/{phone}")
def update_customer(phone: str, body: dict):
    try:
        conn = get_db()
        cur = conn.cursor()
        if body.get("name"):
            cur.execute("UPDATE customers SET name = %s WHERE phone = %s", (body["name"], phone))
        if body.get("address"):
            cur.execute("UPDATE customers SET address = %s WHERE phone = %s", (body["address"], phone))
        conn.commit()
        cur.execute("SELECT * FROM customers WHERE phone = %s", (phone,))
        customer = cur.fetchone()
        conn.close()
        return {"success": True, "customer": dict(customer)}
    except Exception as e:
        return {"error": str(e)}

# ============ STORES ============
@app.post("/stores/register")
def register_store(store: StoreRegister):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM stores WHERE phone = %s", (store.phone,))
        existing = cur.fetchone()
        if existing:
            conn.close()
            return {"error": "Phone number already registered!"}
        store_id = f"s_{uuid.uuid4().hex[:8]}"
        cur.execute("""
            INSERT INTO stores (id, name, owner_name, phone, area, categories,
                opening_time, closing_time, is_open, rating, distance_km,
                delivery_time, total_ratings, rating_sum, status, latitude, longitude)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (store_id, store.name, store.owner_name, store.phone, store.area,
              store.categories, store.opening_time, store.closing_time,
              False, 0.0, 1.0, "25-45 mins", 0, 0.0, "pending",
              store.latitude, store.longitude))
        conn.commit()
        conn.close()
        return {"success": True, "store_id": store_id, "message": f"Welcome to Drip'd, {store.name}!"}
    except Exception as e:
        return {"error": str(e)}

@app.post("/stores/login")
def login_store(body: StoreLogin):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM stores WHERE phone = %s", (body.phone,))
        store = cur.fetchone()
        conn.close()
        if not store:
            return {"error": "Phone number not registered. Please register first!", "not_registered": True}
        if store["status"] == "pending":
            return {"pending": True, "message": "Your store is pending approval. We'll notify you within 24 hours!"}
        if store["status"] == "rejected":
            return {"error": "Your application was not approved. Contact getdripd1@gmail.com", "rejected": True}
        return {"success": True, "store": dict(store)}
    except Exception as e:
        return {"error": str(e)}

@app.get("/stores")
async def get_stores(lat: float = None, lng: float = None, radius_km: float = 25.0):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM stores WHERE status = 'active' ORDER BY rating DESC")
        stores = cur.fetchall()
        conn.close()
        result = await enrich_stores_with_distance(stores, lat, lng, radius_km)
        return {"stores": result}
    except Exception as e:
        return {"error": str(e)}

@app.get("/stores/category/{category}")
async def get_stores_by_category(category: str, lat: float = None, lng: float = None, radius_km: float = 25.0):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM stores WHERE %s = ANY(categories) AND status = 'active'", (category,))
        stores = cur.fetchall()
        conn.close()
        result = await enrich_stores_with_distance(stores, lat, lng, radius_km)
        return {"stores": result}
    except Exception as e:
        return {"error": str(e)}

@app.get("/stores/{store_id}")
def get_store(store_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
        conn.close()
        if not store:
            return {"error": "Store not found"}
        return {"store": dict(store)}
    except Exception as e:
        return {"error": str(e)}

@app.get("/stores/{store_id}/products")
def get_store_products(store_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM products WHERE store_id = %s AND (available = TRUE OR available IS NULL)", (store_id,))
        products = cur.fetchall()
        conn.close()
        return {"products": [dict(p) for p in products]}
    except Exception as e:
        return {"error": str(e)}

@app.patch("/stores/{store_id}/status")
def update_store_status(store_id: str, body: dict):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE stores SET is_open = %s WHERE id = %s", (body.get("is_open"), store_id))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}

@app.patch("/stores/{store_id}/location")
def update_store_location(store_id: str, body: dict):
    try:
        lat = body.get("latitude")
        lng = body.get("longitude")
        if lat is None or lng is None:
            return {"error": "latitude and longitude required"}
        conn = get_db()
        cur = conn.cursor()
        address = (body.get("address") or body.get("area") or "").strip()
        if address:
            cur.execute("UPDATE stores SET latitude = %s, longitude = %s, area = %s WHERE id = %s", (lat, lng, address, store_id))
        else:
            cur.execute("UPDATE stores SET latitude = %s, longitude = %s WHERE id = %s", (lat, lng, store_id))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}

@app.patch("/stores/{store_id}/photo")
def update_store_photo(store_id: str, body: dict):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE stores SET photo = %s WHERE id = %s", (body.get("photo"), store_id))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}

@app.get("/stores/{store_id}/ratings")
def get_store_ratings(store_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM ratings WHERE store_id = %s ORDER BY timestamp DESC", (store_id,))
        ratings = cur.fetchall()
        conn.close()
        return {"ratings": [dict(r) for r in ratings]}
    except Exception as e:
        return {"error": str(e)}

# ============ PRODUCTS ============
@app.post("/products")
def add_product(product: AddProduct):
    try:
        conn = get_db()
        cur = conn.cursor()
        product_id = f"p_{uuid.uuid4().hex[:8]}"
        cur.execute("""
            INSERT INTO products (id, store_id, name, price, sizes, category,
                available, photo, rating, total_ratings, rating_sum)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (product_id, product.store_id, product.name, product.price,
              product.sizes, product.category, True, product.photo, 0.0, 0, 0.0))
        conn.commit()
        cur.execute("SELECT * FROM products WHERE id = %s", (product_id,))
        new_product = cur.fetchone()
        conn.close()
        return {"success": True, "product": dict(new_product)}
    except Exception as e:
        return {"error": str(e)}

@app.patch("/products/{product_id}/photo")
def update_product_photo(product_id: str, body: UpdatePhoto):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE products SET photo = %s WHERE id = %s", (body.photo, product_id))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}

@app.patch("/products/{product_id}/availability")
def toggle_product_availability(product_id: str, body: dict):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE products SET available = %s WHERE id = %s", (body.get("available"), product_id))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}

@app.delete("/products/{product_id}")
def delete_product(product_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM products WHERE id = %s", (product_id,))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}

# ============ ORDERS ============
@app.post("/orders")
def place_order(order: Order):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM stores WHERE id = %s", (order.store_id,))
        store = cur.fetchone()
        cur.execute("SELECT * FROM products WHERE id = %s", (order.product_id,))
        product = cur.fetchone()
        if not store or not product:
            conn.close()
            return {"error": "Store or product not found"}

        order_id = f"DR{uuid.uuid4().hex[:6].upper()}"

        cur.execute("""
            INSERT INTO orders (order_id, customer, phone, address, store_id, store,
                product_id, product, product_price, size, amount, status,
                is_try_and_buy, payment_id, try_status, return_status,
                is_rated, estimated_delivery, current_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            order_id, order.customer_name, order.customer_phone,
            order.customer_address or 'Address not provided',
            order.store_id, store["name"], order.product_id, product["name"],
            product["price"], order.size, int(order.total_amount), "confirmed",
            False, order.payment_id, None, None, False,
            store.get("delivery_time", "~30 min"), "confirmed"
        ))

        # Save multi-item order items if provided
        if order.items:
            for item in order.items:
                cur.execute("""
                    INSERT INTO order_items (id, order_id, product_id, product_name,
                        size, price, photo, decision)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
                    ON CONFLICT DO NOTHING
                """, (str(uuid.uuid4()), order_id,
                      item.get("product_id", order.product_id),
                      item.get("product_name", product["name"]),
                      item.get("size", order.size),
                      item.get("price", product["price"]),
                      item.get("photo", "")))

        cur.execute("""
            INSERT INTO order_status_history (id, order_id, status, updated_by, note)
            VALUES (%s, %s, 'confirmed', 'system', 'Order placed successfully')
        """, (str(uuid.uuid4()), order_id))

        invoice_number = create_invoice(cur, order, order_id, store["name"],
                                       product["name"], product["price"])
        commission_amount, store_payout = create_commission(cur, order_id,
                                                            order.store_id, order.total_amount)

        # GST invoice
        taxable = int(order.total_amount / 1.18)
        tax = order.total_amount - taxable
        gst_invoice_number = f"GST-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        cur.execute("""
            INSERT INTO gst_invoices (id, invoice_number, order_id, store_id,
                customer_name, customer_phone, store_name, taxable_amount,
                cgst_amount, sgst_amount, total_tax, total_amount)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (order_id) DO NOTHING
        """, (str(uuid.uuid4()), gst_invoice_number, order_id,
              order.store_id, order.customer_name, order.customer_phone,
              store["name"], taxable, tax//2, tax//2, tax, order.total_amount))

        conn.commit()
        cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
        new_order = cur.fetchone()
        conn.close()

        return {
            "success": True,
            "order": dict(new_order),
            "invoice_number": invoice_number,
            "commission_amount": commission_amount,
            "store_payout": store_payout,
            "message": "Order placed! Your fashion is on its way in 60 minutes!"
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/orders")
def get_orders(status: str = None, store_id: str = None):
    try:
        conn = get_db()
        cur = conn.cursor()
        if status == "packed":
            cur.execute("""
                SELECT o.*, s.name as store_name, s.area as store_area,
                       s.area as store_address, s.phone as store_phone
                FROM orders o
                LEFT JOIN stores s ON o.store_id = s.id
                WHERE o.current_status = 'packed'
                AND (o.assigned_partner_id IS NULL OR o.assigned_partner_id = '')
                ORDER BY o.timestamp DESC
            """)
            orders = cur.fetchall()
            result = []
            for o in orders:
                order_dict = dict(o)
                cur.execute("SELECT * FROM order_items WHERE order_id = %s", (o["order_id"],))
                items = cur.fetchall()
                order_dict["items"] = [dict(i) for i in items] if items else [{
                    "name": o.get("product", "Item"),
                    "size": o.get("size", "M"),
                    "price": o.get("product_price") or o.get("amount", 0),
                    "photo": None
                }]
                result.append(order_dict)
            conn.close()
            return {"orders": result, "total": len(result)}
        elif store_id:
            cur.execute("SELECT * FROM orders WHERE store_id = %s ORDER BY timestamp DESC", (store_id,))
            orders = cur.fetchall()
            conn.close()
            return {"orders": [dict(o) for o in orders], "total": len(orders)}
        else:
            cur.execute("SELECT * FROM orders ORDER BY timestamp DESC")
            orders = cur.fetchall()
            conn.close()
            return {"orders": [dict(o) for o in orders], "total": len(orders)}
    except Exception as e:
        return {"error": str(e)}

@app.get("/orders/store/{store_id}")
def get_store_orders(store_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE store_id = %s ORDER BY timestamp DESC", (store_id,))
        orders = cur.fetchall()
        conn.close()
        return {"orders": [dict(o) for o in orders], "total": len(orders)}
    except Exception as e:
        return {"error": str(e)}

@app.get("/orders/{order_id}")
def get_order(order_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
        order = cur.fetchone()
        if not order:
            conn.close()
            return {"error": "Order not found"}
        order_dict = dict(order)
        cur.execute("SELECT * FROM order_items WHERE order_id = %s", (order_id,))
        items = cur.fetchall()
        if items:
            order_dict["items"] = [dict(i) for i in items]
        conn.close()
        return {"order": order_dict}
    except Exception as e:
        return {"error": str(e)}

@app.patch("/orders/{order_id}/status")
def update_order_status(order_id: str, body: OrderStatusUpdate):
    try:
        valid_statuses = ["confirmed", "packed", "out_for_delivery", "delivered"]
        if body.status not in valid_statuses:
            return {"error": f"Invalid status. Must be one of: {valid_statuses}"}
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
        order = cur.fetchone()
        if not order:
            conn.close()
            return {"error": "Order not found"}
        timestamp_field = {
            "packed": "packed_at",
            "out_for_delivery": "out_for_delivery_at",
            "delivered": "delivered_at"
        }.get(body.status)
        if timestamp_field:
            cur.execute(f"UPDATE orders SET current_status = %s, {timestamp_field} = NOW() WHERE order_id = %s",
                       (body.status, order_id))
        else:
            cur.execute("UPDATE orders SET current_status = %s WHERE order_id = %s",
                       (body.status, order_id))
        cur.execute("""
            INSERT INTO order_status_history (id, order_id, status, updated_by, updated_by_id, note)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (str(uuid.uuid4()), order_id, body.status,
              body.updated_by, body.updated_by_id, body.note))
        conn.commit()
        conn.close()
        status_messages = {
            "packed": "Order packed! 📦",
            "out_for_delivery": "Order picked up! 🛵",
            "delivered": "Order delivered! ✅"
        }
        return {"success": True, "status": body.status,
                "message": status_messages.get(body.status, "Status updated!")}
    except Exception as e:
        return {"error": str(e)}

@app.get("/orders/{order_id}/status")
def get_order_status(order_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
        order = cur.fetchone()
        if not order:
            conn.close()
            return {"error": "Order not found"}
        cur.execute("SELECT * FROM order_status_history WHERE order_id = %s ORDER BY created_at ASC",
                   (order_id,))
        history = cur.fetchall()
        conn.close()
        current = order["current_status"] or "confirmed"
        stages = [
            {"status": "confirmed", "label": "Order confirmed", "emoji": "✅",
             "eta": "Just now", "done": True},
            {"status": "packed", "label": "Store is packing your order", "emoji": "📦",
             "eta": "~10 mins", "done": current in ["packed", "out_for_delivery", "delivered"],
             "time": str(order["packed_at"]) if order.get("packed_at") else None},
            {"status": "out_for_delivery", "label": "Partner on the way", "emoji": "🛵",
             "eta": "~20 mins", "done": current in ["out_for_delivery", "delivered"],
             "time": str(order["out_for_delivery_at"]) if order.get("out_for_delivery_at") else None},
            {"status": "delivered", "label": "Delivered!", "emoji": "🎉",
             "eta": "~35 mins", "done": current == "delivered",
             "time": str(order["delivered_at"]) if order.get("delivered_at") else None},
        ]
        return {"order_id": order_id, "current_status": current, "stages": stages,
                "history": [dict(h) for h in history],
                "delivery_partner": order.get("assigned_partner_name")}
    except Exception as e:
        return {"error": str(e)}

@app.post("/orders/{order_id}/partner")
def assign_partner_to_order(order_id: str, body: dict):
    try:
        partner_id = body.get("partner_id", "")
        partner_name = body.get("partner_name", "Partner")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM orders WHERE order_id = %s
            AND (assigned_partner_id IS NULL OR assigned_partner_id = '')
        """, (order_id,))
        order = cur.fetchone()
        if not order:
            conn.close()
            return {"success": False, "error": "Order not found or already assigned"}
        cur.execute("SELECT phone FROM delivery_partners WHERE id = %s", (partner_id,))
        partner_row = cur.fetchone()
        partner_phone = partner_row["phone"] if partner_row else ""
        cur.execute("""
            UPDATE orders SET assigned_partner_id = %s, assigned_partner_name = %s,
                assigned_partner_phone = %s, current_status = 'out_for_delivery',
                out_for_delivery_at = NOW()
            WHERE order_id = %s
        """, (partner_id, partner_name, partner_phone, order_id))
        cur.execute("""
            INSERT INTO order_status_history (id, order_id, status, updated_by, note)
            VALUES (%s, %s, 'out_for_delivery', %s, 'Partner assigned')
        """, (str(uuid.uuid4()), order_id, partner_id))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}

@app.post("/orders/{order_id}/complete")
async def complete_order(order_id: str, body: dict):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
        order = cur.fetchone()
        if not order:
            conn.close()
            return {"error": "Order not found"}
        partner_id = body.get("partner_id")
        cur.execute("""
            UPDATE orders SET current_status = 'delivered', delivered_at = NOW()
            WHERE order_id = %s
        """, (order_id,))
        cur.execute("""
            INSERT INTO order_status_history (id, order_id, status, updated_by, note)
            VALUES (%s, %s, 'delivered', %s, 'Delivered by partner')
        """, (str(uuid.uuid4()), order_id, partner_id or 'partner'))
        if partner_id:
            partner_earn = 60
            cur.execute("""
                INSERT INTO partner_earnings (id, partner_id, order_id, amount, status)
                VALUES (%s, %s, %s, %s, 'pending') ON CONFLICT DO NOTHING
            """, (str(uuid.uuid4()), partner_id, order_id, partner_earn))
            cur.execute("""
                UPDATE delivery_partners
                SET delivery_count = COALESCE(delivery_count, 0) + 1,
                    total_earnings = COALESCE(total_earnings, 0) + %s
                WHERE id = %s
            """, (partner_earn, partner_id))
        conn.commit()
        conn.close()
        return {"success": True, "message": "Order completed!"}
    except Exception as e:
        return {"error": str(e)}

@app.post("/orders/{order_id}/rate")
def rate_order(order_id: str, body: RatingRequest):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
        order = cur.fetchone()
        if not order:
            conn.close()
            return {"error": "Order not found"}
        if order["is_rated"]:
            conn.close()
            return {"error": "Already rated"}
        if not 1 <= body.product_rating <= 5 or not 1 <= body.store_rating <= 5:
            conn.close()
            return {"error": "Rating must be 1-5"}
        cur.execute("SELECT rating_sum, total_ratings FROM products WHERE id = %s", (order["product_id"],))
        product = cur.fetchone()
        if product:
            new_sum = product["rating_sum"] + body.product_rating
            new_total = product["total_ratings"] + 1
            cur.execute("UPDATE products SET rating_sum = %s, total_ratings = %s, rating = %s WHERE id = %s",
                       (new_sum, new_total, round(new_sum / new_total, 1), order["product_id"]))
        cur.execute("SELECT rating_sum, total_ratings FROM stores WHERE id = %s", (order["store_id"],))
        store = cur.fetchone()
        if store:
            new_sum = store["rating_sum"] + body.store_rating
            new_total = store["total_ratings"] + 1
            cur.execute("UPDATE stores SET rating_sum = %s, total_ratings = %s, rating = %s WHERE id = %s",
                       (new_sum, new_total, round(new_sum / new_total, 1), order["store_id"]))
        cur.execute("""
            INSERT INTO ratings (rating_id, order_id, product_id, store_id,
                product_rating, store_rating, review, customer)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (str(uuid.uuid4()), order_id, order["product_id"],
              order["store_id"], body.product_rating, body.store_rating,
              body.review, order["customer"]))
        cur.execute("UPDATE orders SET is_rated = TRUE WHERE order_id = %s", (order_id,))
        conn.commit()
        conn.close()
        return {"success": True, "message": "Thank you for your review! ⭐"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/invoice/{order_id}")
def get_invoice(order_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM invoices WHERE order_id = %s", (order_id,))
        invoice = cur.fetchone()
        conn.close()
        if not invoice:
            return {"error": "Invoice not found"}
        return {"invoice": dict(invoice)}
    except Exception as e:
        return {"error": str(e)}

@app.get("/gst/store/{store_id}")
def get_store_gst(store_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM gst_invoices WHERE store_id = %s ORDER BY created_at DESC", (store_id,))
        invoices = cur.fetchall()
        cur.execute("""
            SELECT SUM(total_tax) as total_tax, SUM(total_amount) as gross, COUNT(*) as count
            FROM gst_invoices WHERE store_id = %s
        """, (store_id,))
        summary = cur.fetchone()
        conn.close()
        return {"invoices": [dict(i) for i in invoices], "summary": dict(summary) if summary else {}}
    except Exception as e:
        return {"error": str(e)}

# ============ PARTNERS ============
@app.post("/partners/register")
def register_partner(partner: PartnerRegister):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM delivery_partners WHERE phone = %s", (partner.phone,))
        existing = cur.fetchone()
        if existing:
            conn.close()
            return {"error": "Phone number already registered!", "already_registered": True}
        partner_id = f"dp_{uuid.uuid4().hex[:8]}"
        cur.execute("""
            INSERT INTO delivery_partners
            (id, name, phone, area, vehicle_type, upi_id, account_name,
             status, is_online, total_earnings, delivery_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', FALSE, 0, 0)
        """, (partner_id, partner.name, partner.phone, partner.area,
              partner.vehicle_type, partner.upi_id, partner.account_name))
        conn.commit()
        conn.close()
        return {
            "success": True,
            "partner": {
                "id": partner_id, "name": partner.name, "phone": partner.phone,
                "area": partner.area, "vehicle_type": partner.vehicle_type,
                "upi_id": partner.upi_id, "status": "pending"
            },
            "message": "Registration submitted! Pending approval."
        }
    except Exception as e:
        return {"error": str(e)}

@app.post("/partners/login")
def login_partner(body: PartnerLogin):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM delivery_partners WHERE phone = %s", (body.phone,))
        partner = cur.fetchone()
        conn.close()
        if not partner:
            return {"error": "Phone not registered. Please register first!", "not_registered": True}
        if partner["status"] == "pending":
            return {"error": "Account pending approval. We'll notify you soon!", "pending": True}
        if partner["status"] == "rejected":
            return {"error": "Application not approved. Contact getdripd1@gmail.com", "rejected": True}
        return {"success": True, "partner": dict(partner)}
    except Exception as e:
        return {"error": str(e)}

@app.get("/partners/{phone}")
def get_partner_by_phone(phone: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM delivery_partners WHERE phone = %s", (phone,))
        partner = cur.fetchone()
        conn.close()
        if not partner:
            return {"exists": False}
        return {"exists": True, "partner": dict(partner)}
    except Exception as e:
        return {"error": str(e)}

@app.patch("/partners/{partner_id}/online")
def toggle_partner_online(partner_id: str, body: dict):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE delivery_partners SET is_online = %s WHERE id = %s",
                   (body.get("is_online"), partner_id))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}

@app.get("/partners/{partner_id}/earnings")
def get_partner_earnings(partner_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM delivery_partners WHERE id = %s", (partner_id,))
        partner = cur.fetchone()
        if not partner:
            conn.close()
            return {"error": "Partner not found"}
        cur.execute("SELECT * FROM partner_earnings WHERE partner_id = %s ORDER BY created_at DESC",
                   (partner_id,))
        earnings = cur.fetchall()
        conn.close()
        return {"success": True, "partner": dict(partner), "earnings": [dict(e) for e in earnings]}
    except Exception as e:
        return {"error": str(e)}

@app.post("/partner/payment-details")
def save_partner_payment(body: PartnerPaymentDetails):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO partner_payment_details (partner_phone, upi_id)
            VALUES (%s, %s)
            ON CONFLICT (partner_phone) DO UPDATE SET upi_id = %s, updated_at = NOW()
        """, (body.partner_phone, body.upi_id, body.upi_id))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}

# ============ STORE PAYMENT DETAILS ============
@app.post("/store/payment-details")
def save_store_payment(body: StorePaymentDetails):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO store_payment_details
            (store_id, payment_method, bank_name, account_number,
             ifsc_code, account_holder_name, upi_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (store_id) DO UPDATE SET
                payment_method = %s, bank_name = %s, account_number = %s,
                ifsc_code = %s, account_holder_name = %s, upi_id = %s, updated_at = NOW()
        """, (
            body.store_id, body.payment_method, body.bank_name,
            body.account_number, body.ifsc_code, body.account_holder_name, body.upi_id,
            body.payment_method, body.bank_name, body.account_number,
            body.ifsc_code, body.account_holder_name, body.upi_id
        ))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}

@app.get("/store/payment-details/{store_id}")
def get_store_payment(store_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM store_payment_details WHERE store_id = %s", (store_id,))
        details = cur.fetchone()
        conn.close()
        if not details:
            return {"exists": False}
        return {"exists": True, "details": dict(details)}
    except Exception as e:
        return {"error": str(e)}

# ============ COMMISSIONS ============
@app.get("/commissions/store/{store_id}")
def get_store_commissions(store_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM commissions WHERE store_id = %s ORDER BY created_at DESC", (store_id,))
        commissions = cur.fetchall()
        cur.execute("""
            SELECT SUM(order_amount) as gross, SUM(commission_amount) as total_commission,
                   SUM(store_payout) as total_payout, COUNT(*) as order_count
            FROM commissions WHERE store_id = %s
        """, (store_id,))
        summary = cur.fetchone()
        conn.close()
        return {
            "commissions": [dict(c) for c in commissions],
            "summary": {
                "gross_revenue": summary["gross"] or 0,
                "total_commission": summary["total_commission"] or 0,
                "total_payout": summary["total_payout"] or 0,
                "order_count": summary["order_count"] or 0,
                "commission_rate": "20%"
            }
        }
    except Exception as e:
        return {"error": str(e)}

# ============ PAYOUT REQUESTS ============
@app.post("/payout/request")
def request_payout(body: PayoutRequest):
    try:
        conn = get_db()
        cur = conn.cursor()
        request_id = f"PAY-{uuid.uuid4().hex[:8].upper()}"
        cur.execute("""
            INSERT INTO payout_requests
            (id, request_id, requester_type, requester_id, requester_name,
             amount, payment_method, payment_details, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')
        """, (str(uuid.uuid4()), request_id, body.requester_type,
              body.requester_id, body.requester_name, body.amount,
              body.payment_method, json.dumps(body.payment_details)))
        conn.commit()
        conn.close()
        return {"success": True, "request_id": request_id,
                "message": f"Payout request of ₹{body.amount} submitted!"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/payout/requests/{requester_id}")
def get_payout_requests(requester_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM payout_requests WHERE requester_id = %s ORDER BY created_at DESC",
                   (requester_id,))
        requests = cur.fetchall()
        conn.close()
        return {"requests": [dict(r) for r in requests]}
    except Exception as e:
        return {"error": str(e)}

# ============ ADMIN ============
@app.get("/admin/stores")
def get_all_stores_admin():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM stores ORDER BY id DESC")
        stores = cur.fetchall()
        conn.close()
        result = []
        for s in stores:
            d = dict(s)
            if 'is_approved' not in d:
                d['is_approved'] = d.get('status') == 'active'
            result.append(d)
        return {"stores": result}
    except Exception as e:
        return {"error": str(e)}

@app.get("/admin/stores/pending")
def get_pending_stores():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM stores WHERE status = 'pending' ORDER BY id DESC")
        stores = cur.fetchall()
        conn.close()
        return {"stores": [dict(s) for s in stores], "total": len(stores)}
    except Exception as e:
        return {"error": str(e)}

@app.patch("/admin/stores/{store_id}/approve")
def approve_store(store_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE stores SET status = 'active', is_open = TRUE WHERE id = %s", (store_id,))
        conn.commit()
        conn.close()
        return {"success": True, "message": "Store approved!"}
    except Exception as e:
        return {"error": str(e)}

@app.patch("/admin/stores/{store_id}/reject")
def reject_store(store_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE stores SET status = 'rejected', is_open = FALSE WHERE id = %s", (store_id,))
        conn.commit()
        conn.close()
        return {"success": True, "message": "Store rejected."}
    except Exception as e:
        return {"error": str(e)}

@app.post("/stores/{store_id}/approve")
def approve_store_post(store_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE stores SET status = 'active', is_open = TRUE WHERE id = %s", (store_id,))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}

@app.delete("/stores/{store_id}")
def delete_store(store_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM products WHERE store_id = %s", (store_id,))
        cur.execute("DELETE FROM stores WHERE id = %s", (store_id,))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}

@app.get("/admin/partners/pending")
def get_pending_partners():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM delivery_partners WHERE status = 'pending' ORDER BY id DESC")
        partners = cur.fetchall()
        conn.close()
        return {"partners": [dict(p) for p in partners], "total": len(partners)}
    except Exception as e:
        return {"error": str(e)}

@app.get("/admin/partners/all")
def get_all_partners():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM delivery_partners ORDER BY id DESC")
        partners = cur.fetchall()
        conn.close()
        return {"partners": [dict(p) for p in partners], "total": len(partners)}
    except Exception as e:
        return {"error": str(e)}

@app.patch("/admin/partners/{partner_id}/approve")
def approve_partner(partner_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE delivery_partners SET status = 'approved' WHERE id = %s", (partner_id,))
        conn.commit()
        conn.close()
        return {"success": True, "message": "Partner approved!"}
    except Exception as e:
        return {"error": str(e)}

@app.patch("/admin/partners/{partner_id}/reject")
def reject_partner(partner_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE delivery_partners SET status = 'rejected' WHERE id = %s", (partner_id,))
        conn.commit()
        conn.close()
        return {"success": True, "message": "Partner rejected."}
    except Exception as e:
        return {"error": str(e)}


@app.post("/create-payment-order")
async def create_payment_order(body: dict):
    amount = body.get("amount", 0)
    order_id = body.get("order_id", f"order_{uuid.uuid4().hex[:8]}")
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.cashfree.com/pg/orders",
                headers={
                    "x-client-id": CASHFREE_APP_ID,
                    "x-client-secret": CASHFREE_SECRET_KEY,
                    "x-api-version": "2023-08-01",
                    "Content-Type": "application/json"
                },
                json={
                    "order_id": order_id,
                    "order_amount": amount,
                    "order_currency": "INR",
                    "customer_details": {
                        "customer_id": f"cust_{order_id}",
                        "customer_phone": body.get("phone", "9999999999"),
                        "customer_email": body.get("email", "customer@getdripd.in")
                    },
                    "order_meta": {
                        "return_url": f"https://getdripd.in?order_id={order_id}"
                    }
                },
                timeout=15
            )
            d = r.json()
            print(f"Cashfree response: {d}")
            if not d.get("payment_session_id"):
                return {"success": False, "error": d.get("message", "Cashfree error"), "details": d}
            return {
                "success": True,
                "payment_session_id": d.get("payment_session_id"),
                "cf_order_id": d.get("cf_order_id"),
                "cashfree_app_id": CASHFREE_APP_ID
            }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/verify-payment")
async def verify_payment(body: dict):
    order_id = body.get("order_id")
    if not order_id:
        return {"success": False, "paid": False}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"https://api.cashfree.com/pg/orders/{order_id}/payments",
                headers={
                    "x-client-id": CASHFREE_APP_ID,
                    "x-client-secret": CASHFREE_SECRET_KEY,
                    "x-api-version": "2023-08-01"
                },
                timeout=10
            )
            d = r.json()
            print(f"Payment verify response: {d}")
            # Check if any payment is SUCCESS
            if isinstance(d, list):
                paid = any(p.get("payment_status") == "SUCCESS" for p in d)
                txn_id = next((p.get("cf_payment_id") for p in d if p.get("payment_status") == "SUCCESS"), None)
            else:
                paid = d.get("payment_status") == "SUCCESS"
                txn_id = d.get("cf_payment_id")
            return {"success": True, "paid": paid, "txn_id": txn_id}
    except Exception as e:
        return {"success": False, "paid": False, "error": str(e)}

@app.get("/admin/revenue")
def get_revenue():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) as total_orders,
                   SUM(amount) as gmv,
                   SUM(amount) * 0.20 as commission
            FROM orders
        """)
        revenue = cur.fetchone()
        cur.execute("""
            SELECT SUM(amount) as partner_earnings_total
            FROM partner_earnings
        """)
        partner = cur.fetchone()
        conn.close()
        return {
            "total_orders": revenue["total_orders"] or 0,
            "gmv": revenue["gmv"] or 0,
            "commission": revenue["commission"] or 0,
            "partner_earnings": partner["partner_earnings_total"] or 0
        }
    except Exception as e:
        return {"error": str(e)}
