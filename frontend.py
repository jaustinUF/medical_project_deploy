import os
import threading
import asyncio
from queue import Queue
from typing import Dict, Optional

from nicegui import ui, app
from fastapi import Response

from backend import MCP_ChatBot, start_async_loop

# ---------------------------
# Shared backend engine
# ---------------------------
in_q: Queue = Queue()
out_q: Queue = Queue()

chatbot = MCP_ChatBot()

worker = threading.Thread(
    target=start_async_loop,
    args=(chatbot, in_q, out_q),
    daemon=True,
    name="AsyncWorker",
)
worker.start()

# ---------------------------
# Per-client timer registry
# ---------------------------
timers_by_client: Dict[str, Optional[ui.timer]] = {}

def _cancel_timer_for(client_id: str) -> None:
    t = timers_by_client.get(client_id)
    if t is not None:
        try:
            t.cancel()
        except Exception:
            pass
    timers_by_client[client_id] = None

def _on_disconnect(client):
    _cancel_timer_for(client.id)

app.on_disconnect(_on_disconnect)

# ---------------------------
# Healthcheck
# ---------------------------
@ui.page('/health')
def healthcheck():
    return Response(content='OK', media_type='text/plain')

# ---------------------------
# Small helpers: chat bubbles
# ---------------------------
def add_user_bubble(container: ui.column, text: str):
    """Right-aligned user bubble."""
    with container:
        with ui.row().classes('w-full justify-end'):
            with ui.card().classes('max-w-[80%] bg-blue-50 border border-blue-200 rounded-2xl p-3'):
                ui.label(text).classes('whitespace-pre-wrap text-gray-900')

def add_assistant_bubble(container: ui.column, text: str):
    """Left-aligned assistant bubble."""
    with container:
        with ui.row().classes('w-full justify-start'):
            with ui.card().classes('max-w-[80%] bg-gray-50 border border-gray-200 rounded-2xl p-3'):
                ui.label(text).classes('whitespace-pre-wrap text-gray-900')

# ---------------------------
# Main page (per-client UI)
# ---------------------------
@ui.page('/')
def index():
    is_paas = bool(os.environ.get('PORT'))
    client = ui.context.client

    with ui.column().classes('w-full max-w-4xl mx-auto'):
        # Header with status
        with ui.row().classes('items-center gap-3'):
            ui.label('Drug Finder').classes('text-2xl font-bold')
            status_dot = ui.icon('circle').classes('text-gray-400')
            status_text = ui.label('Tools: —').classes('text-gray-600')

        # Description + sample questions (your modified text version is fine)
        ui.label(
            'I can help you with information about drugs and medications by searching RxNorm, a standardized drug nomenclature database.'
        ).classes('text-gray-600')
        ui.label('Here are some sample questions:').classes('text-gray-600')
        ui.markdown(
            '- What is Lipitor used for?\n'
            '- Tell me about metformin.\n'
            '- What are the properties of ibuprofen?\n'
            '- Is Zoloft the same as sertraline?\n'
            '- What medications contain pseudoephedrine?'
        ).classes('text-gray-600')

        # Input
        query_box = ui.input(
            label='Query',
            placeholder='Enter your query…'
        ).classes('w-full')

        spinner = ui.spinner(size='lg').props('color=blue').classes('mt-2')
        spinner.visible = False

        # Transcript area
        with ui.scroll_area().classes('w-full h-80 border rounded-lg p-3 bg-white mt-4'):
            chat_container = ui.column().classes('w-full gap-2')

        # Ask handler
        async def ask_query():
            q = (query_box.value or '').strip()
            if not q:
                return
            # Clear input and show user bubble immediately
            query_box.value = ''
            add_user_bubble(chat_container, q)
            spinner.visible = True

            # Send to backend
            in_q.put(q)

            # Wait for the assistant response (non-blocking)
            response = await asyncio.to_thread(out_q.get)
            add_assistant_bubble(chat_container, response)
            # print(response)
            spinner.visible = False

        ui.button(
            'Ask',
            on_click=ask_query
        ).classes('mt-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700')

        # Quit (only local)
        def shutdown_app():
            in_q.put("quit")
            try:
                _ = out_q.get(timeout=5)
            except Exception:
                pass
            app.shutdown()
            worker.join(timeout=2)

        if not is_paas:
            ui.button(
                'Quit',
                on_click=shutdown_app
            ).classes('mt-4 px-6 py-3 bg-red-600 text-white rounded-lg hover:bg-red-700')

        # ---------------------------
        # Status updater: show tool names
        # ---------------------------
        def update_status():
            tools = getattr(chatbot, 'available_tools', [])
            names = [t.get('name') for t in tools] if tools else []
            if names:
                status_dot.classes(replace='text-green-500')
                status_text.set_text('Tools: ' + ', '.join(names))
                _cancel_timer_for(client.id)
            else:
                status_dot.classes(replace='text-gray-400')
                status_text.set_text('Tools: —')

        timers_by_client[client.id] = ui.timer(0.5, update_status)

# ---------------------------
# Run mode
# ---------------------------
if __name__ in {"__main__", "__mp_main__"}:
    port = os.environ.get("PORT")
    if port:
        print(f"Starting NiceGUI on 0.0.0.0:{port} (Railway)")
        ui.run(host="0.0.0.0", port=int(port), reload=False)
    else:
        ui.run(native=True, reload=False)
