import os
import re
import json
import base64
import logging
from datetime import datetime
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Daraja Credentials ─────────────────────────────────────────────────────────
CONSUMER_KEY    = os.environ.get("CONSUMER_KEY",    "3WIa0dq1OYyVsADv5XGSgIBCsOfGLHJfuqkoJAB779vE3UUG")
CONSUMER_SECRET = os.environ.get("CONSUMER_SECRET", "wjH4XPnSxZKsSO05hfevEpPSO71LU0Liccm9y3xwRcTrBtY8YrwDizvTINUI8x3D")
SHORTCODE       = os.environ.get("SHORTCODE",       "4574141")
PASSKEY         = os.environ.get("PASSKEY",         "e365eedebcc81a96e1e35b2b03f3d5e03e0b3e6840dec31623eaa218c6671dc6")
BASE_URL        = os.environ.get("BASE_URL",        "https://mtamu-mpesa-server.onrender.com")

# ── Google Sheets Webhook (Apps Script Web App URL) ────────────────────────────
SHEETS_WEBHOOK_URL = os.environ.get("SHEETS_WEBHOOK_URL", "https://script.google.com/macros/s/AKfycbxFhSZQUjgvq_cy2bynHBjAHjFdcREGjJGMGVUnVuBx40AiuR4fgkGWnDFbqHBjatR2/exec")

DARAJA_AUTH_URL = "https://api.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
DARAJA_STK_URL  = "https://api.safaricom.co.ke/mpesa/stkpush/v1/processrequest"

# ── In-memory stores ───────────────────────────────────────────────────────────
pending_payments = {}   # CheckoutRequestID → full order details
recent_webhooks  = []   # Last 20 raw payloads for debugging


# ── Pretty-field parser ────────────────────────────────────────────────────────
# JotForm always includes a 'pretty' field: a human-readable summary of the
# entire form submission, e.g.:
#   "Choose Your Main Meal:Greener Pastures..., Pickup Time:7:00 PM, Full Name:Arlon Gichane, ..."
# We extract values from it using exact label names, which is far more reliable
# than guessing from opaque field names like q5_q5_radio3.

_PRETTY_LABELS = [
    "Choose Your Main Meal",
    "Choose Your Starch",
    "Vegetable Preference",
    "Chili Preference",
    "Quantity",
    "Office Location",
    "Pickup Time",
    "Full Name",
    "Phone Number (M-Pesa)",
    "WhatsApp Confirmation",
]

def parse_pretty_field(pretty: str, label: str) -> str:
    """
    Extract the value of a labelled field from JotForm's 'pretty' summary string.
    Correctly handles values containing commas (e.g. long meal descriptions).
    Returns empty string if the label is not found.
    """
    target = label + ":"
    idx = pretty.find(target)
    if idx == -1:
        return ""
    start = idx + len(target)
    end   = len(pretty)
    # Stop at the earliest occurrence of any other known label boundary
    for other in _PRETTY_LABELS:
        if other == label:
            continue
        boundary = pretty.find(", " + other + ":", start)
        if 0 < boundary < end:
            end = boundary
    return pretty[start:end].strip()


# ── Core helpers ───────────────────────────────────────────────────────────────

def get_access_token():
    credentials = base64.b64encode(f"{CONSUMER_KEY}:{CONSUMER_SECRET}".encode()).decode()
    response = requests.get(
        DARAJA_AUTH_URL,
        headers={"Authorization": f"Basic {credentials}"},
        timeout=30
    )
    response.raise_for_status()
    return response.json()["access_token"]


def format_phone(phone: str) -> str:
    """Normalise any Kenyan phone number to 254XXXXXXXXX."""
    phone = re.sub(r"[^\d]", "", phone)
    if phone.startswith("0") and len(phone) == 10:
        phone = "254" + phone[1:]
    elif not phone.startswith("254"):
        phone = "254" + phone
    return phone


def extract_phone(data: dict):
    """Find phone via regex — works on q11_phoneNumber11 and any phone-labelled field."""
    phone_re = re.compile(r"((?:\+?254|0)[17]\d{8})")
    priority_keys = [k for k in data if any(t in k.lower() for t in ("phone", "mpesa", "tel", "mobile", "number"))]
    search_order  = priority_keys + [k for k in data if k not in priority_keys]
    for key in search_order:
        val = str(data[key]).strip()
        m = phone_re.search(val)
        if m:
            return m.group(1)
    return None


def extract_unit_price(pretty: str):
    """Extract the per-meal price from the meal label in the pretty field."""
    meal_text = parse_pretty_field(pretty, "Choose Your Main Meal")
    # Meal strings end with e.g. "-KES 150" or "KES- 250" or "KES 1" (test items)
    nums = re.findall(r"(?:KES[-\s]*|[-\s]*KES)\s*(\d{1,5})", meal_text, re.IGNORECASE)
    if nums:
        return int(nums[-1])
    # Broader fallback: any 1-5 digit number in range
    all_nums = re.findall(r"\b(\d{1,5})\b", meal_text)
    for n in reversed(all_nums):
        v = int(n)
        if 1 <= v <= 99999:
            return v
    return None


def post_to_sheet(order: dict):
    """POST confirmed order data to the Google Apps Script webhook.

    Google Apps Script executes doPost() on the initial POST to the /exec URL,
    then returns a 302 redirect to a googleusercontent.com echo URL that serves
    the script's response output.  That echo URL only accepts GET — re-POSTing
    to it returns 405.  We therefore:
      1. POST to the /exec URL (this triggers doPost and writes the sheet row)
      2. GET the redirect URL to retrieve and log the script's response
    """
    if not SHEETS_WEBHOOK_URL or SHEETS_WEBHOOK_URL == "PASTE_YOUR_APPS_SCRIPT_URL_HERE":
        logger.warning("SHEETS_WEBHOOK_URL not configured — skipping Sheet update.")
        return
    try:
        resp = requests.post(SHEETS_WEBHOOK_URL, json=order, timeout=15,
                             allow_redirects=False)
        logger.info("Sheet webhook initial response: %s", resp.status_code)

        if resp.status_code in (301, 302, 303, 307, 308):
            redirect_url = resp.headers.get("Location")
            if redirect_url:
                # GET the echo URL — this retrieves doPost's return value
                echo = requests.get(redirect_url, timeout=15)
                logger.info("Sheet webhook script response: %s %s",
                            echo.status_code, echo.text[:300])
            else:
                logger.error("Sheet webhook: redirect with no Location header")
        else:
            logger.info("Sheet webhook direct response: %s %s",
                        resp.status_code, resp.text[:300])

    except Exception as exc:
        logger.error("Failed to post to Sheet: %s", exc)


def nairobi_now() -> str:
    from datetime import timezone, timedelta
    eat = timezone(timedelta(hours=3))
    return datetime.now(eat).strftime("%Y-%m-%d %H:%M:%S")


def flatten_jotform_payload(raw: dict) -> dict:
    flat = {}
    for k, v in raw.items():
        if k == "rawRequest" and isinstance(v, str):
            try:
                flat.update(json.loads(v))
                continue
            except Exception:
                pass
        flat[k] = v
    return flat


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    """Receives JotForm submission → extracts order → triggers M-PESA STK Push."""
    try:
        if request.content_type and "json" in request.content_type:
            raw = request.get_json(force=True) or {}
        else:
            raw = request.form.to_dict()
            if not raw:
                try:
                    raw = json.loads(request.get_data(as_text=True))
                except Exception:
                    raw = {}

        data = flatten_jotform_payload(raw)
        logger.info("Webhook received:\n%s", json.dumps(data, indent=2, ensure_ascii=False))

        recent_webhooks.append({"ts": nairobi_now(), "data": data})
        if len(recent_webhooks) > 20:
            recent_webhooks.pop(0)

        # ── Extract everything from JotForm's 'pretty' field ──────────────────
        pretty = str(data.get("pretty", ""))

        full_name   = parse_pretty_field(pretty, "Full Name")
        name_parts  = full_name.split()
        first_name  = name_parts[0] if name_parts else "—"
        last_name   = " ".join(name_parts[1:]) if len(name_parts) > 1 else "—"

        meal        = parse_pretty_field(pretty, "Choose Your Main Meal") or "—"
        pickup_time = parse_pretty_field(pretty, "Pickup Time") or "—"

        qty_str     = parse_pretty_field(pretty, "Quantity")
        quantity    = int(qty_str) if qty_str.isdigit() else 1

        # Phone via regex (most reliable)
        phone = extract_phone(data)
        if not phone:
            return jsonify({"error": "Could not find M-PESA phone number"}), 400

        # Amount = unit price × quantity
        unit_price = extract_unit_price(pretty)
        if not unit_price:
            return jsonify({"error": "Could not find meal price in pretty field", "pretty": pretty}), 400

        amount    = unit_price * quantity
        phone_fmt = format_phone(phone)

        logger.info("Order → %s %s | %s | %s x%s = KES %s | %s",
                    first_name, last_name, phone_fmt, unit_price, quantity, amount, pickup_time)

        # ── Daraja STK Push ───────────────────────────────────────────────────
        token     = get_access_token()
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        password  = base64.b64encode(f"{SHORTCODE}{PASSKEY}{timestamp}".encode()).decode()

        stk_payload = {
            "BusinessShortCode": SHORTCODE,
            "Password":          password,
            "Timestamp":         timestamp,
            "TransactionType":   "CustomerPayBillOnline",
            "Amount":            amount,
            "PartyA":            phone_fmt,
            "PartyB":            SHORTCODE,
            "PhoneNumber":       phone_fmt,
            "CallBackURL":       f"{BASE_URL}/callback",
            "AccountReference":  "MtamuPreorder",
            "TransactionDesc":   "Mtamu Preorder Payment"
        }

        stk_resp = requests.post(
            DARAJA_STK_URL,
            json=stk_payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30
        )
        result = stk_resp.json()
        logger.info("STK Push response: %s", result)

        # ── Store full order details keyed by CheckoutRequestID ───────────────
        checkout_id = result.get("CheckoutRequestID")
        if checkout_id:
            pending_payments[checkout_id] = {
                "first_name":  first_name,
                "last_name":   last_name,
                "phone":       phone_fmt,
                "meal":        meal,
                "pickup_time": pickup_time,
                "quantity":    quantity,
                "unit_price":  unit_price,
                "amount":      amount,
                "timestamp":   nairobi_now()
            }
            logger.info("Stored pending payment: %s", checkout_id)

        return jsonify({
            "status":          "STK Push initiated",
            "phone":           phone_fmt,
            "amount":          amount,
            "daraja_response": result
        })

    except Exception as exc:
        logger.exception("Unhandled error in /webhook")
        return jsonify({"error": str(exc)}), 500


@app.route("/callback", methods=["POST"])
def callback():
    """
    Daraja calls this after the customer acts on the STK Push.
    On success → appends the full confirmed order to Google Sheet.
    On failure → logs and discards (nothing written to Sheet).
    """
    data = request.get_json(force=True) or {}
    logger.info("Payment callback:\n%s", json.dumps(data, indent=2))

    try:
        stk         = data["Body"]["stkCallback"]
        result_code = stk["ResultCode"]
        result_desc = stk["ResultDesc"]
        checkout_id = stk.get("CheckoutRequestID")

        if result_code == 0:
            # ── Payment confirmed ─────────────────────────────────────────────
            items          = {i["Name"]: i.get("Value") for i in stk["CallbackMetadata"]["Item"]}
            transaction_id = items.get("MpesaReceiptNumber", "—")
            paid_amount    = items.get("Amount", "—")
            logger.info("✅ Payment SUCCESS — Txn: %s | KES %s", transaction_id, paid_amount)

            order = pending_payments.pop(checkout_id, {})
            logger.info("Order: %s", order)

            # Full row — every field captured at submission time
            sheet_row = {
                "timestamp":      nairobi_now(),
                "first_name":     order.get("first_name", "—"),
                "last_name":      order.get("last_name",  "—"),
                "phone":          order.get("phone",      "—"),
                "pickup_time":    order.get("pickup_time","—"),
                "meal":           order.get("meal",       "—"),
                "quantity":       order.get("quantity",   1),
                "amount":         paid_amount,
                "transaction_id": transaction_id,
            }

            logger.info("Writing to Sheet: %s", sheet_row)
            post_to_sheet(sheet_row)

        else:
            order = pending_payments.pop(checkout_id, {})
            logger.warning("❌ Payment FAILED (code %s): %s | Order: %s",
                           result_code, result_desc, order)

    except Exception as exc:
        logger.error("Callback parse error: %s", exc)

    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})


@app.route("/debug", methods=["GET"])
def debug():
    return jsonify({
        "pending_payments": pending_payments,
        "recent_webhooks":  recent_webhooks
    })


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "Mtamu M-PESA server is running 🔥", "time": nairobi_now()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
