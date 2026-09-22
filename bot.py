import hashlib
import hmac
import logging
import random
import string
import time
from datetime import datetime, timedelta
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# --- CONFIGURACIÓN ---
BOT_TOKEN = "TU_TELEGRAM_BOT_TOKEN"
VIP_CHANNEL_ID = -1001234567890  # ID numérico de tu canal (-100...)

# Credenciales de Binance Pay Merchant API (obtenidas desde tu cuenta Binance Merchant)
BINANCE_PAY_KEY = "TU_BINANCE_PAY_API_KEY"
BINANCE_PAY_SECRET = "TU_BINANCE_PAY_SECRET_KEY"
BINANCE_API_URL = "https://bpay.binanceapi.com/binancepay/openapi/v2/order"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# Base de datos simulada
users_db = {}       # { user_id: {"expires_at": datetime, "is_active": bool} }
pending_orders = {} # { merchant_trade_no: {"user_id": int, "days": int} }


# --- CLIENTE DE BINANCE PAY API ---

class BinancePayClient:
    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key

    def _generate_signature(self, timestamp: str, nonce: str, body: str) -> str:
        """Crea la firma HMAC-SHA512 requerida por Binance Pay."""
        payload = f"{timestamp}\n{nonce}\n{body}\n"
        return hmac.new(
            self.secret_key.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha512
        ).hexdigest().upper()

    def _get_headers(self, body_json_str: str) -> dict:
        """Construye los encabezados autenticados de la solicitud."""
        timestamp = str(int(time.time() * 1000))
        nonce = "".join(random.choices(string.ascii_letters + string.digits, k=32))
        signature = self._generate_signature(timestamp, nonce, body_json_str)

        return {
            "Content-Type": "application/json",
            "BinancePay-Timestamp": timestamp,
            "BinancePay-Nonce": nonce,
            "BinancePay-Certificate-SN": self.api_key,
            "BinancePay-Signature": signature,
        }

    async def create_order(self, trade_no: str, amount: float, currency: str, goods_name: str) -> dict:
        """Genera una orden de pago en Binance Pay."""
        import json
        payload = {
            "env": {"terminalType": "MINI_PROGRAM"},
            "merchantTradeNo": trade_no,
            "orderAmount": amount,
            "currency": currency,
            "goods": {
                "goodsType": "02",  # Servicio virtual
                "goodsCategory": "6000",
                "referenceGoodsId": "vip_1m",
                "goodsName": goods_name
            }
        }
        body_str = json.dumps(payload)
        headers = self._get_headers(body_str)

        async with httpx.AsyncClient() as client:
            res = await client.post(BINANCE_API_URL, headers=headers, content=body_str)
            return res.json()

    async def query_order(self, trade_no: str) -> dict:
        """Consulta el estado de una orden registrada."""
        import json
        url = "https://bpay.binanceapi.com/binancepay/openapi/v2/order/query"
        payload = {"merchantTradeNo": trade_no}
        body_str = json.dumps(payload)
        headers = self._get_headers(body_str)

        async with httpx.AsyncClient() as client:
            res = await client.post(url, headers=headers, content=body_str)
            return res.json()


binance_client = BinancePayClient(BINANCE_PAY_KEY, BINANCE_PAY_SECRET)


# --- HANDLERS DEL BOT ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    keyboard = [
        [InlineKeyboardButton("💳 Pagar $20 USDT con Binance Pay", callback_data="buy_1month")],
        [InlineKeyboardButton("🔍 Verificar Mi Suscripción", callback_data="check_status")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"¡Hola {user.first_name}! Accede al Canal VIP en automático.",
        reply_markup=reply_markup
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "buy_1month":
        # Generar identificador único de transacción
        trade_no = f"TRD_{user_id}_{int(time.time())}"
        
        # Guardar en órdenes pendientes
        pending_orders[trade_no] = {"user_id": user_id, "days": 30}

        # Solicitar checkout a la API de Binance Pay
        res = await binance_client.create_order(
            trade_no=trade_no,
            amount=20.00,
            currency="USDT",
            goods_name="Suscripcion Canal VIP 30 Dias"
        )

        if res.get("status") == "SUCCESS":
            data = res["data"]
            checkout_url = data.get("checkoutUrl") or data.get("universalUrl")
            
            keyboard = [
                [InlineKeyboardButton("🔗 Pagar en Binance Pay", url=checkout_url)],
                [InlineKeyboardButton("✅ Ya pagué (Verificar)", callback_data=f"verify_{trade_no}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                "⏳ **Orden de pago creada**\n\n"
                "1. Haz clic en el botón de abajo para completar el pago de **$20 USDT**.\n"
                "2. Una vez completado, presiona **'Ya pagué (Verificar)'**.",
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("❌ Error al comunicarse con Binance Pay. Intenta más tarde.")

    elif query.data.startswith("verify_"):
        trade_no = query.data.replace("verify_", "")
        order_info = pending_orders.get(trade_no)

        if not order_info:
            await query.edit_message_text("❌ Orden no encontrada o ya procesada.")
            return

        # Consultar estado real en los servidores de Binance
        res = await binance_client.query_order(trade_no)

        if res.get("status") == "SUCCESS" and res.get("data", {}).get("status") == "PAID":
            days = order_info["days"]
            exp_date = datetime.now() + timedelta(days=days)

            # Activar usuario en DB
            users_db[user_id] = {"expires_at": exp_date, "is_active": True}
            del pending_orders[trade_no]

            # Crear enlace de invitación único
            invite = await context.bot.create_chat_invite_link(
                chat_id=VIP_CHANNEL_ID,
                member_limit=1,
                expire_date=datetime.now() + timedelta(hours=2)
            )

            await query.edit_message_text(
                f"🎉 **¡Pago recibido con éxito!**\n\n"
                f"Tu suscripción vence el: `{exp_date.strftime('%Y-%m-%d %H:%M')}`\n\n"
                f"Entra a tu canal aquí:\n{invite.invite_link}",
                parse_mode="Markdown"
            )
        else:
            keyboard = [
                [InlineKeyboardButton("🔄 Reintentar Verificación", callback_data=f"verify_{trade_no}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "⌛ **Pago aún no detectado en Binance.**\n"
                "Asegúrate de haber completado la transferencia y vuelve a presionar el botón.",
                reply_markup=reply_markup
            )

    elif query.data == "check_status":
        user_data = users_db.get(user_id)
        if user_data and user_data["is_active"]:
            exp = user_data["expires_at"].strftime('%Y-%m-%d %H:%M')
            await query.edit_message_text(f"🟢 Suscripción **ACTIVA** hasta: `{exp}`", parse_mode="Markdown")
        else:
            await query.edit_message_text("🔴 No tienes una suscripción activa.")


# --- TAREA PROGRAMADA: EXPULSIÓN DE USUARIOS VENCIDOS ---

async def check_expirations(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    for user_id, data in list(users_db.items()):
        if data["is_active"] and now >= data["expires_at"]:
            try:
                await context.bot.ban_chat_member(chat_id=VIP_CHANNEL_ID, user_id=user_id)
                await context.bot.unban_chat_member(chat_id=VIP_CHANNEL_ID, user_id=user_id)
                users_db[user_id]["is_active"] = False
                await context.bot.send_message(
                    chat_id=user_id,
                    text="⚠️ **Tu suscripción ha expirado.** Usa /start para renovar tu acceso."
                )
            except Exception as e:
                logging.error(f"Error expulsando a {user_id}: {e}")


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Tarea en segundo plano para expulsiones (cada 10 min)
    app.job_queue.run_repeating(check_expirations, interval=600, first=10)

    print("Bot en marcha con Binance Pay...")
    app.run_polling()


if __name__ == "__main__":
    main()