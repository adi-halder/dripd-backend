from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
import httpx
import base64
import os

app = FastAPI(title="Drip'd API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

RAZORPAY_KEY_ID = "rzp_test_Smd5WAS1quRuYv"
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_SECRET", "")

# ============ DATA MODELS ============
class Store(BaseModel):
    id: str
    name: str
    area: str
    rating: float
    distance_km: float
    delivery_time: str
    categories: List[str]
    is_open: bool = True
    total_ratings: int = 0
    rating_sum: float = 0

class Product(BaseModel):
    id: str
    store_id: str
    name: str
    price: int
    sizes: List[str]
    category: str
    available: bool = True
    photo: Optional[str] = None
    rating: float = 0
    total_ratings: int = 0
    rating_sum: float = 0

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
    product_rating: float  # 1-5
    store_rating: float    # 1-5
    review: Optional[str] = None

# ============ DATABASE ============
stores_db = [
    Store(id="s1", name="Klassic Boutique", area="Sector 29 Gurgaon",
          rating=4.8, distance_km=1.2, delivery_time="25-35 mins",
          categories=["ethnic", "western"], total_ratings=0, rating_sum=0),
    Store(id="s2", name="Alpha Fashion", area="Sushant Lok Gurgaon",
          rating=4.6, distance_km=2.1, delivery_time="35-45 mins",
          categories=["streetwear", "formals"], total_ratings=0, rating_sum=0),
    Store(id="s3", name="Sequence Style", area="Cyber Hub Gurgaon",
          rating=4.9, distance_km=3.0, delivery_time="45-55 mins",
          categories=["western", "ethnic"], total_ratings=0, rating_sum=0)
]

products_db = [
    Product(id="p1", store_id="s1", name="Floral Kurta", price=899, sizes=["XS","S","M","L","XL"], category="ethnic"),
    Product(id="p2", store_id="s1", name="Silk Saree", price=2499, sizes=["S","M","L"], category="ethnic"),
    Product(id="p3", store_id="s2", name="Streetwear Hoodie", price=1299, sizes=["S","M","L","XL"], category="streetwear"),
    Product(id="p4", store_id="s3", name="Formal Blazer", price=3499, sizes=["M","L","XL"], category="formals"),
]

orders_db = []
ratings_db = []

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
    return {"app": "Drip'd", "version": "1.0", "message": "Fashion delivered in 60 minutes!"}

@app.get("/stores")
def get_stores():
    return {"stores": stores_db}

@app.get("/stores/category/{category}")
def get_stores_by_category(category: str):
    return {"stores": [s for s in stores_db if category in s.categories]}

@app.get("/stores/{store_id}/products")
def get_products(store_id: str):
    return {"products": [p for p in products_db if p.store_id == store_id]}

@app.post("/products")
def add_product(product: AddProduct):
    new_product = Product(
        id=f"p{len(products_db)+1}_{int(datetime.now().timestamp())}",
        store_id=product.store_id, name=product.name, price=product.price,
        sizes=product.sizes, category=product.category, photo=product.photo
    )
    products_db.append(new_product)
    return {"success": True, "product": new_product}

@app.patch("/products/{product_id}/photo")
def update_product_photo(product_id: str, body: UpdatePhoto):
    product = next((p for p in products_db if p.id == product_id), None)
    if not product:
        return {"error": "Product not found"}
    product.photo = body.photo
    return {"success": True, "product": product}

@app.delete("/products/{product_id}")
def delete_product(product_id: str):
    global products_db
    products_db = [p for p in products_db if p.id != product_id]
    return {"success": True}

@app.post("/orders")
def place_order(order: Order):
    store = next((s for s in stores_db if s.id == order.store_id), None)
    product = next((p for p in products_db if p.id == order.product_id), None)
    if not store or not product:
        return {"error": "Store or product not found"}
    new_order = {
        "order_id": f"DR{len(orders_db)+1:04d}",
        "customer": order.customer_name,
        "phone": order.customer_phone,
        "address": order.customer_address,
        "store": store.name,
        "store_id": order.store_id,
        "product": product.name,
        "product_id": order.product_id,
        "product_price": product.price,
        "size": order.size,
        "amount": order.total_amount,
        "status": "confirmed",
        "is_try_and_buy": order.is_try_and_buy,
        "payment_id": order.payment_id,
        "try_status": "pending" if order.is_try_and_buy else None,
        "return_status": None,
        "is_rated": False,
        "estimated_delivery": store.delivery_time,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    orders_db.append(new_order)
    return {"success": True, "order": new_order}

@app.post("/orders/{order_id}/refund")
async def process_refund(order_id: str, body: RefundRequest):
    order = next((o for o in orders_db if o["order_id"] == order_id), None)
    if not order:
        return {"error": "Order not found"}
    if not order.get("is_try_and_buy"):
        return {"error": "Not a Try & Buy order"}
    if not order.get("payment_id"):
        return {"error": "No payment ID found"}
    product_price = order.get("product_price", 0)
    if body.refund_type == "keep":
        refund_amount = 50
        notes = "Try & Buy fee refund"
        order["try_status"] = "kept"
    elif body.refund_type == "return":
        refund_amount = product_price
        notes = "Try & Buy return"
        order["try_status"] = "returned"
    else:
        return {"error": "Invalid refund type"}
    result = await process_razorpay_refund(order["payment_id"], refund_amount, notes)
    if result["success"]:
        return {"success": True, "refund_amount": refund_amount, "refund_id": result.get("refund_id"), "message": f"₹{refund_amount} refund initiated!"}
    return {"error": "Refund failed", "details": result.get("error")}

@app.post("/orders/{order_id}/return")
async def request_return(order_id: str, body: ReturnRequest):
    order = next((o for o in orders_db if o["order_id"] == order_id), None)
    if not order:
        return {"error": "Order not found"}
    if order.get("is_try_and_buy"):
        return {"error": "Use Try & Buy return flow"}
    if order.get("return_status"):
        return {"error": "Return already requested"}
    if not order.get("payment_id"):
        return {"error": "No payment ID found"}
    order_time = datetime.strptime(order["timestamp"], "%Y-%m-%d %H:%M:%S")
    if (datetime.now() - order_time).total_seconds() / 3600 > 24:
        return {"error": "Return window expired. Returns only within 24 hours."}
    product_price = order.get("product_price", 0)
    result = await process_razorpay_refund(order["payment_id"], product_price, f"Return: {body.reason}")
    if result["success"]:
        order["return_status"] = "requested"
        order["return_reason"] = body.reason
        return {"success": True, "refund_amount": product_price, "message": f"₹{product_price} refund initiated!", "pickup_in": "30 minutes"}
    return {"error": "Refund failed", "details": result.get("error")}

@app.post("/orders/{order_id}/rate")
def rate_order(order_id: str, body: RatingRequest):
    order = next((o for o in orders_db if o["order_id"] == order_id), None)
    if not order:
        return {"error": "Order not found"}
    if order.get("is_rated"):
        return {"error": "Already rated this order"}
    if not 1 <= body.product_rating <= 5 or not 1 <= body.store_rating <= 5:
        return {"error": "Rating must be between 1 and 5"}

    # Update product rating
    product = next((p for p in products_db if p.id == order.get("product_id")), None)
    if product:
        product.rating_sum += body.product_rating
        product.total_ratings += 1
        product.rating = round(product.rating_sum / product.total_ratings, 1)

    # Update store rating
    store = next((s for s in stores_db if s.id == order.get("store_id")), None)
    if store:
        store.rating_sum += body.store_rating
        store.total_ratings += 1
        store.rating = round(store.rating_sum / store.total_ratings, 1)

    # Save rating
    rating = {
        "rating_id": f"R{len(ratings_db)+1:04d}",
        "order_id": order_id,
        "product_id": order.get("product_id"),
        "store_id": order.get("store_id"),
        "product_rating": body.product_rating,
        "store_rating": body.store_rating,
        "review": body.review,
        "customer": order.get("customer"),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    ratings_db.append(rating)
    order["is_rated"] = True

    return {"success": True, "message": "Thank you for your review! 🌟"}

@app.get("/products/{product_id}/ratings")
def get_product_ratings(product_id: str):
    product_ratings = [r for r in ratings_db if r["product_id"] == product_id]
    return {"ratings": product_ratings, "total": len(product_ratings)}

@app.get("/stores/{store_id}/ratings")
def get_store_ratings(store_id: str):
    store_ratings = [r for r in ratings_db if r["store_id"] == store_id]
    return {"ratings": store_ratings, "total": len(store_ratings)}

@app.get("/orders")
def get_orders():
    return {"orders": orders_db, "total": len(orders_db)}

@app.get("/orders/{order_id}")
def get_order(order_id: str):
    order = next((o for o in orders_db if o["order_id"] == order_id), None)
    if not order:
        return {"error": "Order not found"}
    return {"order": order}
