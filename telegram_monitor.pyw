import asyncio
import json
import logging
import logging.handlers
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from dotenv import load_dotenv

# Rodando sem console (.pyw / pythonw), o stdout do Windows normalmente vem em
# cp1252 e quebra em qualquer print() com emoji — força UTF-8 pra não crashar.
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None:
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

try:
    from pystray import Icon, MenuItem
    from PIL import Image, ImageDraw
    from plyer import notification
    TRAY_AVAILABLE = True
except Exception:
    TRAY_AVAILABLE = False

# Log persistente: sem console (pythonw), é a única forma de saber o que aconteceu.
LOG_FILE = Path('monitor.log')
_logger = logging.getLogger('telegram_monitor')
_logger.setLevel(logging.INFO)
_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding='utf-8'
)
_file_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
_logger.addHandler(_file_handler)


def log(msg):
    _logger.info(msg)
    try:
        print(msg)
    except Exception:
        pass


def _set_windows_user_env_var(name, value):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment', 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    try:
        import ctypes
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x1A
        SMTO_ABORTIFHUNG = 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, 'Environment', SMTO_ABORTIFHUNG, 5000, None
        )
    except Exception:
        pass


def _prompt(msg, default=''):
    suffix = f" [{default}]" if default else ""
    value = input(f"{msg}{suffix}: ").strip()
    return value or default


def run_first_time_setup():
    print("=" * 50)
    print("👋 Primeira execução — vamos configurar o monitor.")
    print("=" * 50)
    print("Obtenha suas credenciais em https://my.telegram.org (API development tools).\n")

    api_id = _prompt("TELEGRAM_API_ID")
    while not api_id.isdigit():
        api_id = _prompt("TELEGRAM_API_ID deve ser numérico, tente de novo")

    api_hash = _prompt("TELEGRAM_API_HASH")
    while not api_hash:
        api_hash = _prompt("TELEGRAM_API_HASH é obrigatório, tente de novo")

    keywords = _prompt("Palavras-chave que disparam alerta (separadas por vírgula)")
    exclude_keywords = _prompt("Palavras-chave que cancelam o alerta mesmo se bater uma acima (opcional)")
    channels = _prompt("Canais a monitorar (opcional, vazio = todos que você segue)")

    values = {
        'TELEGRAM_API_ID': api_id,
        'TELEGRAM_API_HASH': api_hash,
        'KEYWORDS': keywords,
        'EXCLUDE_KEYWORDS': exclude_keywords,
        'CHANNELS': channels,
    }

    print("\nOnde salvar essa configuração?")
    print("  1) Variável de ambiente do Windows (recomendado, permanente, fora da pasta do projeto)")
    print("  2) Arquivo .env local")
    choice = _prompt("Escolha", "1")

    for key, value in values.items():
        os.environ[key] = value

    if choice == '2':
        Path('.env').write_text(
            "\n".join(f"{k}={v}" for k, v in values.items()) + "\n", encoding='utf-8'
        )
        print(f"✅ Configuração salva em {Path('.env').resolve()}")
    else:
        try:
            for key, value in values.items():
                _set_windows_user_env_var(key, value)
            print("✅ Configuração salva nas variáveis de ambiente do Windows (visível em novos processos a partir de agora).")
        except Exception as e:
            print(f"⚠️ Não foi possível salvar como variável de ambiente ({e}). Salvando em .env local.")
            Path('.env').write_text(
                "\n".join(f"{k}={v}" for k, v in values.items()) + "\n", encoding='utf-8'
            )

    print("=" * 50 + "\n")


# Carrega as variáveis de ambiente
load_dotenv()

if not os.getenv('TELEGRAM_API_ID') or not os.getenv('TELEGRAM_API_HASH'):
    try:
        run_first_time_setup()
    except (EOFError, OSError):
        print("❌ ERRO: primeira configuração precisa rodar num terminal (não dê duplo-clique no .pyw na primeira vez).")
        sys.exit(1)

API_ID = os.getenv('TELEGRAM_API_ID')
API_HASH = os.getenv('TELEGRAM_API_HASH')

# Processa palavras-chave
keywords_raw = os.getenv('KEYWORDS', '')
KEYWORDS = [k.strip().lower() for k in keywords_raw.split(',') if k.strip()]

# Processa palavras-chave excludentes (cancelam o alerta mesmo se uma KEYWORD bateu)
exclude_keywords_raw = os.getenv('EXCLUDE_KEYWORDS', '')
EXCLUDE_KEYWORDS = [k.strip().lower() for k in exclude_keywords_raw.split(',') if k.strip()]

# Processa canais
channels_raw = os.getenv('CHANNELS', '')
CHANNELS = [c.strip() for c in channels_raw.split(',') if c.strip()]

# Validação básica
if not API_ID or not API_HASH:
    print("❌ ERRO: Configure TELEGRAM_API_ID e TELEGRAM_API_HASH (variável de ambiente ou .env)")
    sys.exit(1)

if not KEYWORDS:
    print("⚠️ AVISO: Nenhuma palavra-chave configurada. O script irá apenas logar as mensagens sem filtrar.")

# Se canais estiverem vazios, monitora todos os canais que o usuário participa
entity_list = CHANNELS if CHANNELS else None

STATE_FILE = Path('monitor_state.json')
HISTORY_FALLBACK = timedelta(hours=48)  # sem estado salvo pra um canal, procura a partir daqui


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def update_last_processed(chat_id, message_id):
    state = load_state()
    state[str(chat_id)] = max(message_id, state.get(str(chat_id), 0))
    save_state(state)


# Registro separado de mensagens já alertadas — sobrevive ao "Limpar histórico"
# (que só reseta o ponteiro de varredura) pra não reenviar duplicata pras
# Mensagens Salvas de algo que já tinha sido mandado antes.
ALERTED_FILE = Path('alerted_messages.json')
ALERTED_RETENTION = timedelta(days=14)


def load_alerted():
    if not ALERTED_FILE.exists():
        return {}
    try:
        return json.loads(ALERTED_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_alerted(alerted):
    ALERTED_FILE.write_text(json.dumps(alerted, ensure_ascii=False, indent=2), encoding='utf-8')


def was_already_alerted(chat_id, message_id):
    return f"{chat_id}:{message_id}" in load_alerted()


def mark_alerted(chat_id, message_id):
    alerted = load_alerted()
    cutoff = datetime.now(timezone.utc) - ALERTED_RETENTION
    alerted = {k: v for k, v in alerted.items() if datetime.fromisoformat(v) > cutoff}
    alerted[f"{chat_id}:{message_id}"] = datetime.now(timezone.utc).isoformat()
    save_alerted(alerted)


def create_tray_image():
    image = Image.new('RGB', (64, 64), color=(0, 120, 212))
    draw = ImageDraw.Draw(image)
    draw.ellipse((10, 10, 54, 54), fill='white')
    return image


def show_notification(title, message):
    if not TRAY_AVAILABLE:
        return
    try:
        notification.notify(
            title=title,
            message=message,
            app_name='Telegram Monitor',
            timeout=5,
        )
    except Exception as e:
        print(f"❌ Falha na notificação: {e}")


tray_icon = None
loop = None
ME_ID = None
SESSION_START = None
ALERTS_SENT = 0
LAST_ALERT_SUMMARY = None


def update_tray_status():
    if not tray_icon:
        return
    lines = [f"🟢 Conectado desde {SESSION_START}", f"{ALERTS_SENT} alerta(s) nesta sessão"]
    if LAST_ALERT_SUMMARY:
        lines.append(f"Último: {LAST_ALERT_SUMMARY}")
    tray_icon.title = "\n".join(lines)[:127]


def register_alert_sent(chat_title, matched_keywords):
    global ALERTS_SENT, LAST_ALERT_SUMMARY
    ALERTS_SENT += 1
    time_str = datetime.now().strftime('%H:%M')
    LAST_ALERT_SUMMARY = f"{time_str} {chat_title} ({', '.join(matched_keywords)})"
    update_tray_status()


def quit_action(icon, item):
    icon.stop()
    if loop and not loop.is_closed():
        asyncio.run_coroutine_threadsafe(client.disconnect(), loop)


def open_log_action(icon, item):
    try:
        os.startfile(str(LOG_FILE.resolve()))
    except Exception as e:
        log(f"❌ Erro ao abrir o log: {e}")


# Espaça os envios pra "Mensagens Salvas" pra não disparar o flood protection do
# Telegram quando muitos alertas batem de uma vez (ex.: depois de "Limpar histórico").
MIN_SEND_INTERVAL = 3.0
_send_lock = asyncio.Lock()
_last_send_at = 0.0


async def send_alert(alert_msg):
    global _last_send_at
    async with _send_lock:
        wait = MIN_SEND_INTERVAL - (asyncio.get_event_loop().time() - _last_send_at)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            await client.send_message('me', alert_msg)
        except FloodWaitError as e:
            wait_time = e.seconds + 5
            log(f"⏳ Limite de envio do Telegram atingido — aguardando {wait_time}s antes de tentar de novo.")
            show_notification(
                title='Telegram Ofertas Monitor',
                message=f"Limite de envio do Telegram atingido, pausando {wait_time}s.",
            )
            await asyncio.sleep(wait_time)
            await client.send_message('me', alert_msg)
        finally:
            _last_send_at = asyncio.get_event_loop().time()


def _persist_keyword_vars(keywords_str, exclude_str):
    """Salva KEYWORDS/EXCLUDE_KEYWORDS do mesmo jeito que já estavam configuradas
    (arquivo .env, se existir, ou variável de ambiente do Windows)."""
    os.environ['KEYWORDS'] = keywords_str
    os.environ['EXCLUDE_KEYWORDS'] = exclude_str

    env_path = Path('.env')
    if env_path.exists():
        lines = env_path.read_text(encoding='utf-8').splitlines()
        found = {'KEYWORDS': False, 'EXCLUDE_KEYWORDS': False}
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('KEYWORDS='):
                new_lines.append(f'KEYWORDS={keywords_str}')
                found['KEYWORDS'] = True
            elif stripped.startswith('EXCLUDE_KEYWORDS='):
                new_lines.append(f'EXCLUDE_KEYWORDS={exclude_str}')
                found['EXCLUDE_KEYWORDS'] = True
            else:
                new_lines.append(line)
        if not found['KEYWORDS']:
            new_lines.append(f'KEYWORDS={keywords_str}')
        if not found['EXCLUDE_KEYWORDS']:
            new_lines.append(f'EXCLUDE_KEYWORDS={exclude_str}')
        env_path.write_text('\n'.join(new_lines) + '\n', encoding='utf-8')
    else:
        _set_windows_user_env_var('KEYWORDS', keywords_str)
        _set_windows_user_env_var('EXCLUDE_KEYWORDS', exclude_str)


def _force_foreground(hwnd):
    """Força o foco de teclado de verdade pro hwnd dado.

    O tray icon roda num processo em segundo plano (sem console) — o Windows
    tem uma proteção que impede esse tipo de processo de "roubar" o foco pra
    uma janela nova, mesmo com `-topmost`/`focus_force` do Tk: a janela aparece
    por cima mas o teclado continua mandando teclas pra o que estava em foco
    antes. `AttachThreadInput` contorna isso emprestando temporariamente a
    permissão da thread que está em foreground.
    """
    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        fg_hwnd = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None)
        cur_thread = kernel32.GetCurrentThreadId()
        if fg_hwnd and fg_thread and fg_thread != cur_thread:
            user32.AttachThreadInput(fg_thread, cur_thread, True)
            try:
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
            finally:
                user32.AttachThreadInput(fg_thread, cur_thread, False)
        else:
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
    except Exception as e:
        log(f"⚠️ Não foi possível forçar o foco da janela: {e}")


def edit_keywords_action(icon, item):
    import tkinter as tk

    def on_save():
        global KEYWORDS, EXCLUDE_KEYWORDS
        new_keywords_str = ','.join(l.strip() for l in kw_text.get('1.0', 'end').splitlines() if l.strip())
        new_exclude_str = ','.join(l.strip() for l in ex_text.get('1.0', 'end').splitlines() if l.strip())

        KEYWORDS = [k.strip().lower() for k in new_keywords_str.split(',') if k.strip()]
        EXCLUDE_KEYWORDS = [k.strip().lower() for k in new_exclude_str.split(',') if k.strip()]

        try:
            _persist_keyword_vars(new_keywords_str, new_exclude_str)
            log(f"✏️ Palavras-chave atualizadas. Inclusivas: {', '.join(KEYWORDS) or '(nenhuma)'} | Excludentes: {', '.join(EXCLUDE_KEYWORDS) or '(nenhuma)'}")
            show_notification(title='Telegram Ofertas Monitor', message='Palavras-chave atualizadas.')
        except Exception as e:
            log(f"❌ Erro ao salvar palavras-chave: {e}")
        root.destroy()

    root = tk.Tk()
    root.title('Palavras-chave — Telegram Ofertas Monitor')
    root.attributes('-topmost', True)

    tk.Label(root, text='Inclusivas (uma por linha) — qualquer uma dispara o alerta:').pack(anchor='w', padx=10, pady=(10, 2))
    kw_text = tk.Text(root, width=60, height=12)
    kw_text.pack(padx=10, pady=(0, 8))
    kw_text.insert('1.0', '\n'.join(KEYWORDS))

    tk.Label(root, text='Excludentes (uma por linha) — cancelam o alerta mesmo se uma inclusiva bater:').pack(anchor='w', padx=10, pady=(0, 2))
    ex_text = tk.Text(root, width=60, height=6)
    ex_text.pack(padx=10, pady=(0, 8))
    ex_text.insert('1.0', '\n'.join(EXCLUDE_KEYWORDS))

    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=(0, 10))
    tk.Button(btn_frame, text='Salvar', width=12, command=on_save).pack(side='left', padx=6)
    tk.Button(btn_frame, text='Cancelar', width=12, command=root.destroy).pack(side='left', padx=6)

    # A janela precisa existir de verdade (mapeada na tela) antes de tentar
    # forçar o foco nela — sem o update(), o hwnd pode ainda não estar pronto.
    root.update()
    _force_foreground(root.winfo_id())
    root.lift()
    root.focus_force()
    kw_text.focus_set()
    kw_text.mark_set('insert', 'end')

    root.mainloop()


def clear_history_action(icon, item):
    try:
        save_state({})
        log(f"🧹 Histórico limpo. Refazendo varredura das últimas {int(HISTORY_FALLBACK.total_seconds() // 3600)}h em todos os canais (sem reenviar o que já foi alertado antes)...")
        show_notification(title='Telegram Ofertas Monitor', message='Histórico limpo — reescaneando últimas 48h.')
        if loop and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(resume_missing_messages(), loop)
    except Exception as e:
        log(f"❌ Erro ao limpar histórico: {e}")


async def process_message(chat, message):
    # Ignora a própria conversa de Mensagens Salvas (é lá que o bot posta os alertas)
    if ME_ID is not None and chat.id == ME_ID:
        return

    message_text = getattr(message, 'raw_text', None) or getattr(message, 'message', None)
    if not message_text:
        return

    message_text_lower = message_text.lower()

    if not KEYWORDS:
        chat_title = getattr(chat, 'title', None) or getattr(chat, 'username', None) or 'Direct'
        log(f"[{chat_title}] {message_text[:100]}...")
        update_last_processed(chat.id, message.id)
        return

    matched_keywords = [kw for kw in KEYWORDS if kw in message_text_lower]
    excluding_keywords = [kw for kw in EXCLUDE_KEYWORDS if kw in message_text_lower]

    chat_title = getattr(chat, 'title', 'Canal Desconhecido')

    if matched_keywords and excluding_keywords:
        log(f"🚫 [IGNORADO] Termo(s) {matched_keywords} encontrado(s) em '{chat_title}', mas excluído por {excluding_keywords}.")
    elif matched_keywords and was_already_alerted(chat.id, message.id):
        log(f"🔁 [DUPLICADO] Termo {matched_keywords} em '{chat_title}' já tinha sido alertado antes — não reenviando.")
    elif matched_keywords:
        chat_username = getattr(chat, 'username', None)

        if chat_username:
            msg_link = f"https://t.me/{chat_username}/{message.id}"
        else:
            chat_id = str(chat.id).replace('-100', '')
            msg_link = f"https://t.me/c/{chat_id}/{message.id}"

        posted_at = message.date.astimezone().strftime('%d/%m/%Y %H:%M')

        alert_msg = (
            f"🔔 **Alerta de Oferta Encontrada!**\n\n"
            f"📌 **Termo(s) detectado(s):** {', '.join(matched_keywords)}\n"
            f"📢 **Canal:** {chat_title}\n"
            f"🕒 **Postado em:** {posted_at}\n"
            f"🔗 **Link da Mensagem:** {msg_link}\n\n"
            f"📝 **Conteúdo:**\n{message_text[:600]}"
            f"{'...' if len(message_text) > 600 else ''}"
        )

        try:
            await send_alert(alert_msg)
            log(f"🔥 [MATCH] Termo {matched_keywords} encontrado em '{chat_title}' (postado em {posted_at}). Alerta enviado para suas Mensagens Salvas!")
            show_notification(
                title='Oferta encontrada no Telegram',
                message=f"{chat_title}: {message_text[:120]}{'...' if len(message_text) > 120 else ''}",
            )
            register_alert_sent(chat_title, matched_keywords)
            mark_alerted(chat.id, message.id)
        except Exception as e:
            log(f"❌ Erro ao enviar mensagem de alerta (mesmo após esperar o limite): {e}")

    update_last_processed(chat.id, message.id)


async def scan_chat_for_missed_messages(chat):
    state = load_state()
    last_processed_id = state.get(str(chat.id))

    if last_processed_id:
        messages = client.iter_messages(chat, min_id=last_processed_id, reverse=True)
    else:
        since = datetime.now(timezone.utc) - HISTORY_FALLBACK
        messages = client.iter_messages(chat, offset_date=since, reverse=True)

    async for message in messages:
        if message.id == last_processed_id:
            continue
        await process_message(chat, message)


async def resume_missing_messages():
    log('⏳ Verificando mensagens perdidas desde a última execução...')
    if entity_list:
        for target in entity_list:
            try:
                chat = await client.get_entity(target)
            except Exception as e:
                log(f"⚠️ Não foi possível acessar '{target}': {e}")
                continue
            await scan_chat_for_missed_messages(chat)
    else:
        async for dialog in client.iter_dialogs():
            if ME_ID is not None and dialog.entity.id == ME_ID:
                continue
            await scan_chat_for_missed_messages(dialog.entity)
    log('✅ Verificação de mensagens antigas concluída.')


# Inicializa o cliente do Telethon (cria o arquivo monitor_session.session)
client = TelegramClient('monitor_session', int(API_ID), API_HASH)

@client.on(events.NewMessage(chats=entity_list))
async def handle_new_message(event):
    chat = event.chat if event.chat else await event.get_chat()
    await process_message(chat, event.message)

async def main():
    log("="*50)
    log("✨ MONITOR DE OFERTAS TELEGRAM INICIADO ✨")
    log("="*50)
    log(f"🔍 Palavras-chave: {', '.join(KEYWORDS)}")
    if EXCLUDE_KEYWORDS:
        log(f"🚫 Palavras-chave excludentes: {', '.join(EXCLUDE_KEYWORDS)}")
    channels_desc = ', '.join(CHANNELS) if CHANNELS else 'Todos os canais que você participa'
    log(f"📢 Canais filtrados: {channels_desc}")
    log("="*50)

    # Inicia a sessão e pede autenticação no console (apenas na primeira execução)
    global loop, tray_icon, ME_ID, SESSION_START
    loop = asyncio.get_running_loop()

    if TRAY_AVAILABLE:
        tray_icon = Icon(
            'telegram_monitor',
            create_tray_image(),
            'Telegram Monitor - iniciando...',
            menu=(
                MenuItem('Editar palavras-chave', edit_keywords_action),
                MenuItem('Ver histórico', open_log_action),
                MenuItem('Limpar histórico', clear_history_action),
                MenuItem('Sair', quit_action),
            ),
        )
        tray_icon.run_detached()

    await client.start()
    log("✅ Conectado com sucesso ao Telegram!")
    me = await client.get_me()
    ME_ID = me.id
    SESSION_START = datetime.now().strftime('%H:%M')
    update_tray_status()
    await resume_missing_messages()
    show_notification(
        title='Telegram Ofertas Monitor',
        message=f"Conectado e ouvindo. {channels_desc}.",
    )
    log("Listening... Pressione Ctrl+C para encerrar.\n")
    try:
        await client.run_until_disconnected()
    finally:
        if tray_icon:
            tray_icon.stop()

if __name__ == '__main__':
    import asyncio
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("👋 Monitor encerrado pelo usuário.")
    except Exception as e:
        log(f"❌ Erro fatal: {e}")
        _logger.exception("Traceback completo do erro fatal")
        show_notification(title='Telegram Ofertas Monitor', message=f"Encerrado com erro: {e}")
    finally:
        if tray_icon:
            try:
                tray_icon.stop()
            except Exception:
                pass
