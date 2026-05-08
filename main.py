from fastapi import FastAPI
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

app = FastAPI(title="Drip'd API", version="5.0")

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
    opening_time: str
    closing_time: str

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

# ============ DRIP COINS HELPERS ============
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

# ============ TRENDING HELPERS ============
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

# ============ DROP ALERT HELPERS ============
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

# ============ SOCIAL PROOF HELPER ============
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

# ============ INVOICE HELPER ============
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

# ============ ROUTES ============
@app.get("/")
def home():
    return {"app": "Drip'd", "version": "5.0", "message": "Fashion delivered in 60 minutes!"}

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
            INSERT INTO stores (id, name, owner_name, phone, area, categories, opening_time, closing_time, is_open, rating, distance_km, delivery_time, total_ratings, rating_sum, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (store_id, store.name, store.owner_name, store.phone, store.area, store.categories,
              store.opening_time, store.closing_time, True, 0.0, 1.0, "25-45 mins", 0, 0.0, "active"))
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
            return {"error": "Phone number not registered. Please register first!"}
        return {"success": True, "store": dict(store)}
    except Exception as e:
        return {"error": str(e)}

# ============ STORES ============
@app.get("/stores")
def get_stores():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM stores WHERE is_open = TRUE ORDER BY rating DESC")
        stores = cur.fetchall()
        conn.close()
        return {"stores": [dict(s) for s in stores]}
    except Exception as e:
        return {"error": str(e)}

@app.get("/stores/category/{category}")
def get_stores_by_category(category: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM stores WHERE %s = ANY(categories) AND is_open = TRUE", (category,))
        stores = cur.fetchall()
        conn.close()
        return {"stores": [dict(s) for s in stores]}
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
        cur.execute("SELECT * FROM products WHERE store_id = %s AND available = TRUE", (store_id,))
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

        order_id = f"DR{uuid.uuid4().hex[:6].upper()}"
        cur.execute("""
            INSERT INTO orders (order_id, customer, phone, address, store_id, store, product_id, product,
                product_price, size, amount, status, is_try_and_buy, payment_id, try_status,
                return_status, is_rated, estimated_delivery, current_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            order_id, order.customer_name, order.customer_phone, order.customer_address,
            order.store_id, store["name"], order.product_id, product["name"],
            product["price"], order.size, order.total_amount, "confirmed",
            order.is_try_and_buy, order.payment_id,
            "pending" if order.is_try_and_buy else None,
            None, False, store["delivery_time"], "confirmed"
        ))

        # Initial status history
        cur.execute("""
            INSERT INTO order_status_history (id, order_id, status, updated_by, note)
            VALUES (%s, %s, 'confirmed', 'system', 'Order placed successfully')
        """, (f"SH{uuid.uuid4().hex[:8]}", order_id))

        # Award coins
        coins_earned = 10
        if order.is_try_and_buy:
            coins_earned += 5
        award_coins(cur, order.customer_phone, coins_earned, "order", order_id)
        if delivery_free:
            redeem_coins(cur, order.customer_phone, 100, order_id)

        # Trending & social proof
        city = store["area"]
        update_trending_score(cur, order.product_id, order.store_id, city, is_order=True)
        update_social_proof_order(cur, order.product_id, order.store_id)

        # Style profile
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

        # Size history
        cur.execute("""
            INSERT INTO size_history (id, customer_phone, product_id, order_id, category, size_ordered)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (f"SH{uuid.uuid4().hex[:8]}", order.customer_phone, order.product_id,
              order_id, product["category"], order.size))

        # Personalization
        cur.execute("""
            INSERT INTO personalization_feedback (id, customer_phone, product_id, action)
            VALUES (%s, %s, %s, 'ordered')
        """, (f"PF{uuid.uuid4().hex[:8]}", order.customer_phone, order.product_id))

        # Create invoice
        invoice_number = create_invoice(cur, order, order_id, store["name"], product["name"], product["price"])

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

# ============ REFUND (TRY & BUY) ============
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
            cur.execute("UPDATE orders SET return_status = 'requested', return_reason = %s WHERE order_id = %s", (body.reason, order_id))
            conn.commit()
            conn.close()
            return {"success": True, "refund_amount": product_price, "message": f"₹{product_price} refund initiated!", "pickup_in": "30 minutes"}
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
                cur.execute("UPDATE size_history SET fit_feedback = %s WHERE order_id = %s", (fit_feedback, order_id))

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
            SELECT * FROM coin_transactions
            WHERE customer_phone = %s ORDER BY created_at DESC LIMIT 50
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
        return {"notifications": [dict(n) for n in notifications], "unread_count": unread["unread"] if unread else 0}
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
            predicted = Counter(kept_sizes).most_common(1)[0][0]
            confidence = "high"
        elif perfect_fit_sizes:
            predicted = Counter(perfect_fit_sizes).most_common(1)[0][0]
            confidence = "high"
        elif all_sizes:
            predicted = Counter(all_sizes).most_common(1)[0][0]
            confidence = "medium"
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
        return {"orders_today": orders, "views_today": views, "viewers_now": proof["viewers_now"], "message": message}
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

# ---- ORDER STATUS UPDATES ----

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

        # Update timestamp fields
        timestamp_field = {
            "packed": "packed_at",
            "out_for_delivery": "out_for_delivery_at",
            "delivered": "delivered_at"
        }.get(body.status)

        if timestamp_field:
            cur.execute(f"""
                UPDATE orders SET current_status = %s, {timestamp_field} = NOW()
                WHERE order_id = %s
            """, (body.status, order_id))
        else:
            cur.execute("UPDATE orders SET current_status = %s WHERE order_id = %s", (body.status, order_id))

        # Log status history
        cur.execute("""
            INSERT INTO order_status_history (id, order_id, status, updated_by, updated_by_id, note)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (f"SH{uuid.uuid4().hex[:8]}", order_id, body.status,
              body.updated_by, body.updated_by_id, body.note))

        conn.commit()
        conn.close()

        status_messages = {
            "packed": "Order packed! 📦 Delivery partner notified.",
            "out_for_delivery": "Order picked up! 🛵 On the way.",
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

        cur.execute("""
            SELECT * FROM order_status_history WHERE order_id = %s ORDER BY created_at ASC
        """, (order_id,))
        history = cur.fetchall()
        conn.close()

        current = order["current_status"] or "confirmed"

        stages = [
            {"status": "confirmed", "label": "Order confirmed", "emoji": "✅",
             "eta": "Just now", "done": True},
            {"status": "packed", "label": "Store packing your order", "emoji": "📦",
             "eta": "~10 mins", "done": current in ["packed", "out_for_delivery", "delivered"],
             "time": str(order["packed_at"]) if order.get("packed_at") else None},
            {"status": "out_for_delivery", "label": "Out for delivery", "emoji": "🛵",
             "eta": "~20 mins", "done": current in ["out_for_delivery", "delivered"],
             "time": str(order["out_for_delivery_at"]) if order.get("out_for_delivery_at") else None},
            {"status": "delivered", "label": "Delivered to your door", "emoji": "🏠",
             "eta": "~35 mins", "done": current == "delivered",
             "time": str(order["delivered_at"]) if order.get("delivered_at") else None},
        ]

        return {
            "order_id": order_id,
            "current_status": current,
            "stages": stages,
            "history": [dict(h) for h in history],
            "delivery_partner": order.get("delivery_partner_name"),
        }
    except Exception as e:
        return {"error": str(e)}

# ---- CHAT ----

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
        cur.execute("""
            SELECT * FROM chat_messages WHERE order_id = %s ORDER BY created_at ASC
        """, (order_id,))
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
        # Mark messages from the other party as read
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

@app.get("/chat/{order_id}/unread")
def get_unread_count(order_id: str, sender: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        other_sender = "store" if sender == "customer" else "customer"
        cur.execute("""
            SELECT COUNT(*) as unread FROM chat_messages
            WHERE order_id = %s AND sender = %s AND is_read = FALSE
        """, (order_id, other_sender))
        result = cur.fetchone()
        conn.close()
        return {"unread": result["unread"] if result else 0}
    except Exception as e:
        return {"error": str(e)}

# ---- INVOICES ----

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
        cur.execute("""
            SELECT * FROM invoices WHERE customer_phone = %s ORDER BY created_at DESC
        """, (customer_phone,))
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
        cur.execute("""
            SELECT * FROM invoices WHERE store_id = %s ORDER BY created_at DESC
        """, (store_id,))
        invoices = cur.fetchall()
        conn.close()
        return {"invoices": [dict(i) for i in invoices]}
    except Exception as e:
        return {"error": str(e)}
