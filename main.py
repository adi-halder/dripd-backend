from fastapi import FastAPI, Request
from pydantic import BaseModel
from typing import List, Optional
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, date, timedelta
import httpx
import base64
import os
import psycopg2
from psycopg2.extras import RealDictCursor
import uuid
import json
import random
import string

app = FastAPI(title="Drip'd API", version="6.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============ CONFIG ============
RAZORPAY_KEY_ID = "rzp_test_Smd5WAS1quRuYv"
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_SECRET", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
FAST2SMS_API_KEY = "ct7FUai0fNvT3hAzueIYMHQJsLkOqEb4dW89yxGnXPR5SorjVZ5ytRNE6JxYkBoO4UPAr3c8pTSGhw9b"
otp_store = {}

# ============ DATABASE ============
def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

# ============ DATA MODELS ============
class Order(BaseModel):
    customer_name: str
    customer_phone: str
    customer_address: str
    store_id: str
    product_id: str
    size: str
    total_amount: int
    is_try_and_buy: bool = False
    payment_id: Optional[str] = None
    use_drip_coins: bool = False
    promo_code: Optional[str] = None
    referral_code: Optional[str] = None

class AddProduct(BaseModel):
    store_id: str
    name: str
    price: int
    sizes: List[str]
    category: str
    photo: Optional[str] = None

class UpdatePhoto(BaseModel):
    photo: str

class RefundRequest(BaseModel):
    order_id: str
    refund_type: str

class ReturnRequest(BaseModel):
    order_id: str
    reason: str

class RatingRequest(BaseModel):
    order_id: str
    product_rating: float
    store_rating: float
    review: Optional[str] = None

class StoreRegister(BaseModel):
    name: str
    owner_name: str
    phone: str
    area: str
    categories: List[str]
    opening_time: str = "10:00"
    closing_time: str = "22:00"
    latitude: float = None
    longitude: float = None

class StoreLogin(BaseModel):
    phone: str

class TrackView(BaseModel):
    product_id: str
    store_id: str
    city: str
    customer_phone: Optional[str] = None

class DropAlertSubscribe(BaseModel):
    customer_phone: str
    store_id: Optional[str] = None
    category: Optional[str] = None
    city: str

class PersonalizationFeedback(BaseModel):
    customer_phone: str
    product_id: str
    action: str

class StoryCreate(BaseModel):
    store_id: str
    image_url: str
    caption: Optional[str] = None
    product_id: Optional[str] = None

class StoryView(BaseModel):
    story_id: str
    customer_phone: str

class OrderStatusUpdate(BaseModel):
    order_id: str
    status: str
    updated_by: str
    updated_by_id: Optional[str] = None
    note: Optional[str] = None

class ChatMessage(BaseModel):
    order_id: str
    sender: str
    sender_name: str
    message: str

class PromoValidate(BaseModel):
    code: str
    customer_phone: str
    order_amount: int

class ReferralRegister(BaseModel):
    customer_phone: str
    referral_code: Optional[str] = None

# ============ RAZORPAY ============
async def process_razorpay_refund(payment_id: str, amount: int, notes: str):
    try:
        credentials = base64.b64encode(f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}".encode()).decode()
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://api.razorpay.com/v1/payments/{payment_id}/refund",
                headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/json"},
                json={"amount": amount * 100, "notes": {"reason": notes}}
            )
            data = response.json()
            return {"success": True, "refund_id": data.get("id"), "amount": amount}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ============ HELPERS ============
def award_coins(cur, customer_phone: str, coins: int, reason: str, order_id: str = None):
    cur.execute("""
        INSERT INTO drip_coins (customer_phone, total_coins, redeemed_coins)
        VALUES (%s, %s, 0)
        ON CONFLICT (customer_phone)
        DO UPDATE SET total_coins = drip_coins.total_coins + %s, updated_at = NOW()
    """, (customer_phone, coins, coins))
    cur.execute("""
        INSERT INTO coin_transactions (id, customer_phone, order_id, transaction_type, coins, reason)
        VALUES (%s, %s, %s, 'earn', %s, %s)
    """, (f"CT{uuid.uuid4().hex[:8]}", customer_phone, order_id, coins, reason))

def redeem_coins(cur, customer_phone: str, coins: int, order_id: str):
    cur.execute("""
        UPDATE drip_coins SET redeemed_coins = redeemed_coins + %s, updated_at = NOW()
        WHERE customer_phone = %s
    """, (coins, customer_phone))
    cur.execute("""
        INSERT INTO coin_transactions (id, customer_phone, order_id, transaction_type, coins, reason)
        VALUES (%s, %s, %s, 'redeem', %s, 'free_delivery')
    """, (f"CT{uuid.uuid4().hex[:8]}", customer_phone, order_id, coins))

def update_trending_score(cur, product_id: str, store_id: str, city: str, is_order: bool = False):
    if is_order:
        cur.execute("""
            INSERT INTO trending_scores (id, product_id, store_id, city, views_24h, orders_24h, trending_score, last_updated)
            VALUES (%s, %s, %s, %s, 0, 1, 5.0, NOW())
            ON CONFLICT (product_id, city)
            DO UPDATE SET
                orders_24h = trending_scores.orders_24h + 1,
                trending_score = trending_scores.views_24h + (trending_scores.orders_24h + 1) * 5,
                last_updated = NOW()
        """, (f"TS{uuid.uuid4().hex[:8]}", product_id, store_id, city))
    else:
        cur.execute("""
            INSERT INTO trending_scores (id, product_id, store_id, city, views_24h, orders_24h, trending_score, last_updated)
            VALUES (%s, %s, %s, %s, 1, 0, 1.0, NOW())
            ON CONFLICT (product_id, city)
            DO UPDATE SET
                views_24h = trending_scores.views_24h + 1,
                trending_score = (trending_scores.views_24h + 1) + trending_scores.orders_24h * 5,
                last_updated = NOW()
        """, (f"TS{uuid.uuid4().hex[:8]}", product_id, store_id, city))

def trigger_drop_alerts(cur, store_id: str, product_name: str, product_id: str, product_image: str, category: str, city: str):
    cur.execute("""
        SELECT DISTINCT customer_phone FROM drop_alert_subscriptions
        WHERE city = %s AND is_active = TRUE
        AND (store_id = %s OR store_id IS NULL)
        AND (category = %s OR category IS NULL)
    """, (city, store_id, category))
    subscribers = cur.fetchall()
    for sub in subscribers:
        notif_id = f"DN{uuid.uuid4().hex[:8]}"
        cur.execute("""
            INSERT INTO drop_notifications (id, customer_phone, store_id, product_id, product_name, product_image, message, is_read)
            VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE)
        """, (notif_id, sub["customer_phone"], store_id, product_id, product_name, product_image,
              f"New drop! {product_name} is now available 🔥"))

def update_social_proof_order(cur, product_id: str, store_id: str):
    cur.execute("""
        INSERT INTO social_proof (product_id, store_id, orders_today, views_today, last_reset)
        VALUES (%s, %s, 1, 0, CURRENT_DATE)
        ON CONFLICT (product_id)
        DO UPDATE SET
            orders_today = CASE
                WHEN social_proof.last_reset < CURRENT_DATE THEN 1
                ELSE social_proof.orders_today + 1
            END,
            last_reset = CURRENT_DATE,
            updated_at = NOW()
    """, (product_id, store_id))

def create_invoice(cur, order: Order, order_id: str, store_name: str, product_name: str, product_price: int):
    invoice_number = f"INV-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    try_fee = 50 if order.is_try_and_buy else 0
    cur.execute("""
        INSERT INTO invoices (id, invoice_number, order_id, customer_name, customer_phone,
            customer_address, store_name, store_id, product_name, product_price, size,
            delivery_fee, platform_fee, try_and_buy_fee, total_amount, payment_id, is_try_and_buy)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        f"INV{uuid.uuid4().hex[:8]}", invoice_number, order_id,
        order.customer_name, order.customer_phone, order.customer_address,
        store_name, order.store_id, product_name, product_price, order.size,
        30, 5, try_fee, order.total_amount, order.payment_id, order.is_try_and_buy
    ))
    return invoice_number

def create_commission(cur, order_id: str, store_id: str, order_amount: int):
    commission_rate = 0.09
    commission_amount = int(order_amount * commission_rate)
    store_payout = order_amount - commission_amount
    cur.execute("""
        INSERT INTO commissions (id, order_id, store_id, order_amount, commission_rate,
            commission_amount, store_payout, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
        ON CONFLICT (order_id) DO NOTHING
    """, (f"COM{uuid.uuid4().hex[:8]}", order_id, store_id, order_amount,
          commission_rate, commission_amount, store_payout))
    return commission_amount, store_payout

def generate_referral_code(phone: str) -> str:
    suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"DRIP{suffix}"

def update_city_stats(cur, city: str, increment_orders: bool = False):
    cur.execute("""
        INSERT INTO cities (name, state, is_active, store_count, order_count)
        VALUES (%s, 'India', TRUE, 0, %s)
        ON CONFLICT (name)
        DO UPDATE SET
            order_count = CASE WHEN %s THEN cities.order_count + 1 ELSE cities.order_count END,
            is_active = TRUE
    """, (city, 1 if increment_orders else 0, increment_orders))

# ============ ROUTES ============
@app.get("/")
def home():
    return {"app": "Drip'd", "version": "6.0", "message": "Fashion delivered in 60 minutes!"}

# ============================================
# OTP ROUTES — FAST2SMS
# ============================================

@app.post("/otp/send")
async def send_otp(request: Request):
    data = await request.json()
    phone = data.get("phone", "")
    if not phone:
        return {"success": False, "error": "Phone number required"}
    otp = str(random.randint(100000, 999999))
    otp_store[phone] = {
        "otp": otp,
        "expires": datetime.now() + timedelta(minutes=10),
        "attempts": 0
    }
    clean = phone.replace("+91", "").replace(" ", "").strip()
    if clean.startswith("91") and len(clean) == 12:
        clean = clean[2:]
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://www.fast2sms.com/dev/bulkV2",
                params={
                    "authorization": FAST2SMS_API_KEY,
                    "variables_values": otp,
                    "route": "otp",
                    "numbers": clean
                },
                timeout=10
            )
            res = r.json()
            if res.get("return") == True:
                return {"success": True, "message": "OTP sent to your phone"}
            else:
                return {"success": True, "message": "OTP sent", "debug_otp": otp}
    except Exception:
        return {"success": True, "message": "OTP sent", "debug_otp": otp}

@app.post("/otp/verify")
async def verify_otp(request: Request):
    data = await request.json()
    phone = data.get("phone", "")
    otp = data.get("otp", "")
    stored = otp_store.get(phone)
    if not stored:
        return {"success": False, "error": "OTP expired. Request a new one."}
    if datetime.now() > stored["expires"]:
        del otp_store[phone]
        return {"success": False, "error": "OTP expired. Request a new one."}
    stored["attempts"] += 1
    if stored["attempts"] > 5:
        del otp_store[phone]
        return {"success": False, "error": "Too many attempts. Request a new OTP."}
    if stored["otp"] != otp:
        return {"success": False, "error": "Incorrect OTP. Try again."}
    del otp_store[phone]
    return {"success": True, "message": "Phone verified!"}

# ============ STORE REGISTRATION ============
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
            INSERT INTO stores (id, name, owner_name, phone, area, categories, opening_time, closing_time, is_open, rating, distance_km, delivery_time, total_ratings, rating_sum, status, latitude, longitude)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (store_id, store.name, store.owner_name, store.phone, store.area, store.categories,
              store.opening_time, store.closing_time, False, 0.0, 1.0, "25-45 mins", 0, 0.0, "pending",
              store.latitude, store.longitude))
        update_city_stats(cur, store.area)
        conn.commit()
        conn.close()
        return {"success": True, "store_id": store_id, "message": f"Welcome to Drip'd, {store.name}! 🎉"}
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
            return {"error": "Your store is pending approval. We'll notify you within 24 hours!", "pending": True}
        if store["status"] == "rejected":
            return {"error": "Your application was not approved. Contact getdripd1@gmail.com", "rejected": True}
        return {"success": True, "store": dict(store)}
    except Exception as e:
        return {"error": str(e)}

# ============ STORES ============
@app.get("/stores")
def get_stores(lat: float = None, lng: float = None, radius_km: float = 7.0):
    try:
        import math
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM stores WHERE status = 'active' ORDER BY rating DESC")
        stores = cur.fetchall()
        conn.close()
        result = []
        for s in stores:
            store = dict(s)
            # If customer sent coordinates AND store has coordinates, filter by distance
            if lat is not None and lng is not None and store.get("latitude") and store.get("longitude"):
                slat, slng = float(store["latitude"]), float(store["longitude"])
                # Haversine formula
                R = 6371
                dlat = math.radians(slat - lat)
                dlng = math.radians(slng - lng)
                a = math.sin(dlat/2)**2 + math.cos(math.radians(lat)) * math.cos(math.radians(slat)) * math.sin(dlng/2)**2
                dist = R * 2 * math.asin(math.sqrt(a))
                if dist > radius_km:
                    continue  # too far, skip
                store["distance_km"] = round(dist, 1)
                # Estimate delivery time (avg 20 km/h in city traffic)
                mins = int((dist / 20) * 60) + 10  # +10 for prep
                store["delivery_time"] = f"~{mins} min"
            else:
                store["delivery_time"] = store.get("delivery_time") or "~30 min"
            result.append(store)
        return {"stores": result}
    except Exception as e:
        return {"error": str(e)}

@app.get("/stores/category/{category}")
def get_stores_by_category(category: str, lat: float = None, lng: float = None, radius_km: float = 7.0):
    try:
        import math
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM stores WHERE %s = ANY(categories) AND status = 'active'", (category,))
        stores = cur.fetchall()
        conn.close()
        result = []
        for s in stores:
            store = dict(s)
            if lat is not None and lng is not None and store.get("latitude") and store.get("longitude"):
                slat, slng = float(store["latitude"]), float(store["longitude"])
                R = 6371
                dlat = math.radians(slat - lat)
                dlng = math.radians(slng - lng)
                a = math.sin(dlat/2)**2 + math.cos(math.radians(lat)) * math.cos(math.radians(slat)) * math.sin(dlng/2)**2
                dist = R * 2 * math.asin(math.sqrt(a))
                if dist > radius_km:
                    continue
                store["distance_km"] = round(dist, 1)
                mins = int((dist / 20) * 60) + 10
                store["delivery_time"] = f"~{mins} min"
            else:
                store["delivery_time"] = store.get("delivery_time") or "~30 min"
            result.append(store)
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
def get_products(store_id: str):
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
        if not lat or not lng:
            return {"error": "latitude and longitude required"}
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE stores SET latitude = %s, longitude = %s WHERE id = %s", (lat, lng, store_id))
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
            INSERT INTO products (id, store_id, name, price, sizes, category, available, photo, rating, total_ratings, rating_sum)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (product_id, product.store_id, product.name, product.price,
              product.sizes, product.category, True, product.photo, 0.0, 0, 0.0))
        cur.execute("SELECT area FROM stores WHERE id = %s", (product.store_id,))
        store = cur.fetchone()
        city = store["area"] if store else "unknown"
        trigger_drop_alerts(cur, product.store_id, product.name, product_id, product.photo or "", product.category, city)
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

        delivery_free = False
        if order.use_drip_coins:
            cur.execute("SELECT available_coins FROM drip_coins WHERE customer_phone = %s", (order.customer_phone,))
            coins_row = cur.fetchone()
            if coins_row and coins_row["available_coins"] >= 100:
                delivery_free = True

        promo_discount = 0
        promo_applied = None
        if order.promo_code:
            cur.execute("""
                SELECT * FROM promo_codes
                WHERE code = %s AND is_active = TRUE
                AND (valid_until IS NULL OR valid_until > NOW())
                AND used_count < max_uses
            """, (order.promo_code.upper(),))
            promo = cur.fetchone()
            if promo:
                cur.execute("SELECT id FROM promo_usage WHERE code = %s AND customer_phone = %s",
                           (order.promo_code.upper(), order.customer_phone))
                already_used = cur.fetchone()
                if not already_used:
                    if promo["discount_type"] == "flat":
                        promo_discount = promo["discount_value"]
                    elif promo["discount_type"] == "percent":
                        promo_discount = int(order.total_amount * promo["discount_value"] / 100)
                    elif promo["discount_type"] == "free_delivery":
                        promo_discount = 30
                        delivery_free = True
                    promo_applied = promo["code"]

        order_id = f"DR{uuid.uuid4().hex[:6].upper()}"
        final_amount = max(0, order.total_amount - promo_discount)

        cur.execute("""
            INSERT INTO orders (order_id, customer, phone, address, store_id, store, product_id, product,
                product_price, size, amount, status, is_try_and_buy, payment_id, try_status,
                return_status, is_rated, estimated_delivery, current_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            order_id, order.customer_name, order.customer_phone, order.customer_address,
            order.store_id, store["name"], order.product_id, product["name"],
            product["price"], order.size, final_amount, "confirmed",
            order.is_try_and_buy, order.payment_id,
            "pending" if order.is_try_and_buy else None,
            None, False, store["delivery_time"], "confirmed"
        ))

        cur.execute("""
            INSERT INTO order_status_history (id, order_id, status, updated_by, note)
            VALUES (%s, %s, 'confirmed', 'system', 'Order placed successfully')
        """, (f"SH{uuid.uuid4().hex[:8]}", order_id))

        if promo_applied:
            cur.execute("""
                INSERT INTO promo_usage (id, code, customer_phone, order_id, discount_applied)
                VALUES (%s, %s, %s, %s, %s)
            """, (f"PU{uuid.uuid4().hex[:8]}", promo_applied, order.customer_phone, order_id, promo_discount))
            cur.execute("UPDATE promo_codes SET used_count = used_count + 1 WHERE code = %s", (promo_applied,))

        coins_earned = 10
        if order.is_try_and_buy:
            coins_earned += 5
        award_coins(cur, order.customer_phone, coins_earned, "order", order_id)
        if delivery_free and order.use_drip_coins:
            redeem_coins(cur, order.customer_phone, 100, order_id)

        if order.referral_code:
            cur.execute("""
                UPDATE referrals SET status = 'completed', completed_at = NOW()
                WHERE referred_phone = %s AND status = 'pending'
            """, (order.customer_phone,))
            cur.execute("""
                SELECT referrer_phone FROM referrals
                WHERE referred_phone = %s AND coins_awarded = FALSE
            """, (order.customer_phone,))
            ref = cur.fetchone()
            if ref:
                award_coins(cur, ref["referrer_phone"], 50, "referral_bonus", order_id)
                award_coins(cur, order.customer_phone, 25, "referral_welcome", order_id)
                cur.execute("""
                    UPDATE referrals SET coins_awarded = TRUE WHERE referred_phone = %s
                """, (order.customer_phone,))
                cur.execute("""
                    UPDATE referral_codes SET total_referrals = total_referrals + 1
                    WHERE code = %s
                """, (order.referral_code,))

        city = store["area"]
        update_trending_score(cur, order.product_id, order.store_id, city, is_order=True)
        update_social_proof_order(cur, order.product_id, order.store_id)
        update_city_stats(cur, city, increment_orders=True)

        cur.execute("""
            INSERT INTO customer_style_profile (customer_phone, preferred_categories, preferred_brands)
            VALUES (%s, %s::jsonb, %s::jsonb)
            ON CONFLICT (customer_phone)
            DO UPDATE SET
                preferred_categories = (
                    SELECT jsonb_agg(DISTINCT val)
                    FROM jsonb_array_elements_text(
                        customer_style_profile.preferred_categories || %s::jsonb
                    ) val
                ),
                preferred_brands = (
                    SELECT jsonb_agg(DISTINCT val)
                    FROM jsonb_array_elements_text(
                        customer_style_profile.preferred_brands || %s::jsonb
                    ) val
                ),
                last_updated = NOW()
        """, (order.customer_phone, json.dumps([product["category"]]), json.dumps([store["name"]]),
              json.dumps([product["category"]]), json.dumps([store["name"]])))

        cur.execute("""
            INSERT INTO size_history (id, customer_phone, product_id, order_id, category, size_ordered)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (f"SH{uuid.uuid4().hex[:8]}", order.customer_phone, order.product_id,
              order_id, product["category"], order.size))

        cur.execute("""
            INSERT INTO personalization_feedback (id, customer_phone, product_id, action)
            VALUES (%s, %s, %s, 'ordered')
        """, (f"PF{uuid.uuid4().hex[:8]}", order.customer_phone, order.product_id))

        invoice_number = create_invoice(cur, order, order_id, store["name"], product["name"], product["price"])
        commission_amount, store_payout = create_commission(cur, order_id, order.store_id, final_amount)

        taxable = int(final_amount / 1.18)
        tax = final_amount - taxable
        gst_invoice_number = f"GST-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        cur.execute("""
            INSERT INTO gst_invoices (id, invoice_number, order_id, store_id, customer_name,
                customer_phone, store_name, taxable_amount, cgst_amount, sgst_amount,
                total_tax, total_amount)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (order_id) DO NOTHING
        """, (f"GST{uuid.uuid4().hex[:8]}", gst_invoice_number, order_id, order.store_id,
              order.customer_name, order.customer_phone, store["name"],
              taxable, tax//2, tax//2, tax, final_amount))

        conn.commit()
        cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
        new_order = cur.fetchone()
        conn.close()

        return {
            "success": True,
            "order": dict(new_order),
            "coins_earned": coins_earned,
            "delivery_free": delivery_free,
            "invoice_number": invoice_number,
            "promo_discount": promo_discount,
            "promo_applied": promo_applied,
            "commission_amount": commission_amount,
            "store_payout": store_payout,
            "message": f"🎉 You earned {coins_earned} Drip Coins!"
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/orders")
def get_orders():
    try:
        conn = get_db()
        cur = conn.cursor()
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
        conn.close()
        if not order:
            return {"error": "Order not found"}
        return {"order": dict(order)}
    except Exception as e:
        return {"error": str(e)}

# ============ REFUND ============
@app.post("/orders/{order_id}/refund")
async def process_refund(order_id: str, body: RefundRequest):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
        order = cur.fetchone()
        if not order:
            conn.close()
            return {"error": "Order not found"}
        if not order["is_try_and_buy"]:
            conn.close()
            return {"error": "Not a Try & Buy order"}
        if not order["payment_id"]:
            conn.close()
            return {"error": "No payment ID found"}

        product_price = order["product_price"]
        if body.refund_type == "keep":
            refund_amount = 50
            notes = "Try & Buy fee refund"
            cur.execute("UPDATE orders SET try_status = 'kept' WHERE order_id = %s", (order_id,))
            cur.execute("UPDATE size_history SET kept_item = TRUE WHERE order_id = %s", (order_id,))
        elif body.refund_type == "return":
            refund_amount = product_price
            notes = "Try & Buy return"
            cur.execute("UPDATE orders SET try_status = 'returned' WHERE order_id = %s", (order_id,))
            cur.execute("UPDATE size_history SET kept_item = FALSE WHERE order_id = %s", (order_id,))
        else:
            conn.close()
            return {"error": "Invalid refund type"}

        conn.commit()
        conn.close()
        result = await process_razorpay_refund(order["payment_id"], refund_amount, notes)
        if result["success"]:
            return {"success": True, "refund_amount": refund_amount, "message": f"₹{refund_amount} refund initiated!"}
        return {"error": "Refund failed", "details": result.get("error")}
    except Exception as e:
        return {"error": str(e)}

# ============ RETURN ============
@app.post("/orders/{order_id}/return")
async def request_return(order_id: str, body: ReturnRequest):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
        order = cur.fetchone()
        if not order:
            conn.close()
            return {"error": "Order not found"}
        if order["is_try_and_buy"]:
            conn.close()
            return {"error": "Use Try & Buy return flow"}
        if order["return_status"]:
            conn.close()
            return {"error": "Return already requested"}
        if not order["payment_id"]:
            conn.close()
            return {"error": "No payment ID found"}

        order_time = order["timestamp"]
        hours_since = (datetime.now() - order_time.replace(tzinfo=None)).total_seconds() / 3600
        if hours_since > 24:
            conn.close()
            return {"error": "Return window expired. Returns only within 24 hours."}

        product_price = order["product_price"]
        result = await process_razorpay_refund(order["payment_id"], product_price, f"Return: {body.reason}")
        if result["success"]:
            cur.execute("UPDATE orders SET return_status = 'requested', return_reason = %s WHERE order_id = %s",
                       (body.reason, order_id))
            conn.commit()
            conn.close()
            return {"success": True, "refund_amount": product_price,
                    "message": f"₹{product_price} refund initiated!", "pickup_in": "30 minutes"}
        conn.close()
        return {"error": "Refund failed", "details": result.get("error")}
    except Exception as e:
        return {"error": str(e)}

# ============ RATINGS ============
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
            new_rating = round(new_sum / new_total, 1)
            cur.execute("UPDATE products SET rating_sum = %s, total_ratings = %s, rating = %s WHERE id = %s",
                       (new_sum, new_total, new_rating, order["product_id"]))

        cur.execute("SELECT rating_sum, total_ratings FROM stores WHERE id = %s", (order["store_id"],))
        store = cur.fetchone()
        if store:
            new_sum = store["rating_sum"] + body.store_rating
            new_total = store["total_ratings"] + 1
            new_rating = round(new_sum / new_total, 1)
            cur.execute("UPDATE stores SET rating_sum = %s, total_ratings = %s, rating = %s WHERE id = %s",
                       (new_sum, new_total, new_rating, order["store_id"]))

        rating_id = f"R{uuid.uuid4().hex[:8]}"
        cur.execute("""
            INSERT INTO ratings (rating_id, order_id, product_id, store_id, product_rating, store_rating, review, customer)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (rating_id, order_id, order["product_id"], order["store_id"],
              body.product_rating, body.store_rating, body.review, order["customer"]))

        cur.execute("UPDATE orders SET is_rated = TRUE WHERE order_id = %s", (order_id,))

        bonus_coins = 0
        if body.product_rating == 5:
            bonus_coins = 5
            award_coins(cur, order["phone"], bonus_coins, "review_bonus", order_id)

        if body.review:
            review_lower = body.review.lower()
            fit_feedback = None
            if any(w in review_lower for w in ["perfect", "great fit", "fits well"]):
                fit_feedback = "perfect"
            elif any(w in review_lower for w in ["small", "tight", "too small"]):
                fit_feedback = "too_small"
            elif any(w in review_lower for w in ["large", "loose", "too big", "too large"]):
                fit_feedback = "too_large"
            if fit_feedback:
                cur.execute("UPDATE size_history SET fit_feedback = %s WHERE order_id = %s",
                           (fit_feedback, order_id))

        conn.commit()
        conn.close()
        msg = "Thank you for your review! 🌟"
        if bonus_coins > 0:
            msg += f" +{bonus_coins} Drip Coins for the 5-star rating! 🪙"
        return {"success": True, "message": msg, "bonus_coins": bonus_coins}
    except Exception as e:
        return {"error": str(e)}

# ============================================
# PHASE 2 ROUTES
# ============================================

@app.get("/coins/{customer_phone}")
def get_coins(customer_phone: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM drip_coins WHERE customer_phone = %s", (customer_phone,))
        coins = cur.fetchone()
        conn.close()
        if not coins:
            return {"customer_phone": customer_phone, "total_coins": 0, "redeemed_coins": 0, "available_coins": 0}
        return dict(coins)
    except Exception as e:
        return {"error": str(e)}

@app.get("/coins/{customer_phone}/history")
def get_coin_history(customer_phone: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM coin_transactions WHERE customer_phone = %s
            ORDER BY created_at DESC LIMIT 50
        """, (customer_phone,))
        transactions = cur.fetchall()
        conn.close()
        return {"transactions": [dict(t) for t in transactions]}
    except Exception as e:
        return {"error": str(e)}

@app.post("/products/view")
def track_product_view(body: TrackView):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO product_views (id, product_id, store_id, city, customer_phone)
            VALUES (%s, %s, %s, %s, %s)
        """, (f"PV{uuid.uuid4().hex[:8]}", body.product_id, body.store_id, body.city, body.customer_phone))
        update_trending_score(cur, body.product_id, body.store_id, body.city, is_order=False)
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}

@app.get("/trending/{city}")
def get_trending(city: str, limit: int = 10):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT ts.product_id, ts.store_id, ts.views_24h, ts.orders_24h, ts.trending_score,
                   p.name, p.price, p.photo, p.category, p.rating, p.sizes, s.name as store_name
            FROM trending_scores ts
            JOIN products p ON ts.product_id = p.id
            JOIN stores s ON ts.store_id = s.id
            WHERE ts.city = %s AND p.available = TRUE
            ORDER BY ts.trending_score DESC LIMIT %s
        """, (city, limit))
        trending = cur.fetchall()
        conn.close()
        return {"trending": [dict(t) for t in trending], "city": city}
    except Exception as e:
        return {"error": str(e)}

@app.post("/alerts/subscribe")
def subscribe_drop_alert(body: DropAlertSubscribe):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO drop_alert_subscriptions (id, customer_phone, store_id, category, city, is_active)
            VALUES (%s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (customer_phone, store_id, category, city) DO UPDATE SET is_active = TRUE
        """, (f"DA{uuid.uuid4().hex[:8]}", body.customer_phone, body.store_id, body.category, body.city))
        conn.commit()
        conn.close()
        return {"success": True, "message": "You'll be notified of new drops! 🔔"}
    except Exception as e:
        return {"error": str(e)}

@app.delete("/alerts/unsubscribe")
def unsubscribe_drop_alert(body: DropAlertSubscribe):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            UPDATE drop_alert_subscriptions SET is_active = FALSE
            WHERE customer_phone = %s AND city = %s AND (store_id = %s OR store_id IS NULL)
        """, (body.customer_phone, body.city, body.store_id))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}

@app.get("/alerts/{customer_phone}")
def get_notifications(customer_phone: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM drop_notifications WHERE customer_phone = %s
            ORDER BY created_at DESC LIMIT 30
        """, (customer_phone,))
        notifications = cur.fetchall()
        cur.execute("""
            SELECT COUNT(*) as unread FROM drop_notifications
            WHERE customer_phone = %s AND is_read = FALSE
        """, (customer_phone,))
        unread = cur.fetchone()
        conn.close()
        return {"notifications": [dict(n) for n in notifications],
                "unread_count": unread["unread"] if unread else 0}
    except Exception as e:
        return {"error": str(e)}

@app.patch("/alerts/{customer_phone}/read")
def mark_notifications_read(customer_phone: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE drop_notifications SET is_read = TRUE WHERE customer_phone = %s", (customer_phone,))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}

@app.get("/foryou/{customer_phone}")
def get_for_you(customer_phone: str, city: str = "Gurgaon"):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM customer_style_profile WHERE customer_phone = %s", (customer_phone,))
        profile = cur.fetchone()
        if not profile:
            cur.execute("""
                SELECT p.*, s.name as store_name FROM products p
                JOIN stores s ON p.store_id = s.id
                WHERE p.available = TRUE ORDER BY p.rating DESC LIMIT 10
            """)
            products = cur.fetchall()
            conn.close()
            return {"products": [dict(p) for p in products], "reason": "trending", "is_new_user": True}

        preferred_categories = profile["preferred_categories"] or []
        if preferred_categories:
            cur.execute("""
                SELECT p.*, s.name as store_name FROM products p
                JOIN stores s ON p.store_id = s.id
                WHERE p.available = TRUE AND p.category = ANY(%s)
                ORDER BY p.rating DESC LIMIT 15
            """, (preferred_categories,))
        else:
            cur.execute("""
                SELECT p.*, s.name as store_name FROM products p
                JOIN stores s ON p.store_id = s.id
                WHERE p.available = TRUE ORDER BY p.rating DESC LIMIT 15
            """)
        products = cur.fetchall()
        conn.close()
        return {"products": [dict(p) for p in products], "reason": "personalized",
                "based_on": {"categories": preferred_categories, "brands": profile["preferred_brands"] or []}}
    except Exception as e:
        return {"error": str(e)}

@app.post("/foryou/feedback")
def personalization_feedback(body: PersonalizationFeedback):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO personalization_feedback (id, customer_phone, product_id, action)
            VALUES (%s, %s, %s, %s)
        """, (f"PF{uuid.uuid4().hex[:8]}", body.customer_phone, body.product_id, body.action))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}

@app.get("/profile/{customer_phone}")
def get_style_profile(customer_phone: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM customer_style_profile WHERE customer_phone = %s", (customer_phone,))
        profile = cur.fetchone()
        conn.close()
        if not profile:
            return {"exists": False, "customer_phone": customer_phone}
        return {"exists": True, "profile": dict(profile)}
    except Exception as e:
        return {"error": str(e)}

@app.get("/size/{customer_phone}/{category}")
def predict_size(customer_phone: str, category: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT size_ordered, fit_feedback, kept_item FROM size_history
            WHERE customer_phone = %s AND category = %s
            ORDER BY created_at DESC LIMIT 10
        """, (customer_phone, category))
        history = cur.fetchall()
        conn.close()
        if not history:
            return {"prediction": None, "confidence": "none", "message": "No size history yet"}

        from collections import Counter
        kept_sizes = [h["size_ordered"] for h in history if h.get("kept_item") == True]
        perfect_fit_sizes = [h["size_ordered"] for h in history if h.get("fit_feedback") == "perfect"]
        all_sizes = [h["size_ordered"] for h in history]

        if kept_sizes:
            predicted = Counter(kept_sizes).most_common(1)[0][0]; confidence = "high"
        elif perfect_fit_sizes:
            predicted = Counter(perfect_fit_sizes).most_common(1)[0][0]; confidence = "high"
        elif all_sizes:
            predicted = Counter(all_sizes).most_common(1)[0][0]; confidence = "medium"
        else:
            return {"prediction": None, "confidence": "none"}

        return {"prediction": predicted, "confidence": confidence, "based_on": len(history),
                "message": f"Based on your last {len(history)} orders, we think you're a {predicted} in {category} 👌"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/size/{customer_phone}/history/all")
def get_size_history(customer_phone: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT category, size_ordered, fit_feedback, kept_item, created_at
            FROM size_history WHERE customer_phone = %s ORDER BY created_at DESC
        """, (customer_phone,))
        history = cur.fetchall()
        conn.close()
        return {"history": [dict(h) for h in history]}
    except Exception as e:
        return {"error": str(e)}

# ============================================
# PHASE 3 ROUTES
# ============================================

@app.post("/stories")
def create_story(body: StoryCreate):
    try:
        conn = get_db()
        cur = conn.cursor()
        story_id = f"ST{uuid.uuid4().hex[:8]}"
        cur.execute("""
            INSERT INTO store_stories (id, store_id, image_url, caption, product_id)
            VALUES (%s, %s, %s, %s, %s)
        """, (story_id, body.store_id, body.image_url, body.caption, body.product_id))
        conn.commit()
        conn.close()
        return {"success": True, "story_id": story_id, "message": "Story posted! 🔥 Expires in 24 hours."}
    except Exception as e:
        return {"error": str(e)}

@app.get("/stories")
def get_all_stories():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT ss.*, s.name as store_name, s.area as store_area
            FROM store_stories ss JOIN stores s ON ss.store_id = s.id
            WHERE ss.expires_at > NOW() ORDER BY ss.created_at DESC
        """)
        stories = cur.fetchall()
        conn.close()
        grouped = {}
        for story in stories:
            sid = story["store_id"]
            if sid not in grouped:
                grouped[sid] = {"store_id": sid, "store_name": story["store_name"],
                                "store_area": story["store_area"], "stories": []}
            grouped[sid]["stories"].append(dict(story))
        return {"stores_with_stories": list(grouped.values())}
    except Exception as e:
        return {"error": str(e)}

@app.get("/stories/store/{store_id}")
def get_store_stories(store_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM store_stories WHERE store_id = %s AND expires_at > NOW()
            ORDER BY created_at DESC
        """, (store_id,))
        stories = cur.fetchall()
        conn.close()
        return {"stories": [dict(s) for s in stories]}
    except Exception as e:
        return {"error": str(e)}

@app.post("/stories/{story_id}/view")
def view_story(story_id: str, body: StoryView):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO story_views (id, story_id, customer_phone)
            VALUES (%s, %s, %s) ON CONFLICT (story_id, customer_phone) DO NOTHING
        """, (f"SV{uuid.uuid4().hex[:8]}", story_id, body.customer_phone))
        cur.execute("UPDATE store_stories SET views = views + 1 WHERE id = %s", (story_id,))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}

@app.delete("/stories/{story_id}")
def delete_story(story_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM store_stories WHERE id = %s", (story_id,))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}

@app.post("/social/view/{product_id}")
def track_social_view(product_id: str, store_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO social_proof (product_id, store_id, views_today, viewers_now, last_reset)
            VALUES (%s, %s, 1, 1, CURRENT_DATE)
            ON CONFLICT (product_id)
            DO UPDATE SET
                views_today = CASE WHEN social_proof.last_reset < CURRENT_DATE THEN 1 ELSE social_proof.views_today + 1 END,
                viewers_now = CASE WHEN social_proof.last_reset < CURRENT_DATE THEN 1 ELSE social_proof.viewers_now + 1 END,
                last_reset = CURRENT_DATE, updated_at = NOW()
        """, (product_id, store_id))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}

@app.get("/social/product/{product_id}")
def get_social_proof(product_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM social_proof WHERE product_id = %s AND last_reset = CURRENT_DATE", (product_id,))
        proof = cur.fetchone()
        conn.close()
        if not proof:
            return {"orders_today": 0, "views_today": 0, "viewers_now": 0, "message": None}
        orders = proof["orders_today"]
        views = proof["views_today"]
        message = None
        if orders >= 5: message = f"🔥 {orders} people ordered this today!"
        elif orders >= 2: message = f"✨ {orders} people ordered this today"
        elif views >= 10: message = f"👀 {views} people viewed this today"
        return {"orders_today": orders, "views_today": views,
                "viewers_now": proof["viewers_now"], "message": message}
    except Exception as e:
        return {"error": str(e)}

@app.get("/social/store/{store_id}")
def get_store_social_proof(store_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT SUM(orders_today) as total_orders, SUM(views_today) as total_views
            FROM social_proof WHERE store_id = %s AND last_reset = CURRENT_DATE
        """, (store_id,))
        result = cur.fetchone()
        conn.close()
        total_orders = result["total_orders"] or 0
        total_views = result["total_views"] or 0
        message = None
        if total_orders >= 10: message = f"🔥 {total_orders} orders from this store today!"
        elif total_orders >= 3: message = f"✨ {total_orders} people ordered from here today"
        elif total_views >= 20: message = f"👀 Popular store today!"
        return {"total_orders_today": total_orders, "total_views_today": total_views, "message": message}
    except Exception as e:
        return {"error": str(e)}

# ============================================
# PHASE 4 ROUTES
# ============================================

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

        timestamp_field = {"packed": "packed_at", "out_for_delivery": "out_for_delivery_at",
                          "delivered": "delivered_at"}.get(body.status)

        if timestamp_field:
            cur.execute(f"UPDATE orders SET current_status = %s, {timestamp_field} = NOW() WHERE order_id = %s",
                       (body.status, order_id))
        else:
            cur.execute("UPDATE orders SET current_status = %s WHERE order_id = %s", (body.status, order_id))

        cur.execute("""
            INSERT INTO order_status_history (id, order_id, status, updated_by, updated_by_id, note)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (f"SH{uuid.uuid4().hex[:8]}", order_id, body.status,
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
        cur.execute("SELECT * FROM order_status_history WHERE order_id = %s ORDER BY created_at ASC", (order_id,))
        history = cur.fetchall()
        conn.close()
        current = order["current_status"] or "confirmed"
        stages = [
            {"status": "confirmed", "label": "Order confirmed", "emoji": "✅", "eta": "Just now", "done": True},
            {"status": "packed", "label": "Store packing your order", "emoji": "📦", "eta": "~10 mins",
             "done": current in ["packed", "out_for_delivery", "delivered"],
             "time": str(order["packed_at"]) if order.get("packed_at") else None},
            {"status": "out_for_delivery", "label": "Out for delivery", "emoji": "🛵", "eta": "~20 mins",
             "done": current in ["out_for_delivery", "delivered"],
             "time": str(order["out_for_delivery_at"]) if order.get("out_for_delivery_at") else None},
            {"status": "delivered", "label": "Delivered to your door", "emoji": "🏠", "eta": "~35 mins",
             "done": current == "delivered",
             "time": str(order["delivered_at"]) if order.get("delivered_at") else None},
        ]
        return {"order_id": order_id, "current_status": current, "stages": stages,
                "history": [dict(h) for h in history], "delivery_partner": order.get("delivery_partner_name")}
    except Exception as e:
        return {"error": str(e)}

@app.post("/chat")
def send_chat_message(body: ChatMessage):
    try:
        conn = get_db()
        cur = conn.cursor()
        msg_id = f"MSG{uuid.uuid4().hex[:8]}"
        cur.execute("""
            INSERT INTO chat_messages (id, order_id, sender, sender_name, message)
            VALUES (%s, %s, %s, %s, %s)
        """, (msg_id, body.order_id, body.sender, body.sender_name, body.message))
        conn.commit()
        conn.close()
        return {"success": True, "message_id": msg_id}
    except Exception as e:
        return {"error": str(e)}

@app.get("/chat/{order_id}")
def get_chat_messages(order_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM chat_messages WHERE order_id = %s ORDER BY created_at ASC", (order_id,))
        messages = cur.fetchall()
        conn.close()
        return {"messages": [dict(m) for m in messages]}
    except Exception as e:
        return {"error": str(e)}

@app.patch("/chat/{order_id}/read")
def mark_chat_read(order_id: str, sender: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        other_sender = "store" if sender == "customer" else "customer"
        cur.execute("""
            UPDATE chat_messages SET is_read = TRUE
            WHERE order_id = %s AND sender = %s AND is_read = FALSE
        """, (order_id, other_sender))
        conn.commit()
        conn.close()
        return {"success": True}
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

@app.get("/invoices/customer/{customer_phone}")
def get_customer_invoices(customer_phone: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM invoices WHERE customer_phone = %s ORDER BY created_at DESC", (customer_phone,))
        invoices = cur.fetchall()
        conn.close()
        return {"invoices": [dict(i) for i in invoices]}
    except Exception as e:
        return {"error": str(e)}

@app.get("/invoices/store/{store_id}")
def get_store_invoices(store_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM invoices WHERE store_id = %s ORDER BY created_at DESC", (store_id,))
        invoices = cur.fetchall()
        conn.close()
        return {"invoices": [dict(i) for i in invoices]}
    except Exception as e:
        return {"error": str(e)}

# ============================================
# PHASE 5 ROUTES
# ============================================

@app.post("/referral/register")
def register_referral(body: ReferralRegister):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT code FROM referral_codes WHERE customer_phone = %s", (body.customer_phone,))
        existing = cur.fetchone()
        if not existing:
            code = generate_referral_code(body.customer_phone)
            cur.execute("""
                INSERT INTO referral_codes (id, customer_phone, code)
                VALUES (%s, %s, %s) ON CONFLICT (customer_phone) DO NOTHING
            """, (f"RC{uuid.uuid4().hex[:8]}", body.customer_phone, code))
        else:
            code = existing["code"]

        if body.referral_code:
            cur.execute("SELECT customer_phone FROM referral_codes WHERE code = %s", (body.referral_code.upper(),))
            referrer = cur.fetchone()
            if referrer and referrer["customer_phone"] != body.customer_phone:
                cur.execute("""
                    INSERT INTO referrals (id, referrer_phone, referred_phone, referral_code)
                    VALUES (%s, %s, %s, %s) ON CONFLICT (referred_phone) DO NOTHING
                """, (f"REF{uuid.uuid4().hex[:8]}", referrer["customer_phone"],
                      body.customer_phone, body.referral_code.upper()))

        conn.commit()
        conn.close()
        return {"success": True, "your_referral_code": code,
                "share_message": f"Use my code {code} on Drip'd and get ₹25 off your first order! 🎉"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/referral/{customer_phone}")
def get_referral_info(customer_phone: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM referral_codes WHERE customer_phone = %s", (customer_phone,))
        code_row = cur.fetchone()

        if not code_row:
            code = generate_referral_code(customer_phone)
            cur.execute("""
                INSERT INTO referral_codes (id, customer_phone, code)
                VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
            """, (f"RC{uuid.uuid4().hex[:8]}", customer_phone, code))
            conn.commit()
        else:
            code = code_row["code"]

        cur.execute("""
            SELECT COUNT(*) as total, COUNT(CASE WHEN status='completed' THEN 1 END) as completed
            FROM referrals WHERE referrer_phone = %s
        """, (customer_phone,))
        stats = cur.fetchone()
        conn.close()

        return {
            "code": code,
            "total_referrals": stats["total"] if stats else 0,
            "completed_referrals": stats["completed"] if stats else 0,
            "coins_earned": (stats["completed"] if stats else 0) * 50,
            "share_message": f"Use my code {code} on Drip'd — get ₹25 off your first order! 🎉"
        }
    except Exception as e:
        return {"error": str(e)}

@app.post("/promo/validate")
def validate_promo(body: PromoValidate):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM promo_codes
            WHERE code = %s AND is_active = TRUE
            AND (valid_until IS NULL OR valid_until > NOW())
            AND used_count < max_uses
        """, (body.code.upper(),))
        promo = cur.fetchone()

        if not promo:
            conn.close()
            return {"valid": False, "error": "Invalid or expired promo code"}

        if body.order_amount < promo["min_order_value"]:
            conn.close()
            return {"valid": False, "error": f"Minimum order value ₹{promo['min_order_value']} required"}

        cur.execute("SELECT id FROM promo_usage WHERE code = %s AND customer_phone = %s",
                   (body.code.upper(), body.customer_phone))
        already_used = cur.fetchone()
        if already_used:
            conn.close()
            return {"valid": False, "error": "You've already used this promo code"}

        discount = 0
        if promo["discount_type"] == "flat":
            discount = promo["discount_value"]
        elif promo["discount_type"] == "percent":
            discount = int(body.order_amount * promo["discount_value"] / 100)
        elif promo["discount_type"] == "free_delivery":
            discount = 30

        conn.close()
        return {
            "valid": True,
            "code": promo["code"],
            "description": promo["description"],
            "discount_type": promo["discount_type"],
            "discount_amount": discount,
            "message": f"🎉 {promo['description']} — ₹{discount} off!"
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/promo/active")
def get_active_promos():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT code, description, discount_type, discount_value, min_order_value
            FROM promo_codes
            WHERE is_active = TRUE AND (valid_until IS NULL OR valid_until > NOW())
            AND used_count < max_uses
            ORDER BY created_at DESC
        """)
        promos = cur.fetchall()
        conn.close()
        return {"promos": [dict(p) for p in promos]}
    except Exception as e:
        return {"error": str(e)}

@app.get("/cities")
def get_cities():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM cities ORDER BY is_active DESC, name ASC")
        cities = cur.fetchall()
        conn.close()
        return {"cities": [dict(c) for c in cities]}
    except Exception as e:
        return {"error": str(e)}

@app.get("/cities/active")
def get_active_cities():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM cities WHERE is_active = TRUE ORDER BY order_count DESC")
        cities = cur.fetchall()
        conn.close()
        return {"cities": [dict(c) for c in cities]}
    except Exception as e:
        return {"error": str(e)}

@app.post("/cities/launch")
def launch_city(body: dict):
    try:
        conn = get_db()
        cur = conn.cursor()
        city_name = body.get("name")
        state = body.get("state", "India")
        cur.execute("""
            INSERT INTO cities (name, state, is_active, launch_date)
            VALUES (%s, %s, TRUE, NOW())
            ON CONFLICT (name) DO UPDATE SET is_active = TRUE, launch_date = NOW()
        """, (city_name, state))
        conn.commit()
        conn.close()
        return {"success": True, "message": f"🌆 {city_name} is now live on Drip'd!"}
    except Exception as e:
        return {"error": str(e)}

# ============================================
# PHASE 6 ROUTES
# ============================================

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
                "commission_rate": "9%"
            }
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/commissions/summary")
def get_commission_summary():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT SUM(order_amount) as gross, SUM(commission_amount) as drip_revenue,
                   SUM(store_payout) as store_payouts, COUNT(*) as total_orders
            FROM commissions
        """)
        summary = cur.fetchone()
        conn.close()
        return {
            "gross_gmv": summary["gross"] or 0,
            "drip_revenue": summary["drip_revenue"] or 0,
            "store_payouts": summary["store_payouts"] or 0,
            "total_orders": summary["total_orders"] or 0
        }
    except Exception as e:
        return {"error": str(e)}

@app.post("/payouts/generate/{store_id}")
def generate_payout(store_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
        if not store:
            conn.close()
            return {"error": "Store not found"}

        cur.execute("""
            SELECT SUM(order_amount) as gross, SUM(commission_amount) as commission,
                   SUM(store_payout) as payout, COUNT(*) as orders
            FROM commissions WHERE store_id = %s AND status = 'pending'
        """, (store_id,))
        pending = cur.fetchone()

        if not pending["orders"] or pending["orders"] == 0:
            conn.close()
            return {"error": "No pending commissions for this store"}

        payout_id = f"PAY-{uuid.uuid4().hex[:8].upper()}"
        today = date.today()
        week_start = today.replace(day=today.day - today.weekday())

        cur.execute("""
            INSERT INTO store_payouts (id, payout_id, store_id, store_name, period_start, period_end,
                total_orders, gross_amount, commission_deducted, net_payout, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
        """, (f"PO{uuid.uuid4().hex[:8]}", payout_id, store_id, store["name"],
              week_start, today, pending["orders"], pending["gross"] or 0,
              pending["commission"] or 0, pending["payout"] or 0))

        cur.execute("UPDATE commissions SET status = 'paid' WHERE store_id = %s AND status = 'pending'", (store_id,))
        conn.commit()
        conn.close()

        return {
            "success": True,
            "payout_id": payout_id,
            "store_name": store["name"],
            "total_orders": pending["orders"],
            "gross_amount": pending["gross"] or 0,
            "commission_deducted": pending["commission"] or 0,
            "net_payout": pending["payout"] or 0,
            "message": f"✅ Payout of ₹{pending['payout'] or 0} generated for {store['name']}"
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/payouts/store/{store_id}")
def get_store_payouts(store_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM store_payouts WHERE store_id = %s ORDER BY created_at DESC", (store_id,))
        payouts = cur.fetchall()
        conn.close()
        return {"payouts": [dict(p) for p in payouts]}
    except Exception as e:
        return {"error": str(e)}

@app.get("/payouts/all")
def get_all_payouts():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM store_payouts ORDER BY created_at DESC LIMIT 50")
        payouts = cur.fetchall()
        conn.close()
        return {"payouts": [dict(p) for p in payouts]}
    except Exception as e:
        return {"error": str(e)}

@app.get("/gst/{order_id}")
def get_gst_invoice(order_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM gst_invoices WHERE order_id = %s", (order_id,))
        invoice = cur.fetchone()
        conn.close()
        if not invoice:
            return {"error": "GST invoice not found"}
        return {"invoice": dict(invoice)}
    except Exception as e:
        return {"error": str(e)}

@app.get("/gst/store/{store_id}")
def get_store_gst_invoices(store_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM gst_invoices WHERE store_id = %s ORDER BY created_at DESC", (store_id,))
        invoices = cur.fetchall()
        cur.execute("""
            SELECT SUM(total_tax) as total_tax_collected, SUM(total_amount) as gross,
                   COUNT(*) as invoice_count
            FROM gst_invoices WHERE store_id = %s
        """, (store_id,))
        summary = cur.fetchone()
        conn.close()
        return {
            "invoices": [dict(i) for i in invoices],
            "tax_summary": {
                "total_tax_collected": summary["total_tax_collected"] or 0,
                "gross_revenue": summary["gross"] or 0,
                "invoice_count": summary["invoice_count"] or 0
            }
        }
    except Exception as e:
        return {"error": str(e)}

# ============================================
# STORE PAYMENT DETAILS
# ============================================

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

class PayoutRequest(BaseModel):
    requester_type: str
    requester_id: str
    requester_name: str
    amount: int
    payment_method: str
    payment_details: dict

@app.post("/store/payment-details")
def save_store_payment_details(body: StorePaymentDetails):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO store_payment_details
            (store_id, payment_method, bank_name, account_number, ifsc_code, account_holder_name, upi_id)
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
        return {"success": True, "message": "Payment details saved!"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/store/payment-details/{store_id}")
def get_store_payment_details(store_id: str):
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


# ============ ORDER DISPATCH SYSTEM ============

@app.get("/partner/pending-order/{partner_id}")
def get_pending_order_for_partner(partner_id: str):
    """Delivery app polls this every 5 seconds when online"""
    try:
        conn = get_db()
        cur = conn.cursor()

        # Get partner details
        cur.execute("SELECT * FROM delivery_partners WHERE id = %s AND status = 'approved'", (partner_id,))
        partner = cur.fetchone()
        if not partner:
            conn.close()
            return {"order": None}

        # Check if partner already has an active order
        cur.execute("""
            SELECT o.*, s.name as store_name, s.area as store_area
            FROM orders o
            LEFT JOIN stores s ON o.store_id = s.id
            WHERE o.assigned_partner_id = %s
            AND o.current_status IN ('confirmed', 'packed', 'out_for_delivery')
            ORDER BY o.created_at DESC LIMIT 1
        """, (partner_id,))
        active = cur.fetchone()
        if active:
            conn.close()
            return {"order": dict(active), "type": "active"}

        # Find unassigned orders in partner's area that haven't been skipped by this partner
        cur.execute("""
            SELECT o.*, s.name as store_name, s.phone as store_phone,
                   s.area as store_area, s.address as store_address,
                   p.name as product_name, p.photo as product_photo
            FROM orders o
            LEFT JOIN stores s ON o.store_id = s.id
            LEFT JOIN products p ON o.product_id = p.id
            WHERE o.assigned_partner_id IS NULL
            AND o.current_status = 'confirmed'
            AND o.created_at > NOW() - INTERVAL '30 minutes'
            AND o.order_id NOT IN (
                SELECT order_id FROM partner_skips WHERE partner_id = %s
            )
            ORDER BY o.created_at ASC
            LIMIT 1
        """, (partner_id,))
        order = cur.fetchone()
        conn.close()

        if not order:
            return {"order": None}

        return {"order": dict(order), "type": "new"}
    except Exception as e:
        return {"error": str(e), "order": None}


@app.post("/partner/accept-order")
def accept_order(body: dict):
    """Partner accepts an order"""
    try:
        partner_id = body.get("partner_id")
        order_id = body.get("order_id")
        conn = get_db()
        cur = conn.cursor()

        # Check order is still unassigned
        cur.execute("SELECT * FROM orders WHERE order_id = %s AND assigned_partner_id IS NULL", (order_id,))
        order = cur.fetchone()
        if not order:
            conn.close()
            return {"success": False, "error": "Order already taken"}

        # Get partner name
        cur.execute("SELECT name, phone FROM delivery_partners WHERE id = %s", (partner_id,))
        partner = cur.fetchone()

        # Assign to partner
        cur.execute("""
            UPDATE orders
            SET assigned_partner_id = %s, assigned_partner_name = %s,
                assigned_partner_phone = %s, current_status = 'packed'
            WHERE order_id = %s
        """, (partner_id, partner["name"] if partner else "Partner",
              partner["phone"] if partner else "", order_id))

        # Log status history
        cur.execute("""
            INSERT INTO order_status_history (id, order_id, status, updated_by, note)
            VALUES (%s, %s, 'packed', %s, 'Partner assigned')
        """, (f"SH{__import__('uuid').uuid4().hex[:8]}", order_id, partner_id))

        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}


@app.post("/partner/skip-order")
def skip_order(body: dict):
    """Partner skips an order — try next partner"""
    try:
        partner_id = body.get("partner_id")
        order_id = body.get("order_id")
        conn = get_db()
        cur = conn.cursor()

        # Record skip so this partner won't see this order again
        cur.execute("""
            INSERT INTO partner_skips (id, partner_id, order_id)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (f"PS{__import__('uuid').uuid4().hex[:8]}", partner_id, order_id))

        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}


@app.post("/partner/complete-delivery")
def complete_delivery(body: dict):
    """Partner marks order as delivered"""
    try:
        partner_id = body.get("partner_id")
        order_id = body.get("order_id")
        conn = get_db()
        cur = conn.cursor()

        # Update order status
        cur.execute("""
            UPDATE orders SET current_status = 'delivered', delivered_at = NOW()
            WHERE order_id = %s AND assigned_partner_id = %s
        """, (order_id, partner_id))

        # Log status
        cur.execute("""
            INSERT INTO order_status_history (id, order_id, status, updated_by, note)
            VALUES (%s, %s, 'delivered', %s, 'Delivered by partner')
        """, (f"SH{__import__('uuid').uuid4().hex[:8]}", order_id, partner_id))

        # Add partner earning
        earning_id = f"PE{__import__('uuid').uuid4().hex[:8]}"
        cur.execute("""
            INSERT INTO partner_earnings (id, partner_id, order_id, amount, status)
            VALUES (%s, %s, %s, 40, 'pending')
            ON CONFLICT DO NOTHING
        """, (earning_id, partner_id, order_id))

        # Update partner delivery count
        cur.execute("""
            UPDATE delivery_partners
            SET delivery_count = COALESCE(delivery_count, 0) + 1
            WHERE id = %s
        """, (partner_id,))

        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}


@app.post("/partners/{partner_id}/online")
def set_partner_online(partner_id: str, body: dict):
    """Partner goes online/offline"""
    try:
        conn = get_db()
        cur = conn.cursor()
        is_online = body.get("is_online", False)
        cur.execute("""
            UPDATE delivery_partners SET is_online = %s WHERE id = %s
        """, (is_online, partner_id))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}


@app.get("/partner/active-order/{partner_id}")
def get_active_order(partner_id: str):
    """Get the current active delivery for a partner"""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT o.*, s.name as store_name, s.phone as store_phone,
                   s.address as store_address, s.area as store_area
            FROM orders o
            LEFT JOIN stores s ON o.store_id = s.id
            WHERE o.assigned_partner_id = %s
            AND o.current_status IN ('packed', 'out_for_delivery')
            ORDER BY o.created_at DESC LIMIT 1
        """, (partner_id,))
        order = cur.fetchone()
        conn.close()
        if not order:
            return {"order": None}
        return {"order": dict(order)}
    except Exception as e:
        return {"error": str(e)}


@app.post("/partner/payment-details")
def save_partner_payment_details(body: PartnerPaymentDetails):
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
        return {"success": True, "message": "UPI ID saved!"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/partner/payment-details/{partner_phone}")
def get_partner_payment_details(partner_phone: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM partner_payment_details WHERE partner_phone = %s", (partner_phone,))
        details = cur.fetchone()
        conn.close()
        if not details:
            return {"exists": False}
        return {"exists": True, "details": dict(details)}
    except Exception as e:
        return {"error": str(e)}

@app.post("/partner/earnings/{order_id}")
def log_partner_earning(order_id: str, body: dict):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO partner_earnings (id, partner_phone, order_id, amount, status)
            VALUES (%s, %s, %s, %s, 'pending')
            ON CONFLICT (order_id) DO NOTHING
        """, (f"PE{uuid.uuid4().hex[:8]}", body.get("partner_phone"), order_id, 40))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}

@app.get("/partner/earnings/{partner_phone}")
def get_partner_earnings(partner_phone: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM partner_earnings WHERE partner_phone = %s ORDER BY created_at DESC", (partner_phone,))
        earnings = cur.fetchall()
        cur.execute("""
            SELECT COUNT(*) as total_deliveries, SUM(amount) as total_earned,
                   SUM(CASE WHEN status='pending' THEN amount ELSE 0 END) as pending_amount,
                   SUM(CASE WHEN status='paid' THEN amount ELSE 0 END) as paid_amount
            FROM partner_earnings WHERE partner_phone = %s
        """, (partner_phone,))
        summary = cur.fetchone()
        conn.close()
        return {"earnings": [dict(e) for e in earnings], "summary": dict(summary) if summary else {}}
    except Exception as e:
        return {"error": str(e)}

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
        """, (
            f"PR{uuid.uuid4().hex[:8]}", request_id,
            body.requester_type, body.requester_id, body.requester_name,
            body.amount, body.payment_method, json.dumps(body.payment_details)
        ))
        conn.commit()
        conn.close()
        return {"success": True, "request_id": request_id,
                "message": f"Payout request of ₹{body.amount} submitted successfully!"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/payout/requests/{requester_id}")
def get_payout_requests(requester_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM payout_requests WHERE requester_id = %s ORDER BY created_at DESC", (requester_id,))
        requests = cur.fetchall()
        conn.close()
        return {"requests": [dict(r) for r in requests]}
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


# ============================================
# STORE APPROVAL ROUTES (ADMIN)
# ============================================


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
            # Normalize is_approved based on status if column missing
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


@app.post("/stores/{store_id}/approve")
def approve_store_post(store_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        # Try with is_approved column first, fall back without it
        try:
            cur.execute("UPDATE stores SET status = 'active', is_approved = TRUE, is_open = TRUE WHERE id = %s", (store_id,))
        except Exception:
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

@app.patch("/admin/stores/{store_id}/approve")
def approve_store(store_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE stores SET status = 'active', is_open = TRUE WHERE id = %s", (store_id,))
        conn.commit()
        conn.close()
        return {"success": True, "message": "Store approved and is now live!"}
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

# ============================================
# DELIVERY PARTNER ROUTES
# ============================================

class PartnerRegister(BaseModel):
    name: str
    phone: str
    area: str
    vehicle_type: str
    aadhaar: str
    upi_id: str

class PartnerLogin(BaseModel):
    phone: str

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
            (id, name, phone, area, vehicle_type, aadhaar, upi_id, status, is_online, total_earnings, total_deliveries)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', FALSE, 0, 0)
        """, (partner_id, partner.name, partner.phone, partner.area,
              partner.vehicle_type, partner.aadhaar, partner.upi_id))
        conn.commit()
        conn.close()
        return {"success": True, "partner_id": partner_id, "status": "pending",
                "message": "Registration successful! Pending approval from Drip'd team."}
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
            return {"error": "Phone number not registered. Please register first!", "not_registered": True}
        if partner["status"] == "pending":
            return {"error": "Your account is pending approval. We'll notify you within 24 hours!", "pending": True}
        if partner["status"] == "rejected":
            return {"error": "Your application was not approved. Contact getdripd1@gmail.com", "rejected": True}
        return {"success": True, "partner": dict(partner)}
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
def get_partner_earnings_summary(partner_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM delivery_partners WHERE id = %s", (partner_id,))
        partner = cur.fetchone()
        conn.close()
        if not partner:
            return {"error": "Partner not found"}
        return {"success": True, "partner": dict(partner)}
    except Exception as e:
        return {"error": str(e)}


# ============================================
# CUSTOMER ROUTES
# ============================================

class CustomerRegister(BaseModel):
    name: str
    phone: str
    address: Optional[str] = None
    referral_code: Optional[str] = None

class CustomerLogin(BaseModel):
    phone: str

@app.post("/customers/register")
def register_customer(customer: CustomerRegister):
    try:
        conn = get_db()
        cur = conn.cursor()
        # Check if already registered
        cur.execute("SELECT * FROM customers WHERE phone = %s", (customer.phone,))
        existing = cur.fetchone()
        if existing:
            conn.close()
            return {"success": True, "customer": dict(existing), "already_registered": True}
        # Create new customer
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

@app.post("/customers/login")
def login_customer(body: CustomerLogin):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM customers WHERE phone = %s", (body.phone,))
        customer = cur.fetchone()
        conn.close()
        if not customer:
            return {"success": False, "not_registered": True, "error": "Phone not registered. Please sign up first!"}
        return {"success": True, "customer": dict(customer)}
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
