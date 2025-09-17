#
# https://chatgpt.com/c/68c9afcc-253c-8322-99b7-522c4031f86a
import asyncio
import threading
import logging
import json
from typing import List, Dict, TypedDict
from contextlib import AsyncExitStack
from queue import Queue
from anthropic import Anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from dotenv import load_dotenv

load_dotenv()

# ---------- Logging ----------
logging.basicConfig(
    level=logging.ERROR,  # change to INFO/DEBUG when you want more trace
    # level=logging.INFO,  # change to INFO/DEBUG when you want more trace
    format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
)
log = logging.getLogger(__name__)


# ---------- Types ----------
class ToolDefinition(TypedDict):
    name: str
    description: str
    input_schema: dict


# ---------- Chatbot ----------
class MCP_ChatBot:
    def __init__(self):
        self.sessions: List[ClientSession] = []
        self.exit_stack = AsyncExitStack()
        self.anthropic = Anthropic()
        self.available_tools: List[ToolDefinition] = []
        self.tool_to_session: Dict[str, ClientSession] = {}
        # Persistent conversation memory (Anthropic-compliant turns only)
        # Each item is {'role': 'user'|'assistant', 'content': <str|list[blocks]>}
        self.messages: List[Dict] = []

    # ----- Server connections -----
    async def connect_to_servers(self):
        """Load server_config.json and connect to each MCP server once."""
        log.info("connect_to_servers: start")
        with open("server_config.json", "r", encoding="utf-8") as f:
            data = json.load(f)

        servers = data.get("mcpServers", {})
        for name, config in servers.items():
            await self.connect_to_server(name, config)

        log.info(
            "connect_to_servers: done; sessions=%d tools=%d",
            len(self.sessions), len(self.available_tools)
        )

    async def connect_to_server(self, server_name: str, server_config: dict) -> None:
        """Connect to a single MCP server, list tools, and cache them."""
        log.info("connect_to_server(%s): start", server_name)
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
                "input_schema": tool.inputSchema,
            })
        log.info(
            "connect_to_server(%s): initialized; tools=%s",
            server_name, [t["name"] for t in self.available_tools]
        )

    # ----- Tool invocation -----
    async def _call_tool_text(self, tool_name: str, tool_args: Dict) -> str:
        """Invoke MCP tool and flatten typical result content to text."""
        session = self.tool_to_session.get(tool_name)
        if not session:
            return f"[tool error] Unknown tool: {tool_name}"
        try:
            result = await session.call_tool(name=tool_name, arguments=tool_args)
            parts: List[str] = []
            if getattr(result, "content", None):
                for c in result.content:
                    if getattr(c, "type", "") == "text":
                        parts.append(getattr(c, "text", ""))
                    else:
                        # Fallback: stringify other payloads
                        try:
                            parts.append(json.dumps(getattr(c, "dict", lambda: str(c))(), default=str))
                        except Exception:
                            parts.append(str(c))
            return "\n".join([p for p in parts if p]) or "(empty tool result)"
        except Exception as e:
            log.exception("Tool call failed: %s", tool_name)
            return f"[tool error] {tool_name}: {e}"

    # ----- Core query flow with memory -----
    async def process_query(self, query: str) -> str:
        """
        Iterative tool loop with anti-churn guard (Anthropic-compliant):
          - Append user turn
          - Ask model
          - If tool_use appears, produce tool_result immediately (no interleaving text)
          - Limit to MAX_TOOL_LOOPS and nudge model to stop repeating
        """
        log.info("process_query: begin; query=%r", query[:200])

        # 1) Add user turn to memory
        self.messages.append({'role': 'user', 'content': query})

        from collections import Counter
        seen_tools = Counter()

        MAX_TOOL_LOOPS = 6
        loop_idx = 0

        while True:
            loop_idx += 1
            response = self.anthropic.messages.create(
                model='claude-3-7-sonnet-20250219',
                max_tokens=2024,
                tools=self.available_tools,
                messages=self.messages,
            )

            log.info("model reply types: %s", [getattr(x, "type", "?") for x in response.content])

            tool_calls = []
            free_text_chunks = []

            for item in response.content:
                if item.type == 'text':
                    free_text_chunks.append(item.text or "")
                elif item.type == 'tool_use':
                    tool_calls.append({'id': item.id, 'name': item.name, 'input': item.input})

            # If no tools requested, finalize with text
            if not tool_calls:
                final_text = "".join(free_text_chunks).strip() or "(empty response)"
                self.messages.append({'role': 'assistant', 'content': final_text})
                log.info("process_query: done; loops=%d; len=%d", loop_idx, len(final_text))
                return final_text

            # Record assistant turn (with tool_use)
            self.messages.append({'role': 'assistant', 'content': response.content})

            # --- Anti-churn accounting (do NOT inject user text yet) ---
            for call in tool_calls:
                seen_tools[call['name']] += 1

            # Build tool_result blocks that must come IMMEDIATELY after tool_use
            tool_result_blocks = []
            suppress_msg_needed = False

            for call in tool_calls:
                t_name = call['name']
                t_args = call.get('input', {}) or {}
                if t_name == 'search_drugs' and seen_tools.get('search_drugs', 0) > 2:
                    # Suppress further identical searches; still emit a tool_result for protocol compliance
                    log.info("process_query: suppressing extra search_drugs; args=%s", str(t_args)[:300])
                    tool_result_blocks.append({
                        'type': 'tool_result',
                        'tool_use_id': call['id'],
                        'content': ("[anti-churn] Repeated search_drugs suppressed. "
                                    "Please either call get_drug_properties or summarize results."),
                        'is_error': True,  # optional but useful signal
                    })
                    suppress_msg_needed = True
                else:
                    log.info("process_query: tool_use -> %s args=%s", t_name, str(t_args)[:300])
                    result_text = await self._call_tool_text(t_name, t_args)
                    tool_result_blocks.append({
                        'type': 'tool_result',
                        'tool_use_id': call['id'],
                        'content': result_text,
                    })

            # Post the REQUIRED immediate tool_result message
            self.messages.append({'role': 'user', 'content': tool_result_blocks})

            # Now it's safe to add steering text (if we suppressed)
            if suppress_msg_needed:
                self.messages.append({
                    'role': 'user',
                    'content': (
                        "You already performed multiple searches. Do not call search_drugs again. "
                        "If details are needed, call get_drug_properties; otherwise, write the final answer now."
                    )
                })

            # Safety stop
            if loop_idx >= MAX_TOOL_LOOPS:
                log.warning("process_query: reached MAX_TOOL_LOOPS without final text")
                # Nudge the model once more (AFTER a valid tool_result turn)
                self.messages.append({
                    'role': 'user',
                    'content': 'Stop calling tools. Summarize the findings from the tool results above in clear prose.'
                })
                response2 = self.anthropic.messages.create(
                    model='claude-3-7-sonnet-20250219',
                    max_tokens=2024,
                    tools=self.available_tools,
                    messages=self.messages,
                )
                final_chunks = [c.text for c in response2.content if getattr(c, "type", "") == "text"]
                final_text = "".join(final_chunks).strip() or "(empty response)"
                self.messages.append({'role': 'assistant', 'content': final_text})
                return final_text

    # ----- Background runner (async loop living in a worker thread) -----
    async def run_chatbot(self, in_q: Queue, out_q: Queue):
        """Async loop: read queries from sync queue, process, write responses back to sync queue."""
        try:
            await self.connect_to_servers()
            log.info("run_chatbot: ready for queries")
            while True:
                query = await asyncio.to_thread(in_q.get)  # blocking get in a worker
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

# ---------- Thread entrypoint ----------
def start_async_loop(chatbot: MCP_ChatBot, in_q: Queue, out_q: Queue):
    """Runs the async engine inside a dedicated thread."""
    asyncio.run(chatbot.run_chatbot(in_q, out_q))


# ---------- Simple CLI harness (for local testing) ----------
def main():
    """
    Local CLI runner:
      - Starts the async engine in a worker thread
      - Lets you type queries in the console
      - Type 'quit' to stop
    """
    in_q: Queue = Queue()
    out_q: Queue = Queue()
    bot = MCP_ChatBot()

    worker = threading.Thread(
        target=start_async_loop,
        args=(bot, in_q, out_q),
        daemon=True,
        name="AsyncWorker",
    )
    worker.start()

    print("MCP Chatbot started. Type queries or 'quit' to exit.")
    try:
        while True:
            q = input("\nQuery: ").strip()
            in_q.put(q)
            if q.lower() == "quit":
                msg = out_q.get(timeout=10)
                print(msg)
                break
            resp = out_q.get()
            print("\nResponse:\n", resp)
    except KeyboardInterrupt:
        print("\nInterrupted. Exiting...")
    finally:
        worker.join(timeout=2)


if __name__ == "__main__":
    main()
