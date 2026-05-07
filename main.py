from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime

app = FastAPI(title="Drip'd API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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

class Product(BaseModel):
    id: str
    store_id: str
    name: str
    price: int
    sizes: List[str]
    category: str
    available: bool = True
    photo: Optional[str] = None  # Cloudinary URL

class Order(BaseModel):
    customer_name: str
    customer_phone: str
    customer_address: str
    store_id: str
    product_id: str
    size: str
    total_amount: int

class AddProduct(BaseModel):
    store_id: str
    name: str
    price: int
    sizes: List[str]
    category: str
    photo: Optional[str] = None

class UpdatePhoto(BaseModel):
    photo: str

# ============ DATABASE ============
stores_db = [
    Store(id="s1", name="Klassic Boutique", area="Sector 29 Gurgaon",
          rating=4.8, distance_km=1.2, delivery_time="25-35 mins",
          categories=["ethnic", "western"]),
    Store(id="s2", name="Alpha Fashion", area="Sushant Lok Gurgaon",
          rating=4.6, distance_km=2.1, delivery_time="35-45 mins",
          categories=["streetwear", "formals"]),
    Store(id="s3", name="Sequence Style", area="Cyber Hub Gurgaon",
          rating=4.9, distance_km=3.0, delivery_time="45-55 mins",
          categories=["western", "ethnic"])
]

products_db = [
    Product(id="p1", store_id="s1", name="Floral Kurta", price=899, sizes=["XS","S","M","L","XL"], category="ethnic"),
    Product(id="p2", store_id="s1", name="Silk Saree", price=2499, sizes=["S","M","L"], category="ethnic"),
    Product(id="p3", store_id="s2", name="Streetwear Hoodie", price=1299, sizes=["S","M","L","XL"], category="streetwear"),
    Product(id="p4", store_id="s3", name="Formal Blazer", price=3499, sizes=["M","L","XL"], category="formals"),
]

orders_db = []

# ============ ROUTES ============
@app.get("/")
def home():
    return {"app": "Drip'd", "version": "1.0", "message": "Fashion delivered in 60 minutes!"}

@app.get("/stores")
def get_stores():
    return {"stores": stores_db}

@app.get("/stores/category/{category}")
def get_stores_by_category(category: str):
    filtered = [s for s in stores_db if category in s.categories]
    return {"stores": filtered}

@app.get("/stores/{store_id}/products")
def get_products(store_id: str):
    products = [p for p in products_db if p.store_id == store_id]
    return {"products": products}

# NEW: Add product with photo from store dashboard
@app.post("/products")
def add_product(product: AddProduct):
    new_product = Product(
        id=f"p{len(products_db)+1}_{int(datetime.now().timestamp())}",
        store_id=product.store_id,
        name=product.name,
        price=product.price,
        sizes=product.sizes,
        category=product.category,
        photo=product.photo
    )
    products_db.append(new_product)
    return {"success": True, "product": new_product}

# NEW: Update photo for existing product
@app.patch("/products/{product_id}/photo")
def update_product_photo(product_id: str, body: UpdatePhoto):
    product = next((p for p in products_db if p.id == product_id), None)
    if not product:
        return {"error": "Product not found"}
    product.photo = body.photo
    return {"success": True, "product": product}

# NEW: Delete product
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
        "product": product.name,
        "size": order.size,
        "amount": order.total_amount,
        "status": "confirmed",
        "estimated_delivery": store.delivery_time,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    orders_db.append(new_order)
    return {"success": True, "order": new_order}

@app.get("/orders")
def get_orders():
    return {"orders": orders_db, "total": len(orders_db)}

@app.get("/orders/{order_id}")
def get_order(order_id: str):
    order = next((o for o in orders_db if o["order_id"] == order_id), None)
    if not order:
        return {"error": "Order not found"}
    return {"order": order}
