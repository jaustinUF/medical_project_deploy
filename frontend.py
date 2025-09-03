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
# Per-client timer registry (to cancel on disconnect to avoid KeyError)
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

# Cancel timers when a client disconnects (avoids KeyError races)
def _on_disconnect(client):
    # client has .id
    _cancel_timer_for(client.id)

app.on_disconnect(_on_disconnect)

# ---------------------------
# Healthcheck endpoint
# check if app is up on Railway
#   call url in browser: https://medicalprojectdeploy-production.up.railway.app/health
# ---------------------------
@ui.page('/health')
def healthcheck():
    # Fast, lightweight liveness check for Railway and manual curl
    return Response(content='OK', media_type='text/plain')

# ---------------------------
# Main page
# per-client UI: function is executed fresh for every new browser session
#   - isolates user data
# ---------------------------
@ui.page('/')
def index():
    """A fresh UI for each browser client (prevents cross-user leakage)."""
    is_paas = bool(os.environ.get('PORT'))  # True on Railway (or other PaaS)
    client = ui.context.client              # current client object (has .id)

    # Page container so children can expand to full width
    with ui.column().classes('w-full max-w-4xl mx-auto'):
        # Header with status
        with ui.row().classes('items-center gap-3'):
            ui.label('Medical Finder').classes('text-2xl font-bold')
            status_dot = ui.icon('circle').classes('text-gray-400')
            status_text = ui.label('Loading tools...').classes('text-gray-600')

        # WIDE input: make it fill the available width like the output
        query_box = ui.input(
            label='Query',
            placeholder='Enter your query…'
        ).classes('w-full')

        spinner = ui.spinner(size='lg').props('color=blue').classes('mt-2')
        spinner.visible = False

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

        # Quit button; Local-only hidden on Railway
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
        #   - manages 'loading' dot and tool count
        #   - prevents KeyError on Railway
        # ---------------------------
        # Keep a per-client polling timer and cancel it when tools are ready
        # and again on disconnect (via app.on_disconnect above).
        def update_status():
            ready = len(getattr(chatbot, 'available_tools', [])) > 0
            if ready:
                status_dot.classes(replace='text-green-500')
                status_text.set_text(f'Tools ready ({len(chatbot.available_tools)})')
                # cancel this client's timer once ready
                _cancel_timer_for(client.id)
            else:
                status_dot.classes(replace='text-gray-400')
                status_text.set_text('Loading tools...')

        # Start polling every 0.5s; store handle per client
        timers_by_client[client.id] = ui.timer(0.5, update_status)

# ---------------------------
# Run mode
#   - detects and sets-up for local or Railway execution
# ---------------------------
if __name__ in {"__main__", "__mp_main__"}:
    port = os.environ.get("PORT")
    if port:
        print(f"Starting NiceGUI on 0.0.0.0:{port} (Railway)")
        ui.run(host="0.0.0.0", port=int(port), reload=False)
    else:
        # Local desktop window (requires `pywebview` installed)
        ui.run(native=True, reload=False)
