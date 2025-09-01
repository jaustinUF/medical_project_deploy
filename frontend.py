import os
import threading
import asyncio
from queue import Queue

from nicegui import ui, app
from fastapi import Response

from backend import MCP_ChatBot, start_async_loop

# ---------------------------
# Shared backend engine
# ---------------------------
# One backend engine (thread + queues) for the whole app.
# Each browser/client gets its own UI session, but they all talk to this engine.

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
# Healthcheck endpoint
# ---------------------------
@ui.page('/health')
def healthcheck():
    # Fast, lightweight liveness check for Railway and manual curl
    return Response(content='OK', media_type='text/plain')


# ---------------------------
# Main page (per-client UI)
# ---------------------------
@ui.page('/')
def index():
    """A fresh UI for each browser client (prevents cross-user leakage)."""
    is_paas = bool(os.environ.get('PORT'))  # True on Railway (or other PaaS)

    # Header
    with ui.row().classes('items-center gap-3'):
        ui.label('Medical Finder').classes('text-2xl font-bold')
        # Status dot + text (auto-updates below)
        status_dot = ui.icon('circle').classes('text-gray-400')
        status_text = ui.label('Loading tools...').classes('text-gray-600')

    # Query input + spinner
    query_box = ui.input(label='Query', placeholder='Enter your query...')
    spinner = ui.spinner(size='lg').props('color=blue').classes('mt-2')
    spinner.visible = False

    # Output area (fresh per client)
    with ui.scroll_area().classes('w-full h-60 border rounded-lg p-2 bg-gray-100 mt-4'):
        text_output = ui.label('').classes('whitespace-pre-wrap')
    text_output.text = ''  # ensure empty on load

    async def ask_query():
        query = (query_box.value or '').strip()
        if not query:
            return
        query_box.value = ''
        spinner.visible = True
        text_output.text = ''   # clear immediately on new ask

        # Send the query to the backend (thread-safe queue)
        in_q.put(query)

        # Wait for the response without blocking the event loop
        response = await asyncio.to_thread(out_q.get)

        text_output.text = response
        spinner.visible = False

    ui.button(
        'Ask',
        on_click=ask_query
    ).classes('mt-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700')

    # Local-only Quit button (hidden on Railway)
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
    # Status updater: tools loaded?
    # ---------------------------
    # Poll the shared chatbot to see when tools have been listed.
    # Turns the dot green and updates text when ready.
    def update_status():
        ready = len(getattr(chatbot, 'available_tools', [])) > 0
        if ready:
            status_dot.classes(replace='text-green-500')
            status_text.set_text(f'Tools ready ({len(chatbot.available_tools)})')
        else:
            status_dot.classes(replace='text-gray-400')
            status_text.set_text('Loading tools...')

    # Poll every 0.5s; cheap and effective
    ui.timer(0.5, update_status)


# ---------------------------
# Run mode
# ---------------------------
if __name__ in {"__main__", "__mp_main__"}:
    port = os.environ.get("PORT")
    if port:
        print(f"Starting NiceGUI on 0.0.0.0:{port} (Railway)")
        ui.run(host="0.0.0.0", port=int(port), reload=False)
    else:
        # Local desktop window (requires `pywebview` installed)
        ui.run(native=True, reload=False)
