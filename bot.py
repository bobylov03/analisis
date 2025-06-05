import os
import logging
import random
import tempfile
import subprocess
from datetime import datetime, timedelta
import zipfile
import shutil
import re

from dotenv import load_dotenv
from docx import Document
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
)

# ======== Загрузка переменных из .env ========
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Переменная BOT_TOKEN не найдена в окружении. Проверьте файл .env")

# ======== Настройка логирования ========
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ======== Директория скрипта (для шаблонов .docx) ========
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# === Функция подстановки значений в Word (python-docx) и конвертации в PDF (docx2pdf) ===
def fill_docx_and_convert(input_docx: str, output_docx: str, data: dict):
    """
    1) Распаковывает input_docx во временную папку.
    2) Проходит по всем XML‐файлам внутри word/ (document.xml, header*.xml, drawing*.xml и т.д.)
       и ищет любые вариации {{…KEY…}}, допуская XML-теги между символами, затем заменяет на data[KEY].
    3) Собирает из изменённой временной папки новый output_docx (.docx).
    4) Конвертирует output_docx → output_pdf с помощью LibreOffice CLI.
    5) Возвращает кортеж (output_docx, output_pdf).
    """
    # Если на диске уже существуют файлы с такими именами, удаляем их
    try:
        if os.path.exists(output_docx):
            os.remove(output_docx)
    except Exception:
        pass
    output_pdf = os.path.splitext(output_docx)[0] + ".pdf"
    try:
        if os.path.exists(output_pdf):
            os.remove(output_pdf)
    except Exception:
        pass

    # 1) Распаковать .docx (zip) во временную папку
    tempdir = tempfile.mkdtemp()
    try:
        with zipfile.ZipFile(input_docx, 'r') as zin:
            zin.extractall(tempdir)

        # 2) Обойти все XML-файлы внутри word/ и выполнить замену
        word_dir = os.path.join(tempdir, 'word')
        for root, _, files in os.walk(word_dir):
            for fname in files:
                if not fname.lower().endswith('.xml'):
                    continue
                xml_path = os.path.join(root, fname)
                try:
                    with open(xml_path, 'r', encoding='utf-8') as f:
                        xml = f.read()
                except Exception:
                    # Пропустить файлы, не читающиеся как UTF-8
                    continue

                new_xml = xml
                for key, value in data.items():
                    escaped = re.escape(key)
                    # Шаблон для строгого {{KEY}}
                    pattern_double = (
                        r"\{\{\s*"                       # '{{' + пробелы
                        r"(?:<\/?[^>]+>)*\s*"            # XML-теги <…> + пробелы
                        + escaped +                      # сам ключ
                        r"\s*(?:<\/?[^>]+>)*\s*"         # XML-теги + пробелы
                        r"\}\}"                          # '}}'
                    )
                    new_xml = re.sub(pattern_double, str(value), new_xml, flags=re.IGNORECASE)

                    # Шаблон для случая {KEY}} (одна открывающая скобка)
                    pattern_single = (
                        r"\{\s*"                         # '{' + пробелы
                        r"(?:<\/?[^>]+>)*\s*"            # XML-теги + пробелы
                        + escaped +                      # сам ключ
                        r"\s*(?:<\/?[^>]+>)*\s*"         # XML-теги + пробелы
                        r"\}\}"                          # '}}'
                    )
                    new_xml = re.sub(pattern_single, str(value), new_xml, flags=re.IGNORECASE)

                if new_xml != xml:
                    with open(xml_path, 'w', encoding='utf-8') as f:
                        f.write(new_xml)

        # 3) Собрать обратно во .docx
        with zipfile.ZipFile(output_docx, 'w', zipfile.ZIP_DEFLATED) as zout:
            for root, _, files in os.walk(tempdir):
                for file in files:
                    fullpath = os.path.join(root, file)
                    relpath = os.path.relpath(fullpath, tempdir)
                    zout.write(fullpath, relpath)

    finally:
        # Удаляем распакованную временную папку
        shutil.rmtree(tempdir)

    # 4) Конвертировать output_docx → output_pdf через LibreOffice
    try:
        proc = subprocess.run(
            [
                "libreoffice",
                "--headless",
                "--convert-to", "pdf",
                "--outdir", os.path.dirname(output_docx),
                output_docx
            ],
            capture_output=True,
            text=True
        )
        logging.info(f"LibreOffice stdout: {proc.stdout}")
        logging.info(f"LibreOffice stderr: {proc.stderr}")

        if proc.returncode != 0 or not os.path.exists(output_pdf):
            msg = f"Ошибка конвертации через LibreOffice (код {proc.returncode}). Посмотрите stderr выше."
            logging.error(msg)
            raise RuntimeError(msg)

    except FileNotFoundError:
        msg = "Команда 'libreoffice' не найдена. Убедитесь, что LibreOffice установлен."
        logging.error(msg)
        raise

    return output_docx, output_pdf


# === Состояния ConversationHandler ===
(
    CHOOSE_FUEL,        # 0

    # MDO states (1..13)
    MDO_NAME,           # 1
    MDO_DATE,           # 2
    MDO_DATE_RECEIVED,  # 3
    MDO_LOCATION,       # 4
    MDO_SEAL,           # 5
    MDO_NUMBER,         # 6
    MDO_BARGE,          # 7
    MDO_DENS,           # 8
    MDO_VISC,           # 9
    MDO_FLASH,          # 10
    MDO_POUR,           # 11
    MDO_CARBON,         # 12
    MDO_SULPH,          # 13

    # HFO states (14..27)
    HFO_CHOOSE_TYPE,    # 14
    HFO_NAME,           # 15
    HFO_DATE,           # 16
    HFO_DATE_RECEIVED,  # 17
    HFO_LOCATION,       # 18
    HFO_SEAL,           # 19
    HFO_NUMBER,         # 20
    HFO_BARGE,          # 21
    HFO_DENS,           # 22
    HFO_VISC,           # 23
    HFO_FLASH,          # 24
    HFO_POUR,           # 25
    HFO_CARBON,         # 26
    HFO_SULPH,
    ASK_AGAIN    
) = range(29)


# === Клавиатуры ===
fuel_keyboard = [[KeyboardButton("HFO"), KeyboardButton("MDO")]]
again_keyboard = [
    [KeyboardButton("Сделать ещё один PDF"), KeyboardButton("Завершить работу")]]
hfo_type_keyboard = [[KeyboardButton("LSFO RMG-180"), KeyboardButton("LSFO RMG-380")]]


# === Обработчики ===

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /start — предлагаем выбрать HFO или MDO.
    """
    await update.message.reply_text(
        "Выберите тип топлива для заполнения анализов:",
        reply_markup=ReplyKeyboardMarkup(fuel_keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return CHOOSE_FUEL


# --- MDO flow ---

async def choose_mdo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Пользователь нажал [MDO] → сразу сохраняем FUEL="LSMGO DMA" и спрашиваем NAME.
    """
    context.user_data.clear()
    context.user_data["FUEL"] = "LSMGO DMA"
    await update.message.reply_text("Введите Название судна:")
    return MDO_NAME


async def mdo_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["NAME"] = update.message.text.strip().upper()
    await update.message.reply_text("Введите дату анализа (пример: 28-May-2025):")
    return MDO_DATE


async def mdo_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    try:
        dt = datetime.strptime(raw, "%d-%b-%Y")
        context.user_data["DATE"] = raw.upper()
    except ValueError:
        await update.message.reply_text("Неверный формат. Должно быть 28-May-2025.")
        return MDO_DATE

    await update.message.reply_text("Введите дату получения анализа (пример: 29-May-2025):")
    return MDO_DATE_RECEIVED


async def mdo_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    try:
        dt_recv = datetime.strptime(raw, "%d-%b-%Y")
        context.user_data["DATE_RECEIVED"] = raw.upper()
        context.user_data["DATE_TEST"] = (dt_recv + timedelta(days=1)).strftime("%d-%b-%Y").upper()
    except ValueError:
        await update.message.reply_text("Неверный формат. Должно быть 29-May-2025.")
        return MDO_DATE_RECEIVED

    await update.message.reply_text("Введите LOCATION (место бункеровки):")
    return MDO_LOCATION


async def mdo_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["LOCATION"] = update.message.text.strip().upper()
    await update.message.reply_text("Введите SEAL NUMBER(по пломбе с BDN):")
    return MDO_SEAL


async def mdo_seal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["SEAL"] = update.message.text.strip().upper()
    await update.message.reply_text("Введите REPORT NUMBER (6 цифр, пример: 280525):")
    return MDO_NUMBER


async def mdo_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    if not raw.isdigit() or len(raw) != 6:
        await update.message.reply_text("REPORT NUMBER должен быть ровно из 6 цифр, например 280525.")
        return MDO_NUMBER

    context.user_data["NUMBER"] = raw
    context.user_data["SAMPLE"] = str(random.randint(400000, 900000))
    await update.message.reply_text("Введите название BARGE:")
    return MDO_BARGE


async def mdo_barge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["BARGE"] = update.message.text.strip().upper()
    await update.message.reply_text("Введите DENS (Density) из БДН, погрешность ±несколько единиц):")
    return MDO_DENS


async def mdo_dens(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["DENS"] = update.message.text.strip().upper()
    await update.message.reply_text("Введите VISC (Viscosity) из БДН, погрешность ±несколько единиц):")
    return MDO_VISC


async def mdo_visc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["VISC"] = update.message.text.strip().upper()
    await update.message.reply_text("Введите FLASH (Flash point) из БДН, погрешность ±несколько единиц):")
    return MDO_FLASH


async def mdo_flash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["FLASH"] = update.message.text.strip().upper()
    await update.message.reply_text("Введите POUR (Pour point) из БДН, погрешность ±несколько единиц):")
    return MDO_POUR


async def mdo_pour(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    try:
        pv = float(raw)
        context.user_data["POUR"] = raw.upper()
        context.user_data["CLOUD"] = f"{pv - 2:.1f}"
    except ValueError:
        await update.message.reply_text("Нужно число, пример: 10.5")
        return MDO_POUR

    await update.message.reply_text("Введите CARBON (из БДН, погрешность ±несколько единиц):")
    return MDO_CARBON


async def mdo_carbon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["CARBON"] = update.message.text.strip().upper()
    await update.message.reply_text("Введите SULPH (Sulphure) из БДН, погрешность ±несколько единиц):")
    return MDO_SULPH


async def mdo_sulph(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Получаем значение SULPH от пользователя
    context.user_data["SULPH"] = update.message.text.strip().upper()
    # Случайные значения для ASH и CETANE
    context.user_data["ASH"] = f"{random.uniform(0.001, 0.011):.3f}"
    context.user_data["CETANE"] = f"{random.uniform(42.0, 62.0):.1f}"

    await update.message.reply_text("Генерируется документ MDO и конвертируется в PDF…")

    # Собираем все данные для подстановки
    data = {k.upper(): v for k, v in context.user_data.items()}
    tmpl_docx = os.path.join(SCRIPT_DIR, "MDO.docx")

    # 1) Создаём временный файл .docx
    tf = tempfile.NamedTemporaryFile(delete=False, suffix=".docx")
    out_docx = tf.name
    tf.close()

    # 2) Заполняем шаблон и конвертируем в PDF
    try:
        tmp_docx, tmp_pdf = fill_docx_and_convert(tmpl_docx, out_docx, data)
    except Exception as e:
        logger.error(f"Ошибка при генерации MDO PDF: {e}")
        await update.message.reply_text("Ошибка при создании документа. Попробуйте позже.")
        try:
            os.remove(out_docx)
        except:
            pass
        return ConversationHandler.END

    # 3) Отправляем PDF пользователю
    try:
        with open(tmp_pdf, "rb") as f:
            await update.message.reply_document(f)
    except Exception as e:
        logger.error(f"Ошибка при отправке PDF: {e}")
        await update.message.reply_text("Не удалось отправить PDF. Попробуйте позже.")
    finally:
        # 4) Удаляем временные файлы
        try:
            os.remove(tmp_pdf)
        except:
            pass
        try:
            os.remove(tmp_docx)
        except:
            pass

    # 5) Спрашиваем, что делать дальше
    await update.message.reply_text(
        "Что дальше?",
        reply_markup=ReplyKeyboardMarkup(again_keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return ASK_AGAIN


# --- HFO flow ---

async def choose_hfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Пользователь нажал [HFO]. Спрашиваем тип HFO.
    """
    context.user_data.clear()
    await update.message.reply_text(
        "Выберите тип HFO:",
        reply_markup=ReplyKeyboardMarkup(hfo_type_keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return HFO_CHOOSE_TYPE


async def hfo_choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip().upper()
    if choice not in ("LSFO RMG-180", "LSFO RMG-380"):
        await update.message.reply_text("Нужно выбрать «LSFO RMG-180» или «LSFO RMG-360».")
        return HFO_CHOOSE_TYPE

    context.user_data["FUEL"] = choice
    await update.message.reply_text("Введите Название судна:")
    return HFO_NAME


async def hfo_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["NAME"] = update.message.text.strip().upper()
    await update.message.reply_text("Введите дату анализа (пример: 28-May-2025):")
    return HFO_DATE


async def hfo_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    try:
        dt = datetime.strptime(raw, "%d-%b-%Y")
        context.user_data["DATE"] = raw.upper()
    except ValueError:
        await update.message.reply_text("Неверный формат. Должно быть 28-May-2025.")
        return HFO_DATE

    await update.message.reply_text("Введите дату получения анализа (пример: 29-May-2025):")
    return HFO_DATE_RECEIVED


async def hfo_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    try:
        dt_recv = datetime.strptime(raw, "%d-%b-%Y")
        context.user_data["DATE_RECEIVED"] = raw.upper()
        context.user_data["DATE_TEST"] = (dt_recv + timedelta(days=1)).strftime("%d-%b-%Y").upper()
    except ValueError:
        await update.message.reply_text("Неверный формат. Должно быть 29-May-2025.")
        return HFO_DATE_RECEIVED

    await update.message.reply_text("Введите LOCATION (место бункеровки):")
    return HFO_LOCATION


async def hfo_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["LOCATION"] = update.message.text.strip().upper()
    await update.message.reply_text("Введите SEAL NUMBER (пломба из BDN):")
    return HFO_SEAL


async def hfo_seal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["SEAL"] = update.message.text.strip().upper()
    await update.message.reply_text("Введите REPORT NUMBER (6 цифр, пример: 280525):")
    return HFO_NUMBER


async def hfo_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    if not raw.isdigit() or len(raw) != 6:
        await update.message.reply_text("REPORT NUMBER обязан быть из 6 цифр, пример 280525.")
        return HFO_NUMBER

    context.user_data["NUMBER"] = raw
    context.user_data["SAMPLE"] = str(random.randint(400000, 900000))
    await update.message.reply_text("Введите BARGE:")
    return HFO_BARGE


async def hfo_barge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["BARGE"] = update.message.text.strip().upper()
    await update.message.reply_text("Введите DENS (Density) из БДН, погрешность ±несколько единиц):")
    return HFO_DENS


async def hfo_dens(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["DENS"] = update.message.text.strip().upper()
    await update.message.reply_text("Введите VISC (Viscosity) из БДН, погрешность ±несколько единиц):")
    return HFO_VISC


async def hfo_visc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["VISC"] = update.message.text.strip().upper()
    await update.message.reply_text("Введите FLASH (Flash point) из БДН, погрешность ±несколько единиц):")
    return HFO_FLASH


async def hfo_flash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["FLASH"] = update.message.text.strip().upper()
    await update.message.reply_text("Введите POUR (Pour point) из БДН, погрешность ±несколько единиц):")
    return HFO_POUR


async def hfo_pour(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    try:
        float(raw)
        context.user_data["POUR"] = raw.upper()
    except ValueError:
        await update.message.reply_text("Нужно число, пример 10.5")
        return HFO_POUR

    await update.message.reply_text("Введите CARBON (из БДН, погрешность ±несколько единиц):")
    return HFO_CARBON


async def hfo_carbon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["CARBON"] = update.message.text.strip().upper()
    context.user_data["ASH"] = f"{random.uniform(0.001, 0.011):.3f}"
    await update.message.reply_text("Введите SULPH (Sulphure) из БДН, погрешность ±несколько единиц):")
    return HFO_SULPH


async def hfo_sulph(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Получаем значение SULPH от пользователя
    context.user_data["SULPH"] = update.message.text.strip().upper()
    # Случайные значения для VANAD и SEDIM
    context.user_data["VANAD"] = str(random.randint(220, 300))
    context.user_data["SEDIM"] = f"{random.uniform(0.040, 0.088):.3f}"

    await update.message.reply_text("Генерируется документ HFO и конвертируется в PDF…")

    # Собираем все данные для подстановки
    data = {k.upper(): v for k, v in context.user_data.items()}
    tmpl_docx = os.path.join(SCRIPT_DIR, "HFO.docx")

    # 1) Создаём временный файл .docx
    tf = tempfile.NamedTemporaryFile(delete=False, suffix=".docx")
    out_docx = tf.name
    tf.close()

    # 2) Заполняем шаблон и конвертируем в PDF
    try:
        tmp_docx, tmp_pdf = fill_docx_and_convert(tmpl_docx, out_docx, data)
    except Exception as e:
        logger.error(f"Ошибка при генерации HFO PDF: {e}")
        await update.message.reply_text("Ошибка при создании документа. Попробуйте позже.")
        try:
            os.remove(out_docx)
        except:
            pass
        return ConversationHandler.END

    # 3) Отправляем PDF пользователю
    try:
        with open(tmp_pdf, "rb") as f:
            await update.message.reply_document(f)
    except Exception as e:
        logger.error(f"Ошибка при отправке PDF: {e}")
        await update.message.reply_text("Не удалось отправить PDF. Попробуйте позже.")
    finally:
        # 4) Удаляем временные файлы
        try:
            os.remove(tmp_pdf)
        except:
            pass
        try:
            os.remove(tmp_docx)
        except:
            pass

    # 5) Спрашиваем, что делать дальше
    await update.message.reply_text(
        "Что дальше?",
        reply_markup=ReplyKeyboardMarkup(again_keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return ASK_AGAIN


# --- Обработчик команды /cancel ---
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("ОК, отмена. Напишите /start, чтобы начать заново.")
    return ConversationHandler.END


async def ask_again(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip()
    if choice == "Сделать ещё один PDF":
        context.user_data.clear()
        # Начинаем сначала с выбора типа топлива
        await update.message.reply_text(
            "Выберите тип топлива для заполнения анализов:",
            reply_markup=ReplyKeyboardMarkup(fuel_keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
        return CHOOSE_FUEL

    elif choice == "Завершить работу":
        # Отправляем сообщение о завершении и показываем кнопку /start
        await update.message.reply_text(
            "Работа завершена. Нажмите /start, чтобы начать заново.",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("/start")]], one_time_keyboard=True, resize_keyboard=True)
        )
        return ConversationHandler.END

    else:
        # Если пользователь ввёл что-то не по кнопкам — просим выбрать
        await update.message.reply_text(
            "Пожалуйста, нажмите одну из кнопок:",
            reply_markup=ReplyKeyboardMarkup(again_keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
        return ASK_AGAIN



# === MAIN ===
def main():
    # Замените "ВАШ_ТОКЕН" на токен вашего бота
    BOT_TOKEN = "7529252367:AAGQsPyVB88UXCJK9RJwJ1xL8xtmRcYFSSw"
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSE_FUEL: [
                MessageHandler(filters.Regex("^MDO$"), choose_mdo),
                MessageHandler(filters.Regex("^HFO$"), choose_hfo),
            ],

            # --- MDO flow ---
            MDO_NAME:          [MessageHandler(filters.TEXT & ~filters.COMMAND, mdo_name)],
            MDO_DATE:          [MessageHandler(filters.TEXT & ~filters.COMMAND, mdo_date)],
            MDO_DATE_RECEIVED: [MessageHandler(filters.TEXT & ~filters.COMMAND, mdo_date_received)],
            MDO_LOCATION:      [MessageHandler(filters.TEXT & ~filters.COMMAND, mdo_location)],
            MDO_SEAL:          [MessageHandler(filters.TEXT & ~filters.COMMAND, mdo_seal)],
            MDO_NUMBER:        [MessageHandler(filters.TEXT & ~filters.COMMAND, mdo_number)],
            MDO_BARGE:         [MessageHandler(filters.TEXT & ~filters.COMMAND, mdo_barge)],
            MDO_DENS:          [MessageHandler(filters.TEXT & ~filters.COMMAND, mdo_dens)],
            MDO_VISC:          [MessageHandler(filters.TEXT & ~filters.COMMAND, mdo_visc)],
            MDO_FLASH:         [MessageHandler(filters.TEXT & ~filters.COMMAND, mdo_flash)],
            MDO_POUR:          [MessageHandler(filters.TEXT & ~filters.COMMAND, mdo_pour)],
            MDO_CARBON:        [MessageHandler(filters.TEXT & ~filters.COMMAND, mdo_carbon)],
            MDO_SULPH:         [MessageHandler(filters.TEXT & ~filters.COMMAND, mdo_sulph)],

            # --- HFO flow ---
            HFO_CHOOSE_TYPE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, hfo_choose_type)],
            HFO_NAME:          [MessageHandler(filters.TEXT & ~filters.COMMAND, hfo_name)],
            HFO_DATE:          [MessageHandler(filters.TEXT & ~filters.COMMAND, hfo_date)],
            HFO_DATE_RECEIVED: [MessageHandler(filters.TEXT & ~filters.COMMAND, hfo_date_received)],
            HFO_LOCATION:      [MessageHandler(filters.TEXT & ~filters.COMMAND, hfo_location)],
            HFO_SEAL:          [MessageHandler(filters.TEXT & ~filters.COMMAND, hfo_seal)],
            HFO_NUMBER:        [MessageHandler(filters.TEXT & ~filters.COMMAND, hfo_number)],
            HFO_BARGE:         [MessageHandler(filters.TEXT & ~filters.COMMAND, hfo_barge)],
            HFO_DENS:          [MessageHandler(filters.TEXT & ~filters.COMMAND, hfo_dens)],
            HFO_VISC:          [MessageHandler(filters.TEXT & ~filters.COMMAND, hfo_visc)],
            HFO_FLASH:         [MessageHandler(filters.TEXT & ~filters.COMMAND, hfo_flash)],
            HFO_POUR:          [MessageHandler(filters.TEXT & ~filters.COMMAND, hfo_pour)],
            HFO_CARBON:        [MessageHandler(filters.TEXT & ~filters.COMMAND, hfo_carbon)],
            HFO_SULPH:         [MessageHandler(filters.TEXT & ~filters.COMMAND, hfo_sulph)],
            ASK_AGAIN:         [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_again)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)
    application.run_polling()


if __name__ == "__main__":
    main()
