"""
Copyright 2025 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from typing import Annotated, Any

from typing_extensions import TypedDict

from langgraph.graph import StateGraph, START, END
from langchain_core.runnables.config import RunnableConfig
from langgraph.graph.message import add_messages
from langgraph.types import StreamWriter
from settings import get_settings
from langchain_core.messages import AIMessage, BaseMessage
from psycopg import Connection
from langgraph.checkpoint.postgres import PostgresSaver
from google import genai
from google.genai.types import Part, Content, GenerateContentConfig
from logger import logger
from index import initialize_firebase

settings = get_settings()
MODEL = "gemini-2.0-flash-001"
SYSTEM_PROMPT = """
You are a helpful travel agent assistant.
You can help users to answer questions about travel, 
book travel, and learn about places they are going to go.
Provides users ways to get help about their specific travel plans.
"""
_, vector_store = initialize_firebase()


class State(TypedDict):
    """Type definition for the chat state.

    Attributes:
        messages: A list of chat messages that gets updated using the add_messages function.
                  The `add_messages` function in the annotation defines how this state key should be updated
                  (in this case, it appends messages to the list, rather than overwriting them)
    """

    messages: Annotated[list[BaseMessage], add_messages]


def format_chat_to_gemini_standard(messages: list) -> list[Content]:
    converted_messages = []
    for message in messages:
        if isinstance(message, AIMessage):
            converted_messages.append(
                Content(role="model", parts=[Part.from_text(text=message.content)])
            )
        else:
            converted_messages.append(
                Content(role="user", parts=[Part.from_text(text=message.content)])
            )

    return converted_messages


def get_model_response(
    state: State, config: RunnableConfig, writer: StreamWriter
) -> dict[str, list[AIMessage]]:
    """Generate a streaming response from Gemini

    Args:
        state: The current state containing chat message history
        writer: A StreamWriter object to handle streaming responses
        config: The configuration for the runnable

    Returns:
        dict containing the new AI message to be added to the chat history

    Example:
        {
            "messages": [AIMessage(content="Complete response from Gemini")]
        }
    """
    global SYSTEM_PROMPT

    # TODO: refactor to tool node
    relevant_contexts = get_relevant_contexts(state["messages"][-1].content)

    SYSTEM_PROMPT = (
        SYSTEM_PROMPT
        + f"""
Utilize the following context to generate a response if they're relevant:

## Contexts
{relevant_contexts}  
"""
    )

    client = genai.Client(api_key=settings.GEMINI_API_KEY)

    converted_messages = format_chat_to_gemini_standard(state["messages"][:-1])

    chat_model = client.chats.create(
        model=MODEL,
        history=converted_messages,
        config=GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
    )

    try:
        response = chat_model.send_message_stream(state["messages"][-1].content)

        full_response: list[str] = []
        for chunk in response:
            json_fields = {
                "response": chunk.dict(),
            }
            logger.debug(
                "gemini response is generated", extra={"json_fields": json_fields}
            )

            writer(chunk.text)
            full_response.append(chunk.text)
    except Exception as e:
        writer(f"failed to generate response: {e}")
        logger.error(f"failed to genereate gemini response: {e}")
        return {"messages": []}

    return {"messages": [AIMessage(content="".join(full_response))]}


def get_relevant_contexts(text: str) -> list[str]:
    results = vector_store.similarity_search(text, k=10)
    contexts = [result.page_content for result in results]
    return contexts


# Initialize the chat graph
graph = StateGraph(State)
graph.add_node("model", get_model_response)
graph.add_edge(START, "model")
graph.add_edge("model", END)


class GraphManager:
    """Manages the chatbot's connection to PostgreSQL and graph compilation.

    This class handles the database connection setup and cleanup, as well as
    maintaining the compiled graph instance for the chatbot.

    Attributes:
        conn: PostgreSQL database connection
        checkpointer: PostgreSQL saver for persisting chat history
        compiled_graph: Compiled instance of the chatbot graph
    """

    def __init__(self) -> None:
        """Initialize the ChatbotManager with empty connection and graph."""
        self.conn: Connection | None = None
        self.checkpointer: PostgresSaver | None = None
        self.compiled_graph: Any = None  # Type Any due to langgraph's dynamic typing
        self.setup_connection()

    def setup_connection(self) -> None:
        """Set up the PostgreSQL connection and initialize the graph.

        Establishes a connection to PostgreSQL using settings from the configuration,
        initializes the checkpointer, and compiles the chatbot graph.
        """
        connection_kwargs: dict[str, Any] = {
            "autocommit": True,
            "prepare_threshold": 0,
        }
        if self.conn is None:
            self.conn = Connection.connect(
                settings.CHAT_HISTORY_DB_URI, **connection_kwargs
            )
            self.checkpointer = PostgresSaver(self.conn)
            self.checkpointer.setup()
            self.graph = graph.compile(checkpointer=self.checkpointer)

    def __del__(self) -> None:
        """Clean up database connection on object destruction."""
        if self.conn:
            self.conn.close()
