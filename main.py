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

app = FastAPI(title="Drip'd API", version="2.0")

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

# ============ ROUTES ============
@app.get("/")
def home():
    return {"app": "Drip'd", "version": "2.0", "message": "Fashion delivered in 60 minutes!"}

# ============ STORE REGISTRATION ============
@app.post("/stores/register")
def register_store(store: StoreRegister):
    try:
        conn = get_db()
        cur = conn.cursor()

        # Check if phone already registered
        cur.execute("SELECT id FROM stores WHERE phone = %s", (store.phone,))
        existing = cur.fetchone()
        if existing:
            conn.close()
            return {"error": "Phone number already registered!"}

        # Generate unique store ID
        store_id = f"s_{uuid.uuid4().hex[:8]}"

        # Insert new store
        cur.execute("""
            INSERT INTO stores (id, name, owner_name, phone, area, categories, opening_time, closing_time, is_open, rating, distance_km, delivery_time, total_ratings, rating_sum, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            store_id, store.name, store.owner_name, store.phone,
            store.area, store.categories, store.opening_time,
            store.closing_time, True, 0.0, 1.0, "25-45 mins", 0, 0.0, "active"
        ))
        conn.commit()
        conn.close()

        return {
            "success": True,
            "store_id": store_id,
            "message": f"Welcome to Drip'd, {store.name}! 🎉"
        }
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

        return {
            "success": True,
            "store": dict(store)
        }
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
        """, (
            product_id, product.store_id, product.name, product.price,
            product.sizes, product.category, True, product.photo, 0.0, 0, 0.0
        ))
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

        order_id = f"DR{uuid.uuid4().hex[:6].upper()}"

        cur.execute("""
            INSERT INTO orders (order_id, customer, phone, address, store_id, store, product_id, product, product_price, size, amount, status, is_try_and_buy, payment_id, try_status, return_status, is_rated, estimated_delivery)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            order_id, order.customer_name, order.customer_phone, order.customer_address,
            order.store_id, store["name"], order.product_id, product["name"],
            product["price"], order.size, order.total_amount, "confirmed",
            order.is_try_and_buy, order.payment_id,
            "pending" if order.is_try_and_buy else None,
            None, False, store["delivery_time"]
        ))
        conn.commit()

        cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
        new_order = cur.fetchone()
        conn.close()

        return {"success": True, "order": dict(new_order)}
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
        elif body.refund_type == "return":
            refund_amount = product_price
            notes = "Try & Buy return"
            cur.execute("UPDATE orders SET try_status = 'returned' WHERE order_id = %s", (order_id,))
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

# ============ RETURN (NORMAL ORDER) ============
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

        # Update product rating
        cur.execute("SELECT rating_sum, total_ratings FROM products WHERE id = %s", (order["product_id"],))
        product = cur.fetchone()
        if product:
            new_sum = product["rating_sum"] + body.product_rating
            new_total = product["total_ratings"] + 1
            new_rating = round(new_sum / new_total, 1)
            cur.execute("UPDATE products SET rating_sum = %s, total_ratings = %s, rating = %s WHERE id = %s",
                       (new_sum, new_total, new_rating, order["product_id"]))

        # Update store rating
        cur.execute("SELECT rating_sum, total_ratings FROM stores WHERE id = %s", (order["store_id"],))
        store = cur.fetchone()
        if store:
            new_sum = store["rating_sum"] + body.store_rating
            new_total = store["total_ratings"] + 1
            new_rating = round(new_sum / new_total, 1)
            cur.execute("UPDATE stores SET rating_sum = %s, total_ratings = %s, rating = %s WHERE id = %s",
                       (new_sum, new_total, new_rating, order["store_id"]))

        # Save rating
        rating_id = f"R{uuid.uuid4().hex[:8]}"
        cur.execute("""
            INSERT INTO ratings (rating_id, order_id, product_id, store_id, product_rating, store_rating, review, customer)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (rating_id, order_id, order["product_id"], order["store_id"],
              body.product_rating, body.store_rating, body.review, order["customer"]))

        cur.execute("UPDATE orders SET is_rated = TRUE WHERE order_id = %s", (order_id,))
        conn.commit()
        conn.close()

        return {"success": True, "message": "Thank you for your review! 🌟"}
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
