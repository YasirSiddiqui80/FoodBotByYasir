import os
import re
import chainlit as cl
import gspread
import requests
from oauth2client.service_account import ServiceAccountCredentials
from openai import OpenAI

# ==========================
# CONFIGURATION
# ==========================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

N8N_WEBHOOK_URL = os.getenv(
    "N8N_WEBHOOK_URL",
    "https://siddiquiyasir80.app.n8n.cloud/webhook/Order"
)

SERVICE_ACCOUNT_FILE = "service_account.json"
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
SPREADSHEET_NAME = "Foodbook"
SHEET_NAME = "Menu"

# ==========================
# GOOGLE SHEETS HELPER
# ==========================
def get_menu():
    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, SCOPE)
    client_sheet = gspread.authorize(creds)
    sheet = client_sheet.open(SPREADSHEET_NAME).worksheet(SHEET_NAME)
    data = sheet.get_all_records()
    available_items = [item for item in data if str(item.get("Availability", "")).lower() == "yes"]
    return available_items

def format_menu_text(menu, category=None):
    if category:
        filtered = [i for i in menu if i["Category"].lower() == category.lower()]
        if not filtered:
            return f"❌ Sorry, no items found in {category} category."
        items = filtered
        header = f"📋 Here’s our {category.title()} menu:\n\n"
    else:
        items = menu
        header = "📋 Here’s our full menu:\n\n"

    seen = {}
    for i in items:
        if i["Item Name"] not in seen:
            seen[i["Item Name"]] = i["Price"]

    menu_text = header
    menu_text += "| Item Name | Price (Rs) |\n"
    menu_text += "|-----------|------------|\n"
    for name, price in seen.items():
        menu_text += f"| {name} | {price} |\n"
    return menu_text

# ==========================
# NAME CLEANING HELPER
# ==========================
def clean_name(raw_name: str) -> str:
    raw_name = raw_name.strip()

    # Remove common prefixes
    raw_name = re.sub(
        r"^(my name is|i am|i'm|this is|its|it's)\s+",
        "",
        raw_name,
        flags=re.I
    )

    # Remove common suffixes
    raw_name = re.sub(
        r"\b(here|speaking|on the line)\b",
        "",
        raw_name,
        flags=re.I
    )

    # Extract nickname inside quotes/apostrophes
    nickname_match = re.search(r"['\"]([^'\"]+)['\"]", raw_name)
    if nickname_match:
        raw_name = nickname_match.group(1)

    # Remove leftover punctuation
    raw_name = re.sub(r"[^\w\s]", "", raw_name)

    # Normalize spaces
    cleaned = re.sub(r"\s+", " ", raw_name).strip()

    # Auto-correct ALL CAPS → Proper case
    if cleaned.isupper():
        cleaned = cleaned.title()

    # Always title case multi-word names
    return cleaned.title()

# ==========================
# ORDER HELPERS
# ==========================
def extract_quantity_from_text(text: str) -> int:
    match = re.search(r"(\d+)", text)
    return int(match.group(1)) if match else 1

def find_items_in_order(user_message, menu_data):
    found_items = []
    total_price = 0
    user_message_clean = re.sub(r'[^a-zA-Z0-9 ]', '', user_message.lower())

    categories = {i["Category"].lower() for i in menu_data}
    for cat in categories:
        if cat in user_message_clean and not any(item["Item Name"].lower() in user_message_clean for item in menu_data):
            return None, 0, cat

    added_items = set()
    for item in menu_data:
        name_clean = re.sub(r'[^a-zA-Z0-9 ]', '', item["Item Name"].lower())
        if name_clean in added_items:
            continue
        if re.search(rf"\b{re.escape(name_clean)}\b", user_message_clean):
            qty = extract_quantity_from_text(user_message_clean)
            price = int(item["Price"]) * qty
            found_items.append({
                "item": item["Item Name"],
                "category": item["Category"],
                "price": int(item["Price"]),
                "qty": qty,
                "total": price
            })
            total_price += price
            added_items.add(name_clean)

    return found_items, total_price, None

# ==========================
# CHEF TRIAGE
# ==========================
CHEF_MAP = {
    "Pizza": "Pizza Specialist",
    "BBQ": "BBQ Chef",
    "Burger": "Fastfood Chef",
    "Broast": "Fastfood Chef",
    "Fries": "Fastfood Chef",
    "Cold Drink": "Dessert Bar Chef",
    "Juice": "Dessert Bar Chef",
    "Shake": "Dessert Bar Chef",
    "Dessert": "Dessert Bar Chef",
    "Lassi": "Dessert Bar Chef",
    "Milkshake": "Dessert Bar Chef"
}

def handoff_to_chefs(order_items, user_name):
    chef_orders = {}
    for item in order_items:
        category = item["category"]
        chef = CHEF_MAP.get(category, "General Chef")
        chef_orders.setdefault(chef, []).append(item)

    status_msgs = []
    for chef, items in chef_orders.items():
        total_price = sum(i["total"] for i in items)
        try:
            resp = requests.post(
                N8N_WEBHOOK_URL,
                json={"items": items, "total": total_price, "user_name": user_name, "chef_name": chef},
                timeout=10
            )
            if resp.status_code == 200:
                status_msgs.append(
                    f"✅ {chef}: Order received! Preparing {', '.join(i['item'] for i in items)}."
                )
            else:
                status_msgs.append(f"⚠️ n8n returned {resp.status_code} for {chef}")
        except Exception as e:
            status_msgs.append(f"⚠️ Could not send to {chef}: {e}")
    return "\n\n".join(status_msgs)

# ==========================
# CHAINLIT EVENTS
# ==========================
@cl.on_chat_start
async def start_chat():
    menu_data = get_menu()
    cl.user_session.set("menu_data", menu_data)
    cl.user_session.set("user_name", None)
    cl.user_session.set("orders", [])

    await cl.Message(content="👋 Welcome to FoodBot By Yasir ! I’m your hotel manager today. May I know your name?").send()

@cl.on_message
async def handle_message(message: cl.Message):
    user_message = message.content.strip()
    lower_msg = user_message.lower()
    menu_data = cl.user_session.get("menu_data", [])
    user_name = cl.user_session.get("user_name")
    orders = cl.user_session.get("orders", [])

    # ===== Capture Name =====
    if not user_name:
        cleaned_name = clean_name(user_message)
        cl.user_session.set("user_name", cleaned_name)
        user_name = cleaned_name
        await cl.Message(
            content=f"Pleasure to meet you, {user_name}! 🙏 You can ask for our menu anytime (e.g. 'Pizza menu', 'Burger menu')."
        ).send()
        return

    # ===== Detect Menu Request =====
    categories = list({i["Category"].lower() for i in menu_data})

    # Explicit "menu" request
    if "menu" in lower_msg:
        matched_cat = None
        for cat in categories:
            if cat in lower_msg:
                matched_cat = cat
                break
        menu_text = format_menu_text(menu_data, matched_cat)
        await cl.Message(content=menu_text).send()
        return

    # Natural language menu intent
    menu_triggers = ["show", "what", "any", "do you have", "available"]
    for cat in categories:
        if cat in lower_msg:
            if any(trigger in lower_msg for trigger in menu_triggers):
                menu_text = format_menu_text(menu_data, cat)
                await cl.Message(content=menu_text).send()
                return

    # If user only typed a category (e.g. "Pizza" or "Burger")
    for cat in categories:
        if lower_msg.strip() == cat:
            menu_text = format_menu_text(menu_data, cat)
            await cl.Message(content=menu_text).send()
            return

    # ===== Parse Orders =====
    found_items, total_price, category_request = find_items_in_order(user_message, menu_data)
    if found_items:
        is_first_order = len(orders) == 0
        chef_handoff_msg = handoff_to_chefs(found_items, user_name)

        order_record = {"items": found_items, "total": total_price, "chef": "Multiple", "status": "sent"}
        orders.append(order_record)
        cl.user_session.set("orders", orders)

        grand_total = sum(order["total"] for order in orders)

        order_text = f"✅ {user_name}, I’ve placed your order:\n"
        for item in found_items:
            order_text += f"- {item['qty']} × {item['item']} — Rs {item['price']} each → Rs {item['total']}\n"

        order_text += (
            f"\n💰 This order total: Rs {total_price}\n"
            f"🧾 Grand total so far: Rs {grand_total}\n\n"
        )

        if is_first_order:
            order_text += f"👨‍💼 Manager: Great choice, {user_name}! Let me assign this to the right kitchen.\n\n"

        order_text += chef_handoff_msg
        order_text += f"\n\nWould you like to add anything else, {user_name}? 🍕🥤🍔"

        await cl.Message(content=order_text).send()
        return

    # ===== Show Orders =====
    if lower_msg == "my orders":
        if not orders:
            await cl.Message(content=f"{user_name}, you have no orders yet.").send()
            return
        order_text = f"📦 {user_name}, here are your orders so far:\n"
        for i, order in enumerate(orders):
            order_text += f"{i+1}. Total: Rs {order['total']} (Chef: {order['chef']})\n"
            for item in order['items']:
                order_text += f"   - {item['qty']} × {item['item']} → Rs {item['total']}\n"
        await cl.Message(content=order_text).send()
        return

    # ===== Farewell =====
    clean_msg = re.sub(r'[^a-zA-Z ]', '', lower_msg).strip()
    farewell_keywords = ["no", "no more", "done", "that's all", "nothing else", "finish"]

    if any(kw in clean_msg for kw in farewell_keywords):
        grand_total = sum(order["total"] for order in orders)
        farewell_text = (
            f"🙏 Thank you, {user_name}! Your order has been confirmed.\n"
            f"🧾 Final total: Rs {grand_total}\n"
            "👨‍🍳 Our chefs are preparing your meal and will notify you once it’s ready. 🍴\n"
            "Have a wonderful day! 🌟"
        )
        try:
            requests.post(
                N8N_WEBHOOK_URL,
                json={"user_name": user_name, "message": user_message, "bot_reply": farewell_text, "type": "farewell"},
                timeout=10
            )
        except:
            pass
        await cl.Message(content=farewell_text).send()
        return

    # ===== Fallback =====
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a friendly Restaurant manager helping customers with food ordering."},
            {"role": "user", "content": f"The customer {user_name} just said: {user_message}. Gently guide them back to ordering by suggesting they ask for the menu (pizza, burger, bbq, drinks) or place an order."}
        ],
        temperature=0.7,
        max_tokens=200
    )
    fallback_text = response.choices[0].message.content

    try:
        requests.post(
            N8N_WEBHOOK_URL,
            json={"user_name": user_name, "message": user_message, "bot_reply": fallback_text, "type": "fallback"},
            timeout=10
        )
    except:
        pass

    await cl.Message(content=fallback_text).send()
