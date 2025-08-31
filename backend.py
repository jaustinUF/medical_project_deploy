import asyncio
import threading
import logging
import json
from typing import List, Dict, TypedDict
from contextlib import AsyncExitStack
from queue import Queue  # stdlib, thread-safe
from anthropic import Anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from dotenv import load_dotenv
load_dotenv()

# ---------- Logging ----------
logging.basicConfig(
    level=logging.ERROR,  # INFO is default. DEBUG for detail, ERROR for just errors
    # level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
)
log = logging.getLogger(__name__)

class ToolDefinition(TypedDict):
    name: str
    description: str
    input_schema: dict

class MCP_ChatBot:
    def __init__(self):
        self.sessions: List[ClientSession] = []
        self.exit_stack = AsyncExitStack()
        self.anthropic = Anthropic()
        self.available_tools: List[ToolDefinition] = []
        self.tool_to_session: Dict[str, ClientSession] = {}

    async def connect_to_servers(self):
        log.info("connect_to_servers: start")
        try:
            with open("server_config.json", "r") as file:
                servers = json.load(file).get("mcpServers", {})
            for name, config in servers.items():
                await self.connect_to_server(name, config)
            log.info("connect_to_servers: done; sessions=%d tools=%d",
                     len(self.sessions), len(self.available_tools))
        except Exception as e:
            log.exception("Error loading server configuration: %s", e)
            raise

    async def connect_to_server(self, server_name: str, server_config: dict) -> None:
        log.info("connect_to_server(%s): start", server_name)
        try:
            server_params = StdioServerParameters(**server_config)
            stdio_transport = await self.exit_stack.enter_async_context(stdio_client(server_params))
            read, write = stdio_transport
            session = await self.exit_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self.sessions.append(session)

            tools = (await session.list_tools()).tools
            for tool in tools:
                self.tool_to_session[tool.name] = session
                self.available_tools.append({
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.inputSchema
                })
            log.info("connect_to_server(%s): initialized; tools=%s",
                     server_name, [t["name"] for t in self.available_tools])
        except Exception as e:
            log.exception("Failed to connect to %s: %s", server_name, e)

    async def process_query(self, query: str) -> str:
        log.info("process_query: begin; query=%r", query[:120])
        messages = [{'role': 'user', 'content': query}]
        # NOTE: Anthropics client call is synchronous; it’s okay because we’re on a background thread’s event loop.
        response = self.anthropic.messages.create(
            max_tokens=2024,
            model='claude-3-7-sonnet-20250219',
            tools=self.available_tools,
            messages=messages
        )
        log.debug("process_query: initial response content types=%s",
                  [c.type for c in response.content])

        collected_text = []
        while True:
            for content in response.content:
                if content.type == 'text':
                    collected_text.append(content.text)
                    # If the model returned only text (no tools), we're done.
                    if len(response.content) == 1:
                        final = "".join(collected_text)
                        log.info("process_query: done (text-only); len=%d", len(final))
                        return final

                elif content.type == 'tool_use':
                    # Record assistant tool call and perform it.
                    messages.append({'role': 'assistant', 'content': response.content})
                    tool_name = content.name
                    tool_args = content.input
                    log.info("process_query: tool_use -> %s args=%s", tool_name, str(tool_args)[:200])

                    session = self.tool_to_session[tool_name]
                    result = await session.call_tool(tool_name, arguments=tool_args)
                    log.debug("process_query: tool_result content len=%d", len(result.content or []))

                    messages.append({
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": content.id,
                            "content": result.content
                        }]
                    })
                    response = self.anthropic.messages.create(
                        max_tokens=2024,
                        model='claude-3-7-sonnet-20250219',
                        tools=self.available_tools,
                        messages=messages
                    )
                    log.debug("process_query: next response content types=%s",
                              [c.type for c in response.content])

    async def run_chatbot(self, in_q: Queue, out_q: Queue):
        """Background async loop:
        - Reads blocking sync queue via asyncio.to_thread
        - Writes back to sync queue via asyncio.to_thread to avoid blocking the loop
        """
        try:
            await self.connect_to_servers()
            log.info("run_chatbot: ready for queries")
            while True:
                # get() is blocking; run it in a worker thread to keep the event loop free
                query = await asyncio.to_thread(in_q.get)
                log.info("run_chatbot: got query from sync side: %r", query)

                if isinstance(query, str) and query.lower() == "quit":
                    await asyncio.to_thread(out_q.put, "Exiting chatbot...")
                    break

                try:
                    response = await self.process_query(query)
                    await asyncio.to_thread(out_q.put, response)
                except Exception as e:
                    log.exception("run_chatbot: error while processing query")
                    await asyncio.to_thread(out_q.put, f"[ERROR]: {str(e)}")
        finally:
            log.info("run_chatbot: closing resources")
            await self.exit_stack.aclose()
            log.info("run_chatbot: closed")

def start_async_loop(chatbot: MCP_ChatBot, in_q: Queue, out_q: Queue):
    # Single event loop runs here
    asyncio.run(chatbot.run_chatbot(in_q, out_q))

def main():
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
    #   main thread (MainThread): the scripts default thread
    #   - Reads user input from the console
    #   - Sends the query to the background thread via `in_q`
    #   - Waits for and prints the response from `out_q`
    #
    #  'worker' thread ("AsyncWorker" created/started below)
    #   - runs the asynchronous backend (event loop)
    #   - Listens for queries from the UI via `in_q`
    #   - Calls the async chatbot (Anthropic + tool use)
    #   - Sends the response back via `out_q`
    #   - `daemon=True` means this thread won't block program exit
    #   - `args=(chatbot, in_q, out_q)` passes parameters to the loop
    #
    # start background thread to run the async chatbot engine.
    worker = threading.Thread(
        target=start_async_loop, args=(chatbot, in_q, out_q), daemon=True, name="AsyncWorker"
    )
    worker.start()

    print("MCP Chatbot started. Type your queries or 'quit' to exit.")
    try:
        while True:
            query = input("\nQuery: ").strip()

            log.info("main: putting query into in_q")
            in_q.put(query)

            if query.lower() == 'quit':
                # background will send a final message then exit
                msg = out_q.get(timeout=10)
                print(msg)
                break

            log.info("main: waiting for response on out_q")
            response = out_q.get()  # blocking sync wait
            print("\nResponse:\n", response)

    except KeyboardInterrupt:
        print("Interrupted by user. Exiting...")
    finally:
        worker.join(timeout=2)
        log.info("main: exit")

if __name__ == "__main__":
    main()
