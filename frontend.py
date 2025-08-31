import threading
import asyncio
from queue import Queue
from nicegui import ui, app
from backend import MCP_ChatBot, start_async_loop

# Note: this script runs two threads (see below).
#   but, communication is needed between threads.
# Queue objects (below) provide that communication path
# - `in_q`: used by the main thread to send queries TO the worker
# - `out_q`: used by the worker to send responses BACK to the main thread
#
# create queues
in_q: Queue = Queue()
out_q: Queue = Queue()

chatbot = MCP_ChatBot()
# two threads:
#   main thread (MainThread): The scripts default thread
#   - Sets up the NiceGUI user interface
#   - Handles user input (e.g. Ask button clicks)
#   - Communicates with the background thread via queues
#
#  'worker' thread ("AsyncWorker" created/started below):
#   - runs the asynchronous backend (event loop)
#   - Listens for queries from the UI via `in_q`
#   - Calls the async chatbot (Anthropic + tool use)
#   - Sends the response back via `out_q`
#   - `daemon=True` means this thread won't block program exit
#   - `args=(chatbot, in_q, out_q)` passes parameters to the loop
#
# start background thread to run the async chatbot engine.
worker = threading.Thread(
    target=start_async_loop, args=(chatbot, in_q, out_q),
    daemon=True, name="AsyncWorker"
)
worker.start()

ui.label('Medical Finder').classes('text-2xl font-bold mb-4')

query_box = ui.input(label='Query', placeholder='Enter your query...')

spinner = ui.spinner(size='lg').props('color=blue').classes('mt-2')
spinner.visible = False

with ui.scroll_area().classes('w-full h-60 border rounded-lg p-2 bg-gray-100 mt-4') as scroll_area:
    text_output = ui.label('').classes('whitespace-pre-wrap')

async def ask_query():
    query = query_box.value.strip()
    if not query:
        return
    query_box.value = ''
    spinner.visible = True
    text_output.text = ''       # clear previous response

    # Send query to backend
    in_q.put(query)

    # Wait for response in a background thread
    response = await asyncio.to_thread(out_q.get)

    text_output.text = response # new response
    spinner.visible = False

ui.button('Ask', on_click=ask_query).classes('mt-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700')

def shutdown_app():
    in_q.put("quit")
    try:
        _ = out_q.get(timeout=5)
    except Exception:
        pass
    app.shutdown()
    worker.join(timeout=2)

ui.button('Quit', on_click=shutdown_app).classes('mt-4 px-6 py-3 bg-red-600 text-white rounded-lg hover:bg-red-700')

# at the bottom of frontend.py
if __name__ in {"__main__", "__mp_main__"}:
    import os
    port = os.environ.get("PORT")
    if port:
        # Railway / any PaaS sets PORT
        print(f"Starting NiceGUI on 0.0.0.0:{port} (Railway)")  # will show in Deploy Logs
        ui.run(host="0.0.0.0", port=int(port), reload=False)
    else:
        # Local dev (requires pywebview installed)
        ui.run(native=True, reload=False)

