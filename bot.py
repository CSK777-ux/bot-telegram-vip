import hashlib
import hmac
import logging
import time
import os
from datetime import datetime, timedelta
import httpx
import threading
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from supabase import create_client, Client

# --- CONFIGURACIÓN SEGURA ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
VIP_CHANNEL_ID = int(os.getenv("VIP_CHANNEL_ID", "0"))

# Credenciales de API Personal de Binance (Spot)
BINANCE_API_KEY = os.getenv("BINANCE_PAY_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_PAY_SECRET")
BINANCE_BASE_URL = "https://api.binance.com"

# Credenciales de Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Inicializar cliente de Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

pending_orders = {} # { user_id: {"amount": float, "time": float} }

# --- SERVIDOR WEB INTERNO PARA RENDER (Evita el error de puertos) ---
app_flask = Flask(__name__)

@app_flask.route("/")
def health_check():
    return "Bot VIP Active with Supabase", 200

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)


# --- FUNCIONES DE BASE DE DATOS (SUPABASE) ---

def save_subscriber(user_id: int, expires_at: datetime):
    try:
        supabase.table("subscribers").upsert({
            "user_id": user_id,
            "expires_at": expires_at.isoformat(),
            "is_active": True
        }).execute()
    except Exception as e:
        logging.error(f"Error guardando usuario en Supabase: {e}")

def get_subscriber(user_id: int):
    try:
        response = supabase.table("subscribers").select("*").eq("user_id", user_id).execute()
        if response.data:
            return response.data[0]
    except Exception as e:
        logging.error(f"Error consultando usuario en Supabase: {e}")
    return None

def update_subscriber_status(user_id: int, is_active: bool):
    try:
        supabase.table("subscribers").update({"is_active": is_active}).eq("user_id", user_id).execute()
    except Exception as e:
        logging.error(f"Error actualizando estado en Supabase: {e}")

def get_all_active_subscribers():
    try:
        response = supabase.table("subscribers").select("*").eq("is_active", True).execute()
        return response.data if response.data else []
    except Exception as e:
        logging.error(f"Error obteniendo activos de Supabase: {e}")
        return []


# --- CLIENTE DE BINANCE PERSONAL (SPOT API) ---

class BinancePersonalClient:
    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key

    def _sign(self, query_string: str) -> str:
        return hmac.new(
            self.secret_key.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    async def check_recent_deposit(self, expected_amount: float) -> bool:
        """Verifica si ha llegado un depósito reciente de USDT en la cuenta personal."""
        endpoint = "/sapi/v1/capital/deposit/hisrec"
        timestamp = int(time.time() * 1000)
        query_string = f"timestamp={timestamp}"
        signature = self._sign(query_string)
        
        url = f"{BINANCE_BASE_URL}{endpoint}?{query_string}&signature={signature}"
        headers = {"X-MBX-APIKEY": self.api_key}

        async with httpx.AsyncClient() as client:
            try:
                res = await client.get(url, headers=headers)
                data = res.json()
                if isinstance(data, list):
                    # Revisa depósitos de los últimos 15 minutos con estado 1 (Completado)
                    now_ts = time.time() * 1000
                    for dep in data:
                        if dep.get("coin") == "USDT" and float(dep.get("amount", 0)) >= expected_amount:
                            dep_time = int(dep.get("insertTime", 0))
                            if (now_ts - dep_time) <= 900000: # 15 minutos
                                return True
            except Exception as e:
                logging.error(f"Error consultando depósitos en Binance: {e}")
        return False

binance_client = BinancePersonalClient(BINANCE_API_KEY, BINANCE_API_SECRET)


# --- HANDLERS DEL BOT DE TELEGRAM ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    keyboard = [
        [InlineKeyboardButton("💳 Pagar $20 USDT (Verificación Automática)", callback_data="buy_1month")],
        [InlineKeyboardButton("🔍 Verificar Mi Suscripción", callback_data="check_status")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"¡Hola {user.first_name}!\n\n"
        "Para acceder al Canal VIP, realiza una transferencia interna en Binance de **20 USDT** a tu cuenta y luego presiona verificar.",
        reply_markup=reply_markup
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "buy_1month":
        pending_orders[user_id] = {"amount": 20.0, "time": time.time()}
        
        keyboard = [
            [InlineKeyboardButton("✅ Ya hice el depósito (Verificar)", callback_data="verify_payment")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            "💳 **Instrucciones de Pago:**\n\n"
            "1. Realiza una transferencia o depósito de **20.00 USDT** en tu Binance.\n"
            "2. Una vez hecho, regresa aquí y presiona el botón de abajo para verificar el ingreso de forma automática.",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

    elif query.data == "verify_payment":
        order_info = pending_orders.get(user_id)
        if not order_info:
            await query.edit_message_text("❌ No tienes ninguna orden pendiente. Usa /start")
            return

        # Consultar si llegó el depósito a la cuenta personal de Binance
        paid = await binance_client.check_recent_deposit(order_info["amount"])

        if paid:
            exp_date = datetime.now() + timedelta(days=30)
            
            # Guardar en Supabase de forma persistente
            save_subscriber(user_id, exp_date)
            
            if user_id in pending_orders:
                del pending_orders[user_id]

            invite = await context.bot.create_chat_invite_link(
                chat_id=VIP_CHANNEL_ID,
                member_limit=1,
                expire_date=datetime.now() + timedelta(hours=2)
            )

            await query.edit_message_text(
                f"🎉 **¡Pago verificado con éxito en tu Binance!**\n\n"
                f"Suscripción activa hasta: `{exp_date.strftime('%Y-%m-%d %H:%M')}`\n\n"
                f"Entra al canal VIP aquí:\n{invite.invite_link}",
                parse_mode="Markdown"
            )
        else:
            keyboard = [
                [InlineKeyboardButton("🔄 Reintentar Verificación", callback_data="verify_payment")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "⏳ **Aún no se detecta el depósito de 20 USDT.**\n\n"
                "Asegúrate de que el saldo ya esté acreditado en tu cuenta personal y vuelve a intentar.",
                reply_markup=reply_markup
            )

    elif query.data == "check_status":
        sub_data = get_subscriber(user_id)
        if sub_data and sub_data["is_active"]:
            # Parsear la fecha de Supabase
            exp_str = sub_data["expires_at"].replace("Z", "+00:00")
            exp_dt = datetime.fromisoformat(exp_str)
            
            if datetime.now().astimezone() < exp_dt:
                exp = exp_dt.strftime('%Y-%m-%d %H:%M')
                await query.edit_message_text(f"🟢 Suscripción **ACTIVA** hasta: `{exp}`", parse_mode="Markdown")
                return

        await query.edit_message_text("🔴 No tienes una suscripción activa.")


async def check_expirations(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now().astimezone()
    active_subs = get_all_active_subscribers()
    
    for sub in active_subs:
        user_id = sub["user_id"]
        exp_str = sub["expires_at"].replace("Z", "+00:00")
        exp_dt = datetime.fromisoformat(exp_str)
        
        if now >= exp_dt:
            try:
                await context.bot.ban_chat_member(chat_id=VIP_CHANNEL_ID, user_id=user_id)
                await context.bot.unban_chat_member(chat_id=VIP_CHANNEL_ID, user_id=user_id)
                
                # Actualizar estado en Supabase
                update_subscriber_status(user_id, False)
                
                await context.bot.send_message(
                    chat_id=user_id,
                    text="⚠️ **Tu suscripción ha expirado.** Usa /start para renovar."
                )
            except Exception as e:
                logging.error(f"Error expulsando a {user_id}: {e}")


def main():
    # Iniciar servidor web en segundo plano para cumplir con Render
    t = threading.Thread(target=run_web)
    t.daemon = True
    t.start()

    # Arrancar Bot de Telegram
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))

    if app.job_queue:
        app.job_queue.run_repeating(check_expirations, interval=600, first=10)

    print("Bot personal de Binance, Telegram y Supabase en marcha...")
    app.run_polling()


if __name__ == "__main__":
    main()